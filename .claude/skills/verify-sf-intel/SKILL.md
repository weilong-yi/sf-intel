---
name: verify-sf-intel
description: Launch an isolated copy of the sf-intel web UI (src/server.py + src/ui/index.html) and drive it in headless Chrome the way a user does — Places, event provenance drawer, Watches, SQL, Ask — capturing screenshots, page text, API responses and on-disk side effects as proof. Use after any change to src/server.py, src/ui/, src/dossier.py, src/watch.py, src/assistant.py or the built database, or whenever you need to prove sf-intel behavior in the real app rather than in a script.
---

# verify-sf-intel

Drives the real sf-intel web UI through one stdlib-only helper, `scripts/vctl.py`. It never touches the
user's own server (usually on port 8765) or the repo's `data/watches.json`, `data/alerts.jsonl` and
`data/audit_log.jsonl`: every run gets a scratch copy of the database, its own port and its own Chrome profile.

All commands run from the sf-intel repo root in Git Bash, with the project venv:

```bash
V=".venv/Scripts/python.exe .claude/skills/verify-sf-intel/scripts/vctl.py"
B="$V browser"
```

Chain steps with `&&` so the first failure stops the journey. `vctl` exits non-zero on any failed wait,
missing element or HTTP error ≥ 400.

## Launch

Preconditions: `.venv` exists with `requirements.txt` installed, and `data/sf_intel.duckdb` has been built
(`python -m src.ingest` then `python -m src.build`; README steps 2–3). Chrome or Edge is installed.

```bash
$V launch                 # add --effort low to make Ask cheaper (sets CLAUDE_EFFORT for this instance)
```

It prints the run id, URL and artifact folder:

```json
{"run": "20261116-213536", "url": "http://127.0.0.1:62158", "artifacts": "...\\sf-intel\\artifacts\\verify\\20261116-213536", "git_head": "b8672d7"}
```

What it does: copies `data/sf_intel.duckdb` to `%TEMP%\sf-intel-verify\<run>\data\`, starts
`python -m src.server --no-browser` with that folder as the working directory (`PYTHONPATH` = repo,
`SF_INTEL_PORT` = a free port), waits until `/api/overview` returns events, then starts headless Chrome
with a private profile. About 10 seconds. Launch is ready only when it prints the JSON; on failure it prints
the server log tail, and you run `stop` before retrying.

To keep the id in the shell:

```bash
RUN=$($V launch | .venv/Scripts/python.exe -c "import json,sys; print(json.load(sys.stdin)['run'])")
```

Pass `--run $RUN` to every later command when more than one run is live. With exactly one live run it is optional.

## Doctor

Run first, and again whenever anything looks off:

```bash
$V doctor --run <id>
```

Every line must be `[ok]`. It checks: the server process is alive; the port's listener is that server
(the venv `python.exe` is a launcher, so the listener is its child process); the `Server` header is
`sf-intel/…`; the database has events and its date window; the server serves this run's `watches.json`, not
the repo's; the launched commit equals `HEAD` (relaunch after pulling or editing Python code — the server
does not reload, though `src/ui/index.html` is re-read on every page load); Chrome's debug port answers.
An `[info]` line says whether Claude API auth exists (`ant auth status`, `ANTHROPIC_API_KEY`, or `.env`).

Never drive an instance this run did not launch. If doctor fails, `stop` and `launch` again.

## Drive

`vctl browser` actions, all against the run's single Chrome tab:

| Action | Example | Notes |
|---|---|---|
| `open` | `$B open ""` · `$B open "?view=places&place=16th%20%26%20Mission&radius=100"` | Path **without** a leading `/`: Git Bash rewrites `/…` into a Windows path. |
| `click` | `$B click --css 'button[role=tab][data-view=places]'` | Real mouse press/release at the element's centre. `--text` narrows to elements whose text contains it. |
| `fill` | `$B fill --css '#placeInput' --value '16th and mission'` | Clicks, clears, then types via `Input.insertText` (fires real input events). |
| `press` | `$B press --key Enter` | `Enter`, `Escape`, `Tab`. Enter submits the focused form. |
| `select` | `$B select --css '#wRadius' --value 200` | Sets a `<select>` and fires `change`. |
| `wait` | `$B wait --css '#placeOut h2'` · `--text 'exact intersection'` · `--gone` · `--timeout 120` | Polls every 250 ms; default 30 s. |
| `text` | `$B text --css '#placeOut .kpi .big' --all` | Prints `innerText`. |
| `screenshot` | `$B screenshot --name places-100m` | Full page, to the artifact folder. |
| `eval` | `$B eval "document.title"` | Escape hatch; prefer the actions above for proof. |

`--text` matching is case-insensitive on purpose: many labels are uppercased by CSS (`.eyebrow`, `h3`,
`th`), and `innerText` returns the transformed text.

Stable handles in `src/ui/index.html`: tabs are `button[role=tab][data-view=ask|places|watches|sql]`;
views are `#view-<name>`; event links are `button.evlink[data-ev]` / `button.cite[data-ev]`; place links are
`[data-open-place]`; watch buttons are `[data-run=wN]`, `[data-replay=wN]`, `[data-del=wN]`. Feature
recipes are in [features/README.md](features/README.md). Use the matching feature file as the recipe, and
cover every entry point it lists when proving that feature.

