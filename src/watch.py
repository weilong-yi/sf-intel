"""Monitoring agents: watch a place and report what's new.

The chat assistant answers when someone asks. A watch works when nobody asks:
on a schedule it checks one area for new events and unusual jumps, and writes
an alert with a short summary that cites the events behind it.

Each run has two parts, deliberately separated:

  1. DETECT (plain SQL, no AI, free)
       - new events in the area since the last check
       - jumps: a source/category whose last 7 days beat its earlier weekly
         average by threshold_pct (and at least min_count events)
     Nothing new -> the run is recorded as "quiet" and no model is called.

  2. SUMMARIZE (one model call, tighter limits than the chat assistant)
       - no tools at all: the model only sees what detection found
       - low effort, small max_tokens
       - every [source:id] it cites must be in what it was given; anything
         else marks the alert's citations as invalid
       - if the API fails, a plain-text summary is used instead

Storage is two small files, so the database stays strictly read-only:
  data/watches.json   the watch definitions and their last-checked point
  data/alerts.jsonl   one line per run (also the audit trail for the agent)

Run:
  python -m src.watch add "16th & Mission" --sources police --categories "Drug Offense"
  python -m src.watch list
  python -m src.watch run                 # every watch that is due
  python -m src.watch run --all           # every watch, now
  python -m src.watch replay w1 --weeks 4 # re-run past weeks as if each were new
  python -m src.watch alerts
  python -m src.watch remove w1

Schedule it on Windows (daily at 7:00) with Task Scheduler, e.g.:
  schtasks /Create /SC DAILY /ST 07:00 /TN sf-intel-watch ^
    /TR "C:\\path\\to\\sf-intel\\.venv\\Scripts\\python.exe -m src.watch run"
  (set "Start in" to the sf-intel folder)
"""

import argparse
import datetime as dt
import json
import re
import sys
import threading
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from src import dossier
from src.prompts import AGENT_SUMMARY_PROMPT

WATCHES_FILE = Path("data/watches.json")
ALERTS_FILE = Path("data/alerts.jsonl")
SOURCES = ["police", "fire_ems", "311"]
SCHEDULES = {"hourly": dt.timedelta(hours=1), "daily": dt.timedelta(days=1), "weekly": dt.timedelta(days=7)}
FIRST_LOOKBACK_DAYS = 7      # a new watch's first run reports the last week of data
MAX_EVENTS_TO_MODEL = 40     # newest events listed to the model; counts cover all of them
SUMMARY_MAX_TOKENS = 2000
SUMMARY_EFFORT = "low"
CITATION = re.compile(r"\b(police|fire_ems|311):[\w-]+")

_lock = threading.Lock()  # the web server may write from several threads


# -------------------------------------------------------------------- store
def load_watches() -> list[dict]:
    try:
        return json.loads(WATCHES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def save_watches(watches: list[dict]) -> None:
    WATCHES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCHES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(watches, indent=2, default=str), encoding="utf-8")
    tmp.replace(WATCHES_FILE)  # atomic: a crash never leaves a half-written file


def load_alerts(watch_id: str | None = None, limit: int = 50) -> list[dict]:
    if not ALERTS_FILE.exists():
        return []
    lines = ALERTS_FILE.read_text(encoding="utf-8").splitlines()
    alerts = [json.loads(line) for line in lines if line.strip()]
    if watch_id:
        alerts = [a for a in alerts if a["watch_id"] == watch_id]
    return list(reversed(alerts))[:limit]


def append_alert(alert: dict) -> None:
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ALERTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(alert, default=str) + "\n")


def data_through(con) -> dt.datetime:
    return con.execute("SELECT max(occurred_at) FROM event").fetchone()[0]


def validate_definition(con, place: str, radius_m: float, sources: list[str],
                        categories: list[str], schedule: str) -> dict:
    """Resolve and check a watch definition. Returns the cleaned definition or {"error": ...}.
    Used both when a person creates a watch and when the assistant proposes one."""
    match = dossier.resolve(con, place)
    if "error" in match:
        return match
    bad = [s for s in sources if s not in SOURCES]
    if bad:
        return {"error": f"Unknown source(s) {bad}; use {SOURCES}."}
    if schedule not in SCHEDULES:
        return {"error": f"Unknown schedule '{schedule}'; use {list(SCHEDULES)}."}
    return {"place_input": place, "location_id": match["location_id"], "label": match["label"],
            "resolved_by": match["method"], "radius_m": max(0, min(float(radius_m), 500)),
            "sources": sources, "categories": [c.strip() for c in categories if c.strip()],
            "schedule": schedule}


