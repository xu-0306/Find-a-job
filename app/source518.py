"""Pure parser for 518 search pages and JobPosting JSON-LD details.

This module deliberately has no transport or persistence concerns.  The caller
owns URL validation before making requests, pagination, identity matching, and
normalising the returned rows into the shared Job model.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from html.parser import HTMLParser
import json
import re
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit


BASE_URL = "https://www.518.com.tw/"
_HOSTS = {"518.com.tw", "www.518.com.tw"}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "div", "dl",
    "dt", "dd", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "ul",
}
_CHALLENGE_MARKERS = (
    "cf-chl-", "cloudflare ray id", "checking your browser", "security check",
    "請完成驗證", "人機驗證", "驗證您不是機器人",
)
_CHALLENGE_WIDGETS = ("altcha", "turnstile", "captcha")
_EMPTY_MARKERS = (
    "查無符合條件的職缺", "查無相關職缺", "目前沒有符合條件的職缺",
    "沒有符合條件的工作職缺", "找不到符合條件的職缺", "抱歉沒有搜到適合的職缺",
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_PATTERNS = (
    re.compile(r"(?<!\d)(?:\+?886[- ]?)?09\d{2}[- ]?\d{3}[- ]?\d{3}(?!\d)"),
    re.compile(r"(?<!\d)(?:\+?886[- ]?)?0(?:2|[3-8])[- ]?\d{3,4}[- ]?\d{4}(?!\d)"),
)
_ID_RE = re.compile(r"[A-Za-z0-9]+")


def _looks_like_challenge(body: str) -> bool:
    lowered = body[:20000].lower()
    if any(marker in lowered for marker in _CHALLENGE_MARKERS):
        return True
    for widget in _CHALLENGE_WIDGETS:
        if re.search(rf"(?:id|class|name)\s*=\s*[\"'][^\"']*{re.escape(widget)}", lowered):
            return True
        if widget in lowered and any(word in lowered for word in ("verify", "verification", "human", "robot", "challenge", "驗證")):
            return True
    return False


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str = "", attrs: dict[str, str | None] | None = None) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[_Node | str] = []


class _TreeParser(HTMLParser):
    """Small tolerant HTML tree used only for extracting known source fields."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), {name.lower(): value for name, value in attrs})
        self._stack[-1].children.append(node)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(
            _Node(tag.lower(), {name.lower(): value for name, value in attrs})
        )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        match = next((index for index in range(len(self._stack) - 1, 0, -1)
                      if self._stack[index].tag == tag), None)
        if match is not None:
            del self._stack[match:]

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def _parse_html(value: str) -> _Node:
    parser = _TreeParser()
    parser.feed(value)
    parser.close()
    return parser.root


def _walk(node: _Node) -> Iterator[_Node]:
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk(child)


def _classes(node: _Node) -> set[str]:
    return set((node.attrs.get("class") or "").split())


def _has_class(node: _Node, name: str) -> bool:
    return name in _classes(node)


def _first(node: _Node, predicate) -> _Node | None:
    return next((candidate for candidate in _walk(node) if predicate(candidate)), None)


def _append_space(parts: list[str]) -> None:
    if parts and not parts[-1].endswith((" ", "\n")):
        parts.append(" ")


def _visible_text(node: _Node) -> str:
    """Extract visible text, retaining a separator across block elements."""
    parts: list[str] = []

    def visit(current: _Node) -> None:
        if current.tag in {"script", "style"}:
            return
        block = current.tag in _BLOCK_TAGS
        if block:
            _append_space(parts)
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                visit(child)
        if block:
            _append_space(parts)

    visit(node)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _clean_text(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    try:
        text = _visible_text(_parse_html(value))
    except Exception:
        return None
    text = _EMAIL_RE.sub("[contact redacted]", text)
    for pattern in _PHONE_PATTERNS:
        text = pattern.sub("[contact redacted]", text)
    if not text:
        return None
    return text[:limit]


def _safe_518_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        absolute = urljoin(BASE_URL, value.strip())
        parsed = urlsplit(absolute)
        hostname = parsed.hostname
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or hostname.lower() not in _HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port != 443)
    ):
        return None
    return absolute


def _result_search(
    status: str,
    *,
    items: list[dict] | None = None,
    next_url: str | None = None,
    page_title: str | None = None,
    area_label: str | None = None,
    limitations: list[str] | None = None,
) -> dict:
    return {
        "status": status,
        "items": items or [],
        "next_url": next_url,
        "page_title": page_title,
        "area_label": area_label,
        "limitations": limitations or [],
    }


