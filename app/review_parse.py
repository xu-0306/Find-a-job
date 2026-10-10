"""Pure parsers for company-review and web-search evidence.

The transport, matching, caching, and persistence layers deliberately stay out
of this module.  Each parser turns one already-received source payload into a
small, common evidence shape.
"""

from __future__ import annotations

from datetime import date, datetime
from html.parser import HTMLParser
import math
import re
import unicodedata
from urllib.parse import quote, urlsplit

from .job_normalize import _clean_text


_MAX_ITEMS = 5
_MAX_TITLE = 500
_MAX_EXCERPT = 2000
_HTTP_SCHEMES = {"http", "https"}


def _result(
    status: str,
    evidence: list[dict],
    limitations: list[str] | None = None,
    ratings: list[dict] | None = None,
) -> dict:
    return {
        "status": status,
        "evidence": evidence,
        "limitations": list(dict.fromkeys(limitations or [])),
        "ratings": list(ratings or []),
    }


def _has_value(value: object) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _clean(value: object, limit: int) -> str | None:
    text = _clean_text(value)
    return text[:limit] if text else None


def _identity(value: object) -> str | None:
    if not _has_value(value) or isinstance(value, bool):
        return None
    try:
        return _clean(value, _MAX_TITLE)
    except (TypeError, ValueError):
        return None


def _raw(row: dict, *names: str) -> object | None:
    for name in names:
        value = row.get(name)
        if _has_value(value):
            return value
    return None


def _excerpt(row: dict, names: tuple[str, ...]) -> tuple[str | None, bool]:
    parts: list[str] = []
    invalid = False
    for name in names:
        value = row.get(name)
        if not _has_value(value):
            continue
        try:
            text = _clean(value, _MAX_EXCERPT)
        except (TypeError, ValueError):
            invalid = True
            continue
        if text:
            parts.append(text)
    return (" ".join(parts)[:_MAX_EXCERPT] or None), invalid


