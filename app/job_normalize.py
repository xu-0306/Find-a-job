"""Normalize structured 104 and 518 rows into the shared :class:`Job` model."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urlsplit

from .store import Job


_SOURCES = {"104", "518"}
_MISSING = object()
_HIGHLIGHTS = re.compile(r"\[\[\[(.*?)\]\]\]", re.DOTALL)
_SPACE = re.compile(r"\s+")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = (
    re.compile(r"(?<!\d)(?:\+?886[- ]?)?09\d{2}[- ]?\d{3}[- ]?\d{3}(?!\d)"),
    re.compile(r"(?<!\d)(?:\+?886[- ]?)?0(?:2|[3-8])[- ]?\d{3,4}[- ]?\d{4}(?!\d)"),
)


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self._ignored += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)


def _clean_text(value: Any, *, redact_contacts: bool = True) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("job row has invalid text fields")
    text = str(value)
    parser = _TextParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise ValueError("job row has invalid text fields") from None
    text = _HIGHLIGHTS.sub(r"\1", " ".join(parser.parts))
    text = _SPACE.sub(" ", text).strip()
    if not text:
        return None
    if redact_contacts:
        text = _EMAIL.sub("[contact redacted]", text)
        for pattern in _PHONE:
            text = pattern.sub("[contact redacted]", text)
    return text


def _value(raw: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = raw.get(name, _MISSING)
        if value is _MISSING or value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _required_text(value: Any, *, redact_contacts: bool = True) -> str:
    text = _clean_text(value, redact_contacts=redact_contacts)
    if text is None:
        raise ValueError("job row is missing required fields")
    return text


def _warnings(raw: Mapping[str, Any]) -> list[str]:
    value = raw.get("filter_warnings", [])
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("job row has invalid warnings")
    return list(value)


def _append_warning(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _parse_published(value: Any, warnings: list[str]) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        _append_warning(warnings, "來源只提供日期，沒有明確時間，因此不填入發佈時間。")
        return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                date.fromisoformat(text)
                _append_warning(warnings, "來源只提供日期，沒有明確時間，因此不填入發佈時間。")
                return None
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        except (TypeError, ValueError):
            raise ValueError("job row has invalid published_at") from None
    else:
        raise ValueError("job row has invalid published_at")
    try:
        offset = parsed.utcoffset()
    except (AttributeError, ValueError, OverflowError):
        offset = None
    if parsed.tzinfo is None or offset is None:
        _append_warning(warnings, "來源時間缺少時區，因此不填入發佈時間。")
        return None
    return parsed.astimezone(timezone.utc)


def _parse_fetched(value: Any) -> datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            text = value.strip()
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        except (TypeError, ValueError):
            raise ValueError("job row has invalid fetched_at") from None
    else:
        raise ValueError("job row has invalid fetched_at")
    try:
        offset = parsed.utcoffset()
    except (AttributeError, ValueError, OverflowError):
        offset = None
    if parsed.tzinfo is None or offset is None:
        raise ValueError("job row has invalid fetched_at")
    return parsed.astimezone(timezone.utc)


def _url(value: Any) -> str:
    if value is None or isinstance(value, bool):
        raise ValueError("job row has invalid URL")
    text = str(value).strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        raise ValueError("job row has invalid URL") from None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("job row has invalid URL")
    return text


def _source_fields(source: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    if source == "104":
        return {
            "source_job_id": _value(raw, "source_job_id", "job_no", "jobNo"),
            "title": _value(raw, "title", "job_name", "jobName"),
            "company_name": _value(raw, "company_name", "company", "custName"),
            "company_source_id": _value(raw, "company_source_id", "custNo", "cust_no"),
            "location": _value(raw, "location"),
            "salary_text": _value(raw, "salary_text", "salary"),
            "url": _value(raw, "url"),
            "published_at": _value(raw, "published_at", "appear_date", "appearDate"),
            "summary": _value(raw, "summary", "snippet", "description"),
        }
    return {
        "source_job_id": _value(raw, "source_job_id"),
        "title": _value(raw, "title"),
        "company_name": _value(raw, "company_name"),
        "company_source_id": _value(raw, "company_source_id"),
        "location": _value(raw, "location"),
        "salary_text": _value(raw, "salary_text"),
        "url": _value(raw, "url"),
        "published_at": _value(raw, "published_at"),
        "summary": _value(raw, "summary"),
    }


def normalize_job(source: str, raw: Mapping[str, Any]) -> Job | None:
    """Convert one structured source row to ``Job``; return ``None`` for ads."""
    if source not in _SOURCES:
        raise ValueError("source must be 104 or 518")
    if not isinstance(raw, Mapping):
        raise ValueError("job row must be an object")

    ad_flag = raw.get("is_ad", _MISSING)
    if type(ad_flag) is bool and ad_flag:
        return None
    warnings = _warnings(raw)
    if source == "104" and (ad_flag is _MISSING or type(ad_flag) is not bool):
        _append_warning(warnings, "104 未提供明確的廣告標記，無法判定是否為廣告。")

    fields = _source_fields(source, raw)
    source_job_id = _required_text(fields["source_job_id"], redact_contacts=False)
    title = _required_text(fields["title"])
    company_name = _required_text(fields["company_name"])
    company_source_id = _clean_text(fields["company_source_id"], redact_contacts=False)
    location = _clean_text(fields["location"])
    salary_text = _clean_text(fields["salary_text"])
    summary = _clean_text(fields["summary"])
    published_at = _parse_published(fields["published_at"], warnings)
    payload: dict[str, Any] = {
        "source": source,
        "source_job_id": source_job_id,
        "title": title,
        "company_name": company_name,
        "company_source_id": company_source_id,
        "location": location,
        "salary_text": salary_text,
        "url": _url(fields["url"]),
        "published_at": published_at,
        "summary": summary,
        "filter_warnings": warnings,
    }
    fetched = _parse_fetched(raw.get("fetched_at")) if raw.get("fetched_at", _MISSING) is not _MISSING else None
    if fetched is not None:
        payload["fetched_at"] = fetched
    try:
        return Job.model_validate(payload)
    except Exception:
        raise ValueError("job row has invalid fields") from None


def normalize_jobs(source: str, rows: Iterable[Mapping[str, Any]]) -> tuple[list[Job], list[str]]:
    """Normalize rows, remove source/ID duplicates, and return distinct warnings."""
    if source not in _SOURCES:
        raise ValueError("source must be 104 or 518")
    if isinstance(rows, (str, bytes)):
        raise ValueError("job rows must be iterable objects")
    try:
        iterator = iter(rows)
    except TypeError:
        raise ValueError("job rows must be iterable objects") from None
    jobs: list[Job] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    try:
        for raw in iterator:
            job = normalize_job(source, raw)
            if job is None:
                continue
            for warning in job.filter_warnings:
                if warning not in warnings:
                    warnings.append(warning)
            key = (job.source, job.source_job_id)
            if key not in seen:
                seen.add(key)
                jobs.append(job)
    except ValueError:
        raise
    except Exception:
        raise ValueError("job rows could not be normalized") from None
    return jobs, warnings