def add_watch(con, place: str, radius_m: float = 100, sources: list[str] | None = None,
              categories: list[str] | None = None, schedule: str = "daily",
              threshold_pct: float = 50, min_count: int = 5, name: str | None = None) -> dict:
    d = validate_definition(con, place, radius_m, sources or [], categories or [], schedule)
    if "error" in d:
        return d
    with _lock:
        watches = load_watches()
        n = max([int(w["id"][1:]) for w in watches] + [0]) + 1
        watch = {
            "id": f"w{n}",
            "name": name or describe(d),
            **d,
            "threshold_pct": threshold_pct,
            "min_count": min_count,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            # First run looks back one week of *data* time
            "last_checked_data_at": (data_through(con) - dt.timedelta(days=FIRST_LOOKBACK_DAYS)).isoformat(),
            "last_run_at": None,
            "enabled": True,
        }
        watches.append(watch)
        save_watches(watches)
    return watch


def remove_watch(watch_id: str) -> bool:
    with _lock:
        watches = load_watches()
        kept = [w for w in watches if w["id"] != watch_id]
        save_watches(kept)
    return len(kept) < len(watches)


def describe(w: dict) -> str:
    what = ", ".join(w["categories"]) if w["categories"] else "all activity"
    src = "/".join(w["sources"]) if w["sources"] else "all sources"
    return f"{what} · {src} · {w['radius_m']:.0f} m around {w['label']}"


# ------------------------------------------------------------------- detect
def _filters(w: dict) -> tuple[str, list]:
    sql, params = "list_contains(?, location_id)", []
    params.append(None)  # placeholder for area ids, filled by caller
    if w["sources"]:
        sql += " AND source IN (" + ",".join("?" * len(w["sources"])) + ")"
        params += w["sources"]
    if w["categories"]:
        sql += " AND (" + " OR ".join(["category ILIKE ? OR subcategory ILIKE ?"] * len(w["categories"])) + ")"
        for c in w["categories"]:
            params += [f"%{c}%", f"%{c}%"]
    return sql, params


def detect(con, w: dict, since: dt.datetime, as_of: dt.datetime) -> dict:
    area = dossier.area_location_ids(con, w["location_id"], w["radius_m"])
    where, params = _filters(w)
    params[0] = area

    def rows(sql, extra):
        cur = con.execute(sql, params + extra)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    new_events = rows(f"""
        SELECT event_id, strftime(occurred_at, '%Y-%m-%d %H:%M') AS occurred_at, source,
               category, subcategory, status,
               (SELECT label FROM location l WHERE l.location_id = event.location_id) AS place
        FROM event
        WHERE {where} AND occurred_at > ? AND occurred_at <= ?
        ORDER BY occurred_at DESC""", [since, as_of])

    # Filter params bind first (inner query), then as_of; keeps placeholder order simple.
    trend = rows(f"""
        SELECT e.source, coalesce(e.category, 'Unclassified') AS category,
               count(*) FILTER (e.occurred_at > t.a - INTERVAL 7 DAY)                    AS last_7d,
               round(count(*) FILTER (e.occurred_at <= t.a - INTERVAL 7 DAY) / 4.0, 1)   AS prior_weekly_avg
        FROM (SELECT * FROM event WHERE {where}) e, (SELECT ?::TIMESTAMP AS a) t
        WHERE e.occurred_at > t.a - INTERVAL 35 DAY AND e.occurred_at <= t.a
        GROUP BY 1, 2""", [as_of])

    jumps = []
    for t in trend:
        last, prior = t["last_7d"], t["prior_weekly_avg"]
        if last < w["min_count"]:
            continue
        if prior == 0 or last >= prior * (1 + w["threshold_pct"] / 100):
            change = None if prior == 0 else round(100 * (last - prior) / prior)
            jumps.append({**t, "change_pct": change})
    jumps.sort(key=lambda j: -(j["last_7d"] - j["prior_weekly_avg"]))
    if not new_events:
        jumps = []  # nothing new since the last check: don't re-alert on a jump already reported

    # Every number the summary may use is counted here, in code, so the model never has to count.
    def tally(key):
        out: dict[str, int] = {}
        for e in new_events:
            k = key(e)
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    return {"area_intersections": len(area), "new_count": len(new_events),
            "new_by_category": tally(lambda e: f"{e['source']} · {e['category'] or 'Unclassified'}"),
            "new_by_place": tally(lambda e: e["place"] or "unknown"),
            "new_by_day": dict(sorted(tally(lambda e: e["occurred_at"][:10]).items())),
            "new_by_status": tally(lambda e: e["status"] or "unknown"),
            "new_events": new_events, "jumps": jumps}


