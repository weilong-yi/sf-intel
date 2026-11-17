# Formal check: watch checkpoints

A TLA+ model of how monitoring watches (`src/watch.py`) keep their state in `data/watches.json` and
`data/watch_seen/`, run, and meet data refreshes (`python -m src.ingest` + `python -m src.build`). The TLC model
checker tries every interleaving of web requests, a scheduled `python -m src.watch run`, and rebuilds, within
the bounds below.

## The five promises

| Property | In one sentence | Harm if broken |
|---|---|---|
| `NoLostWatch` | A watch someone created and didn't delete is still in `watches.json`. | Monitoring silently stops for an area someone asked to watch. |
| `NoResurrectedWatch` | A deleted watch is gone from `watches.json`. | A deleted watch comes back and keeps alerting. |
| `NoMissedEvent` | No run leaves out an event that occurred after the watch was created and that the watch has never reported. | An incident in a watched area is never alerted on. |
| `NoDuplicateAlert` | No event appears in two regular alerts of the same watch. | Duplicate alerts, inflated "N new" counts, a doubled model bill. |
| `NoStuckThread` | While any operation is in progress, some thread can still take a step. | The web server or the scheduled job hangs on a lock forever. |

## Run it

```bash
python formal/check.py                       # all 15 checks; exit 0 = every verdict as expected
python formal/check.py code NoMissedEvent    # one check
```

It first checks that the model still matches the code (exit 2 if not), then runs TLC once per scenario ×
property and compares each verdict with `formal/expected.json` (exit 1 if any changed). Counterexample traces are
in `formal/.out/<scenario>-<property>.txt`. Runtime: see "Results" below.

Scenarios (`formal/*.cfg`):

- `code` — the code as it is: one store lock shared by all processes, a per-watch run lock, uid matching, and
  the seen-id checkpoint. The web server (two concurrent requests) and a scheduled CLI run share the files.
- `process_lock` — commit `d5615a5`: the seen-id checkpoint, but a per-process `threading.Lock`, no run lock,
  and saves matched by id. Kept as the failing witness for the lock properties.
- `old_checkpoint` — commit `b8672d7`: the timestamp checkpoint and per-process lock. Kept as the failing
  witness for `NoMissedEvent`.

## Results

| Scenario | NoLostWatch | NoResurrectedWatch | NoMissedEvent | NoDuplicateAlert | NoStuckThread |
|---|---|---|---|---|---|
| `code` | PASS | PASS | PASS | PASS | PASS |
| `process_lock` | VIOLATED (11 steps) | VIOLATED (12) | VIOLATED (16) | VIOLATED (11) | not checked |
| `old_checkpoint` | VIOLATED (12) | VIOLATED (12) | VIOLATED (13) | VIOLATED (10) | not checked |

Each `code` check explores all 3,132,916 reachable states in about 35–40 s; a full `check.py` run takes about
4 minutes. The old designs only need to fail, so they skip `NoStuckThread` (a full search of their larger state
spaces took 12 minutes and longer).

## What it found, and what fixed it

At commit `b8672d7` the first four promises broke. All are fixed now.

1. **A late police report was never alerted on** (`NoMissedEvent`, `old_checkpoint`). A watch reported events
   up to `as_of` = the newest `occurred_at`, then saved `last_checked_data_at = as_of`. The next ingest + build
   added a police incident whose `incident_datetime` (= `occurred_at`) was *before* that checkpoint because it
   was reported late; `detect` only selected `occurred_at > since`. In the current raw data 4,462 of 23,170
   police rows (19%) were reported more than a day after the incident.
   *Fix (commit `d5615a5`):* each watch keeps the event ids it has reported in `data/watch_seen/<id>.json`; a
   run reports every matching event after `baseline_data_at` that is not in that list, and flags late ones.
   Real-code check: `experiments/04_late_police_report.py`.
2. **Creating a watch while the scheduled job ran could delete it** (`NoLostWatch`). Each process had its own
   `threading.Lock`, so the CLI could load `watches.json`, the web server could add `w2` and save, and the CLI
   could then save its stale list over it.
3. **Deleting a watch while the scheduled job ran could bring it back** (`NoResurrectedWatch`). Same
   interleaving with `remove_watch`.
4. **Two runs of one watch reported the same events twice** (`NoDuplicateAlert`). Both read the same state
   outside any lock.
