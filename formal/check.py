"""Model-check the watch checkpoint model (formal/WatchCheckpoint.tla) with TLC.

Run from the sf-intel repo root:
    python formal/check.py                 # every scenario x property, compared to formal/expected.json
    python formal/check.py code NoLostWatch  # one scenario, one property

Each check is one TLC run with TypeOK plus one property, so every property gets its own verdict and
its own shortest counterexample. Traces go to formal/.out/<scenario>-<property>.txt.
Exit code 0 = every verdict matches formal/expected.json; 1 = a verdict changed (read the table).

Needs Java 11+ and tla2tools.jar v1.8.0 (see formal/README.md "Setup"). Override locations with
the JAVA and TLA2TOOLS environment variables.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / ".out"
SPEC = "WatchCheckpoint"
PROPERTIES = ["NoLostWatch", "NoResurrectedWatch", "NoMissedEvent", "NoDuplicateAlert"]
SCENARIOS = ["code", "server_only", "fixed", "old_checkpoint"]


def find_java() -> str:
    if os.getenv("JAVA"):
        return os.environ["JAVA"]
    found = shutil.which("java")
    if found:
        return found
    for pattern in (r"C:\Program Files\Eclipse Adoptium\*\bin\java.exe", r"C:\Program Files\Java\*\bin\java.exe"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    sys.exit("Java not found. Install it (formal/README.md, Setup) or set JAVA=path\\to\\java.exe")


def find_jar() -> str:
    jar = Path(os.getenv("TLA2TOOLS", Path.home() / ".tla" / "tla2tools.jar"))
    if not jar.exists():
        sys.exit(f"{jar} not found. Download it (formal/README.md, Setup) or set TLA2TOOLS.")
    return str(jar)


def run(java: str, jar: str, scenario: str, prop: str) -> tuple[str, str, float]:
    OUT.mkdir(exist_ok=True)
    cfg = OUT / f"{scenario}-{prop}.cfg"
    cfg.write_text((HERE / f"{scenario}.cfg").read_text() + f"\nINVARIANT TypeOK\nINVARIANT {prop}\n")
    started = time.time()
    r = subprocess.run([java, "-XX:+UseParallelGC", "-cp", jar, "tlc2.TLC", "-workers", "auto", "-deadlock", "-noGenerateSpecTE",
                        "-cleanup", "-metadir", str(OUT / "states" / f"{scenario}-{prop}"),
                        "-config", str(cfg), SPEC],
                       cwd=HERE, capture_output=True, text=True)
    out = r.stdout + r.stderr
    (OUT / f"{scenario}-{prop}.txt").write_text(out, encoding="utf-8")
    secs = time.time() - started
    if "No error has been found" in out:
        states = re.findall(r"([\d,]+) distinct states found", out)  # last one is the final total
        return "PASS", f"{states[-1] if states else '?'} states", secs
    m = re.search(r"Invariant (\w+) is violated", out)
    if m:
        steps = len(re.findall(r"^State \d+:", out, re.M))
        return ("VIOLATED" if m.group(1) == prop else f"VIOLATED {m.group(1)}"), f"{steps}-state trace", secs
    return "ERROR", (out.strip().splitlines() or ["no output"])[-1][:100], secs


REPO = HERE.parent
SYNC_FILE = HERE / "SYNC"   # commit at which the model was last confirmed to describe the code
COVERED = ["src/watch.py", "src/server.py", "src/ingest.py", "src/build.py", "src/sources.py"]
LOCK_PATHS = {"add_watch": "add", "remove_watch": "remove", "run_watch": "run_save"}


def code_lock_paths() -> set[str]:
    """Which watch.py functions take `with _lock:` - the model's `Locked` constant must match."""
    import ast
    tree = ast.parse((REPO / "src" / "watch.py").read_text(encoding="utf-8"))
    found = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in LOCK_PATHS:
            for node in ast.walk(fn):
                if isinstance(node, ast.With) and any(
                        isinstance(i.context_expr, ast.Name) and i.context_expr.id == "_lock" for i in node.items):
                    found.add(LOCK_PATHS[fn.name])
    return found


def drift_report() -> bool:
    """Print what changed since the sync commit. Returns False if the model is provably out of sync."""
    ok = True
    code = code_lock_paths()
    for scenario in ("code", "server_only"):
        m = re.search(r"Locked\s*=\s*\{([^}]*)\}", (HERE / f"{scenario}.cfg").read_text())
        model = set(re.findall(r'"(\w+)"', m.group(1))) if m else set()
        if model != code:
            print(f"MODEL OUT OF SYNC: src/watch.py takes _lock in {sorted(code)}, "
                  f"{scenario}.cfg says Locked = {sorted(model)}. Update the cfg (and the model if the "
                  f"code path itself changed), then rerun.")
            ok = False
    # Checkpoint style: the code owes "seen_ids" when run_watch excludes load_seen(...) results.
    src = (REPO / "src" / "watch.py").read_text(encoding="utf-8")
    code_ckpt = "seen_ids" if re.search(r"exclude\s*=\s*load_seen\(", src) else "occurred_at"
    for scenario in ("code", "server_only"):
        m = re.search(r'Checkpoint\s*=\s*"(\w+)"', (HERE / f"{scenario}.cfg").read_text())
        if not m or m.group(1) != code_ckpt:
            print(f"MODEL OUT OF SYNC: src/watch.py checkpoints by {code_ckpt}, "
                  f"{scenario}.cfg says Checkpoint = {m.group(1) if m else 'missing'}.")
            ok = False
    sync = SYNC_FILE.read_text().strip()
    r = subprocess.run(["git", "-C", str(REPO), "diff", "--stat", sync, "--", *COVERED], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"note: cannot diff against sync commit {sync}: {r.stderr.strip()}")
    elif r.stdout.strip():
        print(f"note: covered code changed since sync commit {sync} - confirm the model still describes it "
              f"(formal/README.md, 'Keeping it in step'):\n{r.stdout.rstrip()}")
    else:
        print(f"covered code unchanged since sync commit {sync}")
    return ok


def main():
    if not drift_report():
        sys.exit(2)
    java, jar = find_java(), find_jar()
    expected = json.loads((HERE / "expected.json").read_text())
    if len(sys.argv) == 3:
        jobs = [(sys.argv[1], sys.argv[2])]
    else:
        jobs = [(s, p) for s in SCENARIOS for p in PROPERTIES]
    print(f"{'scenario':<12} {'property':<20} {'result':<10} {'expected':<10} detail")
    changed = 0
    for scenario, prop in jobs:
        verdict, detail, secs = run(java, jar, scenario, prop)
        want = expected.get(scenario, {}).get(prop, "?")
        flag = "" if verdict == want else "   <-- CHANGED"
        changed += bool(flag)
        print(f"{scenario:<12} {prop:<20} {verdict:<10} {want:<10} {detail}, {secs:.0f}s{flag}")
    print(f"\nTraces: {OUT}")
    sys.exit(1 if changed else 0)


if __name__ == "__main__":
    main()