def _date(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if any(character.isspace() or ord(character) < 32 for character in raw):
        return None
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in _HTTP_SCHEMES or not host:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return raw


def _url_with_fallback(value: object, fallback: str) -> tuple[str, bool]:
    if value is None:
        return fallback, False
    safe = _safe_url(value)
    return (safe or fallback), safe is None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _name_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", value).casefold())


def _limitation(limitations: list[str], message: str) -> None:
    if message not in limitations:
        limitations.append(message)


def _add_metadata_note(result: dict) -> dict:
    evidence = result.get("evidence", [])
    missing_excerpt = [item for item in evidence if item.get("excerpt") is None]
    if missing_excerpt:
        message = (
            "目前僅取得資料入口，尚未取得內文。"
            if len(missing_excerpt) == len(evidence)
            else "部分資料僅取得入口，尚未取得內文。"
        )
        _limitation(result["limitations"], message)
    return result


def _finish_rows(
    evidence: list[dict],
    limitations: list[str],
    *,
    malformed: int,
    recognized_empty: bool,
    declared_total: int | None = None,
    observed_total: int | None = None,
) -> dict:
    if malformed:
        _limitation(limitations, "部分來源資料格式無法辨識，已略過。")
    if declared_total is not None and observed_total is not None and declared_total > observed_total:
        _limitation(limitations, "來源宣告的資料筆數多於本次回應。")
    if len(evidence) > _MAX_ITEMS:
        del evidence[_MAX_ITEMS:]
        _limitation(limitations, "只保留前五筆來源資料。")
    if evidence:
        return _add_metadata_note(_result("partial" if limitations else "success", evidence, limitations))
    if recognized_empty and not malformed and not limitations:
        return _result("empty", [], [])
    return _result("failed", [], limitations or ["沒有找到可用的來源資料。"])


def _fallback_104(company_id: str) -> str:
    return "https://reviews.104.com.tw/company/" + quote(company_id, safe="")


def _parse_104_ratings(
    payload: object,
    company_id: str,
    *,
    company_name: str | None = None,
    fetched_at: str | None = None,
) -> tuple[list[dict], list[str]]:
    """Parse only the fields observed on 104's public company-info endpoint."""

    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        return [], []
    info = payload["response"]
    if str(info.get("custno")) != str(company_id):
        return [], ["104 公司資訊識別不一致，未顯示評分。"]
    if company_name and _name_key(info.get("companyName")) != _name_key(company_name):
        return [], ["104 公司資訊名稱不一致，未顯示評分。"]

    ratings: list[dict] = []
    limitations: list[str] = []
    source_url = "https://reviews.104.com.tw/company/" + quote(str(company_id), safe="")
    for metric, value_key, count_key in (
        ("overall", "scoreOverall", "reviewCount"),
        ("interview", "interviewScore", "interviewCount"),
    ):
        count = _count(info.get(count_key))
        value = _number(info.get(value_key))
        if count is None or (value is not None and not 0 <= value <= 5):
            if info.get(count_key) is not None or info.get(value_key) is not None:
                limitations.append(f"104 {metric} 評分欄位無法核對，未顯示評分。")
            continue
        # A zero-count score is the endpoint's no-data sentinel, not a rating.
        if count == 0 or value is None:
            continue
        ratings.append(
            {
                "source": "104",
                "metric": metric,
                "value": value,
                "scale_min": 0,
                "scale_max": 5,
                "sample_count": count,
                "url": source_url,
                "source_company_id": str(company_id),
                "fetched_at": fetched_at,
            }
        )
    return ratings, limitations


def parse_104(
    payload: object,
    company_id: object,
    info_payload: object | None = None,
    fetched_at: str | None = None,
    company_name: str | None = None,
) -> dict:
    """Parse 104 reviews and, when supplied, its public company-info ratings."""

    try:
        identifier = _identity(company_id)
    except (TypeError, ValueError):
        identifier = None
    if not identifier:
        return _result("failed", [], ["104 公司識別資料缺少或無效。"])

    ratings, rating_limits = _parse_104_ratings(
        info_payload,
        identifier,
        company_name=company_name,
        fetched_at=fetched_at,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), (dict, list)):
        return _result("failed", [], ["104 回應格式無法辨識。"] + rating_limits, ratings)

    root = payload["response"]
    if isinstance(root, list):
        rows = root
        declared_total = len(rows)
    else:
        if not isinstance(root.get("items"), list):
            return _result("failed", [], ["104 回應沒有提供評價資料。"] + rating_limits, ratings)
        rows = root["items"]
        declared_total = None
        for key in ("total", "totalCount", "count"):
            if key in root and root[key] is not None:
                declared_total = _count(root[key])
                if declared_total is None:
                    return _result("failed", [], ["104 評價筆數格式無法辨識。"] + rating_limits, ratings)
                break

    if not rows and (declared_total is None or declared_total == 0):
        return _result("success" if ratings else "empty", [], rating_limits, ratings)
    limitations: list[str] = list(rating_limits)
    evidence: list[dict] = []
    malformed = 0
    for row in rows:
        if not isinstance(row, dict):
            malformed += 1
            continue
        review_id = _raw(row, "id", "_id", "reviewId", "review_id", "uuid")
        title_raw = _raw(row, "title", "jobTitle")
        source_url_raw = _raw(row, "url", "link", "reviewUrl")
        title = None
        title_invalid = False
        if title_raw is not None:
            try:
                title = _clean(title_raw, _MAX_TITLE)
            except (TypeError, ValueError):
                title_invalid = True
        valid_identity = any(
            value is not None
            for value in (
                _identity(review_id) if review_id is not None else None,
                title,
                _safe_url(source_url_raw) if source_url_raw is not None else None,
            )
        )
        if not valid_identity:
            malformed += 1
            continue
        if title_invalid:
            _limitation(limitations, "部分 104 評價標題無效，已略過。")
        url, unsafe_url = _url_with_fallback(source_url_raw, _fallback_104(identifier))
        if unsafe_url:
            _limitation(limitations, "104 來源連結不安全，已改用公司公開頁面。")
        excerpt, excerpt_invalid = _excerpt(
            row, ("content", "comment", "advantages", "disadvantages")
        )
        if excerpt_invalid:
            _limitation(limitations, "部分 104 評價內文無法使用，已略過。")
        evidence.append(
            {
                "source": "104",
                "title": title or "104 公司評價",
                "url": url,
                "date": _date(_raw(row, "createdAt", "created_at", "date", "reviewDate", "publishedAt")),
                "excerpt": excerpt,
            }
        )

    if not evidence and rows and declared_total:
        return _result("failed", [], limitations or ["104 回應沒有可用的評價資料。"], ratings)
    result = _finish_rows(
        evidence,
        limitations,
        malformed=malformed,
        recognized_empty=not rows,
        declared_total=declared_total,
        observed_total=len(rows),
    )
    result["ratings"] = ratings
    return result


