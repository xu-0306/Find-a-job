import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from app.daily import commit_results
from app.scheduler import TAIPEI
from app.store import Job, Store
from test_m2_source518 import SEARCH_HTML


STAMP = "2026-10-05T01:00:00+00:00"


class DailyCommitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def job(self, source="104", source_job_id="j1"):
        return Job(
            source=source,
            source_job_id=source_job_id,
            title=f"職缺 {source_job_id}",
            company_name="合成公司",
            url=f"https://jobs.example/{source}/{source_job_id}",
            fetched_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
        )

    def batch(self, preference_id, revision, source="104", items=None, eligible=True, diagnostic=None, area_results=None):
        return {
            "preference_id": preference_id,
            "revision": revision,
            "source": source,
            "eligible": eligible,
            "items": list(items or []),
            "diagnostic": diagnostic or {"status": "success", "truncated": False},
            "area_results": list(area_results or []),
        }

    def commit(self, batches, run_id="run-1", local_date="2026-10-05", timestamp=STAMP):
        with self.store.connect(write=True) as db:
            return commit_results(self.store, db, run_id, local_date, batches, timestamp)

    def rows(self, sql, params=()):
        with self.store.connect() as db:
            return db.execute(sql, params).fetchall()

    def test_first_baseline_marks_all_seen_but_shows_only_twenty(self):
        items = [self.job(source_job_id=f"j{i}") for i in range(25)]
        result = self.commit([self.batch("p", 1, items=items)])

        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["initial_count"], 20)
        self.assertTrue(result["source_results"][0]["baseline"])
        self.assertTrue(result["source_results"][0]["completed"])
        self.assertEqual(len(self.rows("SELECT job_id FROM seen_jobs")), 25)
        self.assertEqual(len(self.rows("SELECT job_id FROM daily_jobs")), 20)
        self.assertEqual(self.rows("SELECT count(*) FROM preference_baselines")[0][0], 1)

    def test_commit_preserves_area_results(self):
        area_results = [{"area_code": None, "area_name": "全台", "source": "104", "status": "empty",
                         "returned_count": 0, "total_declared": 0, "message": None, "completed": True}]
        result = self.commit([self.batch("p", 1, items=[], area_results=area_results)])
        self.assertEqual(result["area_results"], area_results)

    def test_empty_eligible_batch_establishes_baseline(self):
        result = self.commit([self.batch("p", 1, items=[])])

        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["initial_count"], 0)
        self.assertTrue(result["source_results"][0]["baseline"])
        self.assertEqual(self.rows("SELECT count(*) FROM preference_baselines")[0][0], 1)

    def test_ineligible_source_does_not_save_anything(self):
        result = self.commit([self.batch("p", 1, items=[], eligible=False,
                                         diagnostic={"status": "failed", "message": "blocked"})])

        self.assertEqual(result["source_results"], [{
            "status": "failed", "message": "blocked", "preference_id": "p",
            "revision": 1, "source": "104", "baseline": False, "completed": False,
        }])
        for table in ("jobs", "seen_jobs", "preference_baselines", "daily_jobs"):
            self.assertEqual(self.rows(f"SELECT count(*) FROM {table}")[0][0], 0)

    def test_global_seen_deduplicates_next_day_and_other_condition(self):
        first = self.job(source_job_id="old")
        self.commit([self.batch("p1", 1, items=[first])], local_date="2026-10-05")
        # A new condition may show an initial candidate, but it never notifies.
        self.commit([self.batch("p2", 1, items=[first])], run_id="baseline-2", local_date="2026-10-05")
        result = self.commit([self.batch("p2", 1, items=[first, self.job(source_job_id="new")])],
                             run_id="run-2", local_date="2026-10-06")

        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["initial_count"], 0)
        self.assertEqual(self.rows("SELECT count(*) FROM daily_jobs")[0][0], 2)

    def test_chat_saved_job_is_not_daily_seen(self):
        chat_job = self.job(source_job_id="chat")
        self.store.save_job(chat_job)
        self.commit([self.batch("p", 1, items=[])], run_id="baseline")

        result = self.commit([self.batch("p", 1, items=[chat_job])], run_id="daily", local_date="2026-10-06")

        self.assertEqual(result["new_count"], 1)
        self.assertEqual(self.rows("SELECT count(*) FROM seen_jobs")[0][0], 1)

    def test_same_run_and_same_day_do_not_count_twice(self):
        self.commit([self.batch("p", 1, items=[])], run_id="baseline")
        job = self.job(source_job_id="once")
        first = self.commit([self.batch("p", 1, items=[job])], run_id="same", local_date="2026-10-06")
        second = self.commit([self.batch("p", 1, items=[job])], run_id="same", local_date="2026-10-06")
        third = self.commit([self.batch("p", 1, items=[job])], run_id="other", local_date="2026-10-06")

        self.assertEqual(first["new_count"], 1)
        self.assertEqual(second["new_count"], 0)
        self.assertEqual(third["new_count"], 0)
        self.assertEqual(self.rows("SELECT count(*) FROM daily_jobs")[0][0], 1)

    def _overlap(self, order, suffix):
        self.commit([self.batch(f"regular-{suffix}", 1, items=[])], run_id=f"baseline-{suffix}")
        source_job_id = f"overlap-{suffix}"
        job = self.job(source_job_id=source_job_id)
        regular = self.batch(f"regular-{suffix}", 1, items=[job])
        baseline = self.batch(f"new-{suffix}", 1, items=[job])
        result = self.commit([regular, baseline] if order == "regular-first" else [baseline, regular],
                             run_id=f"overlap-{suffix}")
        row = self.rows(
            """SELECT is_initial, run_id FROM daily_jobs
               WHERE job_id=(SELECT id FROM jobs WHERE source_job_id=?)""",
            (source_job_id,),
        )[0]
        return result, row

    def test_baseline_wins_overlap_independent_of_batch_order(self):
        first, first_row = self._overlap("regular-first", "1")
        second, second_row = self._overlap("baseline-first", "2")

        for result, row in ((first, first_row), (second, second_row)):
            self.assertEqual(result["new_count"], 0)
            self.assertEqual(result["initial_count"], 1)
            self.assertEqual(row[0], 1)

    def test_source_mismatch_is_value_error_and_caller_can_rollback(self):
        good = self.job(source_job_id="good")
        wrong = self.job(source="518", source_job_id="wrong")
        with self.assertRaises(ValueError):
            with self.store.connect(write=True) as db:
                commit_results(self.store, db, "bad", "2026-10-05", [
                    self.batch("p", 1, items=[good]),
                    self.batch("p", 1, source="104", items=[wrong]),
                ], STAMP)
        self.assertEqual(self.rows("SELECT count(*) FROM jobs")[0][0], 0)

    def test_write_error_rolls_back_all_prior_saves_in_caller_transaction(self):
        good = self.job(source_job_id="good")
        bad = self.job(source_job_id="bad")
        original = self.store.save_job

        def fail_on_bad(job, db=None):
            if job.source_job_id == "bad":
                raise RuntimeError("synthetic write failure")
            return original(job, db)

        with patch.object(self.store, "save_job", side_effect=fail_on_bad):
            with self.assertRaises(RuntimeError):
                with self.store.connect(write=True) as db:
                    commit_results(self.store, db, "bad", "2026-10-05", [
                        self.batch("p", 1, items=[good, bad]),
                    ], STAMP)
        self.assertEqual(self.rows("SELECT count(*) FROM jobs")[0][0], 0)