def _page_metadata(root: _Node) -> tuple[str | None, str | None, str | None]:
    title_node = _first(root, lambda node: node.tag == "title")
    page_title = _visible_text(title_node) if title_node else None
    page_title = page_title or None
    area_label = None
    if page_title:
        area_label = re.split(r"找[「『\"]", page_title, maxsplit=1)[0].strip() or None
    next_node = _first(
        root,
        lambda node: node.tag == "link"
        and "next" in (node.attrs.get("rel") or "").lower().split(),
    )
    next_url = next_node.attrs.get("href") if next_node else None
    return page_title, area_label, next_url


def _field(card: _Node, class_name: str, limit: int = 500) -> str | None:
    node = _first(card, lambda candidate: _has_class(candidate, class_name))
    return _clean_text(_visible_text(node), limit) if node else None


def _card_location(card: _Node) -> str | None:
    summary = _first(card, lambda node: _has_class(node, "job__summaries"))
    if summary:
        first_li = _first(summary, lambda node: node.tag == "li")
        if first_li:
            return _clean_text(_visible_text(first_li), 500)
    return _field(card, "job__location")


def _company_id(card: _Node) -> str | None:
    company = _first(card, lambda node: _has_class(node, "job__comp"))
    if not company:
        company = _first(card, lambda node: _has_class(node, "job__comp__name"))
    if not company:
        return None
    candidates = [company, *list(_walk(company))]
    for candidate in candidates:
        safe = _safe_518_url(candidate.attrs.get("href"))
        if not safe:
            continue
        path = urlsplit(safe).path
        match = re.fullmatch(r"/company-([A-Za-z0-9]+)\.html", path)
        if match:
            return match.group(1)
    return None


def _parse_search_card(card: _Node) -> dict | None:
    job_id = (card.attrs.get("data-id") or "").strip()
    if not job_id or not _ID_RE.fullmatch(job_id):
        return None

    title_node = _first(card, lambda node: _has_class(node, "job__title"))
    data_url = card.attrs.get("data-url")
    href = title_node.attrs.get("href") if title_node else None
    candidate = data_url if data_url is not None else href
    job_url = _safe_518_url(candidate)
    if not job_url or urlsplit(job_url).path != f"/job-{job_id}.html":
        return None

    title = _clean_text(_visible_text(title_node), 500) if title_node else None
    company = _field(card, "job__comp__name") or _field(card, "job__comp")
    if not title or not company:
        return None
    display_date = _field(card, "job__date")
    warnings: list[str] = []
    if display_date:
        warnings.append("搜尋結果只提供沒有年份或時區的日期，因此不填入發佈時間。")
    return {
        "source_job_id": job_id,
        "title": title,
        "company_name": company,
        "company_source_id": _company_id(card),
        "location": _card_location(card),
        "salary_text": _field(card, "job__salary"),
        "url": job_url,
        "published_at": None,
        "summary": _field(card, "job__intro", 20000),
        "filter_warnings": warnings,
        "_display_date": display_date,
    }


