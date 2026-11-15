# sf-intel

Three San Francisco city data systems, combined into one location-linked model,
with an AI assistant on top that answers only from the data and cites its sources.

| Source | Dataset | One row in the model is |
|---|---|---|
| SFPD incident reports | [`wg3w-h783`](https://data.sf.gov/d/wg3w-h783) | an incident |
| SFFD fire/EMS calls for service | [`nuek-vuh3`](https://data.sf.gov/d/nuek-vuh3) | a call |
| 311 service requests | [`vw6y-z8j6`](https://data.sf.gov/d/vw6y-z8j6) | a request |

The systems share no IDs and record location three different ways: police as
deliberately imprecise intersections, fire/EMS as intersections or call boxes,
and 311 as exact street addresses. The pipeline matches all three onto one set
of street intersections, so you can ask "what's happening around this corner?"
across every system at once.

## What it does

```
DataSF API ──ingest──▶ data/raw/*.csv ──build (SQL in DuckDB)──▶ data/sf_intel.duckdb
                                                                   │
                                     ┌─────────────────────────────┤
                                     ▼                             ▼
                            address profile                 AI assistant
                            (src/dossier.py)                (src/assistant.py)
```

- **Ingest** ([src/ingest.py](src/ingest.py)): downloads the last 90 days of each dataset and saves an unchanged copy.
- **Build** ([src/build.py](src/build.py)):
  - Fixes the grain: police data has one row per offense and fire/EMS one row per unit sent, so they're collapsed to one row per incident and per call.
  - Normalizes street names.
  - Merges different names for the same intersection.
  - Matches records with no name match to the nearest intersection.
  - The result is one `event` table where every row records its source record and how its location was matched.
- **Address profile** ([src/dossier.py](src/dossier.py)): everything all three systems recorded within 100 m of a place. That covers activity and 30-day trends, top categories, fire/EMS response times, the open 311 backlog, and recent events with IDs.
- **Assistant** ([src/assistant.py](src/assistant.py), [src/prompts.py](src/prompts.py)): Claude with two tools (`location_profile` and read-only `run_sql`) in a hand-written tool-use loop. Answers cite event IDs such as `[police:1551210]`.

### Results on the current data (Aug 17 – Nov 15, 2026)

- 292,404 events at 7,288 intersections; 2,600 intersections appear in all three systems.
- Location match rates:
  - **Fire/EMS:** 99.98%
  - **311:** 99.1%
  - **Police:** 93.8%; the remaining police incidents have no location in the source data.
- At 16th & Mission, a strict intersection lookup finds **0** police incidents, while a 100 m area finds **467**. Police publish locations snapped to a nearby intersection, which is why the profile defaults to a stated radius.

## Setup

Windows, Python 3.13.

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

The assistant authenticates with the [`ant` CLI](https://github.com/anthropics/anthropic-cli)
(`ant auth login`), and the SDK picks up that login automatically. Alternatively, put
`ANTHROPIC_API_KEY` in a `.env` file (see `.env.example`).

## Run

```
python -m src.ingest                               # download ~350k rows (~25 s)
python -m src.build                                # build data/sf_intel.duckdb (~8 s)

python -m src.dossier "16th St & Mission St"       # 100 m area profile
python -m src.dossier "16th St & Mission St" --radius 0
python -m src.dossier "1 Dr Carlton B Goodlett Pl" # street addresses work too

python -m src.assistant "What changed around 16th & Mission in the last 30 days?"
python -m src.assistant                            # interactive, keeps context
```

Experiments, each testing one idea:

| Script | What it shows |
|---|---|
| `experiments/01_profile_raw.py` | Raw data profile: grain, missing locations, address formats |
| `experiments/02_match_quality.py` | Match rates, distance percentiles, unmatched addresses |
| `experiments/03_hello_claude.py` | The smallest Claude API call |

## Assistant safety

The model never touches the database. It can only ask the code to run a tool, and the code decides what is allowed.

- DuckDB is opened read-only, with file and network access switched off and that configuration locked.
- Only one `SELECT`/`WITH` statement per call is accepted, and results are capped at 200 rows.
- At most 8 model calls per question. Tool errors go back to the model instead of crashing the loop.
- Every question, tool call and answer is appended to `data/audit_log.jsonl`.

Defaults: `claude-opus-5-5` at `medium` effort. Override them with the `CLAUDE_MODEL` and
`CLAUDE_EFFORT` environment variables. A typical question costs a few cents.

## Layout

```
src/
  sources.py     dataset definitions (add a source here)
  ingest.py      download raw data
  build.py       raw -> staging -> location -> event (SQL)
  dossier.py     place resolution + area profile
  prompts.py     system prompt and tool schemas
  assistant.py   tool-use loop, guardrails, audit log
experiments/     one-idea scripts
data/            generated, not committed
```

Data: [DataSF](https://datasf.org/opendata/), City and County of San Francisco.
