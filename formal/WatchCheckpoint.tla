--------------------------- MODULE WatchCheckpoint ---------------------------
(***************************************************************************)
(* A model of sf-intel monitoring watches (src/watch.py) and data refresh. *)
(*                                                                         *)
(* What the user is promised:                                              *)
(*   - a watch a person created stays until a person deletes it, and a     *)
(*     deleted watch stays deleted (NoLostWatch, NoResurrectedWatch)       *)
(*   - a run never skips an event the watch owes and has not reported      *)
(*     (NoMissedEvent)                                                     *)
(*   - no event is reported twice by regular alerts of one watch           *)
(*     (NoDuplicateAlert)                                                  *)
(*                                                                         *)
(* Code map (every action names the lines it stands for):                  *)
(*   file          data/watches.json, read whole by load_watches() and     *)
(*                 replaced whole by save_watches() (atomic tmp.replace).  *)
(*                 Per watch: base = baseline_data_at,                     *)
(*                 ckpt = last_checked_data_at                             *)
(*   seenFile      data/watch_seen/<id>.json (load_seen / save_seen)       *)
(*   db            the event table in data/sf_intel.duckdb                 *)
(*   DataThrough   data_through(con) = max(occurred_at) FROM event         *)
(*   lock          watch._lock, a threading.Lock: one per PROCESS          *)
(*   ServerThreads request threads of `python -m src.server`               *)
(*                 (ThreadingHTTPServer), sharing one _lock                *)
(*   CliThreads    `python -m src.watch run` (e.g. the Task Scheduler      *)
(*                 job in watch.py's docstring), its own _lock             *)
(*   Checkpoint    "seen_ids": detect owes every event after base that is  *)
(*                 not in seenFile (the code since the late-report fix);   *)
(*                 "occurred_at": detect takes occurred_at > ckpt (the     *)
(*                 code before it, commit b8672d7)                         *)
(*                                                                         *)
(* Abstractions (see formal/README.md "Scope"):                            *)
(*   - an event is identified by its occurred_at; Events = 1..N            *)
(*   - every event is inside every watch's area and filters                *)
(*   - a new watch's base is DataThrough (the code subtracts               *)
(*     FIRST_LOOKBACK_DAYS; that only adds events to the first alert)      *)
(*   - replay runs are left out: they never write watches.json or seen     *)
(*   - the LLM summary is left out: it never changes what is reported      *)
(*   - prune_seen is left out: events never leave db in this model         *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    ServerThreads,   \* e.g. {"s1", "s2"}: concurrent web requests
    CliThreads,      \* e.g. {"c1"}: a scheduled `python -m src.watch run`
    NumWatches,      \* watch ids are 1..NumWatches ("w1", "w2", ...)
    NumEvents,       \* events are 1..NumEvents, each identified by its occurred_at
    MaxOps,          \* operations each thread may start (bounds the run)
    MaxRebuilds,     \* data refreshes (ingest + build)
    Locked,          \* which code paths take watch._lock: subset of {"add", "remove", "run_save"}
    LockScope,       \* "process": threading.Lock (the code); "machine": a cross-process file lock
    RunHoldsLock,    \* TRUE: run_watch reads its state under the lock and holds it to the save
    LateArrivals,    \* TRUE: a rebuild can add events older than DataThrough (police report late)
    Checkpoint       \* "seen_ids" (current code) or "occurred_at" (before the fix)

Threads == ServerThreads \cup CliThreads
Events  == 1..NumEvents
WIds    == 1..NumWatches

VARIABLES
    db,         \* events in the built database
    file,       \* watches.json: watch id -> [base, ckpt]
    seenFile,   \* watch_seen/<id>.json: watch id -> event ids already reported ({} when no file)
    lock,       \* lock name -> holding thread, or "free"
    pc,         \* thread -> program counter
    local,      \* thread -> its in-memory copy of watches.json
    localSeen,  \* thread -> its in-memory copy of one seen list (read under the lock)
    target,     \* thread -> watch id it is working on
    since,      \* thread -> last_checked_data_at it read (run_watch's `since`)
    snapBase,   \* thread -> baseline_data_at it read
    snapSeen,   \* thread -> seen list it read at the start of the run
    asOf,       \* thread -> data_through it read (run_watch's `as_of`)
    found,      \* thread -> events its detect() returned
    ops,        \* thread -> operations started
    rebuilds,
    \* ghost state: what the user has been told, used only by the properties
    live,       \* ids whose add_watch returned and whose remove_watch has not
    created,    \* id -> base at creation (events after it are owed to the user)
    reported,   \* id -> events reported in regular alerts since that creation
    missed,     \* TRUE once a run skipped an owed, never-reported event
    dup         \* TRUE once a regular alert repeated an event already reported

vars == <<db, file, seenFile, lock, pc, local, localSeen, target, since, snapBase, snapSeen,
          asOf, found, ops, rebuilds, live, created, reported, missed, dup>>
threadVars == <<local, localSeen, target, since, snapBase, snapSeen, asOf, found>>
ghostVars  == <<live, created, reported, missed, dup>>

Max(S) == CHOOSE x \in S : \A y \in S : x >= y
DataThrough == IF db = {} THEN 0 ELSE Max(db)

\* watches.json is a JSON list; here a function whose domain is the ids present.
Put(f, k, v) == [x \in DOMAIN f \cup {k} |-> IF x = k THEN v ELSE f[x]]
Drop(f, k)   == [x \in DOMAIN f \ {k} |-> f[x]]
NextId(f)    == IF DOMAIN f = {} THEN 1 ELSE Max(DOMAIN f) + 1   \* add_watch: max(ids) + 1

LockOf(t) == IF LockScope = "machine" THEN "file"
             ELSE IF t \in ServerThreads THEN "server" ELSE "cli"
LockNames == IF LockScope = "machine" THEN {"file"} ELSE {"server", "cli"}

\* Take the lock if this code path uses it; otherwise just proceed.
CanTake(t, path) == path \notin Locked \/ lock[LockOf(t)] = "free"
Take(t, path)    == IF path \in Locked THEN [lock EXCEPT ![LockOf(t)] = t] ELSE lock
Release(t, path) == IF path \in Locked THEN [lock EXCEPT ![LockOf(t)] = "free"] ELSE lock

Init ==
    /\ db = {}
    /\ file = [x \in {} |-> [base |-> 0, ckpt |-> 0]]
    /\ seenFile = [w \in WIds |-> {}]
    /\ lock = [l \in LockNames |-> "free"]
    /\ pc = [t \in Threads |-> "idle"]
    /\ local = [t \in Threads |-> [x \in {} |-> [base |-> 0, ckpt |-> 0]]]
    /\ localSeen = [t \in Threads |-> {}]
    /\ target = [t \in Threads |-> 0]
    /\ since = [t \in Threads |-> 0]
    /\ snapBase = [t \in Threads |-> 0]
    /\ snapSeen = [t \in Threads |-> {}]
    /\ asOf = [t \in Threads |-> 0]
    /\ found = [t \in Threads |-> {}]
    /\ ops = [t \in Threads |-> 0]
    /\ rebuilds = 0
    /\ live = {}
    /\ created = [w \in WIds |-> 0]
    /\ reported = [w \in WIds |-> {}]
    /\ missed = FALSE
    /\ dup = FALSE

Begin(t) == pc[t] = "idle" /\ ops[t] < MaxOps /\ ops' = [ops EXCEPT ![t] = @ + 1]

(* ---- add_watch: `with _lock: watches = load_watches()` ...             *)
(*      `watches.append(watch); save_watches(watches);                    *)
(*      save_seen(watch["id"], set())`. Web: POST /api/watches.           *)
(*      Only the server creates watches in this model.                    *)
AddLoad(t) ==
    /\ t \in ServerThreads /\ Begin(t) /\ CanTake(t, "add")
    /\ lock' = Take(t, "add")
    /\ local' = [local EXCEPT ![t] = file]
    /\ pc' = [pc EXCEPT ![t] = "add_save"]
    /\ UNCHANGED <<db, file, seenFile, localSeen, target, since, snapBase, snapSeen, asOf, found, rebuilds, ghostVars>>

AddSave(t) ==
    LET id == NextId(local[t]) IN
    /\ pc[t] = "add_save" /\ id <= NumWatches
    /\ file' = Put(local[t], id, [base |-> DataThrough, ckpt |-> DataThrough])
    /\ seenFile' = [seenFile EXCEPT ![id] = {}]
    /\ lock' = Release(t, "add")
    /\ pc' = [pc EXCEPT ![t] = "idle"]
    /\ live' = live \cup {id}                          \* add_watch returned the watch to the user
    /\ created' = [created EXCEPT ![id] = DataThrough]
    /\ reported' = [reported EXCEPT ![id] = {}]        \* a new watch, even if the id was used before
    /\ UNCHANGED <<db, threadVars, ops, rebuilds, missed, dup>>

AddFull(t) ==   \* all ids used: add_watch still returns, nothing to model
    /\ pc[t] = "add_save" /\ NextId(local[t]) > NumWatches
    /\ lock' = Release(t, "add")
    /\ pc' = [pc EXCEPT ![t] = "idle"]
    /\ UNCHANGED <<db, file, seenFile, threadVars, ops, rebuilds, ghostVars>>

(* ---- remove_watch: `with _lock: watches = load_watches(); kept = [...]; *)
(*      save_watches(kept); _seen_file(id).unlink()`. Saves even when the *)
(*      id is absent. Web: POST /api/watches/delete.                      *)
RemoveLoad(t, w) ==
    /\ t \in ServerThreads /\ Begin(t) /\ CanTake(t, "remove")
    /\ w \in live
    /\ lock' = Take(t, "remove")
    /\ local' = [local EXCEPT ![t] = file]
    /\ target' = [target EXCEPT ![t] = w]
    /\ pc' = [pc EXCEPT ![t] = "rm_save"]
    /\ UNCHANGED <<db, file, seenFile, localSeen, since, snapBase, snapSeen, asOf, found, rebuilds, ghostVars>>

RemoveSave(t) ==
    /\ pc[t] = "rm_save"
    /\ file' = Drop(local[t], target[t])
    /\ seenFile' = [seenFile EXCEPT ![target[t]] = {}]
    /\ lock' = Release(t, "remove")
    /\ pc' = [pc EXCEPT ![t] = "idle"]
    /\ live' = live \ {target[t]}                      \* remove_watch returned True to the user
    /\ UNCHANGED <<db, threadVars, ops, rebuilds, created, reported, missed, dup>>

(* ---- run_watch, non-replay.                                            *)
(*   RunSnap:   the watch dict `w` was read earlier, outside any lock:    *)
(*              server /api/watches/run does load_watches() per request;  *)
(*              CLI `run` does load_watches() once in main().             *)
(*              since = last_checked_data_at; baseline(w)                 *)
(*   RunSeen:   `exclude=load_seen(w["id"])`, outside the lock            *)
(*   RunDetect: `as_of = data_through(con)`; detect(...) then             *)
(*              append_alert(alert) reports `found`.                      *)
(*              seen_ids:    baseline < occurred_at <= as_of, not seen    *)
(*              occurred_at: since    < occurred_at <= as_of              *)
(*   RunLoad/RunSave: `with _lock: watches = load_watches(); ...          *)
(*              x["last_checked_data_at"] = as_of; save_watches(watches); *)
(*              if id still present: save_seen(id, load_seen(id) | found)`*)
RunSnap(t, w) ==
    /\ Begin(t)
    /\ w \in DOMAIN file
    /\ IF RunHoldsLock THEN CanTake(t, "run_save") ELSE TRUE
    /\ lock' = IF RunHoldsLock THEN Take(t, "run_save") ELSE lock
    /\ target' = [target EXCEPT ![t] = w]
    /\ since' = [since EXCEPT ![t] = file[w].ckpt]
    /\ snapBase' = [snapBase EXCEPT ![t] = file[w].base]
    /\ pc' = [pc EXCEPT ![t] = "run_seen"]
    /\ UNCHANGED <<db, file, seenFile, local, localSeen, snapSeen, asOf, found, rebuilds, ghostVars>>

RunSeen(t) ==
    /\ pc[t] = "run_seen"
    /\ snapSeen' = [snapSeen EXCEPT ![t] = seenFile[target[t]]]
    /\ pc' = [pc EXCEPT ![t] = "run_detect"]
    /\ UNCHANGED <<db, file, seenFile, lock, local, localSeen, target, since, snapBase, asOf, found, ops, rebuilds, ghostVars>>

RunDetect(t) ==
    LET w == target[t]
        f == IF Checkpoint = "seen_ids"
             THEN {e \in db : snapBase[t] < e /\ e <= DataThrough /\ e \notin snapSeen[t]}
             ELSE {e \in db : since[t] < e /\ e <= DataThrough}
        owed == {e \in db : created[w] < e /\ e <= DataThrough}
    IN
    /\ pc[t] = "run_detect"
    /\ asOf' = [asOf EXCEPT ![t] = DataThrough]
    /\ found' = [found EXCEPT ![t] = f]
    /\ missed' = (missed \/ (w \in live /\ \E e \in owed : e \notin f /\ e \notin reported[w]))
    /\ dup' = (dup \/ (f \cap reported[w] # {}))
    /\ reported' = [reported EXCEPT ![w] = @ \cup f]
    /\ pc' = [pc EXCEPT ![t] = "run_load"]
    /\ UNCHANGED <<db, file, seenFile, lock, local, localSeen, target, since, snapBase, snapSeen, ops, rebuilds, live, created>>

RunLoad(t) ==
    /\ pc[t] = "run_load"
    /\ IF RunHoldsLock THEN TRUE ELSE CanTake(t, "run_save")
    /\ lock' = IF RunHoldsLock THEN lock ELSE Take(t, "run_save")
    /\ local' = [local EXCEPT ![t] = file]
    /\ localSeen' = [localSeen EXCEPT ![t] = seenFile[target[t]]]
    /\ pc' = [pc EXCEPT ![t] = "run_save"]
    /\ UNCHANGED <<db, file, seenFile, target, since, snapBase, snapSeen, asOf, found, ops, rebuilds, ghostVars>>

RunSave(t) ==
    LET w == target[t] IN
    /\ pc[t] = "run_save"
    /\ file' = IF w \in DOMAIN local[t] THEN [local[t] EXCEPT ![w].ckpt = asOf[t]] ELSE local[t]
    /\ seenFile' = IF Checkpoint = "seen_ids" /\ w \in DOMAIN local[t]
                   THEN [seenFile EXCEPT ![w] = localSeen[t] \cup found[t]] ELSE seenFile
    /\ lock' = Release(t, "run_save")
    /\ pc' = [pc EXCEPT ![t] = "idle"]
    /\ UNCHANGED <<db, threadVars, ops, rebuilds, ghostVars>>

(* ---- data refresh: `python -m src.ingest` re-downloads the whole       *)
(*      90-day window by its time column, then `python -m src.build`      *)
(*      rebuilds event. Police occurred_at is incident_datetime, so a     *)
(*      report filed days later appears with an occurred_at behind        *)
(*      DataThrough (LateArrivals).                                       *)
Rebuild ==
    /\ rebuilds < MaxRebuilds
    /\ \E new \in SUBSET (Events \ db) :
        /\ new # {}
        /\ LateArrivals \/ \A e \in new : e > DataThrough
        /\ db' = db \cup new
    /\ rebuilds' = rebuilds + 1
    /\ UNCHANGED <<file, seenFile, lock, pc, threadVars, ops, ghostVars>>

Next ==
    \/ Rebuild
    \/ \E t \in Threads :
        \/ AddLoad(t) \/ AddSave(t) \/ AddFull(t)
        \/ RemoveSave(t) \/ RunSeen(t) \/ RunDetect(t) \/ RunLoad(t) \/ RunSave(t)
        \/ \E w \in WIds : RemoveLoad(t, w) \/ RunSnap(t, w)

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* Properties. One plain sentence each, and the harm if it breaks.         *)

\* A watch the user created and did not delete is in watches.json.
\* Harm: monitoring silently stops for an area someone asked to watch.
NoLostWatch == live \subseteq DOMAIN file

\* A watch the user deleted is not in watches.json.
\* Harm: a deleted watch comes back and keeps running and alerting.
NoResurrectedWatch == DOMAIN file \subseteq live

\* No run of a live watch leaves out an event that is in the database, occurred
\* after the watch was created, and has never been reported by that watch.
\* Harm: an incident in a watched area is never alerted on.
NoMissedEvent == ~missed

\* No event is reported twice by regular alerts of the same watch.
\* Harm: duplicate alerts, inflated "N new" counts, and a doubled model bill.
NoDuplicateAlert == ~dup

TypeOK ==
    /\ db \subseteq Events
    /\ DOMAIN file \subseteq WIds
    /\ \A w \in WIds : seenFile[w] \subseteq Events
    /\ \A t \in Threads : pc[t] \in {"idle", "add_save", "rm_save", "run_seen", "run_detect", "run_load", "run_save"}
=============================================================================