# ---------------------------------------------------------------- summarize
def plain_summary(w: dict, found: dict) -> str:
    parts = [f"{found['new_count']} new event(s) within {w['radius_m']:.0f} m of {w['label']}."]
    if found["new_by_category"]:
        parts.append("By type: " + "; ".join(f"{k} {v}" for k, v in list(found["new_by_category"].items())[:6]) + ".")
    for j in found["jumps"][:3]:
        change = "new this week" if j["change_pct"] is None else f"+{j['change_pct']}%"
        parts.append(f"Jump: {j['source']} {j['category']} {j['last_7d']} in the last 7 days vs "
                     f"{j['prior_weekly_avg']}/week before ({change}).")
    return " ".join(parts)


def summarize(w: dict, found: dict, since: dt.datetime, as_of: dt.datetime) -> dict:
    """One model call, no tools. Returns summary, citation check, cost."""
    from src.assistant import MODEL, PRICE_PER_MTOK  # same model and price table as chat

    payload = {
        "watch": {k: w[k] for k in ("name", "label", "radius_m", "sources", "categories")},
        "period": {"from": str(since), "to": str(as_of)},
        "new_event_count": found["new_count"],
        "new_by_category": found["new_by_category"],
        "new_by_place": found["new_by_place"],
        "new_by_day": found["new_by_day"],
        "new_by_status": found["new_by_status"],
        "jumps_last_7_days_vs_prior_4_week_average": found["jumps"][:8],
        "newest_events": found["new_events"][:MAX_EVENTS_TO_MODEL],
    }
    allowed = {e["event_id"] for e in found["new_events"][:MAX_EVENTS_TO_MODEL]}

    load_dotenv()
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=SUMMARY_MAX_TOKENS,
        system=AGENT_SUMMARY_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        output_config={"effort": SUMMARY_EFFORT},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined")
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()

    cited = sorted(set(m.group(0) for m in CITATION.finditer(text)))
    invalid = [c for c in cited if c not in allowed]
    u = response.usage
    cost = (u.input_tokens * PRICE_PER_MTOK["input"] + u.output_tokens * PRICE_PER_MTOK["output"]
            + (u.cache_read_input_tokens or 0) * PRICE_PER_MTOK["cache_read"]
            + (u.cache_creation_input_tokens or 0) * PRICE_PER_MTOK["cache_write"]) / 1e6
    return {"summary": text, "cited": cited, "invalid_citations": invalid,
            "citations_ok": not invalid, "cost_usd": round(cost, 4), "model": MODEL}


# ---------------------------------------------------------------------- run
def run_watch(con, w: dict, as_of: dt.datetime | None = None, since: dt.datetime | None = None,
              use_llm: bool = True, replay: bool = False) -> dict:
    """Detect, summarize if there's anything to say, append one alert line.
    Normal runs advance the watch's checkpoint; replays don't."""
    started = time.time()
    as_of = as_of or data_through(con)
    since = since or dt.datetime.fromisoformat(w["last_checked_data_at"])
    found = detect(con, w, since, as_of)

    alert = {"id": f"{w['id']}-{int(time.time() * 1000)}", "watch_id": w["id"], "watch_name": w["name"],
             "label": w["label"], "run_at": dt.datetime.now().isoformat(timespec="seconds"),
             "period_from": str(since), "period_to": str(as_of), "replay": replay,
             "new_count": found["new_count"], "new_by_category": found["new_by_category"],
             "new_by_place": found["new_by_place"], "new_by_day": found["new_by_day"],
             "jumps": found["jumps"], "new_events": found["new_events"][:MAX_EVENTS_TO_MODEL],
             "status": "quiet", "summary": None, "summary_by": None,
             "citations_ok": None, "invalid_citations": [], "cost_usd": 0.0}

    if found["new_count"]:
        alert["status"] = "attention" if found["jumps"] else "new"
        if use_llm:
            try:
                s = summarize(w, found, since, as_of)
                alert.update(summary=s["summary"], summary_by=s["model"], citations_ok=s["citations_ok"],
                             invalid_citations=s["invalid_citations"], cost_usd=s["cost_usd"])
            except Exception as e:  # API down, sign-in expired, refusal: still deliver the alert
                alert.update(summary=plain_summary(w, found), summary_by=f"plain text (model unavailable: {type(e).__name__})")
        else:
            alert.update(summary=plain_summary(w, found), summary_by="plain text")
    else:
        alert["summary"] = f"No new events within {w['radius_m']:.0f} m of {w['label']} since {since:%Y-%m-%d %H:%M}."
        alert["summary_by"] = "no model call (nothing new)"

    alert["seconds"] = round(time.time() - started, 1)
    append_alert(alert)

    if not replay:
        with _lock:
            watches = load_watches()
            for x in watches:
                if x["id"] == w["id"]:
                    x["last_checked_data_at"] = as_of.isoformat()
                    x["last_run_at"] = alert["run_at"]
            save_watches(watches)
    return alert