def _fallback_goodjob(company_name: str) -> str:
    return "https://www.goodjob.life/companies/" + quote(company_name, safe="")


def parse_goodjob(
    payload: object,
    company_name: object,
    company_id: object | None = None,
    fetched_at: str | None = None,
) -> dict:
    """Parse GoodJob experiences and its verified company aggregate rating."""

    try:
        name = _identity(company_name)
    except (TypeError, ValueError):
        name = None
    if not name:
        return _result("failed", [], ["GoodJob 公司名稱缺少或無效。"])
    if not isinstance(payload, dict):
        return _result("failed", [], ["GoodJob 回應格式無法辨識。"])
    if payload.get("errors"):
        return _result("failed", [], ["GoodJob 回應包含錯誤，無法取得資料。"])
    data = payload.get("data")
    company = data.get("company") if isinstance(data, dict) else None
    if not isinstance(company, dict):
        return _result("failed", [], ["GoodJob 回應沒有提供公司資料。"])
    company_name_raw = _raw(company, "name")
    if company_name_raw is not None:
        try:
            resolved_name = _clean(company_name_raw, _MAX_TITLE) or name
        except (TypeError, ValueError):
            resolved_name = name
        if _name_key(resolved_name) != _name_key(name):
            return _result("failed", [], ["GoodJob 公司名稱不一致，未合併評價。"])
        name = resolved_name

    statistics = company.get("companyRatingStatistics")
    ratings: list[dict] = []
    rating_limits: list[str] = []
    if statistics is not None:
        if not isinstance(statistics, dict) or "ratingCount" not in statistics:
            return _result("failed", [], ["GoodJob 評分資料格式無法辨識。"])
        rating_count = _count(statistics.get("ratingCount"))
        if rating_count is None:
            return _result("failed", [], ["GoodJob 評分樣本數格式無法辨識。"])
        rating_value = _number(statistics.get("averageRating"))
        source_id = _identity(company.get("id"))
        if company_id is not None and str(source_id) != str(company_id):
            return _result("failed", [], ["GoodJob 評分公司識別不一致，未合併評價。"])
        if rating_count and rating_value is not None and 1 <= rating_value <= 5 and source_id:
            ratings.append(
                {
                    "source": "GoodJob",
                    "metric": "overall",
                    "value": rating_value,
                    "scale_min": 1,
                    "scale_max": 5,
                    "sample_count": rating_count,
                    "url": _fallback_goodjob(name),
                    "source_company_id": source_id,
                    "fetched_at": fetched_at,
                }
            )
        elif rating_count and (rating_value is None or not 1 <= rating_value <= 5):
            rating_limits.append("GoodJob 評分數值無法核對，未顯示評分。")
        elif rating_count and not source_id:
            rating_limits.append("GoodJob 評分缺少可核對的公司 ID，未顯示評分。")

    groups = (
        ("workExperiencesResult", "workExperiences", "GoodJob 工作經驗"),
        ("interviewExperiencesResult", "interviewExperiences", "GoodJob 面試經驗"),
    )
    parsed_groups: list[tuple[list, int]] = []
    for result_key, rows_key, _ in groups:
        result = company.get(result_key)
        if not isinstance(result, dict) or not isinstance(result.get(rows_key), list):
            return _result("failed", [], ["GoodJob 回應缺少經驗資料。"])
        count = _count(result.get("count"))
        if count is None:
            return _result("failed", [], ["GoodJob 經驗筆數格式無法辨識。"])
        parsed_groups.append((result[rows_key], count))

    declared_total = sum(count for _, count in parsed_groups)
    if declared_total == 0 and all(not rows for rows, _ in parsed_groups):
        return _result("success" if ratings else "empty", [], rating_limits, ratings)

    limitations: list[str] = list(rating_limits)
    evidence: list[dict] = []
    malformed = 0
    for (rows, count), (_, _, fallback_title) in zip(parsed_groups, groups):
        if count > len(rows):
            _limitation(limitations, "GoodJob 回應宣告有資料，但本次未提供完整項目。")
        if count == 0 and rows:
            _limitation(limitations, "GoodJob 回應包含超出宣告筆數的資料。")
        for row in rows:
            if not isinstance(row, dict):
                malformed += 1
                continue
            review_id = _raw(row, "id", "reviewId", "review_id", "uuid")
            title_raw = _raw(row, "title", "job_title")
            source_url_raw = _raw(row, "url", "link", "reviewUrl")
            title = None
            title_invalid = False
            if title_raw is not None:
                try:
                    title = _clean(title_raw, _MAX_TITLE)
                except (TypeError, ValueError):
                    title_invalid = True
            valid_identity = any(
                value is not None
                for value in (
                    _identity(review_id) if review_id is not None else None,
                    title,
                    _safe_url(source_url_raw) if source_url_raw is not None else None,
                )
            )
            if not valid_identity:
                malformed += 1
                continue
            if title_invalid:
                _limitation(limitations, "部分 GoodJob 標題無效，已略過。")
            url, unsafe_url = _url_with_fallback(source_url_raw, _fallback_goodjob(name))
            if unsafe_url:
                _limitation(limitations, "GoodJob 來源連結不安全，已改用公司公開頁面。")
            excerpt, excerpt_invalid = _excerpt(row, ("content", "experience", "description"))
            if excerpt_invalid:
                _limitation(limitations, "部分 GoodJob 內文無法使用，已略過。")
            evidence.append(
                {
                    "source": "GoodJob",
                    "title": title or fallback_title,
                    "url": url,
                    "date": _date(_raw(row, "created_at", "createdAt", "date", "publishedAt")),
                    "excerpt": excerpt,
                }
            )

    if not evidence and declared_total:
        return _result("failed", [], limitations or ["GoodJob 回應沒有可用的經驗資料。"], ratings)
    result = _finish_rows(
        evidence,
        limitations,
        malformed=malformed,
        recognized_empty=declared_total == 0,
        declared_total=declared_total,
        observed_total=sum(len(rows) for rows, _ in parsed_groups),
    )
    result["ratings"] = ratings
    return result


