"""One daily task for scheduled, catchup, manual and preference-baseline work."""
import asyncio
import json
import time
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import daily
from .sources import DailySearchInput, enabled_sources
from .source1111 import call_seconds, MAX_SEARCH_PAGES
from .security import DEFAULT_USER_ID
from .store import Criteria, encode, now

TAIPEI = ZoneInfo('Asia/Taipei')
# Shared across lock waits, startup and all pages for one preference/source.
DAILY_1111_SECONDS = 600


def daily_1111_seconds(criteria):
    return min(DAILY_1111_SECONDS, call_seconds(len(criteria.area_codes)) * MAX_SEARCH_PAGES)


def effective_preferences(store, db, user_id=None):
    """Conditions the daily search should run, for every enabled account (or just user_id)."""
    result = []
    accounts = db.execute('SELECT id,has_chosen FROM users WHERE disabled=0' + (' AND id=?' if user_id else '') + ' ORDER BY created_at,id',
                          (user_id,) if user_id else ()).fetchall()
    for account in accounts:
        if account['has_chosen']:
            result += [{'id': r['id'], 'revision': r['revision'], 'criteria': json.loads(r['criteria_json']), 'user_id': account['id']}
                       for r in db.execute('SELECT * FROM preferences WHERE enabled=1 AND user_id=? ORDER BY id', (account['id'],))]
        else:
            # Only the built-in account can still be on the admin's default conditions.
            revision = store.get(db, 'default_preferences_revision', 1)
            result += [{'id': f'default:{i}', 'revision': revision, 'criteria': value, 'user_id': account['id']}
                       for i, value in enumerate(store.get(db, 'default_preferences', []))]
    return result


def run_view(row):
    return dict(id=row['id'], status=row['status'], local_date=row['local_date'], trigger=row['trigger'],
                started_at=row['started_at'], finished_at=row['finished_at'], **json.loads(row['result_json']))


def user_run_view(row, user_id):
    """One round's result as one account sees it; other accounts' counts and sources stay private."""
    result = run_view(row)
    users = result.pop('users', None)
    empty = dict(new_count=0, initial_count=0, source_results=[])
    if users is None:  # recorded before accounts existed: it belongs to the built-in account
        return result if user_id == DEFAULT_USER_ID else result | empty
    result.pop('notifications', None)
    return result | empty | users.get(user_id, {})


