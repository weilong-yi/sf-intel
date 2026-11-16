"""The AI assistant: a hand-written tool-use loop over the unified database.

The loop (this is all an "agent" is):
  1. send the question + tool definitions to Claude
  2. if Claude asks for a tool, run it HERE, in our code, and send the result back
  3. repeat until Claude answers in plain text, or MAX_STEPS is hit

Governance, which is the point of the demo:
  - the model never touches the database directly; it can only ask us to run
    a query, and we decide what is allowed
  - the DuckDB connection is read-only with file/network access disabled
  - every question, query and answer is appended to data/audit_log.jsonl

The loop reports what it does as events (step, tool_call, tool_result, answer).
The terminal prints them; the web UI (src/server.py) streams them to the browser.

Run:  python -m src.assistant "What changed around 16th & Mission in the last 30 days?"
      python -m src.assistant          (interactive; follow-up questions keep context)
"""

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable

import anthropic
import duckdb
from dotenv import load_dotenv

from src import dossier
from src.prompts import SYSTEM_PROMPT, TOOLS

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("CLAUDE_EFFORT", "medium")  # low | medium | high | xhigh | max
MAX_STEPS = 8          # hard cap on model calls per question: no runaway loops
MAX_TOKENS = 16000
ROW_LIMIT = 200        # rows returned to the model per query
CELL_CHARS = 120       # long text cells are truncated
AUDIT_LOG = Path("data/audit_log.jsonl")
SCHEMA_TABLES = ["event", "location", "location_alias", "stg_police", "stg_fire", "stg_311"]
PRICE_PER_MTOK = {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00}  # Opus 5.5

DIM, BOLD, CYAN, YELLOW, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m"


# ------------------------------------------------------------------ database
def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(dossier.DB_PATH, read_only=True)
    # Read-only stops writes; this stops read_csv('C:/anything'), http, etc.
    # lock_configuration stops a query from switching it back on.
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


def schema_info(con) -> list[dict]:
    out = []
    for table in SCHEMA_TABLES:
        cols = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position", [table]).fetchall()
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        out.append({"table": table, "rows": n, "columns": [{"name": c, "type": t} for c, t in cols]})
    return out


def describe_schema(con) -> str:
    return "\n".join(
        f"{t['table']} ({t['rows']:,} rows): " + ", ".join(f"{c['name']} {c['type']}" for c in t["columns"])
        for t in schema_info(con))


# --------------------------------------------------------------------- tools
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|create|drop|alter|attach|detach|copy|export|import|"
    r"install|load|pragma|set|call|checkpoint)\b", re.I)


def query_rows(con, sql: str, limit: int = ROW_LIMIT) -> dict:
    """Validate and run one read-only query. Returns columns, rows, truncated.
    Raises ValueError for disallowed SQL, duckdb.Error for bad SQL."""
    sql = sql.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", sql, re.I):
        raise ValueError("Only a single SELECT (or WITH ... SELECT) query is allowed.")
    if ";" in sql:
        raise ValueError("Only one statement per call.")
    if FORBIDDEN.search(re.sub(r"'[^']*'", "''", sql)):  # ignore words inside string literals
        raise ValueError("Query contains a forbidden keyword (writes/config are not allowed).")

    cur = con.execute(f"SELECT * FROM ({sql}) LIMIT {limit + 1}")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return {"columns": cols, "rows": rows[:limit], "truncated": len(rows) > limit}


def rows_to_text(result: dict) -> str:
    def cell(v):
        s = "" if v is None else str(v)
        return s if len(s) <= CELL_CHARS else s[:CELL_CHARS] + "…"

    out = [" | ".join(result["columns"])] + [" | ".join(cell(v) for v in r) for r in result["rows"]]
    note = ", TRUNCATED at limit - aggregate further" if result["truncated"] else ""
    return "\n".join(out + [f"({len(result['rows'])} rows{note})"])


def run_sql(con, sql: str) -> str:
    return rows_to_text(query_rows(con, sql))


def location_profile_data(con, place: str, radius_m: int) -> dict:
    match = dossier.resolve(con, place)
    if "error" in match:
        return match
    profile = dossier.build(con, match["location_id"], max(0, min(radius_m, 500)))
    profile["resolved"] = {"input": place, "matched": match["label"], "method": match["method"]}
    return profile


def location_profile(con, place: str, radius_m: int) -> str:
    return json.dumps(location_profile_data(con, place, radius_m), default=str)


def execute_tool(con, name: str, args: dict) -> dict:
    """Returns {"text": what the model sees, "is_error": bool, "data": structured
    result for the UI}. Errors go back to the model as tool results so it can
    fix its query, instead of crashing the loop."""
    try:
        if name == "run_sql":
            result = query_rows(con, args["sql"])
            return {"text": rows_to_text(result), "is_error": False, "data": result}
        if name == "location_profile":
            data = location_profile_data(con, args["place"], args["radius_m"])
            return {"text": json.dumps(data, default=str), "is_error": "error" in data, "data": data}
        return {"text": f"Unknown tool: {name}", "is_error": True, "data": None}
    except (ValueError, KeyError, duckdb.Error) as e:
        return {"text": f"Error: {type(e).__name__}: {e}", "is_error": True, "data": None}


