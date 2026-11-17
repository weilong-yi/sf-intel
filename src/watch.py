"""Monitoring agents: watch a place and report what's new.

The chat assistant answers when someone asks. A watch works when nobody asks:
on a schedule it checks one area for new events and unusual jumps, and writes
an alert with a short summary that cites the events behind it.

Each run has two parts, deliberately separated:

  1. DETECT (plain SQL, no AI, free)
       - new events in the area: every matching event since the watch was
         created that this watch has not reported yet. "Not reported yet" is
         a list of event ids, not a timestamp, because police publish many
         reports days after the incident time we store as occurred_at; a
         timestamp checkpoint would skip those late reports forever.
       - jumps: a source/category whose last 7 days beat its earlier weekly
         average by threshold_pct (and at least min_count events)
     Nothing new -> the run is recorded as "quiet" and no model is called.

  2. SUMMARIZE (one model call, tighter limits than the chat assistant)
       - no tools at all: the model only sees what detection found
       - low effort, small max_tokens
       - every [source:id] it cites must be in what it was given; anything
         else marks the alert's citations as invalid
       - if the API fails, a plain-text summary is used instead

Storage is small files, so the database stays strictly read-only:
  data/watches.json          the watch definitions and their last-checked point
  data/watch_seen/<id>.json  event ids each watch has already reported
  data/alerts.jsonl          one line per run (also the audit trail for the agent)
  data/locks/                file locks: store.lock guards every read/write of the files
                             above; run-<id>.lock lets one run of a watch go at a time.
                             Both work across processes (web server + scheduled job).

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
import contextlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import anthropic
from dotenv import load_dotenv

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from src import dossier
from src.prompts import AGENT_SUMMARY_PROMPT

WATCHES_FILE = Path("data/watches.json")
ALERTS_FILE = Path("data/alerts.jsonl")
SEEN_DIR = Path("data/watch_seen")
SOURCES = ["police", "fire_ems", "311"]
SCHEDULES = {"hourly": dt.timedelta(hours=1), "daily": dt.timedelta(days=1), "weekly": dt.timedelta(days=7)}
FIRST_LOOKBACK_DAYS = 7      # a new watch's first run reports the last week of data
MAX_EVENTS_TO_MODEL = 40     # newest events listed to the model; counts cover all of them
SUMMARY_MAX_TOKENS = 2000
SUMMARY_EFFORT = "low"
CITATION = re.compile(r"\b(police|fire_ems|311):[\w-]+")

STORE_LOCK_TIMEOUT = 30      # seconds to wait for another process's short read/write of the files
RUN_LOCK_TIMEOUT = 600       # seconds to wait for another run of the same watch (it may call the model)
LOCK_DIR = Path("data/locks")


class WatchGone(ValueError):
    """The watch was deleted (or replaced by a new watch with the same id) before it could run."""


# -------------------------------------------------------------------- locks
@contextlib.contextmanager
def _file_lock(name: str, timeout: float):
    """An exclusive lock on data/locks/<name>.lock, shared by every process and thread:
    the web server's request threads and a scheduled `python -m src.watch run` alike.
    (A threading.Lock only covers one process.) Each acquire opens its own handle, and
    the OS drops the lock if the process dies, so a crash never leaves it stuck."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    with open(LOCK_DIR / f"{name}.lock", "a+b") as f:
        while True:
            try:
                if os.name == "nt":
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError(f"'{name}' is busy in another process; waited {timeout:.0f}s") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _store():
    """Guards every read and write of watches.json, watch_seen/ and alerts.jsonl.
    Reads need it too: on Windows, replacing a file another handle has open fails."""
    return _file_lock("store", STORE_LOCK_TIMEOUT)


# -------------------------------------------------------------------- store
# Functions starting with _ assume the caller holds _store(); the public ones take it.
def _read_watches() -> list[dict]:
    try:
        return json.loads(WATCHES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)  # a crash never leaves a half-written file


def _write_watches(watches: list[dict]) -> None:
    _atomic_write(WATCHES_FILE, json.dumps(watches, indent=2, default=str))


def _seen_file(watch_id: str) -> Path:
    return SEEN_DIR / f"{watch_id}.json"


