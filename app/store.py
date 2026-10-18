import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from typing import Literal


def now():
    return datetime.now(timezone.utc)


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Criteria(Input):
    keyword: str = Field(min_length=1, max_length=100, description="職務名稱或技能詞（一個精簡片語）；地區放 area_codes，薪資、正職兼職、福利等不放這裡（518 會比對任一詞，泛用詞會帶回不相干職缺）。")
    area_codes: list[str] = Field(default_factory=list, max_length=20)
    min_monthly_salary: int | None = Field(default=None, ge=0, le=10000000)

    # The region codes belong to the upstream service; do not invent a local city map.
    def model_post_init(self, context):
        if any(not code.isascii() or not code.isdigit() or len(code) > 20 for code in self.area_codes):
            raise ValueError("地區代碼須為 1～20 位數字")
        self.area_codes = list(dict.fromkeys(self.area_codes))


class Job(Input):
    source: Literal["104", "518", "1111"]
    source_job_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    company_name: str = Field(min_length=1, max_length=500)
    company_source_id: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=500)
    salary_text: str | None = Field(default=None, max_length=500)
    url: HttpUrl
    published_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=now)
    summary: str | None = Field(default=None, max_length=20000)
    filter_warnings: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("published_at", "fetched_at")
    @classmethod
    def utc_dates(cls, value):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("時間須包含時區")
            return value.astimezone(timezone.utc)
        return value


