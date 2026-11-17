"""vctl: drive an isolated sf-intel instance the way a user does. Standard library only.

Run from the sf-intel repo root with the project venv:
  .venv/Scripts/python.exe .claude/skills/verify-sf-intel/scripts/vctl.py <command> ...

  launch                         copy the DB into a scratch dir, start server + headless Chrome, print RUN id
  doctor  --run R                is this instance ours, up, and worth driving?
  state   --run R                print the instance's watches.json and alert count (side-effect view)
  api     --run R GET|POST PATH [JSON]   call the instance's HTTP API (second, read-only view)
  browser --run R open PATH      navigate, e.g. "/?view=places&place=16th%20%26%20Mission"
  browser --run R click --css SEL [--text T]     real mouse click on the first visible match
  browser --run R fill  --css SEL --value V      focus, clear, type V
  browser --run R press --key Enter|Escape|Tab
  browser --run R select --css SEL --value V
  browser --run R wait  --css SEL [--text T] [--gone] [--timeout S]
  browser --run R text  --css SEL [--all]
  browser --run R screenshot --name NAME         saves artifacts/verify/R/NAME.png
  browser --run R eval  JS
  stop    --run R                kill only this run's server and Chrome, delete its scratch dir, keep artifacts

Every browser/api action is appended to artifacts/verify/R/actions.log with its result.
"""

import argparse
import base64
import datetime as dt
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]          # .claude/skills/verify-sf-intel/scripts -> repo
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
SOURCE_DB = REPO / "data" / "sf_intel.duckdb"
RUNS_ROOT = Path(tempfile.gettempdir()) / "sf-intel-verify"
ARTIFACTS_ROOT = REPO / "artifacts" / "verify"
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------- run state
def run_dir(run: str) -> Path:
    return RUNS_ROOT / run


def artifacts(run: str) -> Path:
    p = ARTIFACTS_ROOT / run
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_instance(run: str | None) -> dict:
    if not run:
        live = sorted(p.parent.name for p in RUNS_ROOT.glob("*/instance.json")) if RUNS_ROOT.exists() else []
        if len(live) != 1:
            die(f"pass --run (live runs: {live or 'none'})")
        run = live[0]
    f = run_dir(run) / "instance.json"
    if not f.exists():
        die(f"no instance for run {run} at {f}")
    return json.loads(f.read_text())


def log_action(inst: dict, line: str):
    with (artifacts(inst["run"]) / "actions.log").open("a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%H:%M:%S} {line}\n")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body=None, timeout=120):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8")


def listening_pid(port: int) -> int | None:
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] == f"127.0.0.1:{port}" and parts[3] == "LISTENING":
            return int(parts[4])
    return None


def parent_pid(pid: int) -> int | None:
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').ParentProcessId"],
                       capture_output=True, text=True)
    return int(r.stdout.strip()) if r.stdout.strip().isdigit() else None


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def git_head() -> str:
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


