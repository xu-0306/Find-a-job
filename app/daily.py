"""Commit daily source batches in a caller-owned SQLite transaction."""

from collections.abc import Mapping
from typing import Any

from .security import DEFAULT_USER_ID
from .store import Job


_SOURCES = {"104", "518"}


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _batch(batch: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(batch, Mapping):
        raise ValueError(f"batch {index} must be an object")
    preference_id = _text(batch.get("preference_id"), "preference_id")
    user_id = _text(batch.get("user_id", DEFAULT_USER_ID), "user_id")
    revision = batch.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("revision must be a positive integer")
    source = batch.get("source")
    if not isinstance(source, str) or source not in _SOURCES:
        raise ValueError("source must be 104 or 518")
    eligible = batch.get("eligible")
    if type(eligible) is not bool:
        raise ValueError("eligible must be boolean")
    items = batch.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    diagnostic = batch.get("diagnostic")
    if not isinstance(diagnostic, Mapping):
        raise ValueError("diagnostic must be an object")
    for item in items:
        if not isinstance(item, Job):
            raise ValueError("items must contain Job values")
        if item.source != source:
            raise ValueError("job source does not match batch source")
    return {
        "preference_id": preference_id,
        "user_id": user_id,
        "revision": revision,
        "source": source,
        "eligible": eligible,
        "items": items,
        "diagnostic": dict(diagnostic),
    }


def _insert_daily(db, user_id: str, local_date: str, job_id: str, run_id: str, is_initial: bool) -> bool:
    cursor = db.execute(
        """INSERT OR IGNORE INTO daily_jobs
           (user_id, local_date, job_id, run_id, is_initial, read_at)
           VALUES(?,?,?,?,?,NULL)""",
        (user_id, local_date, job_id, run_id, int(is_initial)),
    )
    return cursor.rowcount == 1


def _commit_account(db, user_id, records, local_date, run_id, timestamp):
    """Baseline, seen and daily rows for one account's batches; accounts never share these."""
    baseline_before = {}
    for record in records:
        batch = record["batch"]
        if not batch["eligible"]:
            continue
        key = (batch["preference_id"], batch["revision"], batch["source"])
        baseline_before[key] = bool(
            db.execute(
                """SELECT 1 FROM preference_baselines
                   WHERE preference_id=? AND revision=? AND source=?""",
                key,
            ).fetchone()
        )

    # This snapshot is intentionally taken before this call's seen inserts.
    # Probe only this round's unique candidates through the job_id index; the
    # history table can grow without making every daily run scan it.
    candidate_job_ids = {
        job_id
        for record in records
        if record["batch"]["eligible"]
        for job_id in record["job_ids"]
    }
    seen_before = {
        job_id
        for job_id in candidate_job_ids
        if db.execute(
            "SELECT 1 FROM seen_jobs WHERE job_id=? AND user_id=? LIMIT 1", (job_id, user_id)
        ).fetchone()
    }
    baseline_ids = set()
    initial_ids = []
    initial_seen = set()
    for record in records:
        batch = record["batch"]
        if not batch["eligible"]:
            continue
        key = (batch["preference_id"], batch["revision"], batch["source"])
        if not baseline_before[key]:
            baseline_ids.update(record["job_ids"])
            for job_id in record["job_ids"][:20]:
                if job_id not in initial_seen:
                    initial_seen.add(job_id)
                    initial_ids.append(job_id)

    # A regular batch can add only a Job this account has not seen that is not a
    # baseline candidate anywhere in this run.  The set makes overlap order
    # independent while daily_jobs supplies the database uniqueness guard.
    regular_ids = []
    regular_seen = set()
    for record in records:
        batch = record["batch"]
        if not batch["eligible"]:
            continue
        key = (batch["preference_id"], batch["revision"], batch["source"])
        if not baseline_before[key]:
            continue
        for job_id in record["job_ids"]:
            if job_id not in seen_before and job_id not in baseline_ids and job_id not in regular_seen:
                regular_seen.add(job_id)
                regular_ids.append(job_id)

    # Baseline and condition-specific seen rows are both durable.  Empty
    # eligible batches therefore establish a successful empty baseline.
    for record in records:
        batch = record["batch"]
        if not batch["eligible"]:
            continue
        key = (batch["preference_id"], batch["revision"], batch["source"])
        if not baseline_before[key]:
            db.execute(
                """INSERT OR IGNORE INTO preference_baselines
                   (preference_id, revision, source, completed_at)
                   VALUES(?,?,?,?)""",
                (*key, timestamp),
            )
        for job_id in record["job_ids"]:
            db.execute(
                """INSERT OR IGNORE INTO seen_jobs
                   (preference_id, revision, job_id, first_seen_at, user_id)
                   VALUES(?,?,?,?,?)""",
                (batch["preference_id"], batch["revision"], job_id, timestamp, user_id),
            )

    initial_count = sum(_insert_daily(db, user_id, local_date, job_id, run_id, True) for job_id in initial_ids)
    new_count = sum(_insert_daily(db, user_id, local_date, job_id, run_id, False) for job_id in regular_ids)

    source_results = []
    for record in records:
        batch = record["batch"]
        result = dict(batch["diagnostic"])
        key = (batch["preference_id"], batch["revision"], batch["source"])
        result.update(
            preference_id=batch["preference_id"],
            revision=batch["revision"],
            source=batch["source"],
            baseline=bool(batch["eligible"] and not baseline_before.get(key, False)),
            completed=batch["eligible"],
        )
        source_results.append(result)
    return {"new_count": new_count, "initial_count": initial_count, "source_results": source_results}


def commit_results(store, db, run_id, local_date, batches, timestamp):
    """Persist eligible daily batches without owning the transaction.

    ``db`` must already be inside the caller's write transaction.  A batch's
    ``baseline`` result means this call created that condition/source baseline;
    ``completed`` means the eligible batch was committed by this function.
    Each batch carries its account's ``user_id`` (the built-in account when
    absent); accounts are committed independently and the per-account result is
    kept under ``users``.  The caller owns rollback when validation or a
    database write raises.
    """
    run_id = _text(run_id, "run_id")
    local_date = _text(local_date, "local_date")
    timestamp = _text(timestamp, "timestamp")
    if not isinstance(batches, list):
        raise ValueError("batches must be a list")
    normalized = [_batch(batch, index) for index, batch in enumerate(batches)]

    records = []
    # Save every eligible Job before deriving seen/baseline state.  Chat Jobs
    # are intentionally irrelevant here: only rows in seen_jobs are daily seen.
    for batch in normalized:
        job_ids = []
        if batch["eligible"]:
            for job in batch["items"]:
                job_id = store.save_job(job, db)
                if job_id not in job_ids:
                    job_ids.append(job_id)
        records.append({"batch": batch, "job_ids": job_ids})

    users = {}
    for user_id in dict.fromkeys(record["batch"]["user_id"] for record in records):
        users[user_id] = _commit_account(
            db, user_id, [record for record in records if record["batch"]["user_id"] == user_id],
            local_date, run_id, timestamp)

    source_results = [item for user_id in users for item in users[user_id]["source_results"]]
    return {
        "new_count": sum(mine["new_count"] for mine in users.values()),
        "initial_count": sum(mine["initial_count"] for mine in users.values()),
        "source_results": source_results,
        "users": users,
    }
