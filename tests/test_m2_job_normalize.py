from datetime import timezone
import unittest

from app.job_normalize import normalize_job, normalize_jobs


def row_104(**overrides):
    row = {
        "job_no": "104-1",
        "job_name": "採購專員",
        "company": "範例公司",
        "url": "https://example.test/jobs/104-1",
    }
    row.update(overrides)
    return row


def row_518(**overrides):
    row = {
        "source_job_id": "518-1",
        "title": "採購人員",
        "company_name": "另一家公司",
        "url": "https://example.test/job-518-1.html",
    }
    row.update(overrides)
    return row


class JobNormalizeTests(unittest.TestCase):
    def test_104_aliases_clean_html_highlights_and_dates(self):
        job = normalize_job(
            "104",
            {
                "jobNo": "104-2",
                "jobName": "<b>採購[[[專員]]]</b>",
                "custName": "<i>範例公司</i>",
                "custNo": "C-2",
                "location": "<span>台南市</span>",
                "salary": "月薪 40,000",
                "url": "https://example.test/jobs/104-2",
                "appearDate": "2026-10-05",
                "description": "<p>工作內容</p><script>不要保留</script><style>不要保留</style>",
                "fetched_at": "2026-10-05T10:00:00+08:00",
            },
        )
        self.assertEqual(job.source_job_id, "104-2")
        self.assertEqual(job.title, "採購專員")
        self.assertEqual(job.company_name, "範例公司")
        self.assertEqual(job.company_source_id, "C-2")
        self.assertEqual(job.summary, "工作內容")
        self.assertIsNone(job.published_at)
        self.assertEqual(job.fetched_at.tzinfo, timezone.utc)
        self.assertTrue(any("只提供日期" in warning for warning in job.filter_warnings))
        self.assertTrue(any("無法判定是否為廣告" in warning for warning in job.filter_warnings))

    def test_ad_flag_is_strict_and_existing_warnings_are_preserved(self):
        self.assertIsNone(normalize_job("104", row_104(is_ad=True)))
        job = normalize_job("104", row_104(is_ad=False, filter_warnings=["company filter is approximate"]))
        self.assertEqual(job.filter_warnings, ["company filter is approximate"])
        self.assertFalse(any("無法判定是否為廣告" in warning for warning in job.filter_warnings))
        missing = normalize_job("104", row_104(filter_warnings=["address unknown"]))
        self.assertEqual(missing.filter_warnings, ["address unknown", "104 未提供明確的廣告標記，無法判定是否為廣告。"])
        unknown = normalize_job("104", row_104(is_ad=1))
        self.assertTrue(any("無法判定是否為廣告" in warning for warning in unknown.filter_warnings))

    def test_518_common_row_accepts_aware_published_date_and_nullable_company_id(self):
        job = normalize_job(
            "518",
            row_518(
                company_source_id=None,
                published_at="2026-10-05T01:30:00+08:00",
                summary="<strong>採購</strong> [[[職缺]]]",
            ),
        )
        self.assertIsNone(job.company_source_id)
        self.assertEqual(job.published_at.isoformat(), "2026-10-04T17:30:00+00:00")
        self.assertEqual(job.summary, "採購 職缺")
        self.assertEqual(job.filter_warnings, [])

    def test_html_paragraphs_keep_a_text_boundary_and_skip_style_text(self):
        job = normalize_job("518", row_518(summary="<p>第一段</p><p>第二段</p><style>隱藏</style>"))
        self.assertEqual(job.summary, "第一段 第二段")
        self.assertNotIn("隱藏", job.summary)

    def test_naive_published_date_is_ignored_but_malformed_date_fails(self):
        job = normalize_job("518", row_518(published_at="2026-10-05T01:30:00"))
        self.assertIsNone(job.published_at)
        self.assertTrue(any("缺少時區" in warning for warning in job.filter_warnings))
        job = normalize_job("518", row_518(published_at="2026-10-05"))
        self.assertIsNone(job.published_at)
        self.assertTrue(any("只提供日期" in warning for warning in job.filter_warnings))
        with self.assertRaises(ValueError):
            normalize_job("518", row_518(published_at="not-a-date"))
        with self.assertRaises(ValueError):
            normalize_job("518", row_518(fetched_at="2026-10-05T01:30:00"))

    def test_required_fields_and_url_are_rejected_without_raw_content_in_error(self):
        with self.assertRaises(ValueError) as missing:
            normalize_job("104", row_104(company=""))
        self.assertNotIn("104-1", str(missing.exception))
        with self.assertRaises(ValueError):
            normalize_job("104", row_104(url="javascript:alert(1)"))
        with self.assertRaises(ValueError):
            normalize_job("104", row_104(url="/relative/job"))

    def test_normalize_jobs_deduplicates_collects_distinct_warnings_and_rejects_bad_rows(self):
        jobs, warnings = normalize_jobs(
            "104",
            [
                row_104(filter_warnings=["limited area filter"]),
                row_104(filter_warnings=["limited area filter", "truncated page"]),
                row_104(job_no="104-ad", is_ad=True),
            ],
        )
        self.assertEqual([job.source_job_id for job in jobs], ["104-1"])
        self.assertEqual(warnings.count("limited area filter"), 1)
        self.assertEqual(warnings.count("truncated page"), 1)
        self.assertEqual(normalize_jobs("518", []), ([], []))
        with self.assertRaises(ValueError):
            normalize_jobs("104", [row_104(), {"job_no": "broken"}])


if __name__ == "__main__":
    unittest.main()
