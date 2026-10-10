import unittest

from app.review_parse import parse_104, parse_goodjob, parse_ptt, parse_qollie, parse_search


class ReviewParserTests(unittest.TestCase):
    def test_104_preserves_excerpt_and_cleans_contacts(self):
        result = parse_104(
            {
                "response": {
                    "total": 1,
                    "items": [
                        {
                            "id": 7,
                            "jobTitle": "工程師",
                            "content": "<p>工作內容</p><script>bad()</script> 聯絡 a@example.com",
                            "createdAt": "2026-10-04T08:00:00Z",
                            "url": "javascript:bad",
                        }
                    ],
                }
            },
            "123",
        )
        self.assertEqual(result["status"], "partial")
        item = result["evidence"][0]
        self.assertEqual(item["date"], "2026-10-04")
        self.assertIn("工作內容", item["excerpt"])
        self.assertNotIn("bad()", item["excerpt"])
        self.assertNotIn("@example.com", item["excerpt"])
        self.assertTrue(item["url"].startswith("https://"))

    def test_104_metadata_only_and_empty_vs_malformed(self):
        metadata = parse_104({"response": {"items": [{"id": "r1", "title": "職場心得"}]}}, "co")
        self.assertEqual(metadata["status"], "success")
        self.assertIsNone(metadata["evidence"][0]["excerpt"])
        self.assertTrue(any("僅取得資料入口" in item for item in metadata["limitations"]))
        self.assertEqual(parse_104({"response": {"items": []}}, "co")["status"], "empty")
        self.assertEqual(parse_104({"response": {"items": [{}]}}, "co")["status"], "failed")
        self.assertEqual(parse_104({"unexpected": []}, "co")["status"], "failed")

    def test_goodjob_both_experience_shapes_and_unknown_date(self):
        result = parse_goodjob(
            {
                "data": {
                    "company": {
                        "name": "公司",
                        "workExperiencesResult": {
                            "count": 1,
                            "workExperiences": [
                                {"id": "w1", "title": "工作心得", "experience": "<b>內容</b>", "created_at": "10/05"}
                            ],
                        },
                        "interviewExperiencesResult": {
                            "count": 1,
                            "interviewExperiences": [{"id": "i1", "job_title": "面試心得", "description": "流程"}],
                        },
                    }
                }
            },
            "公司",
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["evidence"]), 2)
        self.assertIsNone(result["evidence"][0]["date"])
        self.assertEqual(parse_goodjob({"data": {"company": {
            "workExperiencesResult": {"count": 0, "workExperiences": []},
            "interviewExperiencesResult": {"count": 0, "interviewExperiences": []},
        }}}, "公司")["status"], "empty")
        self.assertEqual(parse_goodjob({"errors": [{"message": "bad"}]}, "公司")["status"], "failed")

    def test_ratings_keep_verified_identity_and_do_not_promote_search_numbers(self):
        goodjob = parse_goodjob(
            {
                "data": {
                    "company": {
                        "id": "g1",
                        "name": "公司",
                        "companyRatingStatistics": {"averageRating": 4.25, "ratingCount": 12},
                        "workExperiencesResult": {"count": 0, "workExperiences": []},
                        "interviewExperiencesResult": {"count": 0, "interviewExperiences": []},
                    }
                }
            },
            "公司",
            company_id="g1",
            fetched_at="2026-10-09T00:00:00+00:00",
        )
        self.assertEqual(goodjob["status"], "success")
        self.assertEqual(goodjob["evidence"], [])
        self.assertEqual(goodjob["ratings"][0]["value"], 4.25)
        self.assertEqual(goodjob["ratings"][0]["sample_count"], 12)
        self.assertEqual(goodjob["ratings"][0]["source_company_id"], "g1")
        self.assertEqual(goodjob["ratings"][0]["fetched_at"], "2026-10-09T00:00:00+00:00")

        info = {"response": {"custno": "c1", "companyName": "公司", "reviewCount": 3, "scoreOverall": 3.5,
                              "interviewCount": 0, "interviewScore": 0}}
        result = parse_104({"response": {"items": []}}, "c1", info, "2026-10-09T00:00:00+00:00", "公司")
        self.assertEqual(result["ratings"][0]["source"], "104")
        self.assertEqual(result["ratings"][0]["sample_count"], 3)

    def test_qollie_counts_are_metadata_and_introduction_is_labeled(self):
        result = parse_qollie({
            "uriHash": "abc",
            "name": "公司",
            "commentsSize": 1,
            "introduction": "<p>公司介紹</p>",
            "summary": {"work_experience": {"amount": 2}, "interview_experience": {"amount": 0}},
        })
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["evidence"][0]["excerpt"], "公司介紹")
        self.assertTrue(any("公司資訊，不代表評價內容" in item for item in result["limitations"]))
        self.assertEqual(parse_qollie({
            "uriHash": "abc", "name": "公司", "commentsSize": 0,
            "summary": {"work_experience": {"amount": 0}, "interview_experience": {"amount": 0}},
        })["status"], "empty")
        self.assertEqual(parse_qollie({"name": "公司"})["status"], "failed")

    def test_ptt_only_accepts_canonical_tech_job_links(self):
        html = """
        <div class='r-list-container'>
          <div class='r-ent'><div class='title'><a href='/bbs/Tech_Job/M.1.html'>面試心得</a></div><div class='date'> 10/05</div></div>
          <div class='r-ent'><div class='title'><a href='https://evil.example/x'>不安全</a></div><div class='date'>2026/10/04</div></div>
        </div>
        """
        result = parse_ptt(html)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["evidence"][0]["url"], "https://www.ptt.cc/bbs/Tech_Job/M.1.html")
        self.assertIsNone(result["evidence"][0]["date"])
        self.assertIsNone(result["evidence"][0]["excerpt"])
        self.assertEqual(parse_ptt("<div class='r-list-container'></div>")["status"], "empty")
        self.assertEqual(parse_ptt("<html>changed layout</html>")["status"], "failed")

    def test_search_supports_brave_and_tavily_and_rejects_unsafe_rows(self):
        brave = parse_search("Brave", {"web": {"results": [
            {"title": "結果", "url": "https://example.com/a", "description": "<p>摘要</p>"},
            {"title": "unsafe", "url": "javascript:bad"},
        ]}})
        self.assertEqual(brave["status"], "partial")
        self.assertEqual(brave["evidence"][0]["excerpt"], "摘要")
        tavily = parse_search("Tavily", {"results": [
            {"title": "T", "url": "https://example.com/t", "content": "內文", "published_date": "2026-10-05"},
        ]})
        self.assertEqual(tavily["status"], "success")
        self.assertEqual(tavily["evidence"][0]["date"], "2026-10-05")
        self.assertEqual(parse_search("Tavily", {"results": []})["status"], "empty")
        self.assertEqual(parse_search("Brave", {"web": {"results": [{}]}})["status"], "failed")


if __name__ == "__main__":
    unittest.main()