def _read_seen(watch_id: str) -> set[str]:
    try:
        return set(json.loads(_seen_file(watch_id).read_text(encoding="utf-8")))
    except FileNotFoundError:
        return set()


def _write_seen(watch_id: str, event_ids: set[str]) -> None:
    _atomic_write(_seen_file(watch_id), json.dumps(sorted(event_ids)))


def load_watches() -> list[dict]:
    with _store():
        return _read_watches()


def load_seen(watch_id: str) -> set[str]:
    with _store():
        return _read_seen(watch_id)


def same_watch(a: dict, b: dict) -> bool:
    """Ids are reused after a delete; uid tells two watches with the same id apart.
    Watches created before uids existed have none, and match on id alone."""
    return a["id"] == b["id"] and a.get("uid") == b.get("uid")


def baseline(w: dict) -> dt.datetime:
    """Events after this point are owed to the watch. Watches created before the
    seen-list existed use their checkpoint at the time of the upgrade."""
    return dt.datetime.fromisoformat(w.get("baseline_data_at") or w["last_checked_data_at"])


def load_alerts(watch_id: str | None = None, limit: int = 50) -> list[dict]:
    with _store():
        if not ALERTS_FILE.exists():
            return []
        lines = ALERTS_FILE.read_text(encoding="utf-8").splitlines()
    alerts = [json.loads(line) for line in lines if line.strip()]
    if watch_id:
        alerts = [a for a in alerts if a["watch_id"] == watch_id]
    return list(reversed(alerts))[:limit]


def append_alert(alert: dict) -> None:
    with _store():
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
    with _store():
        watches = _read_watches()
        n = max([int(w["id"][1:]) for w in watches] + [0]) + 1
        # First run looks back one week of *data* time
        start = (data_through(con) - dt.timedelta(days=FIRST_LOOKBACK_DAYS)).isoformat()
        watch = {
            "id": f"w{n}",
            "uid": uuid.uuid4().hex,
            "name": name or describe(d),
            **d,
            "threshold_pct": threshold_pct,
            "min_count": min_count,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "baseline_data_at": start,       # events after this are owed to the watch
            "last_checked_data_at": start,   # newest data the last run saw (shown as CHECKED TO)
            "last_run_at": None,
            "enabled": True,
        }
        watches.append(watch)
        _write_watches(watches)
        _write_seen(watch["id"], set())  # ids are reused after a delete: start with a clean list
    return watch


def remove_watch(watch_id: str) -> bool:
    with _store():
        watches = _read_watches()
        kept = [w for w in watches if w["id"] != watch_id]
        _write_watches(kept)
        _seen_file(watch_id).unlink(missing_ok=True)
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


def detect(con, w: dict, since: dt.datetime, as_of: dt.datetime,
           exclude: set[str] = frozenset(), late_before: dt.datetime | None = None) -> dict:
    """Events matching the watch with since < occurred_at <= as_of, minus `exclude`
    (ids already reported). `late_before` marks events that occurred before the
    previous check, i.e. were published late."""
    area = dossier.area_location_ids(con, w["location_id"], w["radius_m"])
    where, params = _filters(w)
    params[0] = area

    def rows(sql, args):
        cur = con.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # Placeholder order follows the SQL text: SELECT list, then WHERE.
    new_events = rows(f"""
        SELECT event_id, strftime(occurred_at, '%Y-%m-%d %H:%M') AS occurred_at, source,
               category, subcategory, status,
               (SELECT label FROM location l WHERE l.location_id = event.location_id) AS place,
               coalesce(occurred_at <= ?::TIMESTAMP, false) AS reported_late
        FROM event
        WHERE {where} AND occurred_at > ? AND occurred_at <= ?
          AND event_id NOT IN (SELECT unnest(?::VARCHAR[]))
        ORDER BY occurred_at DESC""", [late_before] + params + [since, as_of, sorted(exclude)])

    # Filter params bind first (inner query), then as_of; keeps placeholder order simple.
    trend = rows(f"""
        SELECT e.source, coalesce(e.category, 'Unclassified') AS category,
               count(*) FILTER (e.occurred_at > t.a - INTERVAL 7 DAY)                    AS last_7d,
               round(count(*) FILTER (e.occurred_at <= t.a - INTERVAL 7 DAY) / 4.0, 1)   AS prior_weekly_avg
        FROM (SELECT * FROM event WHERE {where}) e, (SELECT ?::TIMESTAMP AS a) t
        WHERE e.occurred_at > t.a - INTERVAL 35 DAY AND e.occurred_at <= t.a
        GROUP BY 1, 2""", params + [as_of])

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
            "late_count": sum(1 for e in new_events if e["reported_late"]),
            "new_by_category": tally(lambda e: f"{e['source']} · {e['category'] or 'Unclassified'}"),
            "new_by_place": tally(lambda e: e["place"] or "unknown"),
            "new_by_day": dict(sorted(tally(lambda e: e["occurred_at"][:10]).items())),
            "new_by_status": tally(lambda e: e["status"] or "unknown"),
            "new_events": new_events, "jumps": jumps}