Second, non-UI views for side effects:

```bash
$V api GET '/api/watches'                      # HTTP view of the same instance (add --save NAME to keep it)
$V api POST '/api/sql' '{"sql":"SELECT 1"}'
$V state --save after-create                   # this run's data/watches.json + alerts.jsonl, read from disk
```

## Evidence

Everything lands in `artifacts/verify/<run>/` in the repo (git-ignored), and survives `stop`:

- `actions.log` — every browser/api/state action with its result, timestamped. This is the action half of the proof.
- `*.png` — screenshots you name. Take one after each state change you claim, not only at the end.
- `*.json` — `api --save` / `state --save` snapshots.
- `server.log` — the instance's request log, copied at `stop`.

Proof standards:

- Exercise the user path (tabs, forms, buttons, deep links), not the API alone. Use `api`/`state` as the
  second view that confirms what the UI showed.
- Capture the action and the resulting state: a `wait` on the result, then `text` and a screenshot.
- Verify side effects: a created watch must appear in `state` (on disk), not only in the UI list. An alert
  must appear in `alerts.jsonl`.
- Do not mock. The only external boundary is the Claude API (Ask, watch summaries). When auth is missing,
  report Ask as unverified, and expect watch alerts to show `summary_by` = `plain text (model unavailable: …)`.
- Report the run id, the git head from `launch`, and the artifact paths with every claim.

## Cleanup

```bash
$V stop --run <id>
```

Kills only this run's Chrome and server process trees (`taskkill /T` on the recorded PIDs, never by name),
deletes `%TEMP%\sf-intel-verify\<run>\`, copies `server.log` into the artifact folder, and prints how many
evidence files were kept. Run it after every attempt, including failed ones. Delete old
`artifacts/verify/<run>/` folders by hand when they are no longer needed.

## Helpers

- `scripts/vctl.py` — the whole harness. `$V --help` lists commands. Standard library only (a ~60-line
  websocket client speaks the Chrome DevTools Protocol), so it needs no new packages.

## Costs and limits

- Places, event drawer, Watches create/delete and SQL make no model calls.
- Ask always calls Claude (default `claude-opus-5-5`, `CLAUDE_EFFORT=medium`; launch with `--effort low` to
  cut cost). Watch **Run now** and **Replay 4 weeks** call Claude once per run that finds new events
  (low effort, `max_tokens` 2000). Replay = up to 4 calls.
- Ask questions write to the scratch `data/audit_log.jsonl`, never the repo's.