class Store:
    def __init__(self, directory=None, *, target_version=7):
        self.path = Path(directory or os.environ.get("JOB_COMPANION_DATA", "data")) / "app.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if target_version not in (4, 5, 6, 7) or version not in range(target_version + 1):
                raise RuntimeError("資料庫版本不支援；請使用相容程式，勿覆蓋資料。")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(
                    id_hash TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','user')),
                    expires_at TEXT NOT NULL, device_id TEXT);
                CREATE TABLE IF NOT EXISTS login_attempts(
                    peer TEXT PRIMARY KEY, failures INTEGER NOT NULL, blocked_until TEXT);
                CREATE TABLE IF NOT EXISTS preferences(
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision > 0),
                    criteria_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS preference_proposals(
                    id TEXT PRIMARY KEY, operation TEXT NOT NULL CHECK(operation IN ('create','update','delete')),
                    preference_id TEXT, base_revision INTEGER, before_json TEXT, after_json TEXT,
                    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','cancelled')));
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('104','518')),
                    source_job_id TEXT NOT NULL, payload_json TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    UNIQUE(source,source_job_id));
                CREATE TABLE IF NOT EXISTS favorites(
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), created_at TEXT NOT NULL);
            """)
            if version < 2:
                # executescript owns this transaction; a failed migration keeps v1 intact.
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE messages(
                        id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                        role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                        content_json TEXT NOT NULL, created_at TEXT NOT NULL,
                        UNIQUE(request_id, role));
                    PRAGMA user_version = 2;
                    COMMIT;
                """)

            if version < 3:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS review_cache(
                        company_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL, expires_at TEXT NOT NULL);
                    PRAGMA user_version = 3;
                    COMMIT;
                """)
            if version < 4:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE seen_jobs(
                        preference_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
                        job_id TEXT NOT NULL REFERENCES jobs(id), first_seen_at TEXT NOT NULL,
                        PRIMARY KEY(preference_id,revision,job_id));
                    CREATE INDEX seen_job_identity ON seen_jobs(job_id);
                    CREATE TABLE preference_baselines(
                        preference_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
                        source TEXT NOT NULL CHECK(source IN ('104','518')), completed_at TEXT NOT NULL,
                        PRIMARY KEY(preference_id,revision,source));
                    CREATE TABLE runs(
                        id TEXT PRIMARY KEY, local_date TEXT NOT NULL,
                        trigger TEXT NOT NULL CHECK(trigger IN ('scheduled','catchup','manual','baseline')),
                        status TEXT NOT NULL CHECK(status IN ('running','success','partial','failed')),
                        result_json TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT);
                    CREATE INDEX run_date ON runs(local_date,started_at);
                    CREATE TABLE daily_jobs(
                        local_date TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id), run_id TEXT NOT NULL,
                        is_initial INTEGER NOT NULL CHECK(is_initial IN (0,1)), read_at TEXT,
                        PRIMARY KEY(local_date,job_id));
                    CREATE TABLE push_subscriptions(
                        id TEXT PRIMARY KEY, device_id TEXT NOT NULL, endpoint TEXT NOT NULL UNIQUE,
                        keys_json TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE push_attempts(
                        run_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
                        attempted_at TEXT NOT NULL, result TEXT NOT NULL,
                        PRIMARY KEY(run_id,subscription_id));
                    CREATE TABLE sprites(
                        state TEXT PRIMARY KEY CHECK(state IN ('idle','thinking','happy','mail','encourage')),
                        path TEXT NOT NULL, frame_width INTEGER NOT NULL CHECK(frame_width BETWEEN 1 AND 512),
                        frame_height INTEGER NOT NULL CHECK(frame_height BETWEEN 1 AND 512),
                        frame_count INTEGER NOT NULL CHECK(frame_count BETWEEN 1 AND 120),
                        fps INTEGER NOT NULL CHECK(fps BETWEEN 1 AND 30));
                    CREATE TABLE quotes(id TEXT PRIMARY KEY, text TEXT NOT NULL, sort_order INTEGER NOT NULL);
                    PRAGMA user_version = 4;
                    COMMIT;
                """)

            if version < 5 and target_version >= 5:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE conversations(
                        id TEXT PRIMARY KEY, title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 80),
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                    INSERT INTO conversations VALUES('legacy','既有對話',
                        COALESCE((SELECT MIN(created_at) FROM messages),strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
                        COALESCE((SELECT MAX(created_at) FROM messages),strftime('%Y-%m-%dT%H:%M:%f+00:00','now')));
                    ALTER TABLE messages ADD COLUMN conversation_id TEXT REFERENCES conversations(id);
                    UPDATE messages SET conversation_id='legacy';
                    CREATE INDEX messages_conversation ON messages(conversation_id);
                    PRAGMA user_version = 5;
                    COMMIT;
                """)

            if version < 6 and target_version >= 6:
                # Accounts: every existing row belongs to the built-in 'default' (owner) account.
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE users(
                        id TEXT PRIMARY KEY,
                        username TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK(length(username) BETWEEN 3 AND 32),
                        access_token_hash TEXT UNIQUE, disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),
                        has_chosen INTEGER NOT NULL DEFAULT 1 CHECK(has_chosen IN (0,1)), created_at TEXT NOT NULL);
                    INSERT INTO users VALUES('default','owner',
                        (SELECT json_extract(value_json,'$') FROM settings WHERE key='access_token_hash'),0,
                        COALESCE((SELECT value_json='true' FROM settings WHERE key='user_has_chosen'),0),
                        strftime('%Y-%m-%dT%H:%M:%f+00:00','now'));
                    DELETE FROM settings WHERE key IN ('user_has_chosen','access_token_hash');
                    ALTER TABLE sessions ADD COLUMN user_id TEXT REFERENCES users(id);
                    UPDATE sessions SET user_id='default' WHERE role='user';
                    ALTER TABLE conversations ADD COLUMN user_id TEXT REFERENCES users(id);
                    UPDATE conversations SET user_id='default';
                    CREATE INDEX conversation_user ON conversations(user_id);
                    ALTER TABLE preferences ADD COLUMN user_id TEXT REFERENCES users(id);
                    UPDATE preferences SET user_id='default';
                    ALTER TABLE preference_proposals ADD COLUMN user_id TEXT REFERENCES users(id);
                    UPDATE preference_proposals SET user_id='default';
                    ALTER TABLE seen_jobs ADD COLUMN user_id TEXT REFERENCES users(id);
                    UPDATE seen_jobs SET user_id='default';
                    ALTER TABLE push_subscriptions ADD COLUMN user_id TEXT REFERENCES users(id);
                    UPDATE push_subscriptions SET user_id='default';
                    CREATE TABLE favorites_v6(
                        job_id TEXT NOT NULL REFERENCES jobs(id), user_id TEXT NOT NULL REFERENCES users(id),
                        created_at TEXT NOT NULL, PRIMARY KEY(user_id,job_id));
                    INSERT INTO favorites_v6 SELECT job_id,'default',created_at FROM favorites;
                    DROP TABLE favorites;
                    ALTER TABLE favorites_v6 RENAME TO favorites;
                    CREATE TABLE daily_jobs_v6(
                        user_id TEXT NOT NULL REFERENCES users(id), local_date TEXT NOT NULL,
                        job_id TEXT NOT NULL REFERENCES jobs(id), run_id TEXT NOT NULL,
                        is_initial INTEGER NOT NULL CHECK(is_initial IN (0,1)), read_at TEXT,
                        PRIMARY KEY(user_id,local_date,job_id));
                    INSERT INTO daily_jobs_v6 SELECT 'default',local_date,job_id,run_id,is_initial,read_at FROM daily_jobs;
                    DROP TABLE daily_jobs;
                    ALTER TABLE daily_jobs_v6 RENAME TO daily_jobs;
                    PRAGMA user_version = 6;
                    COMMIT;
                """)

            if version < 7 and target_version >= 7:
                # Rebuild only the two finite source checks; preserve IDs and referencing rows.
                db.executescript("""
                    PRAGMA foreign_keys = OFF;
                    BEGIN IMMEDIATE;
                    CREATE TABLE jobs_v7(
                        id TEXT PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('104','518','1111')),
                        source_job_id TEXT NOT NULL, payload_json TEXT NOT NULL, fetched_at TEXT NOT NULL,
                        UNIQUE(source,source_job_id));
                    INSERT INTO jobs_v7 SELECT * FROM jobs;
                    DROP TABLE jobs;
                    ALTER TABLE jobs_v7 RENAME TO jobs;
                    CREATE TABLE preference_baselines_v7(
                        preference_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
                        source TEXT NOT NULL CHECK(source IN ('104','518','1111')), completed_at TEXT NOT NULL,
                        PRIMARY KEY(preference_id,revision,source));
                    INSERT INTO preference_baselines_v7 SELECT * FROM preference_baselines;
                    DROP TABLE preference_baselines;
                    ALTER TABLE preference_baselines_v7 RENAME TO preference_baselines;
                    PRAGMA user_version = 7;
                    COMMIT;
                    PRAGMA foreign_keys = ON;
                """)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def get(db, key, default=None):
        row = db.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put(db, key, value):
        db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                   (key, encode(value)))

    def save_job(self, job: Job, db=None):
        """Internal source integration only; there is no public job-import endpoint."""
        if db is None:
            with self.connect(write=True) as connection:
                return self.save_job(job, connection)
        existing = db.execute("SELECT id FROM jobs WHERE source=? AND source_job_id=?",
                              (job.source, job.source_job_id)).fetchone()
        job_id = existing[0] if existing else str(uuid4())
        db.execute("""INSERT INTO jobs VALUES(?,?,?,?,?) ON CONFLICT(source,source_job_id)
                DO UPDATE SET payload_json=excluded.payload_json, fetched_at=excluded.fetched_at""",
                   (job_id, job.source, job.source_job_id, encode(job.model_dump(mode="json")), job.fetched_at.isoformat()))
        return job_id

    @staticmethod
    def require_session(db, auth_hash):
        if not db.execute('SELECT 1 FROM sessions WHERE id_hash=? AND expires_at>?', (auth_hash, now().isoformat())).fetchone():
            raise PermissionError('session_revoked')

    def start_chat(self, request_id, text, auth_hash=None, attachments=None, conversation_id='legacy',
                   edit_message_id=None, edit_request_id=None, user_id=None):
        with self.connect(write=True) as db:
            if auth_hash: self.require_session(db, auth_hash)
            # user_id=None is for internal callers only; every HTTP route passes the signed-in account.
            if not db.execute('SELECT 1 FROM conversations WHERE id=? AND (? IS NULL OR user_id=?)',
                              (conversation_id, user_id, user_id)).fetchone():
                raise LookupError('conversation_missing')
            user = {'text': text, 'events': [], 'status': 'completed', 'attachments': attachments or []}
            assistant = {'text': '', 'events': [], 'status': 'running'}
            if edit_message_id:
                original = db.execute("SELECT rowid AS seq,* FROM messages WHERE id=? AND role='user' AND conversation_id=?",
                                      (edit_message_id, conversation_id)).fetchone()
                if not original or not db.execute("SELECT 1 FROM messages WHERE request_id=? AND role='assistant'",
                                                 (original['request_id'],)).fetchone():
                    raise LookupError('message_missing')
                if original['request_id'] != edit_request_id:
                    raise ValueError('message_changed')
                if json.loads(original['content_json']).get('attachments') and not attachments:
                    raise ValueError('attachment_required')
                user['edited_at'] = now().isoformat()
                db.execute("UPDATE messages SET request_id=?,content_json=? WHERE id=?", (request_id, encode(user), edit_message_id))
                db.execute("UPDATE messages SET request_id=?,content_json=? WHERE request_id=? AND role='assistant'",
                           (request_id, encode(assistant), edit_request_id))
                # Later context summaries may describe the replaced text; drop them so the next long turn re-summarises.
                db.execute("""UPDATE messages SET content_json=json_remove(content_json,'$.context_summary')
                    WHERE conversation_id=? AND role='user' AND rowid>? AND json_type(content_json,'$.context_summary') IS NOT NULL""",
                           (conversation_id, original['seq']))
            else:
                for role, content in [('user', user), ('assistant', assistant)]:
                    db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                               (str(uuid4()), request_id, role, encode(content), now().isoformat(), conversation_id))
            db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (now().isoformat(), conversation_id))

    def save_reply(self, request_id, content, auth_hash=None):
        with self.connect(write=True) as db:
            if auth_hash: self.require_session(db, auth_hash)
            db.execute("UPDATE messages SET content_json=? WHERE request_id=? AND role='assistant'",
                       (encode(content), request_id))

    def interrupt_reply(self, request_id):
        with self.connect(write=True) as db:
            db.execute("UPDATE messages SET content_json=json_set(content_json,'$.status','interrupted') WHERE request_id=? AND role='assistant'", (request_id,))

    def recover_messages(self):
        with self.connect(write=True) as db:
            db.execute("""UPDATE messages SET content_json=json_set(content_json,'$.status','interrupted')
                WHERE role='assistant' AND json_extract(content_json,'$.status')='running'""")

    @staticmethod
    def search_snapshot(db, conversation_id, search_id):
        row = db.execute("""SELECT e.value FROM messages m, json_each(m.content_json,'$.events') e
            WHERE m.conversation_id=? AND m.role='assistant' AND json_extract(e.value,'$.event')='jobs'
            AND json_extract(e.value,'$.data.search_id')=? ORDER BY m.rowid DESC,CAST(e.key AS INTEGER) DESC LIMIT 1""",
            (conversation_id, search_id)).fetchone()
        if not row:
            raise LookupError('search_missing')
        snapshot = json.loads(row[0])['data']
        snapshot['dismissed_ids'] = Store.search_dismissed(db, snapshot)
        return snapshot

    @staticmethod
    def search_dismissed(db, snapshot):
        row = db.execute("SELECT content_json FROM messages WHERE request_id=? AND role='user'",
                         (snapshot.get('origin_request_id'),)).fetchone()
        return json.loads(row[0]).get('search_dismissed', {}).get(snapshot.get('search_id'), []) if row else []

    def dismiss_search_job(self, conversation_id, search_id, job_id, dismissed, auth_hash):
        with self.connect(write=True) as db:
            self.require_session(db, auth_hash)
            snapshot = self.search_snapshot(db, conversation_id, search_id)
            if job_id not in snapshot.get('candidate_ids', []):
                raise LookupError('job_missing')
            row = db.execute("SELECT id,content_json FROM messages WHERE request_id=? AND role='user' AND conversation_id=?",
                             (snapshot['origin_request_id'], conversation_id)).fetchone()
            if not row:
                raise LookupError('search_missing')
            content = json.loads(row['content_json'])
            ignored = set(snapshot['dismissed_ids'])
            ignored.add(job_id) if dismissed else ignored.discard(job_id)
            # User metadata is separate from the streaming assistant snapshot, so later deltas cannot overwrite it.
            content.setdefault('search_dismissed', {})[search_id] = sorted(ignored)
            db.execute('UPDATE messages SET content_json=? WHERE id=?', (encode(content), row['id']))
            return {'search_id': search_id, 'dismissed_ids': sorted(ignored)}

    @staticmethod
    def conversation_job_ids(db, conversation_id):
        rows = db.execute("""SELECT json_extract(j.value,'$.id') FROM messages m,
            json_each(m.content_json,'$.events') e,json_each(e.value,'$.data.items') j
            WHERE m.conversation_id=? AND json_extract(e.value,'$.event')='jobs'
            ORDER BY m.rowid,CAST(e.key AS INTEGER),CAST(j.key AS INTEGER)""", (conversation_id,)).fetchall()
        return list(dict.fromkeys(row[0] for row in rows if row[0]))

    @staticmethod
    def conversation_state(db, conversation_id):
        """Working-list state replayed from this conversation's saved events (the front end replays the same rules)."""
        rows = db.execute("""SELECT e.value FROM messages m, json_each(m.content_json,'$.events') e
            WHERE m.conversation_id=? AND m.role='assistant' AND json_extract(e.value,'$.event') IN ('jobs','recommendations')
            ORDER BY m.rowid,CAST(e.key AS INTEGER)""", (conversation_id,)).fetchall()
        companies, shortlist, removed, recommendations = [], [], {}, {}
        for row in rows:
            event = json.loads(row[0])
            data = event.get('data') or {}
            if event.get('event') == 'jobs':
                companies = list(dict.fromkeys([*companies, *data.get('excluded_companies', [])]))
                for job_id in data.get('excluded_job_ids', []):
                    removed[job_id] = {'job_id': job_id, 'by': 'company', 'reason': '公司已排除'}
            else:
                ids = [item.get('job_id') for item in data.get('items', []) if item.get('job_id')]
                # Events saved before `replace` existed always carried a non-empty replacement list.
                if data.get('replace', bool(ids)):
                    shortlist = list(dict.fromkeys(ids))
                    recommendations = {}
                    for item in data.get('items', []):
                        if not item.get('job_id'):
                            continue
                        recommendation = {'job_id': item['job_id'], **{
                            key: item[key] if isinstance(item.get(key), list) else []
                            for key in ('reasons', 'concerns', 'unknowns')}}
                        if isinstance(item.get('job'), dict):
                            recommendation['job'] = item['job']
                        recommendations[item['job_id']] = recommendation
                for item in data.get('removed', []):
                    removed[item['job_id']] = {'job_id': item['job_id'], 'by': 'agent', 'reason': item.get('reason', '')}
                for job_id in ids:
                    removed.pop(job_id, None)
        dismissed = []
        for (content,) in db.execute("""SELECT content_json FROM messages WHERE conversation_id=? AND role='user'
                AND json_extract(content_json,'$.search_dismissed') IS NOT NULL ORDER BY rowid""", (conversation_id,)):
            for ids in json.loads(content).get('search_dismissed', {}).values():
                dismissed.extend(ids)
        shortlist = [job_id for job_id in shortlist if job_id not in removed]
        return {'shortlist': shortlist, 'recommendations': [recommendations[job_id] for job_id in shortlist],
                'excluded_companies': companies, 'removed': list(removed.values()),
                'dismissed_ids': list(dict.fromkeys(dismissed))}

    def _refresh_search_events(self, db, events, user_id):
        """Saved jobs events as clients see them, with job and favorite reads shared across events."""
        jobs = [job for data in events for job in data.get('items', [])]
        ids = list(dict.fromkeys(job.get('id') for job in jobs if job.get('id')))
        current, favorites = {}, set()
        # Leave room for the user id under SQLite's legacy 999-parameter limit.
        for offset in range(0, len(ids), 900):
            batch = ids[offset:offset + 900]
            placeholders = ','.join('?' for _ in batch)
            current.update((row[0], json.loads(row[1])) for row in db.execute(
                f'SELECT id,payload_json FROM jobs WHERE id IN ({placeholders})', batch))
            favorites.update(row[0] for row in db.execute(
                f'SELECT job_id FROM favorites WHERE user_id=? AND job_id IN ({placeholders})', [user_id, *batch]))
        for data in events:
            data.pop('_continuation', None)
            if data.get('search_id'):
                data['dismissed_ids'] = self.search_dismissed(db, data)
        for job in jobs:
            job.update(current.get(job.get('id'), {}))
            job['is_favorite'] = job.get('id') in favorites
        return events

    def conversation_searches(self, db, conversation_id, user_id, limit=20):
        """The latest searches of the whole conversation, oldest first, so a client can rebuild its results panel
        however few messages it has loaded."""
        rows = db.execute("""SELECT e.value FROM messages m, json_each(m.content_json,'$.events') e
            WHERE m.conversation_id=? AND m.role='assistant' AND json_extract(e.value,'$.event')='jobs'
            AND json_extract(e.value,'$.data.search_id') IS NOT NULL
            ORDER BY m.rowid DESC,CAST(e.key AS INTEGER) DESC LIMIT ?""", (conversation_id, limit)).fetchall()
        return self._refresh_search_events(db, [json.loads(row[0])['data'] for row in reversed(rows)], user_id)

    def messages(self, cursor=None, limit=20, conversation_id=None, request_id=None, user_id=None):
        with self.connect() as db:
            condition = 'rowid<?'
            parameters = [cursor if cursor is not None else 9223372036854775807]
            if user_id is not None:
                condition += ' AND conversation_id IN (SELECT id FROM conversations WHERE user_id=?)'; parameters.append(user_id)
            if conversation_id is not None:
                condition += ' AND conversation_id=?'; parameters.append(conversation_id)
            if request_id is not None:
                condition += ' AND request_id=?'; parameters.append(request_id)
            rows = db.execute(f'SELECT rowid AS seq,* FROM messages WHERE {condition} ORDER BY rowid DESC LIMIT ?',
                              [*parameters, limit + 1]).fetchall()
            items = [dict(id=row['id'], request_id=row['request_id'], role=row['role'],
                          content=json.loads(row['content_json']), created_at=row['created_at'])
                     for row in reversed(rows[:limit])]
            searches = []
            for message in items:
                for event in message['content'].get('events', []):
                    if event.get('event') == 'jobs':
                        searches.append(event['data'])
                    elif event.get('event') == 'recommendations':
                        for recommendation in event['data'].get('items', []):
                            job = recommendation.get('job')
                            if job:
                                current = db.execute('SELECT payload_json FROM jobs WHERE id=?', (job['id'],)).fetchone()
                                if current:
                                    job.update(json.loads(current[0]))
                                job['is_favorite'] = bool(db.execute('SELECT 1 FROM favorites WHERE job_id=? AND user_id=?', (job['id'], user_id)).fetchone())
            self._refresh_search_events(db, searches, user_id)
            return {'items': items, 'next_cursor': str(rows[limit - 1]['seq']) if len(rows) > limit else None}