class DailyScheduleSourceTests(unittest.IsolatedAsyncioTestCase):
    """Synthetic 104/518 transports; the scheduler only reads its own store and sources."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        with self.store.connect(write=True) as db:
            self.store.put(db, "default_preferences", [{"keyword": "採購", "area_codes": [], "min_monthly_salary": None}])

    def tearDown(self):
        self.directory.cleanup()

    def runner(self, services, clock, automatic=False):
        from app.scheduler import DailyRunner
        from app.sources import Sources
        with self.store.connect(write=True) as db:
            self.store.put(db, "services", services)
        self.requested = []
        self.mcp_args = []
        async def mcp(name, args):
            self.mcp_args.append(args)
            return {"jobs": [{"job_no": "12", "job_name": "採購", "company": "合成公司",
                              "url": "https://www.104.com.tw/job/a12", "appear_date": "20261005", "remote": 1}],
                    "total": 1, "has_next": False}, None
        def transport(request):
            self.requested.append(str(request.url))
            return httpx.Response(200, text=SEARCH_HTML)
        sources = Sources(httpx.MockTransport(transport), mcp, interval=0)
        runner = DailyRunner(self.store, sources, automatic=automatic)
        runner.clock = lambda: clock
        return runner

    async def run_manual(self, runner):
        ident = runner.start("manual")
        await runner.task
        with self.store.connect() as db:
            return json.loads(db.execute("SELECT result_json FROM runs WHERE id=?", (ident,)).fetchone()[0])

    async def test_disabled_518_daily_run_queries_only_104(self):
        runner = self.runner({"source_518_enabled": False}, datetime(2026, 10, 10, 10, 0, tzinfo=TAIPEI))
        result = await self.run_manual(runner)
        self.assertEqual([x["source"] for x in result["source_results"]], ["104"])
        self.assertEqual(self.requested, [])
        self.assertNotIn("remote", self.mcp_args[0], "legacy daily criteria default remote_only to false")

    async def test_remote_daily_search_skips_unsupported_sources(self):
        runner = self.runner({"source_1111_enabled": True}, datetime(2026, 10, 10, 10, 0, tzinfo=TAIPEI))
        with self.store.connect(write=True) as db:
            self.store.put(db, "default_preferences", [{"keyword": "採購", "area_codes": [], "min_monthly_salary": None, "remote_only": True}])
        result = await self.run_manual(runner)
        self.assertTrue(self.mcp_args[0]["remote"])
        self.assertEqual(self.requested, [])
        by_source = {item["source"]: item for item in result["source_results"]}
        self.assertEqual(by_source["104"]["status"], "success")
        for source in ("518", "1111"):
            self.assertEqual(by_source[source]["status"], "unsupported")
            self.assertIn("尚未支援遠端工作篩選，本次未查", by_source[source]["message"])

    async def test_remote_exclusion_still_commits_daily_baseline(self):
        runner = self.runner({"source_518_enabled": False}, datetime(2026, 10, 10, 10, 0, tzinfo=TAIPEI))
        with self.store.connect(write=True) as db:
            self.store.put(db, "default_preferences", [{"keyword": "採購", "area_codes": [],
                "min_monthly_salary": None, "remote_only": True}])
        async def provider(name, args):
            self.mcp_args.append(args)
            return {"jobs": [
                {"job_no": "remote", "job_name": "採購", "company": "合成公司",
                 "url": "https://www.104.com.tw/job/remote", "appear_date": "20261005", "remote": 1},
                {"job_no": "local", "job_name": "採購", "company": "合成公司",
                 "url": "https://www.104.com.tw/job/local", "appear_date": "20261005", "remote": 0},
            ], "total": 2, "has_next": False}, None
        runner.sources.mcp_call = provider
        result = await self.run_manual(runner)
        diagnostic = result["source_results"][0]
        self.assertTrue(self.mcp_args[0]["remote"])
        self.assertEqual(diagnostic["status"], "success")
        self.assertEqual(diagnostic["remote_excluded_count"], 1)
        self.assertTrue(diagnostic["baseline"])
        self.assertTrue(diagnostic["completed"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM preference_baselines").fetchone()[0], 1)

    async def test_enabled_daily_run_still_queries_both_sources(self):
        runner = self.runner({}, datetime(2026, 10, 10, 10, 0, tzinfo=TAIPEI))
        result = await self.run_manual(runner)
        self.assertEqual([x["source"] for x in result["source_results"]], ["104", "518"])
        self.assertTrue(any(url.startswith("https://www.518.com.tw/") for url in self.requested))
        self.assertEqual({(item["area_name"], item["source"]) for item in result["area_results"]},
                         {("全台", "104"), ("全台", "518")})

    async def test_disabled_518_does_not_repeat_baseline_every_morning(self):
        # automatic=True only enables baseline requests; the scheduler is never opened here.
        runner = self.runner({"source_518_enabled": False}, datetime(2026, 10, 10, 8, 0, tzinfo=TAIPEI), automatic=True)
        await runner.tick()
        self.assertIsNotNone(runner.task)
        await runner.task
        runner.clock = lambda: datetime(2026, 10, 11, 8, 0, tzinfo=TAIPEI)
        await runner.tick()
        self.assertIsNone(runner.task)


if __name__ == "__main__":
    unittest.main()
