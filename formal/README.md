# Formal check: watch checkpoints

A TLA+ model of how monitoring watches (`src/watch.py`) store their checkpoint in `data/watches.json`, run,
and meet data refreshes (`python -m src.ingest` + `python -m src.build`). The TLC model checker tries every
interleaving of web requests, a scheduled `python -m src.watch run`, and rebuilds, within the bounds below.

## The four promises

| Property | In one sentence | Harm if broken |
|---|---|---|
| `NoLostWatch` | A watch someone created and didn't delete is still in `watches.json`. | Monitoring silently stops for an area someone asked to watch. |
| `NoResurrectedWatch` | A deleted watch is gone from `watches.json`. | A deleted watch comes back and keeps alerting. |
| `NoMissedEvent` | No run leaves out an event that occurred after the watch was created and that the watch has never reported. | An incident in a watched area is never alerted on. |
| `NoDuplicateAlert` | No event appears in two regular alerts of the same watch. | Duplicate alerts, inflated "N new" counts, a doubled model bill. |

## Run it

```bash
python formal/check.py                       # all 16 checks, ~8 minutes; exit 0 = every verdict as expected
python formal/check.py code NoMissedEvent    # one check
```

It first checks the model still matches the code (exit 2 if not), then runs TLC once per scenario × property
and compares each verdict with `formal/expected.json` (exit 1 if any changed). Counterexample traces are in
`formal/.out/<scenario>-<property>.txt`.

Scenarios (`formal/*.cfg`):

- `code` — the code as it is: the web server (two concurrent requests) plus a scheduled CLI run.
- `server_only` — the same code with no CLI process, to show what `threading.Lock` does protect.
- `fixed` — a design with a machine-wide lock and run state read under that lock, plus the seen-id checkpoint.
  Every property passes here, so a violation in `code` is a property of the code, not a broken property.
- `old_checkpoint` — the code before the late-report fix (timestamp checkpoint, commit `b8672d7`). Kept so
  `NoMissedEvent` is shown failing on the design it guards against.

`code NoMissedEvent` explores the full state space (~41 million states, ~6 minutes); the rest take seconds.

## What it found

At commit `b8672d7` all four promises broke. Finding 1 is fixed in the working tree (see below); 2–4 remain.

1. **Fixed: a late police report was never alerted on** (`NoMissedEvent`; `old_checkpoint` scenario). A watch runs
   and reports events up to `as_of` = the newest `occurred_at`, then saves `last_checked_data_at = as_of`.
   The next `src.ingest` re-downloads 90 days, and `src.build` adds a police incident whose
   `incident_datetime` (= `occurred_at`) is *before* that checkpoint because it was reported late. `detect`
   only selects `occurred_at > since`, so that incident is never reported. This is the common case, not a
   race: in the current raw data 4,462 of 23,170 police rows (19%) were reported more than a day after the
   incident. `experiments/04_late_police_report.py` reproduces it on a scratch copy of the database.

   The fix: each watch keeps the event ids it has reported in `data/watch_seen/<id>.json`, and a run reports
   every matching event after `baseline_data_at` that is not in that list. Ids are added only after the alert
   is written, so nothing is marked reported without being reported, even when processes race (a race can
   only cause a repeat, which is finding 4). Late events are flagged `reported_late` and counted in the alert.
   The model now passes `NoMissedEvent` in `code` and `server_only`, with late arrivals on.
2. **Creating a watch while the scheduled job runs can delete it** (`NoLostWatch`). The CLI `run` loads
   `watches.json` under *its* `_lock`; meanwhile the web server's `add_watch` loads, appends `w2` and saves
   under the *server's* `_lock`. The CLI then saves its stale list with the updated checkpoint, so `w2` is gone.
   `threading.Lock` is per process, so it does not protect the file across processes.
3. **Deleting a watch while the scheduled job runs can bring it back** (`NoResurrectedWatch`). Same
   interleaving with `remove_watch`: the CLI's stale list still has the watch and is saved last.
4. **Two runs of one watch report the same events twice** (`NoDuplicateAlert`; also with only the web server).
   Two `Run now` clicks, or `Run now` during the scheduled run, each read the same `last_checked_data_at`
   outside the lock, so both alerts contain the same events (and each can call Claude).

`server_only` shows `NoLostWatch`, `NoResurrectedWatch` and `NoMissedEvent` hold when only the web server writes: the
`_lock` around each load-modify-save works within one process.

Possible fixes for 2–4, for the owner to choose: a cross-process file lock around every `watches.json` read-modify-write;
re-read `last_checked_data_at` under that lock at the start of a run and skip if another run is in progress.

## Scope

Verified: every interleaving of up to 2 operations per thread on 2 web request threads (+1 CLI thread in
`code`), at most 2 rebuilds, 2 watch ids and 3 events. Bugs this small appear in short traces (9–11 steps), so
small bounds are enough to find them; a PASS means "no violation within these bounds", not "for all sizes".

Assumed or left out:

- An event is identified by its `occurred_at`; every event is inside every watch's area and filters.
- A new watch's first checkpoint is `data_through` (the code subtracts `FIRST_LOOKBACK_DAYS`, which only adds
  events to the first alert).
- Replay runs (they never write `watches.json` or the seen list), the LLM summary (it never changes what is
  reported), `is_due` scheduling, and events leaving the 90-day window (so `prune_seen`) are not modeled.
- `add_watch` writes `watches.json` and the empty seen list as one step (both under its lock).
- `RunLoad` reads the seen list a little earlier than the code does (still under the lock); this allows more
  interleavings than the code, never fewer.
- The CLI's `add` and `remove` commands are not modeled; the web server is the only creator/deleter.
- A rebuild is atomic, and each run reads the database once. On Windows `src.build` may fail while the server
  holds the database open; late events still arrive at the next rebuild after a restart.

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

The model describes the covered code at the commit in `formal/SYNC`. Covered: `src/watch.py`
(`load_watches`, `save_watches`, `load_seen`, `save_seen`, `baseline`, `add_watch`, `remove_watch`, `detect`,
`run_watch`, `prune_seen`, `main` `run`),
`src/server.py` (`/api/watches`, `/api/watches/delete`, `/api/watches/run`), `src/ingest.py` + `src/sources.py`
(the 90-day re-download), `src/build.py` (`occurred_at` per source). `check.py` prints a diff stat when any
of these changed since `SYNC`, and refuses to run when `_lock` usage in `add_watch` / `remove_watch` /
`run_watch` no longer matches `Locked` in the cfgs, or the checkpoint style (`exclude=load_seen(...)` in
`run_watch`) no longer matches `Checkpoint`. The model describes the late-report fix, which is not committed
yet: when it is, write that commit to `formal/SYNC`. Move `SYNC` forward only after confirming the model
describes the code at the new commit.
