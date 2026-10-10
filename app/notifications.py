"""Persist one attempt before sending; never expose subscription secrets."""
import asyncio
import json
from uuid import uuid4

from . import push
from .store import now


def ensure_keys(store):
    with store.connect(write=True) as db:
        if not store.get(db, 'vapid_keys'):
            store.put(db, 'vapid_keys', push.generate_keys())


class Notifications:
    def __init__(self, store):
        self.store = store
        self.sender = push.send
        self.paused = False
        self.active = 0

    async def deliver(self, run_id, new_count, *, test=False, auth_hash=None, user_id=None):
        if self.paused or (not test and new_count <= 0):
            return []
        self.active += 1
        results = []
        try:
            with self.store.connect(write=True) as db:
                db.execute("DELETE FROM push_subscriptions WHERE NOT EXISTS (SELECT 1 FROM sessions WHERE role='user' AND sessions.device_id=push_subscriptions.device_id AND expires_at>?)", (now().isoformat(),))
                keys = self.store.get(db, 'vapid_keys', {})
                subject = self.store.get(db, 'services', {}).get('vapid_subject', '')
                rows = [dict(row) for row in db.execute(
                    'SELECT * FROM push_subscriptions WHERE (? IS NULL OR user_id=?) ORDER BY id', (user_id, user_id))]
            if not keys or not subject:
                return []
            payload = {'title': '測試通知' if test else '今日新職缺',
                       'body': '通知連線測試；點開可回到今日職缺。' if test else f'今天找到 {new_count} 筆新職缺，想看看時再打開就好。',
                       'url': '/?page=daily'}
            for row in rows:
                if self.paused:
                    break
                with self.store.connect(write=True) as db:
                    if auth_hash and not db.execute("SELECT 1 FROM sessions WHERE id_hash=? AND role='admin' AND expires_at>?",
                                                   (auth_hash, now().isoformat())).fetchone():
                        break
                    current = db.execute('SELECT * FROM push_subscriptions WHERE id=?', (row['id'],)).fetchone()
                    valid_device = db.execute("SELECT 1 FROM sessions WHERE role='user' AND device_id=? AND expires_at>?",
                                              (row['device_id'], now().isoformat())).fetchone()
                    if not current or dict(current) != row or not valid_device:
                        continue
                    claimed = db.execute('INSERT OR IGNORE INTO push_attempts VALUES(?,?,?,?)',
                                         (run_id, row['id'], now().isoformat(), 'unknown')).rowcount
                    if not claimed:
                        continue
                operation = asyncio.create_task(asyncio.to_thread(self.sender,
                    {'endpoint': row['endpoint'], 'keys': json.loads(row['keys_json'])}, keys['private_key'], subject, payload))
                cancelled = False
                while True:
                    try:
                        outcome = await asyncio.shield(operation)
                        break
                    except asyncio.CancelledError:
                        cancelled = True
                        # Even repeated cancellation must wait for the bounded HTTP thread.
                        if operation.cancelled():
                            outcome = {'result': 'unknown'}
                            break
                    except Exception:
                        outcome = {'result': 'unknown'}
                        break
                result = outcome.get('result', 'unknown')
                if result not in ('accepted', 'expired', 'failed', 'unknown', 'blocked'):
                    result = 'unknown'
                with self.store.connect(write=True) as db:
                    current = db.execute('SELECT * FROM push_subscriptions WHERE id=?', (row['id'],)).fetchone()
                    if current and dict(current) == row:
                        db.execute('UPDATE push_attempts SET result=? WHERE run_id=? AND subscription_id=?',
                                   (result, run_id, row['id']))
                        if result == 'expired':
                            db.execute('DELETE FROM push_subscriptions WHERE id=?', (row['id'],))
                results.append({'subscription_id': row['id'], 'result': result})
                if cancelled:
                    raise asyncio.CancelledError()
            return results
        finally:
            self.active -= 1

    async def test(self, auth_hash):
        return await self.deliver('test:' + uuid4().hex, 0, test=True, auth_hash=auth_hash)