5. **A run could save into a different watch that reused its id** (`NoMissedEvent`, `process_lock`). Ids are
   `max + 1`, so after a delete a new watch can get the old id. A run of the deleted watch then wrote its
   reported ids into the new watch's seen list, and the new watch never reported those events.

*Fix for 2–5 (this change):* `_store()`, a file lock on `data/locks/store.lock` (`msvcrt.locking` /
`fcntl.flock`) taken by every read and write of `watches.json`, `watch_seen/` and `alerts.jsonl`, in every
process; `_file_lock("run-<id>")`, held by a run from re-reading its watch and seen list to saving them; and a
`uid` per watch, so a run saves only into the watch it started with (`same_watch`).

Outside the model, `experiments/05_concurrent_watch_writes.py` runs the web-server pattern and a scheduled job
as real processes for a minute. On `d5615a5` it found, every time: `watches.json` corrupted (both processes
wrote the same `watches.tmp`), and with that patched out, revived watches, thousands of duplicate reports,
interleaved `alerts.jsonl` lines, and `PermissionError` crashes (Windows refuses to replace a file another
handle has open). On the current code: none of these, over ~900 runs a minute.

## Scope

Verified: every interleaving of up to 2 operations per thread on 2 web request threads plus 1 CLI thread, at
most 2 rebuilds, 2 watch ids and 3 events. These bugs appear in short traces (9–13 steps), so small bounds are
enough to find them; a PASS means "no violation within these bounds", not "for all sizes".

Assumed or left out:

- An event is identified by its `occurred_at`; every event is inside every watch's area and filters.
- A new watch's baseline is `data_through` (the code subtracts `FIRST_LOOKBACK_DAYS`, which only adds events to
  the first alert).
- Replay runs (they never write `watches.json` or the seen list), the LLM summary (it never changes what is
  reported), `is_due` scheduling, and events leaving the 90-day window (so `prune_seen`) are not modeled.
- File-system effects (a corrupt or unreadable file from unlocked writes) are not modeled; experiment 05 covers
  them.
- `add_watch` writes `watches.json` and the empty seen list as one step (both under the store lock).
- In `code`, a run's first step reads its watch and seen list under the store lock without holding it
  afterwards, which matches `run_watch`.
- The CLI's `add` and `remove` commands are not modeled; the web server is the only creator/deleter. Both take
  the same store lock, so this changes no interleaving that matters for the properties.
- A lock wait is unbounded in the model; in the code it times out (30 s for the store, 600 s for a run) with
  a clear error.
- A rebuild is atomic, and each run reads the database once.

## Setup

Java 11+ and the TLA+ tools jar, v1.8.0:

```powershell
winget install --id EclipseAdoptium.Temurin.21.JRE -e
mkdir $HOME\.tla
curl.exe -L -o $HOME\.tla\tla2tools.jar https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar
# sha256 7e53aa636dac8a3cb0533f343115c0548e03fc0ce5edc7739becf58d3d0fb84f
```

`check.py` finds Java on `PATH` or under `C:\Program Files\Eclipse Adoptium\`; set `JAVA` or `TLA2TOOLS`
to override.

## Keeping it in step

The model describes the covered code at the commit in `formal/SYNC`. Covered: `src/watch.py` (`_file_lock`,
`_store`, `_read_watches` / `_write_watches`, `_read_seen` / `_write_seen`, `load_watches`, `load_seen`,
`same_watch`, `baseline`, `append_alert`, `add_watch`, `remove_watch`, `detect`, `run_watch`, `_run_once`,
`prune_seen`, `main` `run`), `src/server.py` (`/api/watches`, `/api/watches/delete`, `/api/watches/run`),
`src/ingest.py` + `src/sources.py` (the 90-day re-download), `src/build.py` (`occurred_at` per source).

`check.py` prints a diff stat when any of these changed since `SYNC`, and refuses to run (exit 2) when the code
no longer matches `formal/code.cfg` on any of: which of `add_watch` / `remove_watch` / `_run_once` take
`_store()` (`Locked`), whether `_store` is a file lock (`LockScope`), whether `run_watch` takes the run lock
(`RunLock`), whether `_run_once` saves via `same_watch` (`MatchUid`), and the checkpoint style (`Checkpoint`).
Move `SYNC` forward only after confirming the model describes the code at the new commit.
