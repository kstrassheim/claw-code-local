"""Writing planning documents: local spool first, MongoDB when it can.

WRITE LOCALLY, FLUSH OPPORTUNISTICALLY
--------------------------------------
Every write lands in an append-only spool on the workspace volume BEFORE
anything touches the network, and the spool is flushed to MongoDB when it can
be. Three reasons:

  - A runner flushes its call count repeatedly during a long run. Those
    writes must be cheap and must not fail the run.
  - The pod restarts. Anything only in memory is gone; anything in the spool
    survives on the PVC and is flushed by the next run.
  - The store will be unreachable sometimes. A planning store that can stop
    the issue solver is a worse trade than one that is briefly behind.

So: NOTHING here raises. Every public function returns a bool or a value and
logs to stderr. A planning store that kills a solver run has cost more than it
will ever be worth.

IDEMPOTENCE MAKES THE SPOOL SIMPLE
----------------------------------
Document ids are deterministic (planning_docs derives a work event's id from
runId+role), that id becomes MongoDB's `_id`, and every write is an UPSERT via
replace_one(..., upsert=True). A document flushed twice is therefore a no-op,
which means the spool never needs a two-phase commit: on a crash mid-flush the
worst case is re-sending something already stored.

THE DRIVER IS IMPORTED LAZILY
-----------------------------
`import pymongo` happens inside the functions that talk to the server, never at
module scope. The spool is the part that must never fail, and it needs no
driver at all — so a missing or broken pymongo degrades this module to "writes
are being kept locally" instead of breaking every import of it. It also keeps
the unit tests free of a pip dependency: they exercise the spool, the flush
loop and the error classification against an injected fake collection.

For the same reason nothing here classifies a driver exception with
isinstance() — see `_is_permanent`.

CONFIGURATION
-------------
    PLANNING_MONGO_URI          mongodb://claw-code-mongodb:27017
    PLANNING_MONGO_DB           database   (default: planning)
    PLANNING_MONGO_COLLECTION   collection (default: planning)

Credentials, if any, travel inside the URI — which is why nothing ever prints
it unredacted. See `redact_uri`.
"""

from __future__ import annotations

import json
import os
import sys

SPOOL_PATH = os.environ.get(
    "PLANNING_SPOOL", os.path.expanduser("~/.openclaw/planning-spool.jsonl"))

MONGO_URI = os.environ.get("PLANNING_MONGO_URI", "").strip()
DATABASE = os.environ.get("PLANNING_MONGO_DB", "planning")
COLLECTION = os.environ.get("PLANNING_MONGO_COLLECTION", "planning")

# Milliseconds, because that is the unit every pymongo timeout takes. Short on
# purpose: this runs inside a runner's exit path, and a store that is down must
# cost seconds, not minutes. The spool is what keeps the data safe meanwhile.
TIMEOUT_MS = int(os.environ.get("PLANNING_MONGO_TIMEOUT_MS", "5000"))

# A long outage must not fill the volume. At ~1 KB a document this is a few
# megabytes — far more than any plausible backlog, and still bounded. When it
# is hit the OLDEST entries go: the newest state of a story is the one worth
# keeping, and older work events are the most replaceable thing in here.
SPOOL_MAX_LINES = int(os.environ.get("PLANNING_SPOOL_MAX", "5000"))


def _log(msg: str) -> None:
    sys.stderr.write(f"[planning] {msg}\n")


def redact_uri(uri: str) -> str:
    """A connection string with any credentials removed, safe to print.

    `planning status` is run from chat, and its output is pasted into issues.
    A URI of the form mongodb://user:password@host/ would put the password in
    both. Nothing in this module prints a raw URI; everything goes through
    here.

    Deliberately a dumb string operation rather than a URL parse: the input may
    be malformed (that is often exactly why someone is running `status`), and a
    parser that raises on a broken URI would take the diagnostic down with it.
    """
    text = str(uri or "")
    if "@" not in text:
        return text
    scheme, sep, rest = text.partition("://")
    if not sep:
        # No scheme to anchor on; drop everything before the last @ anyway
        # rather than risk printing a credential.
        return "***@" + text.rsplit("@", 1)[1]
    creds, at, host = rest.partition("@")
    if not at:
        return text
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}" if user else f"{scheme}://***@{host}"