# --------------------------------------------------------------- CDP websocket
class CDP:
    """Minimal Chrome DevTools Protocol client over a hand-rolled websocket."""

    def __init__(self, ws_url: str):
        u = urllib.parse.urlparse(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=120)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("websocket handshake closed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(f"websocket handshake failed: {head[:200]!r}")
        self.next_id = 0

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("websocket closed")
            buf += chunk
        return buf

    def _send_frame(self, opcode: int, data: bytes):
        hdr = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv_message(self) -> str:
        parts = []
        while True:
            b1, b2 = self._exact(2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            payload = self._exact(n)  # server frames are never masked
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise ConnectionError("websocket closed by Chrome")
            parts.append(payload)
            if b1 & 0x80:
                return b"".join(parts).decode("utf-8")

    def call(self, method: str, **params):
        self.next_id += 1
        self._send_frame(0x1, json.dumps({"id": self.next_id, "method": method, "params": params}).encode())
        while True:
            msg = json.loads(self._recv_message())
            if msg.get("id") == self.next_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, js: str):
        r = self.call("Runtime.evaluate", expression=js, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError((d.get("exception") or {}).get("description") or d.get("text"))
        return r["result"].get("value")


def page(inst: dict) -> CDP:
    _, _, body = http("GET", f"http://127.0.0.1:{inst['chrome_port']}/json/list", timeout=10)
    pages = [t for t in json.loads(body) if t["type"] == "page"]
    if not pages:
        die("Chrome has no page target; run doctor")
    return CDP(pages[0]["webSocketDebuggerUrl"])


FIND_JS = """(() => {
  const sel = %s, text = %s;
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && !e.closest('[hidden]'); };
  const all = [...document.querySelectorAll(sel)];
  // innerText reflects CSS text-transform (many labels here are uppercased), so match case-insensitively.
  const hits = all.filter(e => vis(e) && (text === null || (e.innerText || e.value || '').toLowerCase().includes(text.toLowerCase())));
  return {count: all.length, visible: hits.length, el: hits[0] || null};
})()"""


def find_expr(css: str, text: str | None) -> str:
    return FIND_JS % (json.dumps(css), json.dumps(text))


def wait_for(cdp: CDP, css: str, text: str | None, gone: bool, timeout: float) -> int:
    deadline = time.time() + timeout
    while True:
        n = cdp.eval(f"{find_expr(css, text)}.visible")
        if (n == 0) if gone else (n > 0):
            return n
        if time.time() > deadline:
            die(f"timeout after {timeout}s waiting for {css!r} text={text!r} {'to disappear' if gone else 'to appear'}")
        time.sleep(0.25)


# --------------------------------------------------------------------- commands
def cmd_launch(args):
    if not PYTHON.exists():
        die(f"no venv python at {PYTHON}; create it per README step 1")
    if not SOURCE_DB.exists():
        die(f"no database at {SOURCE_DB}; run `python -m src.ingest` then `python -m src.build`")
    chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if not chrome:
        die("no Chrome or Edge found")

    run = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    rd = run_dir(run)
    (rd / "data").mkdir(parents=True)
    shutil.copy2(SOURCE_DB, rd / "data" / "sf_intel.duckdb")  # server writes watches/alerts/audit next to it

    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(REPO), "SF_INTEL_PORT": str(port), "PYTHONUNBUFFERED": "1",
           "PYTHONIOENCODING": "utf-8"}
    if args.effort:
        env["CLAUDE_EFFORT"] = args.effort
    log = (rd / "server.log").open("w", encoding="utf-8")
    server = subprocess.Popen([str(PYTHON), "-m", "src.server", "--no-browser"], cwd=rd, env=env,
                              stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=DETACHED)
    inst = {"run": run, "run_dir": str(rd), "server_pid": server.pid, "port": port,
            "url": f"http://127.0.0.1:{port}", "git_head": git_head(), "started": dt.datetime.now().isoformat()}
    (rd / "instance.json").write_text(json.dumps(inst, indent=2))

    # Ready = usable: the overview endpoint answers with events, not just an open socket.
    deadline = time.time() + 60
    while True:
        try:
            status, _, body = http("GET", inst["url"] + "/api/overview", timeout=5)
            if status == 200 and sum(s["events"] for s in json.loads(body)["sources"]) > 0:
                break
        except (OSError, ValueError):
            pass
        if server.poll() is not None or time.time() > deadline:
            print((rd / "server.log").read_text(encoding="utf-8")[-2000:], file=sys.stderr)
            die("server did not become ready; see log above. Run `stop` to clean up.")
        time.sleep(0.5)
    # The venv python.exe is a launcher; the real interpreter is its child and owns the socket.
    inst["listener_pid"] = listening_pid(port)
    (rd / "instance.json").write_text(json.dumps(inst, indent=2))

    profile = rd / "chrome-profile"
    browser = subprocess.Popen([chrome, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                                "--window-size=1400,1000", "about:blank"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                               creationflags=DETACHED)
    inst["chrome_pid"] = browser.pid
    port_file = profile / "DevToolsActivePort"
    deadline = time.time() + 30
    while not (port_file.exists() and port_file.read_text().strip()):
        if time.time() > deadline:
            (rd / "instance.json").write_text(json.dumps(inst, indent=2))
            die("Chrome did not open a debug port. Run `stop` to clean up.")
        time.sleep(0.2)
    inst["chrome_port"] = int(port_file.read_text().splitlines()[0])
    (rd / "instance.json").write_text(json.dumps(inst, indent=2))
    artifacts(run)
    print(json.dumps({"run": run, "url": inst["url"], "artifacts": str(artifacts(run)), "git_head": inst["git_head"]}, indent=2))


def cmd_doctor(args):
    inst = load_instance(args.run)
    checks = []

    def check(name, ok, detail=""):
        checks.append(ok)
        print(f"  [{'ok' if ok else 'FAIL'}] {name}{': ' + str(detail) if detail else ''}")

    print(f"run {inst['run']}  {inst['url']}")
    check("server process alive", pid_alive(inst["server_pid"]), f"pid {inst['server_pid']}")
    owner = listening_pid(inst["port"])
    check("port owned by this run's server",
          owner is not None and owner in (inst["server_pid"], inst.get("listener_pid")) and
          (owner == inst["server_pid"] or parent_pid(owner) == inst["server_pid"]),
          f"port {inst['port']} -> pid {owner} (launcher {inst['server_pid']})")
    try:
        status, headers, body = http("GET", inst["url"] + "/api/overview", timeout=10)
        o = json.loads(body)
        check("server is sf-intel", (headers.get("Server") or "").startswith("sf-intel/"), headers.get("Server"))
        check("database has events", status == 200 and sum(s["events"] for s in o["sources"]) > 0,
              f"{o['window']['start']} -> {o['window']['end']}")
    except Exception as e:  # noqa: BLE001 - doctor reports, never crashes
        check("overview endpoint", False, e)
    watches_file = Path(inst["run_dir"]) / "data" / "watches.json"
    _, _, wbody = http("GET", inst["url"] + "/api/watches", timeout=10)
    served = len(json.loads(wbody)["watches"])
    on_disk = len(json.loads(watches_file.read_text())) if watches_file.exists() else 0
    check("server reads this run's data dir (not the repo's)", served == on_disk, f"{served} watches served, {on_disk} in {watches_file}")
    check("build matches checkout", inst["git_head"] == git_head(), f"launched at {inst['git_head']}, HEAD {git_head()}")
    try:
        _, _, v = http("GET", f"http://127.0.0.1:{inst['chrome_port']}/json/version", timeout=5)
        check("headless Chrome reachable", True, json.loads(v)["Browser"])
    except Exception as e:  # noqa: BLE001
        check("headless Chrome reachable", False, e)
    auth = bool(os.getenv("ANTHROPIC_API_KEY")) or (REPO / ".env").exists()
    if not auth:
        r = subprocess.run(["ant", "auth", "status"], capture_output=True, text=True, shell=True)
        auth = r.returncode == 0
    print(f"  [info] Claude API auth for Ask / watch summaries: {'available' if auth else 'NOT available (Ask fails; watches fall back to plain text)'}")
    sys.exit(0 if all(checks) else 1)


def cmd_state(args):
    inst = load_instance(args.run)
    data = Path(inst["run_dir"]) / "data"
    wf, af = data / "watches.json", data / "alerts.jsonl"
    watches = json.loads(wf.read_text()) if wf.exists() else []
    alerts = [json.loads(x) for x in af.read_text(encoding="utf-8").splitlines() if x.strip()] if af.exists() else []
    out = {"watches": watches, "alert_count": len(alerts),
           "last_alert": {k: alerts[-1].get(k) for k in ("id", "watch_id", "status", "new_count", "summary_by", "replay")} if alerts else None}
    text = json.dumps(out, indent=2, default=str)
    print(text)
    log_action(inst, f"state -> {len(watches)} watches, {len(alerts)} alerts")
    if args.save:
        (artifacts(inst["run"]) / f"{args.save}.json").write_text(text, encoding="utf-8")


def cmd_api(args):
    inst = load_instance(args.run)
    body = json.loads(args.json) if args.json else ({} if args.method == "POST" else None)
    status, _, text = http(args.method, inst["url"] + args.path, body)
    print(f"HTTP {status}")
    print(text[:20000])
    log_action(inst, f"api {args.method} {args.path} {args.json or ''} -> HTTP {status}")
    if args.save:
        (artifacts(inst["run"]) / f"{args.save}.json").write_text(text, encoding="utf-8")
    sys.exit(0 if status < 400 else 2)


def cmd_browser(args):
    inst = load_instance(args.run)
    cdp = page(inst)
    a, result = args.action, None
    if a == "open":
        t = args.target or ""
        if len(t) > 2 and t[1] == ":" and t[2] in "\\/":
            die(f"{t!r} looks like a path Git Bash rewrote; pass the target without a leading slash, e.g. '?view=places'")
        url = t if t.startswith("http") else inst["url"] + "/" + t.lstrip("/")
        cdp.call("Page.navigate", url=url)
        deadline = time.time() + 30
        time.sleep(0.3)
        while cdp.eval("document.readyState") != "complete":
            if time.time() > deadline:
                die("page did not finish loading")
            time.sleep(0.2)
        result = cdp.eval("location.href")
    elif a in ("click", "fill", "select"):
        wait_for(cdp, args.css, args.text, False, args.timeout)
        box = cdp.eval(f"""(() => {{ const e = {find_expr(args.css, args.text)}.el;
            e.scrollIntoView({{block: 'center'}}); const r = e.getBoundingClientRect();
            return {{x: r.x + r.width / 2, y: r.y + r.height / 2, tag: e.tagName, label: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 60)}}; }})()""")
        if a == "select":
            cdp.eval(f"""(() => {{ const e = {find_expr(args.css, None)}.el; e.focus(); e.value = {json.dumps(args.value)};
                e.dispatchEvent(new Event('input', {{bubbles: true}})); e.dispatchEvent(new Event('change', {{bubbles: true}})); }})()""")
            result = f"{box['tag']} value={args.value!r}"
        else:
            for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
                cdp.call("Input.dispatchMouseEvent", type=typ, x=box["x"], y=box["y"], button="left", clickCount=1)
            result = f"{box['tag']} {box['label']!r}"
            if a == "fill":
                cdp.eval(f"(() => {{ const e = {find_expr(args.css, args.text)}.el; e.focus(); e.select && e.select(); }})()")
                cdp.call("Input.dispatchKeyEvent", type="keyDown", key="Delete", code="Delete", windowsVirtualKeyCode=46)
                cdp.call("Input.dispatchKeyEvent", type="keyUp", key="Delete", code="Delete", windowsVirtualKeyCode=46)
                cdp.call("Input.insertText", text=args.value)
                result += f" <- {args.value!r}"
    elif a == "press":
        codes = {"Enter": 13, "Escape": 27, "Tab": 9}
        if args.key not in codes:
            die(f"key must be one of {list(codes)}")
        extra = {"text": "\r"} if args.key == "Enter" else {}
        cdp.call("Input.dispatchKeyEvent", type="keyDown", key=args.key, code=args.key, windowsVirtualKeyCode=codes[args.key], **extra)
        cdp.call("Input.dispatchKeyEvent", type="keyUp", key=args.key, code=args.key, windowsVirtualKeyCode=codes[args.key])
        result = args.key
    elif a == "wait":
        n = wait_for(cdp, args.css, args.text, args.gone, args.timeout)
        result = "gone" if args.gone else f"{n} visible"
    elif a == "text":
        if args.all:
            result = cdp.eval(f"[...document.querySelectorAll({json.dumps(args.css)})].map(e => e.innerText.trim())")
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            wait_for(cdp, args.css, args.text, False, args.timeout)
            result = cdp.eval(f"{find_expr(args.css, args.text)}.el.innerText")
            print(result)
        result = f"{len(json.dumps(result))} chars"
    elif a == "screenshot":
        shot = cdp.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        path = artifacts(inst["run"]) / f"{args.name}.png"
        path.write_bytes(base64.b64decode(shot["data"]))
        result = str(path)
    elif a == "eval":
        result = cdp.eval(args.target)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if a not in ("text", "eval"):
        print(result)
    log_action(inst, f"browser {a} {args.css or args.target or args.key or args.name or ''}"
                     f"{' text=' + repr(args.text) if args.text else ''} -> {result}")


def cmd_stop(args):
    inst = load_instance(args.run)
    for key in ("chrome_pid", "server_pid"):
        pid = inst.get(key)
        if pid and pid_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            print(f"killed {key} {pid}")
    rd = Path(inst["run_dir"])
    if RUNS_ROOT not in rd.parents:
        die(f"refusing to delete {rd}: not under {RUNS_ROOT}")
    keep = artifacts(inst["run"])
    shutil.copy2(rd / "server.log", keep / "server.log") if (rd / "server.log").exists() else None
    for _ in range(20):  # Chrome releases profile files a moment after exit
        try:
            shutil.rmtree(rd)
            break
        except OSError:
            time.sleep(0.5)
    print(f"scratch {'removed' if not rd.exists() else 'NOT fully removed'}: {rd}")
    print(f"evidence kept: {keep} ({len(list(keep.iterdir()))} files)")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="vctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    la = sub.add_parser("launch")
    la.add_argument("--effort", choices=["low", "medium", "high"], help="CLAUDE_EFFORT for Ask (lower = cheaper)")
    for name in ("doctor", "stop"):
        sub.add_parser(name).add_argument("--run")
    st = sub.add_parser("state")
    st.add_argument("--run")
    st.add_argument("--save", help="also write artifacts/verify/R/<SAVE>.json")
    ap = sub.add_parser("api")
    ap.add_argument("--run")
    ap.add_argument("--save")
    ap.add_argument("method", choices=["GET", "POST"])
    ap.add_argument("path")
    ap.add_argument("json", nargs="?")
    b = sub.add_parser("browser")
    b.add_argument("--run")
    b.add_argument("action", choices=["open", "click", "fill", "press", "select", "wait", "text", "screenshot", "eval"])
    b.add_argument("target", nargs="?", help="path for open, JS for eval")
    b.add_argument("--css")
    b.add_argument("--text")
    b.add_argument("--value")
    b.add_argument("--key")
    b.add_argument("--name")
    b.add_argument("--gone", action="store_true")
    b.add_argument("--all", action="store_true")
    b.add_argument("--timeout", type=float, default=30)
    args = p.parse_args()
    {"launch": cmd_launch, "doctor": cmd_doctor, "state": cmd_state, "api": cmd_api,
     "browser": cmd_browser, "stop": cmd_stop}[args.cmd](args)


if __name__ == "__main__":
    main()