def is_due(w: dict, now: dt.datetime) -> bool:
    if not w.get("enabled", True):
        return False
    if not w.get("last_run_at"):
        return True
    return now - dt.datetime.fromisoformat(w["last_run_at"]) >= SCHEDULES[w["schedule"]]


def replay(con, w: dict, weeks: int, use_llm: bool = True) -> list[dict]:
    """Re-run the last `weeks` weeks one at a time, as if each week's data had just arrived."""
    end = data_through(con)
    out = []
    for i in range(weeks, 0, -1):
        as_of = end - dt.timedelta(days=7 * (i - 1))
        out.append(run_watch(con, w, as_of=as_of, since=as_of - dt.timedelta(days=7),
                             use_llm=use_llm, replay=True))
    return out


# ---------------------------------------------------------------------- CLI
def print_alert(a: dict) -> None:
    tag = {"attention": "ATTENTION", "new": "NEW", "quiet": "QUIET"}[a["status"]]
    print(f"\n[{tag}] {a['watch_name']}  ({a['period_from'][:16]} -> {a['period_to'][:16]})"
          f"{'  [replay]' if a['replay'] else ''}")
    print(f"  {a['new_count']} new · {len(a['jumps'])} jump(s) · summary by {a['summary_by']}"
          f" · ${a['cost_usd']:.3f}" + ("" if a["citations_ok"] in (None, True) else
                                        f" · INVALID CITATIONS {a['invalid_citations']}"))
    print("  " + (a["summary"] or "").replace("\n", "\n  "))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="python -m src.watch", description="Monitoring agents")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="create a watch")
    a.add_argument("place")
    a.add_argument("--radius", type=float, default=100)
    a.add_argument("--sources", nargs="*", default=[], choices=SOURCES)
    a.add_argument("--categories", nargs="*", default=[], help="substring match, e.g. 'Drug Offense' Graffiti")
    a.add_argument("--schedule", default="daily", choices=list(SCHEDULES))
    a.add_argument("--threshold", type=float, default=50, help="jump threshold in percent")
    a.add_argument("--min-count", type=int, default=5, help="ignore jumps smaller than this many events")
    a.add_argument("--name")
    sub.add_parser("list", help="list watches")
    r = sub.add_parser("run", help="run due watches")
    r.add_argument("ids", nargs="*")
    r.add_argument("--all", action="store_true", help="ignore schedules")
    r.add_argument("--no-llm", action="store_true", help="plain-text summaries, no API calls")
    rp = sub.add_parser("replay", help="re-run past weeks as if each were new")
    rp.add_argument("id")
    rp.add_argument("--weeks", type=int, default=4)
    rp.add_argument("--no-llm", action="store_true")
    al = sub.add_parser("alerts", help="show recent alerts")
    al.add_argument("--watch")
    al.add_argument("--limit", type=int, default=10)
    rm = sub.add_parser("remove", help="delete a watch")
    rm.add_argument("id")
    args = p.parse_args()

    from src.assistant import connect
    con = connect()

    if args.cmd == "add":
        w = add_watch(con, args.place, args.radius, args.sources, args.categories, args.schedule,
                      args.threshold, args.min_count, args.name)
        print(json.dumps(w, indent=2) if "error" in w else f"Created {w['id']}: {w['name']}\n  ({w['resolved_by']})")
    elif args.cmd == "list":
        ws = load_watches()
        if not ws:
            print("No watches yet. Create one with: python -m src.watch add \"16th & Mission\"")
        for w in ws:
            print(f"{w['id']:<4} {w['schedule']:<7} last run {str(w['last_run_at'] or 'never'):<19}  {w['name']}")
    elif args.cmd == "run":
        now = dt.datetime.now()
        ws = [w for w in load_watches() if (not args.ids or w["id"] in args.ids)]
        due = ws if (args.all or args.ids) else [w for w in ws if is_due(w, now)]
        if not due:
            print("Nothing due. Use --all to run every watch now.")
        for w in due:
            print_alert(run_watch(con, w, use_llm=not args.no_llm))
    elif args.cmd == "replay":
        w = next((x for x in load_watches() if x["id"] == args.id), None)
        if not w:
            sys.exit(f"No watch {args.id}")
        for alert in replay(con, w, args.weeks, use_llm=not args.no_llm):
            print_alert(alert)
    elif args.cmd == "alerts":
        for alert in reversed(load_alerts(args.watch, args.limit)):
            print_alert(alert)
    elif args.cmd == "remove":
        print("Removed." if remove_watch(args.id) else f"No watch {args.id}")


if __name__ == "__main__":
    main()
