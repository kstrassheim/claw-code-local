"""The planning store, which is allowed to be late and never allowed to fail.

Every test here defends one of two properties, and they are the reason the
module is shaped the way it is:

  1. NOTHING RAISES. This code runs in a runner's exit path. A store that can
     throw is a store that can fail a run which has already merged its work,
     and no planning data is worth that.

  2. THE SPOOL IS THE DURABLE PART. A write is finished once it is on the
     volume; reaching MongoDB is a separate, retryable, idempotent step. So an
     unreachable store must cost a delay and never a document.

The fake collection below is deliberately tiny — the four calls the store
actually makes. It is also what keeps this file runnable with no pymongo
installed, which matters because the suite must run anywhere the repository
is checked out, not only inside the built image.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

from harness import TMP_ROOT, load, temp_env

URI = "mongodb://claw-code-mongodb:27017"


# --- exceptions shaped like the driver's -------------------------------------
#
# planning_store classifies failures by exception NAME and `.code`, never by
# isinstance, precisely so it never has to import pymongo to decide. That is
# what lets these stand in for the real thing.

class ServerSelectionTimeoutError(Exception):
    pass


class DocumentTooLarge(Exception):
    pass


class OperationFailure(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, spec):
        for field, direction in reversed(list(spec)):
            self.rows.sort(key=lambda d: d.get(field) or 0,
                           reverse=direction < 0)
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    def __iter__(self):
        return iter(self.rows)


class FakeDatabase:
    def __init__(self, collection):
        self.collection = collection

    def command(self, name):
        if self.collection.ping_error:
            raise self.collection.ping_error
        return {"ok": 1}


class FakeCollection:
    """Only what planning_store calls: replace_one, find, aggregate, count."""

    def __init__(self, write_error=None, read_error=None, ping_error=None):
        self.docs = {}
        self.write_error = write_error
        self.read_error = read_error
        self.ping_error = ping_error
        self.attempts = []
        self.pipelines = []

    @property
    def database(self):
        return FakeDatabase(self)

    def replace_one(self, filt, doc, upsert=False):
        self.attempts.append(filt["_id"])
        if self.write_error:
            raise self.write_error
        assert upsert, "every write must be an upsert or re-flushing duplicates"
        # Keyed on _id exactly as MongoDB would: this is what makes a second
        # flush of the same document a no-op rather than a second row.
        self.docs[filt["_id"]] = dict(doc)
        return object()

    def _matches(self, doc, filt):
        for key, want in (filt or {}).items():
            got = doc.get(key)
            if isinstance(want, dict):
                if "$ne" in want and got == want["$ne"]:
                    return False
                if "$regex" in want:
                    import re
                    if not re.search(want["$regex"], str(got or "")):
                        return False
            elif got != want:
                return False
        return True

    def find(self, filt=None, projection=None):
        if self.read_error:
            raise self.read_error
        return FakeCursor([d for d in self.docs.values()
                           if self._matches(d, filt)])

    def find_one(self, filt=None, projection=None):
        if self.read_error:
            raise self.read_error
        return next(iter(self.find(filt)), None)

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return iter([{"_id": "solver", "calls": 7}])

    def estimated_document_count(self):
        if self.read_error:
            raise self.read_error
        return len(self.docs)


class StoreTestCase(unittest.TestCase):
    """Each test gets its own spool file and its own freshly imported module.

    Fresh matters: the spool path, the connection string and the spool bound
    are all read at import, so a test that changes the environment needs the
    module re-read rather than the copy an earlier test imported.
    """

    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="planning-", dir=TMP_ROOT)
        self.spool_file = os.path.join(self.dir, "planning-spool.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def store(self, collection=None, **env):
        env.setdefault("PLANNING_SPOOL", self.spool_file)
        ctx = temp_env(**env)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        store = load("planning_store")
        if collection is not None:
            # The seam the whole suite hangs on: everything above _collection
            # is pure logic, everything below it is the driver.
            store._collection = lambda: collection
        return store

    def spool_lines(self) -> list[dict]:
        """The spool file as documents, in the order it holds them."""
        with open(self.spool_file, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def work(self, run_id="r1", role="solver", calls=1):
        docs = load("planning_docs")
        return docs.work_doc(host="github", repo="acme/web", number=42,
                             run_id=run_id, role=role, llm_calls=calls,
                             now="2026-08-17T10:00:00+00:00")


class TheSpoolIsTheDurablePart(StoreTestCase):
    def test_enabled_is_false_when_nothing_is_configured(self):
        # And it must stay false rather than defaulting to localhost: a
        # default connection string would make every flush attempt a
        # connection to nothing, on every runner, forever.
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertFalse(store.enabled())

    def test_enabled_is_true_once_a_connection_string_exists(self):
        store = self.store(PLANNING_MONGO_URI=URI)
        self.assertTrue(store.enabled())

    def test_a_write_with_no_store_configured_still_spools(self):
        # The data is being produced NOW. Refusing to keep it until the store
        # is provisioned would lose exactly the history the estimator needs.
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertTrue(store.write(self.work()))
        self.assertEqual(store.spool_depth(), 1)
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 1)

    def test_an_unreachable_store_never_raises_and_keeps_the_document(self):
        fake = FakeCollection(
            write_error=ServerSelectionTimeoutError("no servers"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        self.assertTrue(store.write(self.work()))
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 1,
                         "a transient failure must not lose the document")

    def test_a_missing_driver_degrades_to_spooling(self):
        # Simulates an image built without the pip layer. `import pymongo`
        # raises ImportError when the entry is None, which is exactly what the
        # lazy import is there to absorb.
        self.addCleanup(sys.modules.pop, "pymongo", None)
        sys.modules["pymongo"] = None
        store = self.store(PLANNING_MONGO_URI=URI)
        self.assertTrue(store.write(self.work()))
        self.assertEqual(store.spool_depth(), 1)
        self.assertEqual(store.driver_version(), "")
        ok, reason = store.probe()
        self.assertFalse(ok)
        self.assertIn("pymongo", reason)

    def test_a_corrupt_spool_line_does_not_strand_the_ones_behind_it(self):
        store = self.store(PLANNING_MONGO_URI=None)
        store.spool(self.work(run_id="a"))
        with open(self.spool_file, "a", encoding="utf-8") as f:
            f.write("{ this was truncated by a kill -9\n")
        store.spool(self.work(run_id="b"))
        self.assertEqual(store.spool_depth(), 2)

    def test_an_invalid_document_is_refused_before_it_is_spooled(self):
        # A malformed document in the store is worse than one rejected here:
        # it is queryable, it looks like data, and it skews every report.
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertFalse(store.write({"type": "nonsense"}))
        self.assertEqual(store.spool_depth(), 0)

    def test_validation_can_be_waived_for_a_document_read_back(self):
        # Closing a sprint writes back the document the store returned, which
        # carries fields validate() knows nothing about.
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertTrue(store.write({"id": "x", "pk": "sprint#1",
                                     "type": "sprint", "_id": "x"},
                                    validate=False))
        self.assertEqual(store.spool_depth(), 1)


class TheSpoolIsBounded(StoreTestCase):
    def test_the_oldest_entries_go_first(self):
        # A long outage must not fill the volume. When the bound is hit the
        # OLDEST go: the newest state of a story is the one worth keeping, and
        # an old work event is the most replaceable thing in here.
        fake = FakeCollection(
            write_error=ServerSelectionTimeoutError("still down"))
        store = self.store(fake, PLANNING_MONGO_URI=URI, PLANNING_SPOOL_MAX=5)
        for i in range(10):
            store.spool(self.work(run_id=f"run-{i}"))
        self.assertEqual(store.spool_depth(), 10)

        # The bound is applied when the spool is rewritten, which is what a
        # flush against a dead store does.
        store.flush()
        kept = self.spool_lines()
        self.assertEqual(len(kept), 5)
        self.assertEqual([d["runId"] for d in kept],
                         [f"run-{i}" for i in range(5, 10)])


class FlushingTwiceIsANoOp(StoreTestCase):
    def test_the_deterministic_id_becomes_the_primary_key(self):
        store = self.store(PLANNING_MONGO_URI=URI)
        doc = self.work()
        self.assertEqual(store._mongo_doc(doc)["_id"], doc["id"])

    def test_re_flushing_the_same_documents_stores_one_copy_each(self):
        # The property the whole spool design rests on. Without it the spool
        # would need a two-phase commit, and a crash mid-flush would either
        # lose a document or double a run's recorded cost.
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        for run in ("r1", "r2"):
            store.spool(self.work(run_id=run))
        self.assertEqual(store.flush(), 2)
        self.assertEqual(store.spool_depth(), 0)

        for run in ("r1", "r2"):
            store.spool(self.work(run_id=run, calls=99))
        self.assertEqual(store.flush(), 2)
        self.assertEqual(len(fake.docs), 2, "a re-flush must replace, not add")
        self.assertEqual(fake.attempts.count("work#r1#solver"), 2)
        self.assertEqual(fake.docs["work#r1#solver"]["llmCalls"], 99,
                         "the later write is the one that survives")

    def test_a_partial_record_is_replaced_by_the_final_one(self):
        # planning-record --follow writes the running total every couple of
        # minutes. Each write must overwrite the last, or a long solve would
        # appear in the reports as fifty separate runs.
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        for calls in (10, 50, 137):
            store.write(self.work(run_id="same-run", calls=calls))
        self.assertEqual(len(fake.docs), 1)
        self.assertEqual(fake.docs["work#same-run#solver"]["llmCalls"], 137)

    def test_a_document_with_no_id_is_dropped_rather_than_duplicated(self):
        store = self.store(FakeCollection(), PLANNING_MONGO_URI=URI)
        sent, permanent, detail = store._upsert({"pk": "story#github#a/b#1"})
        self.assertFalse(sent)
        self.assertTrue(permanent, "it would duplicate on every re-flush")
        self.assertIn("id", detail)


class WhatIsWorthRetrying(StoreTestCase):
    def test_a_document_the_server_rejects_is_dropped(self):
        # It will fail identically forever, and a poisoned spool blocks
        # everything queued behind it.
        fake = FakeCollection(write_error=DocumentTooLarge("17 MB"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        store.spool(self.work())
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 0)

    def test_document_validation_is_permanent(self):
        fake = FakeCollection(
            write_error=OperationFailure("failed validation", code=121))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        store.spool(self.work())
        store.flush()
        self.assertEqual(store.spool_depth(), 0)

    def test_an_authorisation_failure_keeps_the_backlog(self):
        # Credentials get fixed; the spool is exactly the backlog that should
        # then flush. Dropping documents over a 13 would throw away the data
        # the spool exists to hold.
        fake = FakeCollection(
            write_error=OperationFailure("not authorized", code=13))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        store.spool(self.work())
        self.assertEqual(store.flush(), 0)
        self.assertEqual(store.spool_depth(), 1)

    def test_a_paused_flush_preserves_order(self):
        fake = FakeCollection(write_error=ServerSelectionTimeoutError("down"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        for i in range(3):
            store.spool(self.work(run_id=f"r{i}"))
        store.flush()
        kept = self.spool_lines()
        self.assertEqual([d["runId"] for d in kept], ["r0", "r1", "r2"])

    def test_the_limit_leaves_the_rest_spooled(self):
        # A flush runs inside a runner's exit path; it must be bounded work.
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        for i in range(5):
            store.spool(self.work(run_id=f"r{i}"))
        self.assertEqual(store.flush(limit=2), 2)
        self.assertEqual(store.spool_depth(), 3)


class ReadingBack(StoreTestCase):
    def test_a_query_without_a_store_is_empty_not_an_error(self):
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertEqual(store.query({"type": "sprint"}), [])

    def test_a_find_is_sorted_and_limited_by_the_server(self):
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        for n in (1, 3, 2):
            fake.docs[f"sprint#{n}"] = {"_id": f"sprint#{n}", "type": "sprint",
                                        "number": n}
        rows = store.query({"type": "sprint"}, sort=[("number", -1)], limit=2)
        self.assertEqual([r["number"] for r in rows], [3, 2])

    def test_a_pipeline_goes_to_aggregate(self):
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        rows = store.query(pipeline=[{"$match": {"type": "work"}}])
        self.assertEqual(rows, [{"_id": "solver", "calls": 7}])
        self.assertEqual(fake.pipelines, [[{"$match": {"type": "work"}}]])

    def test_a_failing_query_is_an_empty_list(self):
        # Reports print what they get. A raise here would turn "the store is
        # down" into a traceback in a chat window.
        fake = FakeCollection(read_error=ServerSelectionTimeoutError("down"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        self.assertEqual(store.query({"type": "story"}), [])

    def test_document_count_is_none_when_it_cannot_be_had(self):
        # None and 0 are different answers, and printing 0 for "could not ask"
        # describes an empty store that is in fact merely unreachable.
        fake = FakeCollection(read_error=ServerSelectionTimeoutError("down"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        self.assertIsNone(store.document_count())

    def test_document_count_reports_what_is_there(self):
        fake = FakeCollection()
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        store.write(self.work())
        self.assertEqual(store.document_count(), 1)


class TellingTheTruthAboutTheStore(StoreTestCase):
    def test_a_configured_store_is_not_a_working_one(self):
        # The worst possible output is "reachable: yes" printed above a query
        # that returned nothing: the store looks healthy and merely empty.
        fake = FakeCollection(ping_error=ServerSelectionTimeoutError("down"))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        self.assertTrue(store.enabled())
        ok, reason = store.probe()
        self.assertFalse(ok)
        self.assertIn("no server answered", reason)

    def test_connecting_and_reading_are_separate_questions(self):
        # A user that can connect and cannot read this collection is a real
        # state, and a successful ping says nothing about it.
        fake = FakeCollection(read_error=OperationFailure("not authorized",
                                                         code=13))
        store = self.store(fake, PLANNING_MONGO_URI=URI)
        ok, reason = store.probe()
        self.assertFalse(ok)
        self.assertIn("not authorised", reason)

    def test_a_healthy_store_probes_clean(self):
        store = self.store(FakeCollection(), PLANNING_MONGO_URI=URI)
        self.assertEqual(store.probe(), (True, ""))

    def test_probe_without_a_connection_string_says_so(self):
        store = self.store(PLANNING_MONGO_URI=None)
        ok, reason = store.probe()
        self.assertFalse(ok)
        self.assertIn("no connection string", reason)

    def test_status_carries_the_spool_depth_and_the_collection(self):
        store = self.store(FakeCollection(), PLANNING_MONGO_URI=URI,
                           PLANNING_MONGO_DB="planning",
                           PLANNING_MONGO_COLLECTION="planning")
        store.spool(self.work())
        st = store.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["spoolDepth"], 1)
        self.assertEqual(st["database"], "planning")
        self.assertEqual(st["collection"], "planning")
        self.assertEqual(st["spoolPath"], self.spool_file)


class CredentialsNeverReachTheOutput(StoreTestCase):
    def test_a_password_is_redacted(self):
        store = self.store(PLANNING_MONGO_URI=None)
        out = store.redact_uri("mongodb://claw:sup3rs3cret@mongo:27017/planning")
        self.assertNotIn("sup3rs3cret", out)
        self.assertIn("claw", out, "the user is worth seeing; the secret is not")

    def test_a_uri_without_credentials_is_unchanged(self):
        store = self.store(PLANNING_MONGO_URI=None)
        self.assertEqual(store.redact_uri(URI), URI)

    def test_garbage_is_returned_rather_than_raising(self):
        # `status` is most often run BECAUSE the connection string is wrong. A
        # parser that raised on a malformed URI would take the diagnostic down
        # with it.
        store = self.store(PLANNING_MONGO_URI=None)
        for junk in ("", "not a uri", "user:pw@host", "mongodb://@/"):
            with self.subTest(junk=junk):
                self.assertIsInstance(store.redact_uri(junk), str)

    def test_status_reports_only_the_redacted_form(self):
        secret = "mongodb://claw:sup3rs3cret@mongo:27017"
        store = self.store(FakeCollection(), PLANNING_MONGO_URI=secret)
        self.assertNotIn("sup3rs3cret", json.dumps(store.status()))


if __name__ == "__main__":
    unittest.main()
