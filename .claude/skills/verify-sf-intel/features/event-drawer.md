# Event provenance drawer

Any event ID shown in the app opens a side drawer that traces the unified event back to its sources: the
unified `event` row, the linked place, the cleaned staging record, and the raw rows exactly as the city published them.

## Sub-features

- `drawer-open` opens the drawer from an event ID button.
- `drawer-sections` shows Unified event, Linked place (when matched), Cleaned source record and Raw rows.
- `drawer-close` closes with `Close`, the scrim, or Escape.
- `drawer-deep-link` opens the drawer from `?event=<source>:<id>`.
- `drawer-open-place` jumps from the linked place to its Places profile.

## How to get to it (user POV)

- Choose an event ID in Places → `Most recent events`.
- Choose an event ID in an SQL result (`event_id` column) or a citation chip in an Ask answer.
- Open a URL such as `?event=police:<incident_id>`.

## Driving it with vctl

Preconditions:

- Baseline preconditions hold. A place profile is open (see [places.md](./places.md), **Search**).

- **Open.** Choose the first recent event. Run `$B click --css '#placeOut button.evlink'` and `$B wait --css '#drawer'`. `$B text --css '#drawerTitle'` prints the event ID that was clicked (for example `311:202000309615`).
- **Sections.** Run `$B text --css '#drawerBody h3' --all`. It lists `UNIFIED EVENT`, `LINKED PLACE`, `CLEANED SOURCE RECORD`, `RAW ROWS AS PUBLISHED BY THE SOURCE`. Run `$B screenshot --name drawer-sections`.
- **Second view.** Run `$V api GET '/api/event?id=<that id>' --save drawer-api`. `event.event_id` matches and `raw_rows` is non-empty.
- **Close.** Run `$B press --key Escape` and `$B wait --css '#drawer' --gone --timeout 5`.
- **Deep link.** Run `$B open "?event=<that id>"` and `$B wait --css '#drawer'`. The title shows the same ID.
- **Open place.** In the drawer, run `$B click --css '#drawerBody [data-open-place]'` and `$B wait --css '#placeOut h2'`. The drawer closes and the Places profile of the linked place renders.

## Gotchas

- `police` events with no location have no `LINKED PLACE` section and no `Open place profile` button.
- Police raw rows are one per offense, so one incident can show several raw rows (the drawer caps at 25).
- An event ID that does not exist returns HTTP 404 from `/api/event`; the drawer shows the error text.