def parse_search_response(http_status: int, body: str) -> dict:
    """Parse one already-fetched 518 search response without performing I/O."""
    if http_status in {403, 429}:
        return _result_search(
            "blocked",
            limitations=["HTTP 回應遭來源驗證或限流阻擋；系統未嘗試繞過。"],
        )
    if not isinstance(body, str):
        return _result_search("failed", limitations=["來源回應不是可讀的文字內容。"])
    if http_status < 200 or http_status >= 300:
        if _looks_like_challenge(body):
            return _result_search(
                "blocked",
                limitations=["HTTP 回應遭來源驗證或限流阻擋；系統未嘗試繞過。"],
            )
        return _result_search("failed", limitations=[f"HTTP {http_status} 回應未成功。"])

    try:
        root = _parse_html(body)
        page_title, area_label, next_url = _page_metadata(root)
        cards = [node for node in _walk(root) if node.tag == "div" and _has_class(node, "job__card")]
        items: list[dict] = []
        seen: set[str] = set()
        invalid_cards = 0
        for card in cards:
            item = _parse_search_card(card)
            if item is None:
                invalid_cards += 1
                continue
            if item["source_job_id"] not in seen:
                seen.add(item["source_job_id"])
                items.append(item)

        if items:
            status = "partial" if invalid_cards else "success"
            limitations = (
                ["部分職缺卡片因必要識別資料無效而略過。"]
                if invalid_cards else []
            )
            return _result_search(
                status,
                items=items,
                next_url=next_url,
                page_title=page_title,
                area_label=area_label,
                limitations=limitations,
            )

        if _looks_like_challenge(body):
            return _result_search(
                "blocked",
                page_title=page_title,
                area_label=area_label,
                limitations=["回應符合來源驗證頁面；系統未嘗試繞過。"],
            )
        container = _first(root, lambda node: node.attrs.get("id") == "listContent")
        visible = _visible_text(container) if container else ""
        explicit_empty = bool(container) and any(marker in visible for marker in _EMPTY_MARKERS)
        if explicit_empty and not invalid_cards:
            return _result_search(
                "empty",
                page_title=page_title,
                area_label=area_label,
                limitations=[],
            )
        if invalid_cards:
            reason = "找到職缺卡片，但沒有卡片具備可信的必要識別資料。"
        elif not container:
            reason = "找不到職缺結果區塊，來源頁面可能已變更。"
        else:
            reason = "找不到職缺卡片或明確的空結果提示，來源頁面可能已變更。"
        return _result_search(
            "failed",
            page_title=page_title,
            area_label=area_label,
            limitations=[reason],
        )
    except Exception:
        return _result_search(
            "failed",
            limitations=["無法解析來源頁面，頁面格式可能已變更。"],
        )


class _JSONLDScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        media_type = (dict(attrs).get("type") or "").lower().split(";", 1)[0].strip()
        if media_type in {"application/ld+json", "application/jsonld"}:
            self.current = []

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


def _jsonld_postings(value: Any) -> Iterator[dict]:
    if isinstance(value, list):
        for entry in value:
            yield from _jsonld_postings(entry)
        return
    if not isinstance(value, dict):
        return
    kind = value.get("@type")
    types = kind if isinstance(kind, list) else [kind]
    if any(
        isinstance(entry, str)
        and entry.rsplit("/", 1)[-1].rsplit("#", 1)[-1].casefold() == "jobposting"
        for entry in types
    ):
        yield value
    graph = value.get("@graph")
    if graph is not None:
        yield from _jsonld_postings(graph)


def _extract_postings(body: str) -> list[dict]:
    parser = _JSONLDScriptParser()
    parser.feed(body)
    parser.close()
    postings: list[dict] = []
    for script in parser.scripts:
        try:
            value = json.loads(script)
        except (json.JSONDecodeError, TypeError):
            continue
        postings.extend(_jsonld_postings(value))
    return postings


def _location(posting: dict) -> str | None:
    locations = posting.get("jobLocation")
    if not isinstance(locations, list):
        locations = [locations]
    for location in locations:
        if not isinstance(location, dict):
            continue
        address = location.get("address")
        if not isinstance(address, dict):
            continue
        parts = []
        for key in ("addressRegion", "addressLocality"):
            value = _clean_text(address.get(key), 500)
            if value:
                parts.append(value)
        if parts:
            return "-".join(parts)[:500]
    return None


def _salary_text(posting: dict) -> str | None:
    for key in ("baseSalary", "estimatedSalary", "salary"):
        value = posting.get(key)
        if isinstance(value, str):
            cleaned = _clean_text(value, 500)
            if cleaned:
                return cleaned
        if isinstance(value, dict):
            description = value.get("description")
            cleaned = _clean_text(description, 500)
            if cleaned:
                return cleaned
    return None


def _published(value: Any, warnings: list[str]) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    source_date = value.strip() if isinstance(value, str) else None
    if not isinstance(value, str) or not source_date:
        warnings.append("來源日期格式無法辨識，因此不填入發佈時間。")
        return source_date, None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date):
        try:
            date.fromisoformat(source_date)
        except ValueError:
            warnings.append("來源日期格式無法辨識，因此不填入發佈時間。")
        else:
            warnings.append("來源只提供日期，沒有明確時間，因此不填入發佈時間。")
        return source_date, None
    try:
        parsed = datetime.fromisoformat(
            source_date[:-1] + "+00:00" if source_date.endswith(("Z", "z")) else source_date
        )
        offset = parsed.utcoffset()
    except (TypeError, ValueError, OverflowError):
        warnings.append("來源日期格式無法辨識，因此不填入發佈時間。")
        return source_date, None
    if parsed.tzinfo is None or offset is None:
        warnings.append("來源時間缺少時區，因此不填入發佈時間。")
        return source_date, None
    return source_date, parsed.astimezone(timezone.utc).isoformat()