# ---------------------------------------------------------------------- loop
Event = dict
EventHandler = Callable[[Event], None]


class Assistant:
    def __init__(self, con: duckdb.DuckDBPyConnection | None = None):
        load_dotenv()
        self.client = anthropic.Anthropic()
        self.con = con or connect()
        self.system = SYSTEM_PROMPT.format(schema=describe_schema(self.con), row_limit=ROW_LIMIT)
        self.messages: list = []  # whole conversation, so follow-ups keep context
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    def ask(self, question: str, on_event: EventHandler | None = None) -> str:
        emit = on_event or print_event
        audit = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "model": MODEL,
                 "question": question, "tool_calls": []}
        self.messages.append({"role": "user", "content": question})
        answer = None
        started = time.time()

        for step in range(1, MAX_STEPS + 1):
            t = time.time()
            response = self.client.beta.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=self.system,
                tools=TOOLS,
                messages=self.messages,
                output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"},  # system + tools + history reused across steps
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # if the model declines, the API retries on a fallback model
            )
            self._track(response.usage)
            # Keep the full content (incl. thinking blocks) so the next call has the whole turn
            self.messages.append({"role": "assistant", "content": response.content})
            emit({"type": "step", "step": step, "stop_reason": response.stop_reason,
                  "seconds": round(time.time() - t, 1)})

            if response.stop_reason == "refusal":
                answer = "The model declined to answer this request."
                break
            if response.stop_reason == "max_tokens":
                answer = "Stopped: response hit the max_tokens limit."
                break

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                answer = "\n".join(b.text for b in response.content if b.type == "text")
                break

            results = []
            for tu in tool_uses:
                emit({"type": "tool_call", "id": tu.id, "name": tu.name, "input": tu.input})
                out = execute_tool(self.con, tu.name, tu.input)
                emit({"type": "tool_result", "id": tu.id, "name": tu.name,
                      "is_error": out["is_error"], "text": out["text"], "data": out["data"]})
                audit["tool_calls"].append({"tool": tu.name, "input": tu.input, "is_error": out["is_error"],
                                            "result_preview": out["text"][:300]})
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": out["text"], "is_error": out["is_error"]})
            # All results for this step go back in ONE user message
            self.messages.append({"role": "user", "content": results})
        else:
            answer = f"Stopped after {MAX_STEPS} steps without a final answer."

        audit["answer"] = answer
        audit["usage_so_far"] = dict(self.usage)
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(audit, default=str) + "\n")
        emit({"type": "answer", "text": answer, "seconds": round(time.time() - started, 1),
              "usage": dict(self.usage), "cost_usd": round(self.cost(), 4), "model": MODEL})
        return answer

    def _track(self, u):
        self.usage["input"] += u.input_tokens
        self.usage["output"] += u.output_tokens
        self.usage["cache_read"] += u.cache_read_input_tokens or 0
        self.usage["cache_write"] += u.cache_creation_input_tokens or 0

    def cost(self) -> float:
        return sum(self.usage[k] * PRICE_PER_MTOK[k] for k in self.usage) / 1e6


# ------------------------------------------------------------ terminal output
def print_event(ev: Event) -> None:
    kind = ev["type"]
    if kind == "step":
        print(f"{DIM}[step {ev['step']}] {ev['stop_reason']} · {ev['seconds']}s{RESET}")
    elif kind == "tool_call":
        args = ev["input"]
        if ev["name"] == "run_sql":
            print(f"{CYAN}→ run_sql: {args.get('purpose', '')}{RESET}")
            for line in args.get("sql", "").strip().splitlines():
                print(f"{DIM}    {line}{RESET}")
        else:
            print(f"{CYAN}→ {ev['name']}({json.dumps(args)}){RESET}")
    elif kind == "tool_result":
        text = ev["text"]
        lines = text.splitlines()
        color = YELLOW if ev["is_error"] else DIM
        preview = lines[:8] if not text.startswith("{") else [text[:300] + "…"]
        for line in preview:
            print(f"{color}    ← {line[:160]}{RESET}")
        if len(lines) > 8:
            print(f"{DIM}    ← … {len(lines) - 8} more lines{RESET}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    os.system("")  # enables ANSI colours in the Windows console
    bot = Assistant()
    print(f"{DIM}model={MODEL} effort={EFFORT} max_steps={MAX_STEPS}{RESET}")
    questions = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else None

    while True:
        q = questions.pop(0) if questions else (None if questions is not None else input(f"\n{BOLD}ask> {RESET}").strip())
        if not q or q in {"exit", "quit"}:
            break
        print(f"\n{BOLD}Q: {q}{RESET}")
        print(f"\n{BOLD}A:{RESET} {bot.ask(q)}")
        print(f"{DIM}[tokens in={bot.usage['input']} cache_read={bot.usage['cache_read']} "
              f"cache_write={bot.usage['cache_write']} out={bot.usage['output']} · ~${bot.cost():.3f}]{RESET}")
        if questions is not None and not questions:
            break


if __name__ == "__main__":
    main()
