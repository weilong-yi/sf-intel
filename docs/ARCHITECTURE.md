# Architecture and design decisions

This document explains how sf-intel is built, why each choice was made, what it costs, and
when it should change. It's written for someone reviewing the code or extending it.

- [1. The problem](#1-the-problem)
- [2. Concepts](#2-concepts)
- [3. System overview](#3-system-overview)
- [4. Data foundation](#4-data-foundation)
- [5. The ontology: places and events](#5-the-ontology-places-and-events)
- [6. Search and place profiles](#6-search-and-place-profiles)
- [7. The assistant](#7-the-assistant)
- [8. Governance](#8-governance)
- [9. Web UI and API](#9-web-ui-and-api)
- [10. Data quality results](#10-data-quality-results)
- [11. Known limitations](#11-known-limitations)
- [12. Roadmap: monitoring agents, skills, AI-assisted integration](#12-roadmap)
- [Decision index](#decision-index)

---

## 1. The problem

The information a city needs to answer "what's happening at this corner?" is spread across
systems that were never built to work together. This project uses three of them:

| System | What it records | How it records place | One raw row is |
|---|---|---|---|
| SFPD incident reports | crimes and incidents | an intersection, deliberately imprecise | one *offense* on an incident |
| SFFD fire/EMS calls | dispatches | an intersection, or a call box | one *unit* sent to a call |
| 311 | service requests | a street address with coordinates | one request |

They share no IDs, don't agree on what a "record" is, and describe the same corner in
different words. The project's job is to clean, connect and make sense of that data. It
**does not collect new data**: everything comes from DataSF's public datasets, and the raw
copy is kept exactly as published.

## 2. Concepts

| Concept | Meaning here | Where |
|---|---|---|
| **Data foundation** | The pipeline that lands, cleans and connects source data. Everything else depends on it. | `ingest.py`, `build.py` |
| **Ontology** | The shared real-world objects every source maps onto: **Place** (`location`) and **Event** (`event`), linked by `location_id`. | `build.py` |
| **Entity resolution** | Deciding which records refer to the same real place. | `build.py` |
| **Search** | Starting from what a person types (an intersection or address) and finding the place. | `dossier.resolve` |
| **Dossier / profile** | Everything all systems know about one place, in one view. | `dossier.build`, Places view |
| **Assistant** | An LLM that answers questions using tools over the ontology, citing the records it used. | `assistant.py` |
| **Provenance** | For any fact: which source, which record, and how it was linked. | `event.source`, `source_record_id`, `match_method` |
| **Agents** *(planned)* | Automated, recurring work on the same foundation, e.g. watching a neighborhood. | [§12](#12-roadmap) |
| **Skills** *(planned)* | Reusable, domain-specific knowledge (terminology, data quirks) the assistant loads when relevant. | [§12](#12-roadmap) |

The order matters: search, profiles and the assistant only work because the foundation and
ontology underneath them are sound. Most of the effort in this project went into sections 4 and 5.

## 3. System overview

```
             ┌───────────────────────── DATA FOUNDATION ─────────────────────────┐
DataSF API ──┤ ingest ──▶ raw copy ──▶ staging ──▶ ontology (location, event)    │
 (3 sources) │ src/ingest.py  data/raw   stg_*       src/build.py · DuckDB file  │
             └───────────────────────────────────┬───────────────────────────────┘
                                                 │ read-only, file access off
                      ┌──────────────────────────┼──────────────────────────┐
                      ▼                          ▼                          ▼
              SEARCH + PROFILE               ASSISTANT                   WEB UI
              src/dossier.py           src/assistant.py            src/server.py
              resolve(text) → place    Claude + 2 tools in a       src/ui/index.html
              build(place, radius)     hand-written loop           Ask · Places · SQL
                      ▲                          │                          │
                      └──── called as a tool ────┘                          │
                                                 └── streams events ───────►┘
                                       data/audit_log.jsonl (every question)
```

| Layer | Files | Runs | Output |
|---|---|---|---|
| Ingest | `sources.py`, `ingest.py` | on demand, ~25 s | `data/raw/*.csv` |
| Build | `build.py` | on demand, ~8 s | `data/sf_intel.duckdb` |
| Search and profile | `dossier.py` | per request, < 1 s | JSON / terminal |
| Assistant | `assistant.py`, `prompts.py` | per question, 10–25 s | answer + audit line |
| Web UI | `server.py`, `ui/index.html` | long-running, localhost | browser app |

## 4. Data foundation

### 4.1 Ingest

`sources.py` defines each source as configuration: dataset ID, columns to keep, and the
timestamp used for the window. `ingest.py` pulls the last 90 days of each as CSV and saves it
unchanged to `data/raw/`.

| Decision | Why | Cost | Revisit when |
|---|---|---|---|
| **D1. Sources defined as configuration** | Adding a source is a config entry, not new code. | Source-specific cleaning still lives in `build.py`. | Many sources: generate staging from config too. |
| **D2. Keep an unchanged raw copy** | Rebuild without calling the source again; prove what the source said if a number is questioned. | Disk space; one more layer. | Never; it's cheap insurance. |
| **D3. Full download of a 90-day window** | Simple and deterministic; fits easily in memory. | Every refresh re-downloads; numbers shift as the window moves. | Live operations: incremental loads keyed on `data_loaded_at`. |
| **D4. Standard library HTTP, CSV format** | No dependencies; CSV loads straight into DuckDB. | No retries or pagination beyond a row cap. | Larger pulls: add retries and paging. |

Field note: during development the publisher moved from `data.sfgov.org` to `data.sf.gov`.
Source systems change underneath you; the raw copy and a config-driven source list keep that
cheap to absorb.

### 4.2 Staging: fixing the grain

"Grain" is what one row represents. Counting raw rows would badly overstate activity:

| Source | Raw rows | Staged rows | Grain after staging | Overcount if you count raw rows |
|---|---|---|---|---|
| Police | 23,170 | 17,354 | incident | +34% |
| Fire/EMS | 94,568 | 46,865 | call | +100% |
| 311 | 228,185 | 228,185 | request | none |

Staging also computes source-specific facts once, so nothing downstream recomputes them:
fire **response time** (first unit on scene − call received), police `filed_online`,
311 `address_shape` (street address / intersection / unusable).

| Decision | Why | Cost | Revisit when |
|---|---|---|---|
| **D5. One staging table per source, at the right grain** | Every later count is per real-world thing. | Detail is collapsed; e.g. a police incident keeps a primary category plus a list of all. | Need per-offense analysis: query `raw_police` directly. |
| **D6. All transforms in SQL (DuckDB)** | Readable by analysts; fast; one file; no server. | One writer at a time; no row-level security. | Multiple users or live feeds: Postgres/PostGIS or a warehouse. |
| **D7. Full rebuild every run** | Deterministic; ~8 s. | Doesn't scale to years or streaming data. | Large or live data: incremental models. |
| **D8. Every layer is a table** | Any stage can be inspected and debugged with SQL. | More storage than views. | Storage pressure: turn middle layers into views. |

## 5. The ontology: places and events

### 5.1 Why the intersection is the shared place

Police data is published snapped to intersections, so no cross-system comparison can be finer
than that. Fire/EMS also reports at intersections. 311 is more precise but can be linked
*down* to the nearest intersection. The least precise source sets the level every source can
be joined at.

| Decision | Why | Cost | Revisit when |
|---|---|---|---|
| **D9. Place = street intersection** | The only level all three systems can share. | Loses 311's address precision in cross-source views (it's kept on each event). | A city address master is available: resolve to parcels and addresses. |

### 5.2 Resolving every record to a place

In order:

1. **Normalise street names** (SQL macro `norm_street`): `06TH`→`6TH`, `AV`→`AVE`, `STREET`→`ST`, punctuation removed.
2. **Build an order-independent key** (`intersection_key`): split on the source's separator, normalise, sort, join with ` & `.
3. **Collect every name** any system used (`location_key`, 7,654 names), with median coordinates.
4. **Merge alternate names** (`location_alias`): where 3+ streets meet, systems name different subsets. A name whose streets are a subset of a longer name **within 60 m** becomes its alias. Result: **7,288 places** (`location`), 366 names merged.
5. **Exact match** each record's key through `location_alias`.
6. **Nearest-intersection fallback** (`event_snap`): anything unmatched that has coordinates goes to the nearest place **within 250 m**. A grid of ~220 m cells (0.002° lat × 0.0025° lon), checking the 3×3 neighbouring cells, keeps this a cheap join.
7. **Record how it matched** on every event: `match_method` (`exact_name` / `nearest_intersection` / null) and `match_distance_m`.

Fire/EMS call-box addresses (`CALL BOX: 16TH ST/MISSION ST`) are cleaned before keying. Before
that fix they created fake places a few metres from the real ones.

| Decision | Why | Cost | Revisit when |
|---|---|---|---|
| **D10. Rule-based matching, not ML** | Explainable; every link carries its method and distance; easy to debug. | Misses unusual spellings; thresholds are judgement calls. | Matching *people* or free text: probabilistic matching with human review. |
| **D11. Subset-within-60 m aliasing** | Fixes multi-street corners named differently by each system. | Two genuinely different corners within 60 m that share streets could merge. | Use the city's street-network IDs (CNN) as ground truth where present. |
| **D12. 250 m snap limit** | p99 snap distance is 118 m, so 250 m catches nearly all valid matches. | Some matches are a block off. Tightening to 100 m drops a little over 1% of 311 into "unmatched". | Address-level analysis needed: tighten and filter on `match_distance_m`. |
| **D13. Flat-earth distance + grid, no GIS library** | No dependencies; metre-accurate at city scale. | An approximation; polygons (districts, beats) aren't supported. | Boundary analysis: DuckDB spatial or PostGIS. |
| **D14. Provenance on every event** | Any number can be traced to the record and the matching method behind it. | Extra columns. | Never. |

### 5.3 The Event object

`event` holds one row per police incident, fire/EMS call or 311 request (292,404), with a
shared set of fields (`category`, `subcategory`, `description`, `status`, `occurred_at`,
`location_id`) so cross-source questions are one `GROUP BY`. Source-specific detail stays in
`stg_*` and is joined when needed. `event_id` (`police:1614966`) is the citation format.

| Decision | Why | Cost | Revisit when |
|---|---|---|---|
| **D15. One shared event shape + source tables for detail** | Simple cross-source queries without losing detail. | Field meanings differ slightly by source (a police `status` is a resolution; a 311 `status` is open/closed). | Add a field dictionary (see Skills, §12). |
| **D16. No cross-source incident linking** | No shared ID exists; time-and-place matching has real false-match risk. | A police and an EMS response to the same overdose count as two events. The assistant is told never to add counts across sources. | Needed for a workflow: build it as its own matching step with review. |

## 6. Search and place profiles

### 6.1 Search: from what a person types to a place

`dossier.resolve(text)` tries, in order:

| Input | Strategy | Example |
|---|---|---|
| Intersection | Normalise + key, look up through aliases. If that fails, treat each typed street as the **start** of a real street name (`16TH` → `16TH ST`), preferring the match with the fewest extra streets, then the busiest. | `16th & Mission`, `16th and mission`, `market & 6th` |
| Street address | Use 311's ~200k geocoded addresses as a geocoder: same street, closest house number, then the nearest intersection. | `1 Dr Carlton B Goodlett Pl` |
| Anything else | Return candidate intersections instead of guessing. Typos fall back to any street in the query. | `Valencia`, `116th & Mission` |

Every result says **how** it matched ("street names completed", "geocoded via 311, snapped 9 m").

| Decision | Why | Cost | Revisit when |
|---|---|---|---|
| **D17. Forgiving lookup** | The strict version failed on first real use: people type "16th & Mission", not "16TH ST & MISSION ST". | Ambiguous input could resolve to the wrong place, so the method is always shown. | Larger gazetteer: a real geocoder or fuzzy search with a confirmation step. |
| **D18. 311 as the geocoder** | Already in the data; no external service. | Only covers addresses someone has reported to 311. | A proper address master or geocoding service. |

### 6.2 Profile: everything about one place

`dossier.build(location_id, radius_m=100)` returns the **area** around the place:

- the intersections included and their per-source counts
- activity per source: 90 days, last 30 days, earlier 30-day average, how many matched by distance
- top categories per source
- fire/EMS response time (median and 90th percentile)
- open 311 requests by type and age
- the 10 most recent events with IDs

Time windows are anchored on the newest event, not today's date, so results are reproducible.

**Why an area:** at 16th & Mission, an exact lookup finds **0** police incidents; within 100 m
there are **467**, filed at 16th & Wiese, 69 m away. Police location snapping means exact
lookups silently undercount.

| Decision | Why | Cost | Revisit when |
|---|---|---|---|
| **D19. 100 m area by default, stated and adjustable** | Exact lookups undercount police activity; making the radius visible keeps it honest. | Can include a neighbouring block's activity. | Strict attribution needed: radius 0 (the UI's "Exact"). |
| **D20. Windows anchored to the newest event** | The same command gives the same answer tomorrow. | "Last 30 days" means the data's last 30 days, not the calendar's. | Live data: anchor on now and show data freshness. |

## 7. The assistant

### 7.1 The loop

```
messages = [question]
repeat up to MAX_STEPS (8):
    response = Claude(system prompt + live schema, 2 tools, messages)
    for each tool request: our code runs it → tool result (errors returned, not raised)
    if no tool requests: answer = text; stop
append one line to data/audit_log.jsonl
```

The loop emits events (`step`, `tool_call`, `tool_result`, `answer`, `error`). The terminal
prints them; the web server streams them to the browser. One loop, two front ends.

### 7.2 Tools

| Tool | Input | Returns |
|---|---|---|
| `location_profile` | `place`, `radius_m` | the place profile (§6.2) as JSON |
| `run_sql` | `purpose`, `sql` | one read-only query, ≤ 200 rows, cells ≤ 120 characters |

Both use strict JSON schemas. The `purpose` field makes the model state why it's running each
query, which makes the audit log and the UI readable.

### 7.3 Prompt

`prompts.py` holds the system prompt and tool definitions. The prompt requires that every
number comes from a tool result, that specific events are cited as `[source:id]`, that the
time window and caveats are stated, and that answers lead with the conclusion. The live
schema (tables, columns, row counts) is generated from the database at startup, so it can't
go stale. Known data caveats are written in: police location snapping, distance-matched 311
records, no shared IDs across sources, cancelled EMS calls.

### 7.4 Model and API settings

- `claude-opus-5-5` at `medium` effort. Override with `CLAUDE_MODEL` / `CLAUDE_EFFORT`.
- **Prompt caching** on the system prompt, tools and history, so later steps in a question are mostly cache reads.
- **Refusal fallback** (`fallbacks: "default"`): if the model declines, the API retries on a fallback model.
- **Auth** through the `ant` CLI login, so no API key is stored on disk.
- **Measured:** 2–3 steps, 9–20 s, $0.03–$0.07 per question.

| Decision | Why | Cost | Revisit when |
|---|---|---|---|
| **D21. Hand-written loop, not a framework** | Every step visible and logged; checks are easy to add. | More code than the SDK's tool runner. | Production: the tool runner, with hooks for the same checks. |
| **D22. One open-ended SQL tool + one high-level tool** | Answers questions nobody anticipated. | Valid SQL can still answer the wrong question. | High-stakes or repeated questions: specific tools with fixed queries. |
| **D23. Caveats in the prompt** | The model can't know local data quirks; they came from profiling. | The prompt grows and needs maintaining. | Many caveats: move them to skills loaded when relevant (§12). |
| **D24. Live schema in the prompt** | Never out of date. | Costs tokens on every call (mitigated by caching). | Very large schemas: a `describe_table` tool instead. |
| **D25. Strongest model, medium effort** | Accuracy on multi-step SQL reasoning; cost is fine at demo volume. | Slower and pricier than smaller models. | High volume: test smaller models on a question set; choose by cost per *correct* answer. |
| **D26. Errors returned to the model** | It can fix its own SQL instead of the run crashing. | A bad query costs an extra step. | Never. |

### 7.5 Verification

The first real answer was checked against the database. The main number and all four cited
events were correct. It miscounted one detail ("three of four" were life-threatening; all four
were). It also described two EMS calls as "no unit reached the scene" when both were
cancelled or redirected. Fix: that caveat was added to the prompt, and a later answer applied
it unprompted. Citations are what made the check take a minute instead of an afternoon.

## 8. Governance

The model never touches the database. It can only ask the code to run a tool, and the code
decides what's allowed. Each layer works on its own:

| # | Control | Stops |
|---|---|---|
| 1 | Tools are executed by our code, not the model | the model acting directly |
| 2 | DuckDB opened `read_only` | writes |
| 3 | `enable_external_access = false` + `lock_configuration = true` | reading local files or the web through SQL (tested: `read_csv('.env.example')` is refused) |
| 4 | Text check: one statement, `SELECT`/`WITH` only, no write or config keywords | multi-statement tricks |
| 5 | Row cap (200 to the model, 500 in the SQL view) and 120-character cells | cost blow-ups, flooding the context |
| 6 | `MAX_STEPS = 8`, `max_tokens` cap | runaway loops |
| 7 | Audit log: question, every tool call and input, answer, token usage | "why did it say that?" |
| 8 | Web server binds to 127.0.0.1 only | network exposure |
| 9 | Public, published data only; no personal information | privacy risk |

| Decision | Why | Cost | Revisit when |
|---|---|---|---|
| **D27. Several independent controls** | If one fails (e.g. the text check misses something), the database setting still blocks it. | Some redundancy. | Never; defence in depth is the point. |
| **D28. Audit log as local JSONL** | Simple, append-only, readable. | Not tamper-proof; no user identity. | Multi-user: central logging with identity and retention. |

## 9. Web UI and API

### 9.1 Server

`server.py` uses only the Python standard library (`ThreadingHTTPServer`) and listens on
127.0.0.1. One guarded DuckDB connection is opened at startup; each request gets its own
cursor, because DuckDB connections aren't shared across threads.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/overview` | data window, per-source counts and match rates, busiest places |
| GET | `/api/schema` | tables, columns and row counts |
| GET | `/api/profile?place=&radius=` | place profile, or candidates if not found |
| GET | `/api/event?id=` | the event, its place, the cleaned source row, and the raw rows |
| POST | `/api/sql` | read-only query result (same checks as the assistant) |
| POST | `/api/ask` | the assistant's events, streamed as newline-delimited JSON |
| POST | `/api/reset` | forget a conversation |

### 9.2 Front end

`ui/index.html` is one file with no build step. Three views (Ask, Places, SQL) plus an event
panel that opens from any event ID, in a table or in an answer's citations, and shows the
**raw rows the city published**. Pages can be linked directly (`?view=places&place=…`,
`?event=…`, `?q=…`).

The visual style is an operations console: Inter and IBM Plex Mono, a cool grey scale with a
navy top bar and orange accents, thin shadow rings instead of borders, dense 13–14 px text,
monospace all-caps labels, and one colour per source system. It works in light and dark mode.

| Decision | Why | Cost | Revisit when |
|---|---|---|---|
| **D29. Standard-library server** | No new dependencies; the API is small. | No auth or sessions; conversations live in memory. | More than one user: a framework with auth and stored conversations. |
| **D30. One HTML file, no framework** | Readable in one sitting; nothing to build. | Gets unwieldy as views grow. | A team or many views: a component framework with a build step. |
| **D31. Stream steps as NDJSON over fetch** | Plain HTTP; users see each tool call as it happens instead of a 20-second spinner. | One-way; a running question can't be cancelled. | Need cancel or server push: WebSockets or SSE plus a cancel endpoint. |
| **D32. Event panel shows raw source rows** | One click from any number or citation to the source. | Raw columns aren't friendly. | Non-technical users: hide raw rows behind an "advanced" toggle. |
| **D33. Localhost only** | No exposure of data or credentials. | Only usable at the machine running it. | Team use: deploy behind single sign-on with role-based access. |

## 10. Data quality results

On the window Aug 17 – Nov 15, 2026:

| Source | Exact name | Nearest intersection | Unmatched | Note |
|---|---|---|---|---|
| Police | 93.8% | — | 6.2% | unmatched have no location in the source (online / out-of-SF reports) |
| Fire/EMS | 98.2% | 1.8% | 10 records | nearest = call boxes |
| 311 | 12.6% | 86.5% | 0.9% | snap distance median 34 m, p90 73 m, p99 118 m |

2,600 of 7,288 places appear in all three systems. Reproduce with
`python experiments/02_match_quality.py`.

## 11. Known limitations

- **Public data only.** No people, cases, warrants or narratives. A real deployment would add person and case objects under access controls.
- **No cross-source incident linking** (D16).
- **Batch only.** No incremental loads or scheduling.
- **No evaluation set.** Answers are spot-checked, not scored against known answers.
- **Single user, local.** No authentication; conversations are lost on restart.
- **Police locations are approximate by design**, so place-level police counts are area estimates.

## 12. Roadmap

What a fuller operational intelligence platform has that this project doesn't yet, and how
each would be built on the same foundation.

### 12.1 Monitoring agents

The assistant answers questions when someone asks. Many needs recur: *tell me when something
new happens in this neighborhood*, *send a weekly summary*, *flag a jump in activity*.

Design:
1. A `watch` table: place or neighborhood, radius, event filters, schedule, last-run time.
2. A scheduled job (`python -m src.watch`, run by Task Scheduler or cron) that runs incremental ingest, then queries `event` for rows newer than each watch's last run.
3. If anything is new or a threshold is crossed, the assistant writes a short summary **with citations**, using the same tools and safeguards.
4. Results go to an inbox view in the UI (and later email or chat), each with an audit-log entry.

Tradeoff: autonomous runs need tighter limits (smaller step cap, fixed tools only, no open-ended SQL) because nobody is watching each step.

### 12.2 Skills: domain knowledge as files, not prompt text

Today the data caveats live inside the system prompt (D23). Instead:
- `skills/police-locations.md`, `skills/ems-dispositions.md`, `skills/311-terminology.md`, each describing a quirk, the terminology, and how to query it correctly.
- A short index in the prompt; the assistant loads a skill when a question touches it.
- New lessons from the field (like the cancelled-EMS-call misreading) become a new skill file, reviewed like code.

Tradeoff: one more step per question when a skill is needed, in exchange for a prompt that doesn't grow with every lesson.

### 12.3 AI-assisted integration

Adding a source today means hand-writing its staging SQL. With the ontology defined, an
LLM could draft that work:
1. Profile a new dataset (columns, samples, grain, null rates, address formats).
2. Propose a mapping to the ontology (`occurred_at`, `category`, address → place) and the staging SQL.
3. Run the existing match-quality checks on the result.
4. A person reviews the mapping and the match rates before it's merged.

Tradeoff: faster integration, but the review step is mandatory; a wrong mapping silently corrupts every downstream answer.

### 12.4 Also next

- An **evaluation set**: 20–30 questions with hand-written SQL answers, run before every prompt or model change.
- **Incremental ingest** keyed on `data_loaded_at`, with data-quality checks that fail the build.
- **Field dictionary**: plain-language meaning of each field, per source (feeds 12.2).

---

## Decision index

| # | Decision | Section |
|---|---|---|
| D1 | Sources defined as configuration | [4.1](#41-ingest) |
| D2 | Keep an unchanged raw copy | [4.1](#41-ingest) |
| D3 | Full download of a 90-day window | [4.1](#41-ingest) |
| D4 | Standard library HTTP, CSV | [4.1](#41-ingest) |
| D5 | One staging table per source, right grain | [4.2](#42-staging-fixing-the-grain) |
| D6 | All transforms in SQL (DuckDB) | [4.2](#42-staging-fixing-the-grain) |
| D7 | Full rebuild every run | [4.2](#42-staging-fixing-the-grain) |
| D8 | Every layer is a table | [4.2](#42-staging-fixing-the-grain) |
| D9 | Place = street intersection | [5.1](#51-why-the-intersection-is-the-shared-place) |
| D10 | Rule-based matching | [5.2](#52-resolving-every-record-to-a-place) |
| D11 | Subset-within-60 m aliasing | [5.2](#52-resolving-every-record-to-a-place) |
| D12 | 250 m snap limit | [5.2](#52-resolving-every-record-to-a-place) |
| D13 | Flat-earth distance + grid | [5.2](#52-resolving-every-record-to-a-place) |
| D14 | Provenance on every event | [5.2](#52-resolving-every-record-to-a-place) |
| D15 | Shared event shape + source detail tables | [5.3](#53-the-event-object) |
| D16 | No cross-source incident linking | [5.3](#53-the-event-object) |
| D17 | Forgiving lookup | [6.1](#61-search-from-what-a-person-types-to-a-place) |
| D18 | 311 as the geocoder | [6.1](#61-search-from-what-a-person-types-to-a-place) |
| D19 | 100 m area by default | [6.2](#62-profile-everything-about-one-place) |
| D20 | Windows anchored to the newest event | [6.2](#62-profile-everything-about-one-place) |
| D21 | Hand-written loop | [7.4](#74-model-and-api-settings) |
| D22 | Open-ended SQL + high-level tool | [7.4](#74-model-and-api-settings) |
| D23 | Caveats in the prompt | [7.4](#74-model-and-api-settings) |
| D24 | Live schema in the prompt | [7.4](#74-model-and-api-settings) |
| D25 | Strongest model, medium effort | [7.4](#74-model-and-api-settings) |
| D26 | Errors returned to the model | [7.4](#74-model-and-api-settings) |
| D27 | Several independent controls | [8](#8-governance) |
| D28 | Audit log as local JSONL | [8](#8-governance) |
| D29 | Standard-library server | [9](#9-web-ui-and-api) |
| D30 | One HTML file | [9](#9-web-ui-and-api) |
| D31 | NDJSON streaming | [9](#9-web-ui-and-api) |
| D32 | Raw rows in the event panel | [9](#9-web-ui-and-api) |
| D33 | Localhost only | [9](#9-web-ui-and-api) |