def _fallback_qollie(identifier: str) -> str:
    return "https://www.qollie.com/companies/" + quote(identifier, safe="")


def parse_qollie(row: object) -> dict:
    """Parse one Qollie search result row and its review-count summary."""

    if not isinstance(row, dict):
        return _result("failed", [], ["Qollie 結果不是可辨識的資料項目。"])
    identifier_raw = _raw(row, "uriHash", "_id")
    name_raw = _raw(row, "name")
    try:
        identifier = _identity(identifier_raw)
        name = _clean(name_raw, _MAX_TITLE) if name_raw is not None else None
    except (TypeError, ValueError):
        identifier = name = None
    if not identifier or not name:
        return _result("failed", [], ["Qollie 結果缺少公司識別資料。"])
    summary = row.get("summary")
    if not isinstance(summary, dict):
        return _result("failed", [], ["Qollie 結果缺少評價摘要。"])
    work = summary.get("work_experience")
    interview = summary.get("interview_experience")
    if not isinstance(work, dict) or not isinstance(interview, dict):
        return _result("failed", [], ["Qollie 評價摘要格式無法辨識。"])
    work_count = _count(work.get("amount"))
    interview_count = _count(interview.get("amount"))
    comments_count = _count(row.get("commentsSize"))
    if work_count is None or interview_count is None or comments_count is None:
        return _result("failed", [], ["Qollie 評價筆數無效。"])
    if work_count + interview_count + comments_count == 0:
        return _result("empty", [], [])

    source_url, unsafe_url = _url_with_fallback(
        _raw(row, "url", "link"), _fallback_qollie(identifier)
    )
    limitations: list[str] = []
    if unsafe_url:
        _limitation(limitations, "Qollie 來源連結不安全，已改用公司公開頁面。")
    excerpt = None
    intro_invalid = False
    if _has_value(row.get("introduction")):
        try:
            excerpt = _clean(row["introduction"], _MAX_EXCERPT)
        except (TypeError, ValueError):
            intro_invalid = True
    if intro_invalid:
        _limitation(limitations, "Qollie 公司介紹文字無法使用，已略過。")
    elif excerpt is not None:
        _limitation(limitations, "Qollie 公司介紹是公司資訊，不代表評價內容。")
    return _add_metadata_note(_result(
        "partial" if limitations and unsafe_url or intro_invalid else "success",
        [
            {
                "source": "Qollie",
                "title": name,
                "url": source_url,
                "date": _date(_raw(row, "createdAt", "created_at", "date", "publishedAt")),
                "excerpt": excerpt,
            }
        ],
        limitations,
    ))


