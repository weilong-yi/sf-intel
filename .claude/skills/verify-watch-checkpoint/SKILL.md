---
name: verify-watch-checkpoint
description: Model-check sf-intel's monitoring-watch checkpoints with TLA+ (formal/WatchCheckpoint.tla, run by `python formal/check.py`). Proves or refutes five promises — no lost watch, no resurrected watch, no missed event, no duplicate alert, no thread stuck on a lock — over every interleaving of web requests, the scheduled `python -m src.watch run`, and data rebuilds. Use before and after any change to src/watch.py (file locks, store, add/remove, detect, run_watch), the watch endpoints in src/server.py, or how src/ingest.py / src/build.py produce occurred_at; and when a watch alert seems to have missed, repeated, lost or revived something.
---

# verify-watch-checkpoint

The check lives in `formal/`. `formal/README.md` states the five properties, what the check found and what fixed it,
the scope, and setup. Read it once before acting on a result.

## 1. Does the change touch the verified code?

Covered, as of the commit in `formal/SYNC`:

| File | What the model stands for |
|---|---|
| `src/watch.py` | `_file_lock` (cross-process, `data/locks/`), `_store` (every read/write of the watch files), `_read_watches` / `_write_watches` / `_atomic_write`, `_read_seen` / `_write_seen` / `prune_seen` (`data/watch_seen/<id>.json`), `load_watches`, `load_seen`, `same_watch` (id + `uid`), `baseline`, `append_alert`, `add_watch` (id = max + 1, `uid`, `baseline_data_at`, empty seen list), `remove_watch` (deletes seen list), `detect` (`baseline < occurred_at <= as_of`, minus seen ids), `run_watch` (run lock, re-read under `_store`), `_run_once` non-replay save, `main` `run` (skips `WatchGone`) |
| `src/server.py` | `POST /api/watches`, `/api/watches/delete`, `/api/watches/run` (loads `w` per request), `ThreadingHTTPServer` |
| `src/ingest.py`, `src/sources.py` | each ingest re-downloads the whole `WINDOW_DAYS` window by time column |
| `src/build.py` | which source timestamp becomes `event.occurred_at` (police: `incident_datetime`) |

Run `git diff --stat $(cat formal/SYNC) -- src/watch.py src/server.py src/ingest.py src/build.py src/sources.py`.
If nothing in that list changed in a way that touches those names, the check still applies as-is.

## 2. Run the check

```bash
.venv/Scripts/python.exe formal/check.py
```

See `formal/README.md` "Results" for runtime. Needs Java and `~/.tla/tla2tools.jar` v1.8.0 (`formal/README.md`, Setup). Output is one line
per scenario × property: verdict, expected verdict, and trace length or state count. Exit codes:

- `0` — every verdict equals `formal/expected.json`: `code` passes everything, and the two old designs still fail
  (they show each property can fail).
- `1` — a verdict changed (`<-- CHANGED`). Go to step 3.
- `2` — `MODEL OUT OF SYNC`: a design choice read from `src/watch.py` (`Locked`, `LockScope`, `RunLock`,
  `MatchUid`, `Checkpoint`) no longer matches `formal/code.cfg`. Go to step 4 first.

A `note: covered code changed since sync commit` line is a reminder, not a failure: do step 4's review.

## 3. Read a result

Read every trace (`formal/.out/<scenario>-<property>.txt`) with the pstack `/statechart` skill: it turns a
violation into its shortest path in plain words, and charts the state space when a check passes. Then retell
the path in this repo's words — `add_watch`, `run_watch`, `last_checked_data_at`, `watches.json`, `src.build`
— as `formal/README.md` "What it found" does.

Real bug or modeling error? Walk the trace against the code, one action at a time; each action in
`WatchCheckpoint.tla` names the lines it stands for in a comment.

- Every step is something the code allows → a real bug. Report it with the retold trace. Do not weaken the
  property, shrink the bounds, or flip `expected.json` to make it pass, and do not change product code unless
  the user asks.
- A step the code cannot take (the model is coarser or finer than the code, or an action no longer matches its
  cited lines) → a modeling error. Fix the model, say what was wrong, rerun.
- `VIOLATED` in `code` → a regression. Retell the trace and report it before anything else.
- A change in `process_lock` or `old_checkpoint` → those cfgs describe old commits, so the model changed under
  them: check that edit, not the code.

## 4. Update the model when the code changes on purpose

1. Map the change onto the model: which paths take the store lock → `Locked`; its scope → `LockScope`; the
   per-watch run lock → `RunLock`; saving by uid → `MatchUid`; a new checkpoint basis → a new `Checkpoint` value
   in `RunDetect`/`RunSave`; new writers of `watches.json` or `data/watch_seen/` → a new action that names its
   lines. If `check.py`'s `code_design()` can't see the new mechanism, teach it, so drift stays detectable.
2. Keep each action one indivisible step of the code: if the code reads and writes in separate statements
   without a shared lock, they are separate actions.
3. Show the changed property can still fail: break the code path it guards on a scratch edit (for example drop
   `with _store():` from `add_watch`), confirm `check.py` exits 2, mirror the edit in `code.cfg`, and confirm
   the property is reported `VIOLATED`. Then revert both.
4. Rerun `formal/check.py`. Update `formal/expected.json` only for verdicts the change intended.
5. Only after confirming the model describes the code at the new commit, write that commit to `formal/SYNC`
   (`git rev-parse --short HEAD > formal/SYNC`) and update `formal/README.md`.

## Scope reminder

A pass covers 2 web request threads (+1 CLI thread), ≤2 operations each, ≤2 rebuilds, 2 watch ids, 3 events,
with every event in every watch's area. Replay runs, LLM summaries, scheduling (`is_due`), the CLI `add`/`remove`
commands, and events aging out of the 90-day window (`prune_seen`) are outside the model.
File-system effects (corrupt or unreadable files) are outside the model too. Real-code checks that go with it:

- `experiments/04_late_police_report.py` (`NoMissedEvent`): hides a police incident, runs a watch, restores the
  incident behind the checkpoint, and confirms the next run reports it. Exit 0 = pass.
- `experiments/05_concurrent_watch_writes.py [SECONDS]` (the lock properties, plus corruption and crashes): runs
  the web-server pattern and a scheduled job as real processes on a scratch database. Exit 0 = pass.