def enabled() -> bool:
    """False until a connection string is configured.

    Writes still spool while disabled — that is deliberate. The data is being
    produced now; it can be flushed once the store is provisioned, instead of
    being lost in the meantime.
    """
    return bool(MONGO_URI)


# --- the spool ---------------------------------------------------------------


def spool(doc: dict) -> bool:
    """Append one document. Returns False only if the volume refused it."""
    try:
        os.makedirs(os.path.dirname(SPOOL_PATH), exist_ok=True)
        line = json.dumps(doc, separators=(",", ":"), default=str)
        # Single append of one line: on POSIX a write this size to a file
        # opened O_APPEND does not interleave with another process's, which
        # is what lets the solver and the reviewer share one spool without
        # locking.
        with open(SPOOL_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except OSError as e:
        _log(f"could not spool a document: {e}")
        return False


def _read_spool() -> list[dict]:
    if not os.path.exists(SPOOL_PATH):
        return []
    out = []
    try:
        with open(SPOOL_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    # One corrupt line must not strand every document behind
                    # it. Drop it and say so.
                    _log("dropping an unparseable spool line")
    except OSError as e:
        _log(f"could not read the spool: {e}")
    return out


def _write_spool(docs: list[dict]) -> None:
    tmp = SPOOL_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for d in docs[-SPOOL_MAX_LINES:]:
                f.write(json.dumps(d, separators=(",", ":"), default=str) + "\n")
        os.replace(tmp, SPOOL_PATH)
    except OSError as e:
        _log(f"could not rewrite the spool: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def spool_depth() -> int:
    return len(_read_spool())


# --- the driver, imported lazily ---------------------------------------------


# One client per process, reused. A runner flushes many times, and building a
# MongoClient per flush would rebuild the connection pool and repeat server
# discovery for every 400-byte document.
_client = None

# Why the collection could not be had, recorded when it happens rather than
# guessed afterwards. `status` prints this, and the three causes — no URI, no
# driver, no server — lead to three completely different fixes.
_last_error = ""


def driver_version() -> str:
    """The installed pymongo version, or "" when the package is absent.

    Reported by `status` because "the store is unreachable" and "there is no
    driver in this image to reach it with" are the same symptom and a
    different problem, and the second one is invisible from any server-side
    log.
    """
    mongo = _pymongo()
    return getattr(mongo, "__version__", "unknown") if mongo else ""


def _pymongo():
    """The driver module, or None. Import failures are reported, never raised.

    Lazy on purpose — see the module docstring. This is also the ONLY place
    that imports it, so the fallback message is written once.
    """
    global _last_error
    try:
        import pymongo
        return pymongo
    except Exception as e:  # noqa: BLE001 — never raise out of the store
        _last_error = (f"the pymongo driver is not importable here "
                       f"({str(e)[:80]}) — documents are being spooled only")
        return None


def _collection():
    """The collection handle, or None when the store cannot be used.

    Returning None rather than raising is the whole contract of this module:
    every caller below treats None as "keep it in the spool and move on".
    """
    global _client, _last_error
    if not enabled():
        _last_error = "no connection string configured"
        return None
    if _client is None:
        mongo = _pymongo()
        if mongo is None:
            return None
        try:
            _client = mongo.MongoClient(
                MONGO_URI,
                # All three matter. Without serverSelectionTimeoutMS the
                # driver blocks for 30s on an unreachable server, which turns
                # a best-effort flush in a runner's exit path into half a
                # minute of dead time on every single run.
                serverSelectionTimeoutMS=TIMEOUT_MS,
                connectTimeoutMS=TIMEOUT_MS,
                socketTimeoutMS=TIMEOUT_MS,
                appname="claw-code-planning",
            )
        except Exception as e:  # noqa: BLE001
            _last_error = f"could not build a client: {str(e)[:160]}"
            return None
    try:
        return _client[DATABASE][COLLECTION]
    except Exception as e:  # noqa: BLE001
        _last_error = f"could not open {DATABASE}/{COLLECTION}: {str(e)[:120]}"
        return None


# Failures that are about THIS DOCUMENT and will therefore fail identically
# forever. Everything else — a server that is down, a credential that is wrong,
# a replica set electing — is transient, so the document stays spooled.
#
# Matched by exception NAME rather than by isinstance so that classifying an
# error never requires importing the driver. That keeps the rule readable in
# one place, keeps this module importable without pymongo, and lets the tests
# exercise both branches against a fake that raises look-alike exceptions.
_PERMANENT_ERRORS = {
    "InvalidDocument",      # not encodable as BSON at all
    "DocumentTooLarge",     # over the 16 MB limit; re-sending cannot help
    "InvalidBSON",
    "BSONError",
    "WriteError",           # rejected by the server for what it contains
    "InvalidOperation",
}

# Server error codes with the same property. 121 is document validation, 2 and
# 9 are a malformed command or value. Notably ABSENT: 13 (Unauthorized) and 18
# (AuthenticationFailed) — those are a deployment problem, they get fixed, and
# then the spool flushes. Dropping documents over them would throw away exactly
# the backlog the spool exists to hold.
_PERMANENT_CODES = (2, 9, 121)


def _is_permanent(exc: BaseException) -> bool:
    """Whether re-sending this document could ever work."""
    if type(exc).__name__ in _PERMANENT_ERRORS:
        return True
    return getattr(exc, "code", None) in _PERMANENT_CODES


def _mongo_doc(doc: dict) -> dict:
    """The document as MongoDB stores it: the deterministic `id` as `_id`.

    `_id` is the only field MongoDB guarantees is unique, and it is what makes
    the upsert idempotent. Writing the id into an ordinary field instead would
    let the same run land twice under two generated ObjectIds, which is not a
    duplicate anyone would notice — it is a story that appears to have cost
    twice what it did.

    `id` is kept alongside so a document read back looks the same as the one
    planning_docs produced.
    """
    out = dict(doc)
    out["_id"] = doc.get("id") or doc.get("_id")
    return out


def document_count() -> int | None:
    """How many documents the collection holds, or None if it cannot be had.

    `estimated_document_count()` and NOT `count_documents({})`: the exact count
    is a full collection scan, and this is printed by a status command that is
    run casually and often. The estimate comes from collection metadata and is
    exact except while a write is in flight.
    """
    col = _collection()
    if col is None:
        return None
    try:
        return int(col.estimated_document_count())
    except Exception as e:  # noqa: BLE001
        _log(f"could not count documents: {str(e)[:120]}")
        return None


def _upsert(doc: dict) -> tuple[bool, bool, str]:
    """(sent, permanent, detail) for one document.

    `permanent` is only meaningful when `sent` is False: True means drop it,
    False means keep it spooled and try again later.
    """
    col = _collection()
    if col is None:
        return (False, False, _last_error or "no collection")
    body = _mongo_doc(doc)
    if not body.get("_id"):
        # A document with no deterministic id cannot be upserted idempotently,
        # so letting it through would mean a duplicate on every re-flush.
        return (False, True, "the document has no id")
    try:
        col.replace_one({"_id": body["_id"]}, body, upsert=True)
        return (True, False, "")
    except Exception as e:  # noqa: BLE001
        return (False, _is_permanent(e), f"{type(e).__name__}: {str(e)[:140]}")


def query(filter: dict | None = None, *, projection: dict | None = None,
          sort=None, limit: int = 0,
          pipeline: list | None = None) -> list[dict]:
    """Read documents. Returns [] on any failure — callers report, never crash.

    Two shapes, and only two:

        query({"type": "story", "sprintId": 4})           an ordinary find
        query(pipeline=[{"$match": ...}, {"$group": ...}]) an aggregation

    `sort` takes what pymongo takes — [("number", -1)] — and `limit` caps the
    rows. Both are done SERVER-side, which is worth stating because the reports
    in `planning` mostly do not use them: they fetch and aggregate in Python
    where the result sets are small enough that a round trip costs more than
    the arithmetic.

    Scoping a query to one `pk` is what every per-story question should do; it
    is an equality match on the field the documents are grouped by, and it is
    the closest thing here to reading one aggregate at once.
    """
    if not enabled():
        return []
    col = _collection()
    if col is None:
        _log(f"query skipped: {_last_error}")
        return []
    try:
        if pipeline is not None:
            return list(col.aggregate(list(pipeline)))
        cursor = col.find(dict(filter or {}), projection)
        if sort:
            cursor = cursor.sort(sort)
        if limit:
            cursor = cursor.limit(int(limit))
        return list(cursor)
    except Exception as e:  # noqa: BLE001
        _log(f"query failed: {type(e).__name__}: {str(e)[:160]}")
        return []


# --- the public write path ---------------------------------------------------


def write(doc: dict, *, validate=True) -> bool:
    """Spool a document, then try to flush. Always returns quickly.

    Returns whether it reached the SPOOL — not whether it reached MongoDB. The
    caller cares that the data is durable; when it arrives is this module's
    problem, not the runner's.
    """
    if validate:
        try:
            import planning_docs
            problems = planning_docs.validate(doc)
        except Exception:  # noqa: BLE001
            problems = []
        if problems:
            # Refuse rather than store: a malformed document in the store is
            # queryable, looks like data, and skews every report on it.
            _log("refusing an invalid document: " + "; ".join(problems[:3]))
            return False
    if not spool(doc):
        return False
    flush()
    return True


def flush(limit: int = 200) -> int:
    """Push spooled documents to MongoDB. Returns how many landed.

    Documents that fail stay in the spool for the next attempt. A failure that
    is about the document itself is dropped, though: it will fail identically
    forever, and a permanently poisoned spool blocks everything queued behind
    it. See `_is_permanent` for where that line is drawn.
    """
    if not enabled():
        return 0
    docs = _read_spool()
    if not docs:
        return 0

    sent, keep = 0, []
    for i, doc in enumerate(docs):
        if i >= limit:
            keep.extend(docs[i:])
            break
        ok, permanent, detail = _upsert(doc)
        if ok:
            sent += 1
        elif permanent:
            _log(f"dropping a document the store rejected: {detail}")
        else:
            # Transient: keep it and everything after it, so order is
            # preserved and we stop hammering a store that is down.
            keep.extend(docs[i:])
            _log(f"flush paused at {sent} sent: {detail}")
            break
    _write_spool(keep)
    if sent:
        _log(f"flushed {sent} document(s), {len(keep)} still spooled")
    return sent


def probe() -> tuple[bool, str]:
    """Actually reach the store. Returns (ok, reason-when-not).

    `enabled()` answers "is a connection string configured", which is NOT the
    question anyone asking about the store means. Reporting configuration as
    reachability produces the worst possible output: a confident "reachable:
    yes" printed directly above a query that returned nothing — so the store
    looks healthy and merely empty, and the two facts on screen contradict
    each other with nothing connecting them.

    Two round trips, because they answer different questions. `ping` settles
    the network and the credentials; the read settles AUTHORISATION on this
    database and collection, which a successful ping says nothing about — a
    user that can connect and cannot read is a real and confusing state.
    """
    if not enabled():
        return (False, "no connection string configured")
    col = _collection()
    if col is None:
        return (False, _last_error or "the store could not be opened")
    try:
        col.database.command("ping")
    except Exception as e:  # noqa: BLE001
        return (False, _explain(e, "cannot reach the server"))
    try:
        col.find_one({}, {"_id": 1})
    except Exception as e:  # noqa: BLE001
        return (False, _explain(e, f"cannot read {DATABASE}/{COLLECTION}"))
    return (True, "")


def _explain(exc: BaseException, what: str) -> str:
    """One line a reader can act on, from a driver exception.

    The driver's own messages are long and lead with topology detail; the two
    distinctions that actually change what you do next — unreachable versus
    unauthorised — are buried in them.
    """
    name = type(exc).__name__
    if name in ("ServerSelectionTimeoutError", "NetworkTimeout",
                "ConnectionFailure", "AutoReconnect"):
        return (f"{what}: no server answered within {TIMEOUT_MS} ms "
                f"({redact_uri(MONGO_URI)})")
    if getattr(exc, "code", None) in (13, 18) or name in (
            "OperationFailure", "InvalidOperation"):
        return (f"{what}: {name} — this user is not authorised on "
                f"{DATABASE}/{COLLECTION} ({str(exc)[:100]})")
    return f"{what}: {name}: {str(exc)[:140]}"


def status() -> dict:
    """For a chat skill: is this thing working, and how far behind is it?"""
    ok, reason = probe()
    return {
        "enabled": enabled(),
        "reachable": ok,
        "reason": reason,
        # Redacted, always — this ends up in chat and in issue comments.
        "uri": redact_uri(MONGO_URI) or None,
        "database": DATABASE,
        "collection": COLLECTION,
        "spoolPath": SPOOL_PATH,
        "spoolDepth": spool_depth(),
        # "" means the package is absent, which is a different problem from an
        # unreachable server and is invisible from the server side.
        "driver": driver_version(),
    }
