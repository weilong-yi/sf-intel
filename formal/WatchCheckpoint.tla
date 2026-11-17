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
(*   lock          the store lock. LockScope "machine": _store(), a file    *)
(*                 lock on data/locks/store.lock shared by all processes   *)
(*                 (the code); "process": the old threading.Lock, one per  *)
(*                 process (commits b8672d7, d5615a5)                      *)
(*   runLock       _file_lock("run-<id>"), held by a run from reading its  *)
(*                 state to saving it (RunLock = TRUE; the code)           *)
(*   inc           a watch's uid: tells apart two watches that got the     *)
(*                 same id (ids are reused after a delete). MatchUid: the  *)
(*                 code saves only into the same uid (same_watch)          *)
(*   ServerThreads request threads of `python -m src.server`               *)
(*                 (ThreadingHTTPServer)                                   *)
(*   CliThreads    `python -m src.watch run` (e.g. the Task Scheduler      *)
(*                 job in watch.py's docstring), a second process          *)
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
(*   - file-system effects are left out: corrupt or unreadable files from  *)
(*     unlocked writes are covered by experiments/05_concurrent_...py      *)
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
    LockScope,       \* "machine": a cross-process file lock (the code); "process": threading.Lock
    RunLock,         \* TRUE: a per-watch run lock, state re-read under it (the code)
    MatchUid,        \* TRUE: a run saves only into the watch with the uid it read (the code)
    LateArrivals,    \* TRUE: a rebuild can add events older than DataThrough (police report late)
    Checkpoint       \* "seen_ids" (current code) or "occurred_at" (before the fix)

Threads == ServerThreads \cup CliThreads
Events  == 1..NumEvents
WIds    == 1..NumWatches

VARIABLES
    db,         \* events in the built database
    file,       \* watches.json: watch id -> [base, ckpt, inc]
    seenFile,   \* watch_seen/<id>.json: watch id -> event ids already reported ({} when no file)
    lock,       \* lock name -> holding thread, or "free"
    runLock,    \* watch id -> thread holding its run lock, or "free"
    nextInc,    \* uid generator: every add_watch gets a fresh one
    pc,         \* thread -> program counter
    local,      \* thread -> its in-memory copy of watches.json
    localSeen,  \* thread -> its in-memory copy of one seen list (read under the lock)
    target,     \* thread -> watch id it is working on
    since,      \* thread -> last_checked_data_at it read (run_watch's `since`)
    snapBase,   \* thread -> baseline_data_at it read
    snapSeen,   \* thread -> seen list it read at the start of the run
    snapInc,    \* thread -> uid of the watch it is running
    asOf,       \* thread -> data_through it read (run_watch's `as_of`)
    found,      \* thread -> events its detect() returned
    ops,        \* thread -> operations started
    rebuilds,
    \* ghost state: what the user has been told, used only by the properties
    live,       \* ids whose add_watch returned and whose remove_watch has not
    created,    \* id -> base at creation (events after it are owed to the user)
    incOf,      \* id -> uid of the watch the user currently has under that id
    reported,   \* id -> events reported in regular alerts since that creation
    missed,     \* TRUE once a run skipped an owed, never-reported event
    dup         \* TRUE once a regular alert repeated an event already reported

vars == <<db, file, seenFile, lock, runLock, nextInc, pc, local, localSeen, target, since, snapBase,
          snapSeen, snapInc, asOf, found, ops, rebuilds, live, created, incOf, reported, missed, dup>>
threadVars == <<local, localSeen, target, since, snapBase, snapSeen, snapInc, asOf, found>>
ghostVars  == <<live, created, incOf, reported, missed, dup>>

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
    /\ file = [x \in {} |-> [base |-> 0, ckpt |-> 0, inc |-> 0]]
    /\ seenFile = [w \in WIds |-> {}]
    /\ lock = [l \in LockNames |-> "free"]
    /\ runLock = [w \in WIds |-> "free"]
    /\ nextInc = 1
    /\ pc = [t \in Threads |-> "idle"]
    /\ local = [t \in Threads |-> [x \in {} |-> [base |-> 0, ckpt |-> 0, inc |-> 0]]]
    /\ localSeen = [t \in Threads |-> {}]
    /\ target = [t \in Threads |-> 0]
    /\ since = [t \in Threads |-> 0]
    /\ snapBase = [t \in Threads |-> 0]
    /\ snapSeen = [t \in Threads |-> {}]
    /\ snapInc = [t \in Threads |-> 0]
    /\ asOf = [t \in Threads |-> 0]
    /\ found = [t \in Threads |-> {}]
    /\ ops = [t \in Threads |-> 0]
    /\ rebuilds = 0
    /\ live = {}
    /\ created = [w \in WIds |-> 0]
    /\ incOf = [w \in WIds |-> 0]
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
    /\ UNCHANGED <<db, file, seenFile, runLock, nextInc, localSeen, target, since, snapBase, snapSeen, snapInc, asOf, found, rebuilds, ghostVars>>

AddSave(t) ==
    LET id == NextId(local[t]) IN
    /\ pc[t] = "add_save" /\ id <= NumWatches
    /\ file' = Put(local[t], id, [base |-> DataThrough, ckpt |-> DataThrough, inc |-> nextInc])
    /\ seenFile' = [seenFile EXCEPT ![id] = {}]
    /\ lock' = Release(t, "add")
    /\ pc' = [pc EXCEPT ![t] = "idle"]
    /\ live' = live \cup {id}                          \* add_watch returned the watch to the user
    /\ created' = [created EXCEPT ![id] = DataThrough]
    /\ incOf' = [incOf EXCEPT ![id] = nextInc]
    /\ nextInc' = nextInc + 1                          \* uuid.uuid4()
    /\ reported' = [reported EXCEPT ![id] = {}]        \* a new watch, even if the id was used before
    /\ UNCHANGED <<db, runLock, threadVars, ops, rebuilds, missed, dup>>

AddFull(t) ==   \* all ids used: add_watch still returns, nothing to model
    /\ pc[t] = "add_save" /\ NextId(local[t]) > NumWatches
    /\ lock' = Release(t, "add")
    /\ pc' = [pc EXCEPT ![t] = "idle"]
    /\ UNCHANGED <<db, file, seenFile, runLock, nextInc, threadVars, ops, rebuilds, ghostVars>>

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
    /\ UNCHANGED <<db, file, seenFile, runLock, nextInc, localSeen, since, snapBase, snapSeen, snapInc, asOf, found, rebuilds, ghostVars>>

RemoveSave(t) ==
    /\ pc[t] = "rm_save"
    /\ file' = Drop(local[t], target[t])
    /\ seenFile' = [seenFile EXCEPT ![target[t]] = {}]
    /\ lock' = Release(t, "remove")
    /\ pc' = [pc EXCEPT ![t] = "idle"]
    /\ live' = live \ {target[t]}                      \* remove_watch returned True to the user
    /\ UNCHANGED <<db, runLock, nextInc, threadVars, ops, rebuilds, created, incOf, reported, missed, dup>>

(* ---- run_watch, non-replay.                                            *)
(*  RunLock = TRUE (the code):                                            *)
(*   RunSnap:   `with _file_lock(f"run-{w['id']}")`, then under _store(): *)
(*              re-read the watch (same_watch) and `_read_seen(id)`;      *)
(*              since = last_checked_data_at; baseline(w). One step.      *)
(*  RunLock = FALSE (before): the dict `w` was read earlier, outside any  *)
(*              lock (server per request; CLI once in main()), and the    *)
(*              seen list later by `load_seen` (RunSnap, then RunSeen).   *)
(*   RunDetect: `as_of = data_through(con)`; detect(...) then             *)
(*              append_alert(alert) reports `found`.                      *)
(*              seen_ids:    baseline < occurred_at <= as_of, not seen    *)
(*              occurred_at: since    < occurred_at <= as_of              *)
(*   RunLoad/RunSave: under the store lock: re-read watches.json; if the  *)
(*              same watch (same uid when MatchUid, else same id) is      *)
(*              there: set last_checked_data_at = as_of, write, and       *)
(*              write seen = re-read seen | found. Then the run lock is   *)
(*              released.                                                 *)
RunSnap(t, w) ==
    /\ Begin(t)
    /\ w \in DOMAIN file
    /\ IF RunLock THEN runLock[w] = "free" /\ CanTake(t, "run_save") ELSE TRUE
    /\ runLock' = IF RunLock THEN [runLock EXCEPT ![w] = t] ELSE runLock
    /\ target' = [target EXCEPT ![t] = w]
    /\ since' = [since EXCEPT ![t] = file[w].ckpt]
    /\ snapBase' = [snapBase EXCEPT ![t] = file[w].base]
    /\ snapInc' = [snapInc EXCEPT ![t] = file[w].inc]
    /\ snapSeen' = IF RunLock THEN [snapSeen EXCEPT ![t] = seenFile[w]] ELSE snapSeen
    /\ pc' = [pc EXCEPT ![t] = IF RunLock THEN "run_detect" ELSE "run_seen"]
    /\ UNCHANGED <<db, file, seenFile, lock, nextInc, local, localSeen, asOf, found, rebuilds, ghostVars>>

RunSeen(t) ==
    /\ pc[t] = "run_seen"
    /\ snapSeen' = [snapSeen EXCEPT ![t] = seenFile[target[t]]]
    /\ pc' = [pc EXCEPT ![t] = "run_detect"]
    /\ UNCHANGED <<db, file, seenFile, lock, runLock, nextInc, local, localSeen, target, since, snapBase,
                   snapInc, asOf, found, ops, rebuilds, ghostVars>>

RunDetect(t) ==
    LET w == target[t]
        f == IF Checkpoint = "seen_ids"
             THEN {e \in db : snapBase[t] < e /\ e <= DataThrough /\ e \notin snapSeen[t]}
             ELSE {e \in db : since[t] < e /\ e <= DataThrough}
        owed == {e \in db : created[w] < e /\ e <= DataThrough}
        current == w \in live /\ snapInc[t] = incOf[w]   \* this run is of the watch the user has now
    IN
    /\ pc[t] = "run_detect"
    /\ asOf' = [asOf EXCEPT ![t] = DataThrough]
    /\ found' = [found EXCEPT ![t] = f]
    /\ missed' = (missed \/ (current /\ \E e \in owed : e \notin f /\ e \notin reported[w]))
    /\ dup' = (dup \/ (current /\ f \cap reported[w] # {}))
    /\ reported' = IF current THEN [reported EXCEPT ![w] = @ \cup f] ELSE reported
    /\ pc' = [pc EXCEPT ![t] = "run_load"]
    /\ UNCHANGED <<db, file, seenFile, lock, runLock, nextInc, local, localSeen, target, since, snapBase,
                   snapSeen, snapInc, ops, rebuilds, live, created, incOf>>

RunLoad(t) ==
    /\ pc[t] = "run_load"
    /\ CanTake(t, "run_save")
    /\ lock' = Take(t, "run_save")
    /\ local' = [local EXCEPT ![t] = file]
    /\ localSeen' = [localSeen EXCEPT ![t] = seenFile[target[t]]]
    /\ pc' = [pc EXCEPT ![t] = "run_save"]
    /\ UNCHANGED <<db, file, seenFile, runLock, nextInc, target, since, snapBase, snapSeen, snapInc,
                   asOf, found, ops, rebuilds, ghostVars>>

RunSave(t) ==
    LET w == target[t]
        mine == w \in DOMAIN local[t] /\ (~MatchUid \/ local[t][w].inc = snapInc[t])
    IN
    /\ pc[t] = "run_save"
    \* MatchUid (the code) writes only when it updated its own watch; before, it always wrote back.
    /\ file' = IF mine THEN [local[t] EXCEPT ![w].ckpt = asOf[t]]
               ELSE IF MatchUid THEN file ELSE local[t]
    /\ seenFile' = IF Checkpoint = "seen_ids" /\ mine
                   THEN [seenFile EXCEPT ![w] = localSeen[t] \cup found[t]] ELSE seenFile
    /\ lock' = Release(t, "run_save")
    /\ runLock' = IF RunLock THEN [runLock EXCEPT ![w] = "free"] ELSE runLock
    /\ pc' = [pc EXCEPT ![t] = "idle"]
    /\ UNCHANGED <<db, nextInc, threadVars, ops, rebuilds, ghostVars>>

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
    /\ UNCHANGED <<file, seenFile, lock, runLock, nextInc, pc, threadVars, ops, ghostVars>>

ThreadStep ==
    \E t \in Threads :
        \/ AddLoad(t) \/ AddSave(t) \/ AddFull(t)
        \/ RemoveSave(t) \/ RunSeen(t) \/ RunDetect(t) \/ RunLoad(t) \/ RunSave(t)
        \/ \E w \in WIds : RemoveLoad(t, w) \/ RunSnap(t, w)

Next == Rebuild \/ ThreadStep

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

\* While any operation is in progress, some thread can take a step (no lock cycle).
\* Needed because check.py runs TLC with deadlock checking off (runs are bounded).
\* Harm: the web server or the scheduled job hangs forever.
NoStuckThread == (\E t \in Threads : pc[t] # "idle") => ENABLED ThreadStep

TypeOK ==
    /\ db \subseteq Events
    /\ DOMAIN file \subseteq WIds
    /\ \A w \in WIds : seenFile[w] \subseteq Events
    /\ \A t \in Threads : pc[t] \in {"idle", "add_save", "rm_save", "run_seen", "run_detect", "run_load", "run_save"}
=============================================================================
