import json
import unittest

from app import source518
from app.job_normalize import normalize_jobs


SEARCH_HTML = """
<html><head>
  <title>台南市找「採購」工作職缺機會</title>
  <link rel="next" href="/job-index-P-2.html?ad=%E6%8E%A1%E8%B3%BC&amp;aa=3001008">
</head><body>
  <header>頁首文字</header>
  <div id="listContent">
    <div class="job__card" data-id="A1" data-url="/job-A1.html">
      <h2><a class="job__title" href="/job-A1.html">採購<span>人員</span></a></h2>
      <a class="job__comp" href="/company-C1.html"><span class="job__comp__name">測試公司</span></a>
      <p class="job__salary">月薪 32,000 至 35,000 元</p>
      <ul class="job__summaries"><li>台南市-安定區</li><li>經歷不拘</li></ul>
      <p class="job__intro">負責<span>採購</span>作業，聯絡 hello@example.test 或 0912-345-678。</p>
      <span class="job__date">10/05</span>
    </div>
  </div>
</body></html>
"""


def _detail(payloads) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{payload}</script>' for payload in payloads
    )
    return f"<html><head>{scripts}</head><body></body></html>"


class Source518Tests(unittest.TestCase):
    def test_search_rows_normalize_and_clean_contacts(self):
        parsed = source518.parse_search_response(200, SEARCH_HTML)
        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["next_url"], "/job-index-P-2.html?ad=%E6%8E%A1%E8%B3%BC&aa=3001008")
        item = parsed["items"][0]
        self.assertEqual(item["title"], "採購人員")
        self.assertEqual(item["company_source_id"], "C1")
        self.assertEqual(item["location"], "台南市-安定區")
        self.assertIsNone(item["published_at"])
        self.assertIn("[contact redacted]", item["summary"])
        jobs, warnings = normalize_jobs("518", parsed["items"])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source_job_id, "A1")
        self.assertTrue(any("沒有年份或時區" in warning for warning in warnings))

    def test_relative_and_unsafe_job_urls_and_company_identity(self):
        good = SEARCH_HTML.replace('data-url="/job-A1.html"', 'data-url="https://518.com.tw/job-A1.html"')
        self.assertEqual(source518.parse_search_response(200, good)["status"], "success")
        unsafe = SEARCH_HTML.replace('data-url="/job-A1.html"', 'data-url="https://evil.example/job-A1.html"')
        self.assertEqual(source518.parse_search_response(200, unsafe)["status"], "failed")
        company = SEARCH_HTML.replace('href="/company-C1.html"', 'href="https://evil.example/company-C1.html"')
        self.assertIsNone(source518.parse_search_response(200, company)["items"][0]["company_source_id"])

    def test_duplicate_and_malformed_cards_are_partial(self):
        malformed = '<div class="job__card" data-id="bad"><a class="job__title" href="/job-bad.html">缺公司</a></div>'
        body = SEARCH_HTML.replace("</div>\n  </div>", malformed + "</div>\n  </div>", 1)
        body = body.replace(SEARCH_HTML.split('data-id="A1"')[0], SEARCH_HTML.split('data-id="A1"')[0], 1)
        body = body.replace('  </div>\n</body>', '  <div class="job__card" data-id="A1" data-url="/job-A1.html"><a class="job__title" href="/job-A1.html">重複</a><span class="job__comp__name">公司</span></div>\n  </div>\n</body>')
        parsed = source518.parse_search_response(200, body)
        self.assertEqual(parsed["status"], "partial")
        self.assertEqual([item["source_job_id"] for item in parsed["items"]], ["A1"])
        self.assertTrue(parsed["limitations"])

    def test_empty_changed_and_script_header_markers(self):
        empty = '<html><head><title>搜尋</title></head><body><header>查無符合條件的職缺</header><div id="listContent"><p>查無符合條件的職缺</p></div></body></html>'
        self.assertEqual(source518.parse_search_response(200, empty)["status"], "empty")
        changed = '<html><body><header>查無符合條件的職缺</header><div id="listContent"><script>查無符合條件的職缺</script><p>系統維護中</p></div></body></html>'
        self.assertEqual(source518.parse_search_response(200, changed)["status"], "failed")
        no_container = '<html><body><script>查無符合條件的職缺</script><p>頁首</p></body></html>'
        self.assertEqual(source518.parse_search_response(200, no_container)["status"], "failed")

    def test_518_zero_result_notice_is_empty_not_failed(self):
        zero = '<html><head><title>台北市找「研發替代役」工作職缺機會</title></head><body><div id="listContent"><p>抱歉沒有搜到適合的職缺</p></div></body></html>'
        parsed = source518.parse_search_response(200, zero)
        self.assertEqual(parsed["status"], "empty")
        self.assertEqual(parsed["items"], [])
        self.assertEqual(parsed["limitations"], [])

    def test_http_and_challenge_states_keep_search_shape(self):
        for status, body in ((403, ""), (429, SEARCH_HTML), (200, "<div>Checking your browser</div>")):
            parsed = source518.parse_search_response(status, body)
            self.assertEqual(parsed["status"], "blocked")
            self.assertEqual(set(parsed), {"status", "items", "next_url", "page_title", "area_label", "limitations"})
        unknown = '<div id="listContent"><p>CAPTCHA appears in a changed source notice.</p></div>'
        self.assertEqual(source518.parse_search_response(200, unknown)["status"], "failed")

    def test_challenge_words_inside_valid_job_content_do_not_block(self):
        search = SEARCH_HTML.replace("負責<span>採購</span>作業，聯絡 hello@example.test 或 0912-345-678。", "整合 CAPTCHA 與 Turnstile 服務。")
        self.assertEqual(source518.parse_search_response(200, search)["status"], "success")
        posting = {"@type": "JobPosting", "title": "職缺", "hiringOrganization": {"name": "公司"}, "description": "整合 CAPTCHA 與 Turnstile 服務。"}
        self.assertEqual(source518.parse_detail_response(200, _detail([json.dumps(posting)]), "A1")["status"], "success")

    def test_detail_graph_types_description_location_salary_and_identifier(self):
        posting = {
            "@type": ["Thing", "JobPosting"],
            "title": "採購人員",
            "hiringOrganization": {"name": "測試公司"},
            "jobLocation": {"address": {"addressRegion": "台南市", "addressLocality": "安定區", "streetAddress": "不保留"}},
            "baseSalary": {"description": "面議"},
            "description": "<p>第一段</p><p>第二段，聯絡 hello@example.test。</p>",
            "datePosted": "2026-10-05T01:00:00+08:00",
            "identifier": {"name": "測試公司", "value": "C123"},
        }
        graph = {"@graph": [{"@type": "WebPage"}, posting]}
        parsed = source518.parse_detail_response(200, _detail(["{bad json", json.dumps(graph)]), "REQ1")
        self.assertEqual(parsed["status"], "success")
        detail = parsed["detail"]
        self.assertEqual(detail["source_job_id"], "REQ1")
        self.assertIsNone(detail["company_source_id"])
        self.assertEqual(detail["location"], "台南市-安定區")
        self.assertEqual(detail["salary_text"], "面議")
        self.assertIn("第一段 第二段", detail["summary"])
        self.assertNotIn("streetAddress", str(detail))
        self.assertEqual(detail["published_at"], "2026-10-04T17:00:00+00:00")
        self.assertEqual(detail["posting_identifier"], "C123")
        self.assertTrue(any("尚未核實" in warning for warning in detail["filter_warnings"]))

    def test_detail_dates_and_missing_fields(self):
        base = {"@type": "JobPosting", "title": "職缺", "hiringOrganization": {"name": "公司"}}
        for value, warning in (("2026-10-05", "只提供日期"), ("2026-10-05T01:00:00", "缺少時區"), ("bad", "格式無法辨識")):
            posting = {**base, "datePosted": value}
            detail = source518.parse_detail_response(200, _detail([json.dumps(posting)]), "A1")["detail"]
            self.assertIsNone(detail["published_at"])
            self.assertTrue(any(warning in item for item in detail["filter_warnings"]))
        missing = {"@type": "JobPosting", "title": "職缺"}
        self.assertEqual(source518.parse_detail_response(200, _detail([json.dumps(missing)]), "A1")["status"], "failed")

    def test_detail_alternate_mime_and_mismatch_identifier_do_not_merge(self):
        payload = {"@type": "JobPosting", "title": "另一職缺", "hiringOrganization": {"name": "另一公司"}, "identifier": {"value": "OTHER"}}
        html = _detail([json.dumps(payload)]).replace("application/ld+json", "application/jsonld; charset=utf-8")
        parsed = source518.parse_detail_response(200, html, "REQUESTED")
        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["detail"]["source_job_id"], "REQUESTED")
        self.assertEqual(parsed["detail"]["posting_identifier"], "OTHER")

    def test_detail_identity_can_use_jsonld_url_when_identifier_is_company_id(self):
        payload = {
            "@type": "JobPosting", "url": "https://www.518.com.tw/job-A1.html",
            "title": "職缺", "hiringOrganization": {"name": "公司"},
        }
        parsed = source518.parse_detail_response(
            200, _detail([json.dumps(payload)]), "A1",
            expected_url="https://www.518.com.tw/job-A1.html",
        )
        self.assertTrue(parsed["identity_verified"])
        self.assertEqual(parsed["detail"]["identity_evidence"]["url"], "https://www.518.com.tw/job-A1.html")

    def test_detail_identity_does_not_trust_ambiguous_identifier(self):
        payload = {
            "@type": "JobPosting", "identifier": {"value": "A1"},
            "title": "職缺", "hiringOrganization": {"name": "公司"},
        }
        parsed = source518.parse_detail_response(
            200, _detail([json.dumps(payload)]), "A1",
            expected_url="https://www.518.com.tw/job-A1.html",
        )
        self.assertFalse(parsed["identity_verified"])

    def test_detail_challenge_and_non_success(self):
        self.assertEqual(source518.parse_detail_response(403, "", "A1")["status"], "blocked")
        self.assertEqual(source518.parse_detail_response(429, "", "A1")["status"], "blocked")
        self.assertEqual(source518.parse_detail_response(500, "", "A1")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
