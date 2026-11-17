# sf-intel verification map

This directory is the maintained source for verifying the user-facing behavior of the sf-intel web UI.
Read this index before driving the app, then use the matching feature file as the recipe.

## Baseline preconditions

- `data/sf_intel.duckdb` is built from the current `data/raw/*.csv` (README steps 2–3).
- An instance is launched with `$V launch` and `$V doctor --run <id>` is all `[ok]`.
- `V=".venv/Scripts/python.exe .claude/skills/verify-sf-intel/scripts/vctl.py"` and `B="$V browser"` are set in the shell.
- The run's scratch `data/` starts with no watches and no alerts.
- Never drive an instance that was not started by this verification run (the user's own server on 8765 included).

## Driving conventions

- Start every recipe with `$B open ""` (a fresh page load) unless its preconditions say otherwise.
- Prefer ids, `role`/`data-*` attributes and visible text over DOM position.
- Treat every command as literal. Keep quoted selectors and values unchanged.
- Pass `open` targets without a leading `/` (Git Bash path conversion).
- `--text` matching is case-insensitive; on-screen labels are often uppercased by CSS.
- Data-dependent numbers (counts, IDs) change with every ingest. Assert relationships (exact ≤ 100 m area,
  UI count = API count) and the README's documented facts only when the data window matches
  (2026-08-17 → 2026-11-15).
- Restore mutated state (delete created watches) at the end of a recipe. Never remove proof artifacts.

## Proof and skip reporting

- Capture the user action and the resulting state, not only the final screen: `actions.log` plus a screenshot after each claimed state.
- UI proof includes a `text` read of the asserted element and a screenshot in which the SF Intel header is visible.
- Mutation proof includes a second view of the stored value: `$V state` (disk) or `$V api GET …`.
- Record the feature ID and entry point used with every artifact (put the feature ID in the screenshot name).
- Report an unreachable path with the attempted command and the unmet precondition.
- Do not report a skipped entry point as verified through a different path.
- Features that call Claude (Ask; watch Run now / Replay when events are found) cost money. Run them only
  when the change touches them, and report "unverified: no Claude auth" when doctor says auth is missing.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then
uses exactly four H2 sections in this order: `Sub-features`, `How to get to it (user POV)`,
`Driving it with vctl`, `Gotchas`.

## Features

- [Place profile](./places.md) covers search, radius switching, busiest-place links, deep links and unknown-place suggestions.
- [Event provenance drawer](./event-drawer.md) covers opening any event ID and seeing its unified, cleaned and raw records.
- [Watches](./watches.md) covers creating, running, replaying and deleting a monitoring watch, and its alerts.
- [SQL view](./sql.md) covers read-only queries, example queries and the write/config guardrail.
- [Ask the assistant](./ask.md) covers a streamed, cited answer with visible tool calls, and a proposed watch. Calls Claude.
