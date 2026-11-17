# Place profile

Places lets a user look up an intersection or street address and see what police, fire/EMS and 311 recorded
around it over 90 days: per-source totals and 30-day trends, intersections in the area, top categories, fire/EMS
response times, open 311 requests and the most recent events. The area radius (Exact, 50, 100, 200 m) changes
every number on the page.

## Sub-features

- `places-overview` lists the busiest intersections and match rates when no place is searched.
- `places-search` resolves free text ("16th and mission", an address) to one intersection and renders its profile.
- `places-radius` re-renders the profile when the user picks another area radius.
- `places-busiest-link` opens a profile from the busiest-places list.
- `places-deep-link` opens a profile from a `?view=places&place=…&radius=…` URL.
- `places-not-found` shows an error and "Did you mean" buttons for an unresolvable place.

## How to get to it (user POV)

- Choose the `Places` tab, type in the place box, press Enter or choose `Show profile`.
- Choose a place in the busiest-places list on the empty Places view.
- Open a URL such as `?view=places&place=16th%20%26%20Mission&radius=100`.
- Choose `Open in Places` / `Open place profile` from an Ask answer or the event drawer.

## Driving it with vctl

Preconditions:

- Baseline preconditions hold and doctor is `[ok]`.

- **Open Places.** Load the app and choose the tab. Run `$B open ""` and `$B click --css 'button[role=tab][data-view=places]'`. The busiest-places list is visible: `$B wait --css '#placeOut [data-open-place]'`.
- **Search.** Type a place and press Enter. Run `$B fill --css '#placeInput' --value '16th and mission'` and `$B press --key Enter`, then `$B wait --css '#placeOut h2'`. `$B text --css '#placeOut .place-head'` shows `16TH ST & MISSION ST`, `100 M AREA`, and `Matched “16th and mission” by street names completed`.
- **Read the cards.** Run `$B text --css '#placeOut .kpi' --all`. Three cards, police / fire/EMS / 311, each with a 90-day total, last 30 days and earlier average. On the 2026-08-17 → 2026-11-15 data, police = 467. Run `$B screenshot --name places-search-100m`.
- **Switch to Exact.** Choose `Exact`. Run `$B click --css '#radiusSeg button' --text 'Exact'` and `$B wait --css '#placeOut .eyebrow' --text 'exact intersection'`. `$B text --css '#placeOut .kpi .big' --all` shows a police total of `0` (README fact on that data window) and every total ≤ its 100 m value. Run `$B screenshot --name places-radius-exact`.
- **Cross-check.** Run `$V api GET '/api/profile?place=16th%20and%20mission&radius=0' --save places-radius-exact-api`. Its `activity_by_source` `events_90d` counts equal the cards; a source with no events is absent from the list and its card shows `0`.
- **Busiest link.** Reload Places with no search: `$B open "?view=places"`, then `$B click --css '#placeOut [data-open-place]'` and `$B wait --css '#placeOut h2'`. The heading equals the label of the button clicked.
- **Deep link.** Run `$B open "?view=places&place=16th%20%26%20Mission&radius=200"` and `$B wait --css '#placeOut .eyebrow' --text '200 m area'`. The `200 m` radius button has `aria-pressed="true"`: `$B eval "document.querySelector('#radiusSeg [aria-pressed=true]').innerText"` prints `200 m`.
- **Not found.** Run `$B fill --css '#placeInput' --value 'Nowhere Blvd & Fake St'`, `$B press --key Enter`, `$B wait --css '#placeOut .err'`, then `$B text --css '#placeOut'`. An error is shown; any candidates render as buttons under `Did you mean`. Run `$B screenshot --name places-not-found`.

## Gotchas

- The radius buttons reload the profile only when the place box is non-empty.
- Labels in `.eyebrow` and `h3` are uppercased by CSS; `text` returns them uppercased.
- Police locations are snapped to nearby intersections, so Exact can show 0 police where 100 m shows hundreds. That is the documented behavior, not a bug.
- Numbers change after every `src.ingest` + `src.build`. Assert the data window from `$V doctor` before comparing to README figures.
- The view choice is remembered in `localStorage` (`sfintel-view`) per Chrome profile; each run has a fresh profile, so the first load opens Ask.
