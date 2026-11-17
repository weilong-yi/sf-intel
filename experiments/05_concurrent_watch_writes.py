"""Do concurrent watch writers lose, revive, or double-report watches?

The real setup: the web server (one process, many request threads) creates, deletes and runs
watches, while a scheduled `python -m src.watch run` (a second process) runs them too. This
experiment recreates that on a scratch copy of the database for SECONDS seconds:

  "server" process   thread A: create a watch, sometimes delete one of its own, repeat
                     thread B: run every watch ("Run now"), repeat
  "cli" process      run every watch (the scheduled job), repeat

Then it checks the files the app wrote:
  lost        a watch whose last operation was "created" is missing from watches.json
  revived     a watch whose last operation was "deleted" is back in watches.json
  duplicate   one event appears in two regular alerts of the same watch
  corrupted   watches.json is not valid JSON; broken_alert_lines: alerts.jsonl lines that are not
  errors      an operation crashed (e.g. PermissionError reading a file being replaced)

No API calls (use_llm=False). Nothing in data/ is touched.

Run:  python experiments/05_concurrent_watch_writes.py [SECONDS] [--code DIR]
      --code points at another checkout's root (e.g. a git worktree of an older commit) to test that code.
"""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLACE = "16th & Mission"   # police Drug Offense within 200 m: < 40 events a week, so alerts list every event


def worker(role: str, code: Path, seconds: float) -> None:
    """Runs inside a child process whose working directory is the scratch folder."""
    sys.path.insert(0, str(code))
    from src import watch
    from src.assistant import connect

    con = connect()
    stop = time.time() + seconds
    errors = []

    def attempt(what, fn, *a, **kw):
        """Call fn; on any error record it (a crash is a finding too) and return None."""
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{role} {what}: {type(e).__name__}: {e}")
            return None

    def runner():
        cur = con.cursor()
        while time.time() < stop:
            for w in attempt("load", watch.load_watches) or []:
                attempt(f"run {w['id']}", watch.run_watch, cur, w, use_llm=False)

    def editor():
        cur, mine, n = con.cursor(), [], 0
        with open("ops.jsonl", "a", encoding="utf-8") as log:
            while time.time() < stop:
                n += 1
                w = attempt("add", watch.add_watch, cur, PLACE, 200, ["police"], ["Drug Offense"], name=f"exp-{n}")
                if w:  # only log what the user was told succeeded
                    log.write(json.dumps({"op": "add", "id": w["id"], "name": w["name"]}) + "\n")
                    log.flush()
                    mine.append(w)
                if len(mine) > 2 and random.random() < 0.6:
                    gone = mine.pop(random.randrange(len(mine)))
                    if attempt("remove", watch.remove_watch, gone["id"]) is not None:
                        log.write(json.dumps({"op": "remove", "id": gone["id"], "name": gone["name"]}) + "\n")
                        log.flush()
                time.sleep(random.uniform(0, 0.05))

    threads = [threading.Thread(target=runner)] + ([threading.Thread(target=editor)] if role == "server" else [])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    Path(f"errors-{role}.json").write_text(json.dumps(errors))


def check(scratch: Path) -> dict:
    raw = (scratch / "data" / "watches.json").read_text()
    try:
        watches = {w["id"]: w["name"] for w in json.loads(raw)}
    except json.JSONDecodeError as e:
        return {"watches_json": f"CORRUPTED ({e})", "first_bytes": raw[:120], "corrupted": True,
                "lost": [], "revived": [], "duplicate_reports": 0, "errors": 0, "alerts_truncated": 0}
    last = {}  # only the server's editor thread adds/removes, so its log order is the true order
    for line in (scratch / "ops.jsonl").read_text().splitlines():
        op = json.loads(line)
        last[op["id"]] = op
    lost = [f"{o['id']} ({o['name']})" for o in last.values() if o["op"] == "add" and watches.get(o["id"]) != o["name"]]
    revived = [f"{o['id']} ({o['name']})" for o in last.values() if o["op"] == "remove" and watches.get(o["id"]) == o["name"]]

    reported, truncated, runs, broken = defaultdict(Counter), 0, 0, 0
    alerts_file = scratch / "data" / "alerts.jsonl"
    for line in (alerts_file.read_text(encoding="utf-8").splitlines() if alerts_file.exists() else []):
        try:
            a = json.loads(line)
        except json.JSONDecodeError:  # two appends interleaved
            broken += 1
            continue
        if a["replay"]:
            continue
        runs += 1
        truncated += a["new_count"] != len(a["new_events"])
        reported[a["watch_name"]].update(e["event_id"] for e in a["new_events"])
    duplicates = sum(1 for c in reported.values() for n in c.values() if n > 1)
    everything = [e for p in scratch.glob("errors-*.json") for e in json.loads(p.read_text())]
    # A run that finds its watch deleted meanwhile and skips it is correct behavior, not an error.
    skipped = [e for e in everything if ": WatchGone:" in e]
    errors = [e for e in everything if ": WatchGone:" not in e]
    kinds = Counter(e.split(":")[0].split(" w")[0] + ": " + e.split(": ")[1] for e in errors)
    return {"watches_created": sum(1 for line in (scratch / "ops.jsonl").read_text().splitlines() if '"add"' in line),
            "runs": runs, "broken_alert_lines": broken, "lost": lost, "revived": revived, "duplicate_reports": duplicates,
            "alerts_truncated": truncated, "skipped_deleted": len(skipped), "errors": len(errors), "error_kinds": dict(kinds.most_common(6))}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if args and args[0] == "worker":
        return worker(args[1], Path(args[2]), float(args[3]))
    code = Path(args[args.index("--code") + 1]).resolve() if "--code" in args else REPO
    seconds = float(next((a for a in args if a.replace(".", "").isdigit()), 60))

    scratch = Path(tempfile.mkdtemp(prefix="sf-intel-race-"))
    (scratch / "data").mkdir()
    shutil.copy2(REPO / "data" / "sf_intel.duckdb", scratch / "data" / "sf_intel.duckdb")
    print(f"Testing code at {code} for {seconds:.0f}s (scratch {scratch})")

    py = sys.executable
    procs = [subprocess.Popen([py, str(Path(__file__).resolve()), "worker", role, str(code), str(seconds)], cwd=scratch)
             for role in ("server", "cli")]
    for p in procs:
        p.wait()
    result = check(scratch)
    print(json.dumps(result, indent=2))
    bad = (result.get("corrupted") or result.get("broken_alert_lines") or result["lost"] or result["revived"]
           or result["duplicate_reports"] or result["errors"])
    print(f"\nRESULT: {'PROBLEMS FOUND' if bad else 'no lost, revived, duplicate or crashed operations'}"
          f"{' (some alerts truncated: duplicates undercounted)' if result['alerts_truncated'] else ''}")
    shutil.rmtree(scratch, ignore_errors=True)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