class _PttParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[dict] = []
        self.current: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if self.current is None:
            if tag == "div" and "r-ent" in (attributes.get("class") or "").split():
                self.current = {
                    "depth": 1,
                    "title_depth": None,
                    "date_depth": None,
                    "title": [],
                    "date": [],
                    "url": None,
                }
            return
        current = self.current
        if tag == "div":
            current["depth"] += 1
            classes = (attributes.get("class") or "").split()
            if "title" in classes:
                current["title_depth"] = current["depth"]
            elif "date" in classes:
                current["date_depth"] = current["depth"]
        elif tag == "a" and current["title_depth"] is not None and current["url"] is None:
            current["url"] = attributes.get("href")

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        if self.current["title_depth"] is not None:
            self.current["title"].append(data)
        elif self.current["date_depth"] is not None:
            self.current["date"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None or tag.lower() != "div":
            return
        current = self.current
        if current["title_depth"] == current["depth"]:
            current["title_depth"] = None
        if current["date_depth"] == current["depth"]:
            current["date_depth"] = None
        current["depth"] -= 1
        if current["depth"] == 0:
            self.entries.append(current)
            self.current = None

    def finish(self) -> None:
        if self.current is not None:
            self.entries.append(self.current)
            self.current = None


def _ptt_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = urlsplit(raw if not raw.startswith("/") or raw.startswith("//") else "https://www.ptt.cc" + raw)
        host = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in _HTTP_SCHEMES or host is None:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if host.casefold() not in {"www.ptt.cc", "ptt.cc"} or not parsed.path.startswith("/bbs/Tech_Job/"):
        return None
    if ".." in parsed.path.split("/"):
        return None
    suffix = parsed.path + (("?" + parsed.query) if parsed.query else "")
    return "https://www.ptt.cc" + suffix


def parse_ptt(html: object) -> dict:
    """Parse PTT Tech_Job listing entries without reading post bodies."""

    if not isinstance(html, str):
        return _result("failed", [], ["PTT 回應不是可讀的頁面內容。"])
    try:
        parser = _PttParser()
        parser.feed(html)
        parser.close()
        parser.finish()
    except Exception:
        return _result("failed", [], ["PTT 頁面格式無法辨識。"])

    has_container = bool(
        re.search(r"<div\b[^>]*\bclass\s*=\s*['\"][^'\"]*\br-list-container\b", html, re.I)
    )
    if not has_container and not parser.entries:
        return _result("failed", [], ["找不到 PTT 職缺列表。"])
    if not parser.entries and has_container:
        return _result("empty", [], [])

    evidence: list[dict] = []
    limitations: list[str] = []
    malformed = 0
    deleted = 0
    for entry in parser.entries:
        try:
            title = _clean("".join(entry["title"]), _MAX_TITLE)
            raw_url = entry.get("url")
            url = _ptt_url(raw_url)
            if title and title.startswith(("[本文已刪除]", "(本文已刪除)")):
                deleted += 1
                continue
            if not title or not url:
                malformed += 1
                continue
            date_label = _clean("".join(entry["date"]), 200)
        except (TypeError, ValueError):
            malformed += 1
            continue
        evidence.append(
            {
                "source": "PTT",
                "title": title,
                "url": url,
                "date": _date(date_label),
                "excerpt": None,
            }
        )

    if evidence:
        if malformed:
            _limitation(limitations, "部分 PTT 列表項目格式無法辨識，已略過。")
        if len(parser.entries) - deleted > _MAX_ITEMS:
            _limitation(limitations, "只保留前五筆 PTT 列表項目。")
        if len(evidence) > _MAX_ITEMS:
            del evidence[_MAX_ITEMS:]
        return _add_metadata_note(_result("partial" if limitations else "success", evidence, limitations))
    if deleted == len(parser.entries):
        return _result("empty", [], [])
    return _result("failed", [], ["PTT 回應沒有可用的列表項目。"])


def parse_search(provider: object, payload: object) -> dict:
    """Parse Brave or Tavily result envelopes without fetching result links."""

    if not isinstance(provider, str):
        return _result("failed", [], ["不支援這個搜尋來源。"])
    key = provider.casefold()
    if key not in {"brave", "tavily"}:
        return _result("failed", [], ["不支援這個搜尋來源。"])
    source = "Brave" if key == "brave" else "Tavily"
    if not isinstance(payload, dict):
        return _result("failed", [], ["搜尋回應格式無法辨識。"])

    rows: list | None = None
    if key == "brave":
        web = payload.get("web")
        if isinstance(web, dict) and isinstance(web.get("results"), list):
            rows = web["results"]
        elif isinstance(payload.get("results"), list):
            rows = payload["results"]
    else:
        if isinstance(payload.get("results"), list):
            rows = payload["results"]
    if rows is None:
        return _result("failed", [], ["搜尋回應沒有提供結果列表。"])
    if not rows:
        return _result("empty", [], [])

    evidence: list[dict] = []
    limitations: list[str] = []
    malformed = 0
    for row in rows:
        if not isinstance(row, dict):
            malformed += 1
            continue
        title_raw = row.get("title")
        url = _safe_url(row.get("url"))
        try:
            title = _clean(title_raw, _MAX_TITLE) if _has_value(title_raw) else None
        except (TypeError, ValueError):
            title = None
        if not title or not url:
            malformed += 1
            continue
        excerpt, excerpt_invalid = _excerpt(row, ("description", "content"))
        if excerpt_invalid:
            _limitation(limitations, "部分搜尋摘要文字無法使用，已略過。")
        date_value = row.get("published_date") if key == "tavily" else row.get("page_age")
        if date_value is None:
            date_value = _raw(row, "date", "publishedAt", "published_at")
        evidence.append(
            {
                "source": source,
                "title": title,
                "url": url,
                "date": _date(date_value),
                "excerpt": excerpt,
            }
        )

    if len(rows) > _MAX_ITEMS:
        _limitation(limitations, "只保留前五筆搜尋結果。")
    if len(evidence) > _MAX_ITEMS:
        del evidence[_MAX_ITEMS:]
    if evidence:
        if malformed:
            _limitation(limitations, "部分搜尋結果格式無法辨識，已略過。")
        return _add_metadata_note(_result("partial" if limitations else "success", evidence, limitations))
    return _result("failed", [], limitations or ["搜尋回應沒有可用的結果。"])


__all__ = [
    "parse_104",
    "parse_goodjob",
    "parse_qollie",
    "parse_ptt",
    "parse_search",
]