class DailyRunner:
    def __init__(self, store, sources, *, automatic=True):
        self.store, self.sources = store, sources
        self.task = None
        self.pending_baseline = False
        self.closing = False
        self.clock = now
        self.scheduler = None
        self.automatic = automatic
        self.paused = False
        self.notify = None

    def recover(self):
        with self.store.connect(write=True) as db:
            db.execute("""UPDATE runs SET status='failed', finished_at=?,
                result_json=json_set(result_json,'$.interrupted',json('true'),'$.message',?)
                WHERE status='running'""", (self.clock().isoformat(), '服務重啟，這輪已中斷。'))

    async def open(self):
        self.recover()
        if not self.automatic:
            return
        self.scheduler = AsyncIOScheduler(timezone=TAIPEI)
        # The persisted clock is editable; check it every 30 seconds with one scheduler.
        self.scheduler.add_job(self.tick, 'interval', seconds=30, max_instances=1, coalesce=True)
        self.scheduler.start()
        await self.tick(catchup=True)

    async def close(self):
        self.closing = True
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        if self.task:
            self.task.cancel()
            await self.task

    async def tick(self, catchup=False):
        if self.task or self.closing or self.paused:
            return
        instant = self.clock().astimezone(TAIPEI)
        with self.store.connect() as db:
            preferences = effective_preferences(self.store, db)
            if not preferences:
                return
            enabled = enabled_sources(self.store.get(db, 'services', {}))
            time = self.store.get(db, 'schedule_time', '09:00')
            hour, minute = map(int, time.split(':'))
            due = instant.replace(hour=hour, minute=minute, second=0, microsecond=0)
            attempted = db.execute("""SELECT 1 FROM runs WHERE local_date=?
                AND trigger IN ('scheduled','catchup','manual') AND started_at>=? LIMIT 1""",
                (instant.date().isoformat(), due.astimezone(now().tzinfo).isoformat())).fetchone()
            baseline_attempted = db.execute("SELECT 1 FROM runs WHERE local_date=? LIMIT 1",
                                           (instant.date().isoformat(),)).fetchone()
            missing = any(not db.execute('SELECT 1 FROM preference_baselines WHERE preference_id=? AND revision=? AND source=?',
                         (p['id'], p['revision'], source)).fetchone()
                         for p in preferences for source in enabled)
        if instant >= due and not attempted:
            self.start('catchup' if catchup else 'scheduled')
        elif self.pending_baseline or (instant < due and missing and not baseline_attempted):
            self.request_baseline()

    def request_baseline(self):
        if not self.automatic:
            return
        self.pending_baseline = True
        if not self.task and not self.closing and not self.paused:
            self.start('baseline')

    def start(self, trigger, auth_hash=None):
        if self.task or self.closing or self.paused:
            raise RuntimeError('daily_busy')
        instant = self.clock()
        ident = str(uuid4())
        with self.store.connect(write=True) as db:
            if auth_hash and not db.execute("SELECT 1 FROM sessions WHERE id_hash=? AND role='admin' AND expires_at>?",
                                           (auth_hash, instant.isoformat())).fetchone():
                raise PermissionError('session_revoked')
            preferences = effective_preferences(self.store, db)
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)',
                       (ident, instant.astimezone(TAIPEI).date().isoformat(), trigger, 'running',
                        encode({'new_count': 0, 'initial_count': 0, 'source_results': []}), instant.isoformat(), None))
        self.pending_baseline = False
        self.task = asyncio.create_task(self.execute(ident, instant, preferences, auth_hash))
        return ident

    async def execute(self, ident, instant, preferences, auth_hash):
        batches = []
        local_date = instant.astimezone(TAIPEI).date().isoformat()
        try:
            with self.store.connect() as db:
                enabled = enabled_sources(self.store.get(db, 'services', {}))
            for pref in preferences:
                for source in enabled:
                    criteria = Criteria.model_validate(pref['criteria'])
                    result = await self.sources.search(
                        DailySearchInput(criteria=criteria, sources=[source]),
                        **({'deadline': time.monotonic() + daily_1111_seconds(criteria)} if source == '1111' else {}))
                    diag = result['source_results'][0]
                    batches.append(dict(preference_id=pref['id'], revision=pref['revision'], source=source, user_id=pref['user_id'],
                                        eligible=diag.get('baseline_eligible') is True, items=result['items'], diagnostic=diag))
            await asyncio.sleep(0)
            with self.store.connect(write=True) as db:
                if auth_hash and not db.execute("SELECT 1 FROM sessions WHERE id_hash=? AND role='admin' AND expires_at>?",
                                               (auth_hash, self.clock().isoformat())).fetchone():
                    raise PermissionError('session_revoked')
                current = {(p['id'], p['revision']): p['criteria'] for p in effective_preferences(self.store, db)}
                snapshots = {(p['id'], p['revision']): p['criteria'] for p in preferences}
                for batch in batches:
                    key = (batch['preference_id'], batch['revision'])
                    if current.get(key) != snapshots[key]:
                        batch['eligible'] = False
                        batch['diagnostic'] = batch['diagnostic'] | {'status': 'failed', 'message': '條件已變更，晚回结果未列入每日職缺。'}
                result = daily.commit_results(self.store, db, ident, local_date, batches, self.clock().isoformat())
                count = sum(b['eligible'] for b in batches)
                status = ('partial' if any(b['diagnostic']['status'] == 'partial' for b in batches) else 'success') if count == len(batches) else ('partial' if count else 'failed')
                if not preferences:
                    result.update(paused=True, message='沒有有效條件，每日搜尋已暫停。')
                db.execute('UPDATE runs SET status=?,result_json=?,finished_at=? WHERE id=?',
                           (status, encode(result), self.clock().isoformat(), ident))
                cutoff = (self.clock() - timedelta(days=30)).isoformat()
                db.execute('DELETE FROM push_attempts WHERE attempted_at<?', (cutoff,))
                db.execute("DELETE FROM runs WHERE started_at<? AND status!='running'", (cutoff,))
                db.execute('DELETE FROM review_cache WHERE expires_at<?', (self.clock().isoformat(),))
            if self.notify and result['new_count'] and not self.paused:
                try:
                    outcomes = []
                    # Each account is told only its own new count, on its own devices.
                    for user_id, mine in result.get('users', {}).items():
                        if mine['new_count'] and not self.paused:
                            outcomes += await self.notify(ident, mine['new_count'], user_id=user_id)
                    result['notifications'] = {name: sum(item['result'] == name for item in outcomes)
                        for name in ('accepted','expired','failed','unknown','blocked')}
                    with self.store.connect(write=True) as db:
                        db.execute('UPDATE runs SET result_json=? WHERE id=?', (encode(result), ident))
                except (Exception, asyncio.CancelledError):
                    # Push failure cannot undo the already committed daily results.
                    pass
        except (Exception, asyncio.CancelledError):
            # Never persist raw source/transport exceptions or credentials.
            with self.store.connect(write=True) as db:
                db.execute("UPDATE runs SET status='failed',finished_at=?,result_json=? WHERE id=?",
                           (self.clock().isoformat(), encode({'new_count': 0, 'initial_count': 0, 'source_results': [],
                             'interrupted': True, 'message': '這輪未完成，已停止；可由後台明確重試。'}), ident))
        finally:
            self.task = None
            if self.pending_baseline and not self.closing:
                self.request_baseline()
