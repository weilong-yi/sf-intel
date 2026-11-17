# Ask the assistant

Ask lets a user question the data in plain English. The answer streams in step by step: each model step,
each tool call (`location_profile`, `run_sql`, `propose_watch`) with its result, then a cited answer whose
`[source:id]` chips open the event drawer. Follow-up questions keep context until `New conversation`. The
assistant can propose a watch, which a person must confirm.

## Sub-features

- `ask-answer` streams steps and tool calls, then shows an answer with model name and running cost.
- `ask-citations` renders cited event IDs as buttons that open the event drawer.
- `ask-followup` answers a follow-up using earlier context.
- `ask-new` clears the thread with `New conversation`.
- `ask-propose-watch` shows a proposal card whose `Create watch` writes a watch.
- `ask-from-place` asks about the open place from Places' `Ask the AI about this place`.

## How to get to it (user POV)

- Choose the `Ask` tab (the default first view), type in `Question`, press Enter or choose `Ask`.
- Choose a suggested question on the empty Ask view.
- Choose `Ask the AI about this place` on a Places profile.
- Open `?q=<question>`.

## Driving it with vctl

Preconditions:

- Baseline preconditions hold, and doctor's `[info]` line says Claude auth is available. Otherwise report every Ask sub-feature as unverified with that reason.
- Consider `$V launch --effort low` to reduce cost. Each question costs cents, not dollars, at default settings.

- **Ask.** Run `$B open ""`, `$B fill --css '#askInput' --value 'How many police incidents were within 100 m of 16th & Mission in the last 30 days? Cite two of them.'`, `$B press --key Enter`. Then `$B wait --css '#thread .turn .answer' --timeout 240`.
- **Read the work.** Run `$B text --css '#thread .turn details.work summary'`. It shows `N tool call(s) · Ss · show work` with N ≥ 1. `$B text --css '#thread .tcall .head' --all` lists the tools used.
- **Answer and citations.** Run `$B text --css '#thread .turn .ans'`. It contains a number and the model name with `conversation so far ~$`. `$B eval "[...document.querySelectorAll('#thread .cite')].map(b => b.dataset.ev)"` returns at least one `police:` ID. Run `$B screenshot --name ask-answer`.
- **Citation opens drawer.** Run `$B click --css '#thread .cite'` and `$B wait --css '#drawer'`. The drawer title equals the cited ID. Close with `$B press --key Escape`.
- **Second view.** Run `tail -n 3 "$TEMP/sf-intel-verify/<run>/data/audit_log.jsonl"`. The last lines hold this question, the tool calls and the answer. The repo's own `data/audit_log.jsonl` is unchanged.
- **Propose watch.** Run `$B fill --css '#askInput' --value 'Watch 16th & Mission for drug offenses daily.'`, `$B press --key Enter`, `$B wait --css '#thread .proposal' --timeout 240`. `$V state` still shows no watches. Run `$B click --css '#thread .proposal button' --text 'Create watch'` and `$B wait --css '#thread .proposal .pmsg' --text 'Created'`. Now `$V state` shows one watch. Run `$B screenshot --name ask-propose-watch`.
- **New conversation.** Run `$B click --css '#newChat'` and `$B wait --css '#hello'`.

## Gotchas

- The model's answer text varies run to run. Assert structure (tool calls happened, citations exist and resolve), not wording.
- Enter sends; Shift+Enter adds a newline. `press --key Enter` sends.
- Errors (auth expired, API down) appear in the turn as `.err` with `Stopped` in the summary; wait for `.ans .err` too if a run hangs.
- `MAX_STEPS` is 8. A question that needs more steps ends without an answer; that is a cap, not a hang.
- Delete the watch created in **Propose watch** before ending the run (see [watches.md](./watches.md), **Delete**).