# ---------------------------------------------------------------- summarize
def plain_summary(w: dict, found: dict) -> str:
    parts = [f"{found['new_count']} new event(s) within {w['radius_m']:.0f} m of {w['label']}."]
    if found.get("late_count"):
        parts.append(f"{found['late_count']} of them occurred before the previous check but were published since.")
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
        "reported_late_count": found.get("late_count", 0),
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
    Normal runs advance the watch's checkpoint and seen list; replays don't.

    A normal run holds the watch's own run lock from reading its state to saving it, so
    two runs of one watch (Run now during the scheduled job) take turns and the second
    sees what the first reported. `w` only names the watch: its state is re-read here,
    because the caller's copy may be stale. Raises WatchGone if it was deleted."""
    if replay:  # a past week as if it were new: no memory, writes nothing but the alert
        return _run_once(con, w, as_of, since, use_llm, replay=True, seen=set())
    with _file_lock(f"run-{w['id']}", RUN_LOCK_TIMEOUT):
        with _store():
            current = next((x for x in _read_watches() if same_watch(x, w)), None)
            seen = _read_seen(w["id"]) if current else set()
        if current is None:
            raise WatchGone(f"Watch {w['id']} was deleted before it ran.")
        return _run_once(con, current, as_of, None, use_llm, replay=False, seen=seen)


def _run_once(con, w: dict, as_of: dt.datetime | None, since: dt.datetime | None,
              use_llm: bool, replay: bool, seen: set[str]) -> dict:
    started = time.time()
    as_of = as_of or data_through(con)
    since = since or dt.datetime.fromisoformat(w["last_checked_data_at"])
    if replay:  # a past week as if it were new: no memory of what was reported
        found = detect(con, w, since, as_of)
    else:       # everything owed since creation that this watch has not reported yet
        found = detect(con, w, baseline(w), as_of, exclude=seen, late_before=since)

    alert = {"id": f"{w['id']}-{int(time.time() * 1000)}", "watch_id": w["id"], "watch_name": w["name"],
             "label": w["label"], "run_at": dt.datetime.now().isoformat(timespec="seconds"),
             "period_from": str(since), "period_to": str(as_of), "replay": replay,
             "new_count": found["new_count"], "late_count": found["late_count"],
             "new_by_category": found["new_by_category"],
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
        with _store():
            watches = _read_watches()
            mine = [x for x in watches if same_watch(x, w)]  # deleted (or id reused) meanwhile: save nothing
            for x in mine:
                x["last_checked_data_at"] = as_of.isoformat()
                x["last_run_at"] = alert["run_at"]
            if mine:
                _write_watches(watches)
                # Only ids that just went out in an alert are added, so nothing is marked
                # reported without being reported.
                reported = _read_seen(w["id"]) | {e["event_id"] for e in found["new_events"]}
                _write_seen(w["id"], prune_seen(con, reported))
    return alert


def prune_seen(con, seen: set[str]) -> set[str]:
    """Drop ids no longer in the database (aged out of the 90-day window): they can
    never be detected again, so remembering them only grows the file."""
    if not seen:
        return seen
    rows = con.execute("SELECT event_id FROM event WHERE event_id IN (SELECT unnest(?::VARCHAR[]))",
                       [sorted(seen)]).fetchall()
    return {r[0] for r in rows}


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
            try:
                print_alert(run_watch(con, w, use_llm=not args.no_llm))
            except WatchGone as e:  # deleted in the web UI while this loop ran
                print(f"\nSkipped: {e}")
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
