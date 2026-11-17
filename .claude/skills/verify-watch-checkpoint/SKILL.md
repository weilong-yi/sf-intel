---
name: verify-watch-checkpoint
description: Model-check sf-intel's monitoring-watch checkpoints with TLA+ (formal/WatchCheckpoint.tla, run by `python formal/check.py`). Proves or refutes four promises — no lost watch, no resurrected watch, no missed event, no duplicate alert — over every interleaving of web requests, the scheduled `python -m src.watch run`, and data rebuilds. Use before and after any change to src/watch.py (store, add/remove, detect, run_watch), the watch endpoints in src/server.py, or how src/ingest.py / src/build.py produce occurred_at; and when a watch alert seems to have missed, repeated, lost or revived something.
---

# verify-watch-checkpoint

The check lives in `formal/`. `formal/README.md` states the four properties, the findings at the sync commit,
the scope, and setup. Read it once before acting on a result.

## 1. Does the change touch the verified code?

Covered, as of the commit in `formal/SYNC`:

| File | What the model stands for |
|---|---|
| `src/watch.py` | `load_watches` / `save_watches` (whole-file read, atomic replace), `load_seen` / `save_seen` / `prune_seen` (`data/watch_seen/<id>.json`), `baseline`, `add_watch` (id = max + 1, `baseline_data_at`, empty seen list), `remove_watch` (always saves, deletes seen list), `detect` (`baseline < occurred_at <= as_of`, minus seen ids), `run_watch` non-replay path (snapshot `w`, `load_seen`, `data_through`, `append_alert`, locked checkpoint + seen save), `main` `run` (loads watches once), `_lock` |
| `src/server.py` | `POST /api/watches`, `/api/watches/delete`, `/api/watches/run` (loads `w` per request), `ThreadingHTTPServer` |
| `src/ingest.py`, `src/sources.py` | each ingest re-downloads the whole `WINDOW_DAYS` window by time column |
| `src/build.py` | which source timestamp becomes `event.occurred_at` (police: `incident_datetime`) |

Run `git diff --stat $(cat formal/SYNC) -- src/watch.py src/server.py src/ingest.py src/build.py src/sources.py`.
If nothing in that list changed in a way that touches those names, the check still applies as-is.

## 2. Run the check

```bash
.venv/Scripts/python.exe formal/check.py
```

About 8 minutes (`code NoMissedEvent` alone is ~6). Needs Java and `~/.tla/tla2tools.jar` v1.8.0 (`formal/README.md`, Setup). Output is one line
per scenario × property: verdict, expected verdict, and trace length or state count. Exit codes:

- `0` — every verdict equals `formal/expected.json`. Nothing regressed; the known findings are still there.
- `1` — a verdict changed (`<-- CHANGED`). Go to step 3.
- `2` — `MODEL OUT OF SYNC`: the code's `with _lock:` usage in `add_watch` / `remove_watch` / `run_watch` no
  longer matches `Locked` in `formal/code.cfg` and `formal/server_only.cfg`, or `run_watch`'s checkpoint style
  (`exclude=load_seen(...)`) no longer matches `Checkpoint`. Go to step 4 first.

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
- `PASS` where `expected.json` says `VIOLATED` → if the change was meant to fix that finding, confirm the
  `fixed`-style mechanism is really in the code and in the model, then update `expected.json` and
  `formal/README.md` "What it found". Otherwise the model probably drifted from the code: step 4.

## 4. Update the model when the code changes on purpose

1. Map the change onto the model: a new lock or lock scope → `Locked` / `LockScope` (+ a new scope value if
   needed, e.g. a file lock); run state read under the lock → `RunHoldsLock`; a new checkpoint basis → a new
   `Checkpoint` value in `RunDetect`/`RunSave` (keep `old_checkpoint.cfg` as the failing witness); new writers
   of `watches.json` or `data/watch_seen/` → a new action that names its lines.
2. Keep each action one indivisible step of the code: if the code reads and writes in separate statements
   without a shared lock, they are separate actions.
3. Show the changed property can still fail: break the code path it guards on a scratch edit (for example drop
   `with _lock:` from `add_watch`), mirror that in the cfg, and confirm `check.py` reports the violation. Then
   revert the scratch edit.
4. Rerun `formal/check.py`. Update `formal/expected.json` only for verdicts the change intended.
5. Only after confirming the model describes the code at the new commit, write that commit to `formal/SYNC`
   (`git rev-parse --short HEAD > formal/SYNC`) and update `formal/README.md`.

## Scope reminder

A pass covers 2 web request threads (+1 CLI thread), ≤2 operations each, ≤2 rebuilds, 2 watch ids, 3 events,
with every event in every watch's area. Replay runs, LLM summaries, scheduling (`is_due`), the CLI `add`/`remove`
commands, and events aging out of the 90-day window (`prune_seen`) are outside the model.
`experiments/04_late_police_report.py` is the matching real-code check for `NoMissedEvent`: it hides a police
incident, runs a watch, restores the incident behind the checkpoint, and confirms the next run reports it.
