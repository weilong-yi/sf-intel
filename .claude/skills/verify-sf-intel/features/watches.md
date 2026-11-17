# Watches

A watch monitors one area for new events and unusual jumps. A person creates it in the Watches view (the
assistant can only propose one), runs it now or replays past weeks, reads the resulting alerts, and deletes it.
Definitions live in `data/watches.json` and every run appends one alert to `data/alerts.jsonl`.

## Sub-features

- `watch-create` creates a watch from the form and shows it in the list.
- `watch-create-error` shows an error with suggestions for an unresolvable place.
- `watch-run` runs a watch now, appends an alert and advances `CHECKED TO`.
- `watch-replay` replays the last 4 weeks as 4 alerts marked `REPLAY` without moving the checkpoint.
- `watch-select` filters the alert inbox to one watch; `Show all watches` clears the filter.
- `watch-delete` removes a watch after a two-step confirm.

## How to get to it (user POV)

- Choose the `Watches` tab and fill `Place`, `Area`, `Schedule`, sources and `Categories`, then `Create watch`.
- In Ask, choose `Create watch` on a proposal card, then `open in Watches` (see [ask.md](./ask.md)).
- Open `?watch=w1` to land on Watches with that watch selected.
- The CLI `python -m src.watch …` is a second surface for the same files; it is not driven here.

## Driving it with vctl

Preconditions:

- Baseline preconditions hold; `$V state` shows `"watches": []` and `"alert_count": 0`.
- For `watch-run` and `watch-replay`: doctor's `[info]` line says Claude auth is available, or you accept plain-text summaries.

- **Open.** Run `$B open ""`, `$B click --css 'button[role=tab][data-view=watches]'`, `$B wait --css '#watchList .empty' --text 'No watches yet'`.
- **Create.** Run `$B fill --css '#wPlace' --value '16th & Mission'`, `$B select --css '#wRadius' --value 200`, `$B click --css '#watchForm .checks label' --text 'Police'`, `$B fill --css '#wCats' --value 'Drug Offense'`, `$B click --css '#watchForm button[type=submit]'`. Then `$B wait --css '#wFormMsg' --text 'Created w1'`. `$B text --css '#watchList .wcard'` shows `w1 · Drug Offense · police · 200 m around 16TH ST & MISSION ST` and `LAST RUN NEVER`. Run `$B screenshot --name watch-create`.
- **On disk.** Run `$V state --save watch-create-state`. `watches[0]` has `id` `w1`, `radius_m` `200.0`, `sources` `["police"]`, `categories` `["Drug Offense"]`, `last_run_at` `null`, and `last_checked_data_at` 7 days before the data end.
- **Run now.** Run `$B click --css 'button[data-run=w1]'` and `$B wait --css '#alertList .alert' --timeout 180`. `$B text --css '#alertList .alert'` shows a status (`NEW`, `ATTENTION` or `QUIET`), the period, `N NEW` and `SUMMARY: …`. `$V state` shows `alert_count` 1, `last_alert.replay` false, and the watch's `last_run_at` set and `last_checked_data_at` = the data end. Run `$B screenshot --name watch-run`.
- **Replay.** Run `$B click --css 'button[data-replay=w1]'` and `$B wait --css '#alertList .alert-meta' --text 'REPLAY' --timeout 300`. `$V state` shows `alert_count` 5 and `last_checked_data_at` unchanged from the previous step.
- **Show all.** Run `$B click --css '#showAll'` and `$B wait --css '#inboxTitle' --text 'All watches'`.
- **Create error.** Run `$B fill --css '#wPlace' --value 'Nowhere Blvd & Fake St'`, `$B click --css '#watchForm button[type=submit]'`, `$B wait --css '#wFormMsg .err'`. `$V state` still shows one watch.
- **Delete.** Run `$B click --css 'button[data-del=w1]'`, then `$B click --css 'button[data-del=w1]' --text 'Confirm delete'`, then `$B wait --css '#watchList .empty' --text 'No watches yet'`. `$V state` shows `"watches": []` (alerts stay).

## Gotchas

- `Run now` and `Replay 4 weeks` call Claude once per run that finds new events. A quiet run makes no call. Without auth the alert still appears with `summary_by` `plain text (model unavailable: …)`; that proves the fallback, not the model summary.
- The first `Delete` click only arms the button (`Confirm delete`). A single click deletes nothing.
- The form's `Place` field is `required`; submitting it empty is blocked by the browser, so no request is sent.
- `CHECKED TO` uses data time (the newest event's `occurred_at`), not wall-clock time.
- Watch IDs are `max(existing)+1`, so after deletes a new watch may not be `w1`. Read the ID from `#wFormMsg`.