def _identifier(posting: dict) -> tuple[str | None, str | None, list[str]]:
    raw = posting.get("identifier")
    name = value = None
    if isinstance(raw, dict):
        name = _clean_text(raw.get("name"), 500)
        value = _clean_text(raw.get("value"), 500)
    elif isinstance(raw, str):
        value = _clean_text(raw, 500)
    warnings = (
        ["已保留來源提供的識別資訊作為職缺資料，但公司身分尚未核實。"]
        if value else []
    )
    return value, name, warnings


def _reference_url(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get('@id') or value.get('url')
    return _safe_518_url(value) if isinstance(value, str) else None


def _detail_identity(posting: dict, job_id: str, expected_url: str | None) -> tuple[bool, dict]:
    expected_path = None
    if expected_url:
        expected = _safe_518_url(expected_url)
        expected_path = urlsplit(expected).path if expected else None
    matching_reference = None
    for key in ('url', '@id', 'mainEntityOfPage'):
        reference = _reference_url(posting.get(key))
        if reference and expected_path and urlsplit(reference).path == expected_path:
            matching_reference = reference
            break
    # 518's JSON-LD ``identifier`` is commonly the employer identifier (the
    # sampled pages name the company there), so an equal value must not be
    # treated as proof that the requested job page is the same posting.
    return bool(matching_reference), {'url': matching_reference}


def _detail_result(status: str, detail: dict | None, limitations: list[str], identity_verified: bool = False) -> dict:
    return {
        'status': status,
        'detail': detail,
        'identity_verified': identity_verified,
        'limitations': limitations,
    }


def parse_detail_response(http_status: int, body: str, job_id: str, expected_url: str | None = None) -> dict:
    """Parse one already-fetched 518 detail page without performing I/O."""
    if http_status in {403, 429}:
        return _detail_result("blocked", None, ["HTTP 回應遭來源驗證或限流阻擋；系統未嘗試繞過。"])
    if not isinstance(body, str):
        return _detail_result("failed", None, ["來源回應不是可讀的文字內容。"])
    if http_status < 200 or http_status >= 300:
        if _looks_like_challenge(body):
            return _detail_result("blocked", None, ["HTTP 回應遭來源驗證或限流阻擋；系統未嘗試繞過。"])
        return _detail_result("failed", None, [f"HTTP {http_status} 回應未成功。"])

    try:
        postings = _extract_postings(body)
    except Exception:
        return _detail_result("failed", None, ["無法解析職缺詳細資料，頁面格式可能已變更。"])
    if not postings:
        if _looks_like_challenge(body):
            return _detail_result("blocked", None, ["回應符合來源驗證頁面；系統未嘗試繞過。"])
        return _detail_result("failed", None, ["找不到可用的職缺詳細資料，頁面格式可能已變更。"])

    for posting in postings:
        title = _clean_text(posting.get("title"), 500)
        organization = posting.get("hiringOrganization")
        company = _clean_text(organization.get("name"), 500) if isinstance(organization, dict) else None
        if not title or not company:
            continue
        warnings: list[str] = []
        posting_identifier, identifier_name, identifier_warnings = _identifier(posting)
        warnings.extend(identifier_warnings)
        source_date, published_at = _published(posting.get("datePosted"), warnings)
        identity_verified, identity_evidence = _detail_identity(posting, job_id, expected_url)
        detail = {
            "source_job_id": job_id,
            "jsonld_type": "JobPosting",
            "title": title,
            "company_name": company,
            "company_source_id": None,
            "location": _location(posting),
            "salary_text": _salary_text(posting),
            "summary": _clean_text(posting.get("description"), 20000),
            "published_at": published_at,
            "source_date": source_date,
            "posting_identifier": posting_identifier,
            "posting_identifier_name": identifier_name,
            "identity_evidence": identity_evidence,
            "filter_warnings": warnings,
        }
        limitations = [] if identity_verified else ["JSON-LD 未提供可核對的職缺識別資訊，詳細內容未覆蓋已保存職缺。"]
        return _detail_result("success", detail, limitations, identity_verified)
    return _detail_result("failed", None, ["職缺詳細資料缺少可用的職缺標題或公司名稱。"])
