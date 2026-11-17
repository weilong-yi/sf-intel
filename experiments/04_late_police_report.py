"""Does a watch report a police incident that is published late?

Police events use the incident time as occurred_at, and about a fifth of police reports are filed
more than a day after the incident. So a data refresh can add an event whose occurred_at is already
behind a watch's checkpoint. This experiment replays that on a scratch copy of the database:

  1. hide one recent police incident near 16th & Mission (as if not yet published)
  2. create a watch and run it          -> checkpoint moves to the newest data
  3. un-hide the incident (the refresh that brings the late report)
  4. run the watch again                -> is the late incident reported?

No API calls (use_llm=False). Nothing in data/ is touched: everything runs in a temp folder.

Run:  python experiments/04_late_police_report.py
"""

import datetime as dt
import os
import shutil
import sys
import tempfile
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src import dossier, watch  # noqa: E402

PLACE, RADIUS = "16th & Mission", 200


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    scratch =Path(tempfile.mkdtemp(prefix="sf-intel-late-"))
    (scratch / "data").mkdir()
    shutil.copy2(REPO / "data" / "sf_intel.duckdb", scratch / "data" / "sf_intel.duckdb")
    os.chdir(scratch)  # watch.py keeps watches.json / alerts.jsonl relative to the working directory
    con = duckdb.connect("data/sf_intel.duckdb")  # writable scratch copy, so we can hide/restore one event

    end = watch.data_through(con)
    loc = dossier.resolve(con, PLACE)["location_id"]
    area = dossier.area_location_ids(con, loc, RADIUS)
    late = con.execute("""
        SELECT event_id, occurred_at FROM event
        WHERE source = 'police' AND list_contains(?, location_id)
          AND occurred_at BETWEEN ? AND ?
        ORDER BY occurred_at DESC LIMIT 1""", [area, end - dt.timedelta(days=6), end - dt.timedelta(days=1)]).fetchone()
    event_id, occurred = late
    print(f"Data through {end:%Y-%m-%d %H:%M}. Late incident: {event_id} at {occurred:%Y-%m-%d %H:%M}")

    con.execute("CREATE TEMP TABLE held AS SELECT * FROM event WHERE event_id = ?", [event_id])
    con.execute("DELETE FROM event WHERE event_id = ?", [event_id])
    print("1. hidden it, as if not yet published")

    w = watch.add_watch(con, PLACE, RADIUS, sources=["police"])
    a1 = watch.run_watch(con, w, use_llm=False)
    print(f"2. created {w['id']} and ran it: {a1['new_count']} new, checkpoint now {a1['period_to'][:16]}")

    con.execute("INSERT INTO event SELECT * FROM held")
    print(f"3. refresh brings the late report ({occurred:%Y-%m-%d %H:%M}, behind the checkpoint)")

    w = next(x for x in watch.load_watches() if x["id"] == w["id"])
    a2 = watch.run_watch(con, w, use_llm=False)
    ids = {e["event_id"] for e in a2["new_events"]}
    print(f"4. ran it again: {a2['new_count']} new ({a2.get('late_count', '?')} published late), status {a2['status']}")
    print(f"   summary: {a2['summary']}")

    w = next(x for x in watch.load_watches() if x["id"] == w["id"])
    a3 = watch.run_watch(con, w, use_llm=False)
    print(f"5. ran it a third time with no new data: {a3['new_count']} new (expect 0: nothing reported twice)")

    replays = watch.replay(con, w, 2, use_llm=False)
    print(f"6. replayed 2 weeks: {[r['new_count'] for r in replays]} new per week (replay ignores what was reported)")

    ok = event_id in ids and a3["new_count"] == 0 and sum(r["new_count"] for r in replays) > 0
    print(f"\nRESULT: late incident {'REPORTED' if event_id in ids else 'MISSED'}; "
          f"{'no repeats, replay works' if ok else 'CHECK STEPS 5-6'}")
    ids = ids if ok else set()

    con.close()
    os.chdir(REPO)
    shutil.rmtree(scratch, ignore_errors=True)
    sys.exit(0 if event_id in ids else 1)


if __name__ == "__main__":
    main()
