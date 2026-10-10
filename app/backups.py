"""Consistent snapshots and compensated replacement; caller pauses all writers."""
import hashlib
import base64
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4
from contextlib import closing

from .store import encode


def validate_settings(path, settings_model, defaults_model, schedule_model):
    """Validate settings that would otherwise prevent login or scheduler startup."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            values = {key: json.loads(value) for key, value in db.execute('SELECT * FROM settings')}
            settings_model.model_validate(values.get('services', {}))
            defaults_model.model_validate({'preferences': values.get('default_preferences', [])})
            schedule_model.model_validate({'time': values.get('schedule_time', '09:00')})
            if 'user_has_chosen' in values and type(values['user_has_chosen']) is not bool:
                raise ValueError()
            for key in ('llm_api_key', 'tavily_api_key', 'brave_api_key', 'access_token_hash'):
                if key in values and not isinstance(values[key], str):
                    raise ValueError()
            if 'admin_password_hash' in values:
                algorithm, salt, derived = values['admin_password_hash'].split('$')
                if algorithm != 'scrypt' or len(bytes.fromhex(salt)) != 16 or len(bytes.fromhex(derived)) != 64:
                    raise ValueError()
            keys = values.get('vapid_keys')
            if keys:
                private = serialization.load_der_private_key(base64.urlsafe_b64decode(keys['private_key'] + '==='), password=None)
                if not isinstance(private, ec.EllipticCurvePrivateKey) or not isinstance(private.curve, ec.SECP256R1):
                    raise ValueError()
                expected = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
                if expected != base64.urlsafe_b64decode(keys['public_key'] + '==='):
                    raise ValueError()
    except Exception:
        raise ValueError('備份中的服務、排程或登入設定無效。') from None


def schema_catalog(db):
    return [tuple(row) for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]


def create_backup(store):
    from .backup_verify import MAX_ARCHIVE_BYTES, MAX_TOTAL_BYTES
    root = store.path.parent
    if store.path.stat().st_size > MAX_TOTAL_BYTES:
        raise ValueError('資料超過第一版備份大小上限，備份未建立。')
    destination = root / 'backups'
    destination.mkdir(exist_ok=True)
    ident = uuid4().hex
    path = destination / (ident + '.zip')
    partial = destination / (ident + '.partial')
    try:
        with tempfile.TemporaryDirectory(prefix='job-companion-backup-', dir=root) as temporary:
            staging = Path(temporary)
            snapshot = staging / 'app.db'
            with store.connect() as source, closing(sqlite3.connect(snapshot)) as target:
                source.backup(target)
                target.execute('DELETE FROM sessions')
                target.execute('DELETE FROM login_attempts')
                target.execute('DELETE FROM push_subscriptions')
                target.execute('DELETE FROM push_attempts')
                version = target.execute('PRAGMA user_version').fetchone()[0]
                sprite_paths = [row[0] for row in target.execute('SELECT path FROM sprites')]
                target.commit()
            entries = [('app.db', snapshot)]
            for name in sprite_paths:
                if Path(name).name != name or '/' in name or '\\' in name:
                    raise ValueError('素材路徑不符，備份未建立。')
                source = root / 'sprites' / name
                if source.is_symlink() or not source.is_file():
                    raise ValueError('素材檔案缺失，備份未建立。')
                entries.append(('sprites/' + name, source))
            manifest = {'format_version': 1, 'schema_version': version, 'files': [
                {'path': name, 'size': file.stat().st_size, 'sha256': file_hash(file)} for name, file in entries]}
            with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', encode(manifest))
                for name, file in entries:
                    archive.write(file, name)
            if partial.stat().st_size > MAX_ARCHIVE_BYTES or sum(e['size'] for e in manifest['files']) > MAX_TOTAL_BYTES:
                raise ValueError('資料超過第一版備份大小上限，備份未建立。')
            os.replace(partial, path)
        return {'id': ident, 'size': path.stat().st_size}
    finally:
        partial.unlink(missing_ok=True)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class RestoreError(Exception):
    def __init__(self, backup_id, recovery_required=False):
        self.backup_id, self.recovery_required = backup_id, recovery_required


def restore_backup(store, staging, *, checkpoint=lambda stage: None):
    """Use a verified staging folder; retain backups and roll back both resources."""
    staging = Path(staging)
    pre_restore = create_backup(store)['id']
    root = store.path.parent
    live_sprites = root / 'sprites'
    previous = Path(tempfile.mkdtemp(prefix='job-companion-recovery-', dir=root))
    db_moved = new_db = sprites_moved = new_sprites = False
    recoverable = True
    try:
        checkpoint('backup_created')
        (staging / 'sprites').mkdir(exist_ok=True)
        os.replace(store.path, previous / 'app.db')
        db_moved = True
        os.replace(staging / 'app.db', store.path)
        new_db = True
        checkpoint('database_replaced')
        if live_sprites.exists():
            os.replace(live_sprites, previous / 'sprites')
            sprites_moved = True
        os.replace(staging / 'sprites', live_sprites)
        new_sprites = True
        checkpoint('sprites_replaced')
        with store.connect(write=True) as db:
            checkpoint('before_session_revoke')
            db.execute('DELETE FROM sessions')
            db.execute('DELETE FROM login_attempts')
            # Session-bound device ids are never carried into the restored installation.
            db.execute('DELETE FROM push_subscriptions')
            db.execute('DELETE FROM push_attempts')
            db.execute("UPDATE runs SET status='failed',finished_at=NULL,result_json=json_set(result_json,'$.interrupted',json('true')) WHERE status='running'")
            db.execute("UPDATE messages SET content_json=json_set(content_json,'$.status','interrupted') WHERE role='assistant' AND json_extract(content_json,'$.status')='running'")
            checkpoint('after_session_revoke')
        checkpoint('restored')
    except BaseException as exc:
        try:
            if new_sprites:
                os.replace(live_sprites, previous / 'rejected-sprites')
            if sprites_moved:
                os.replace(previous / 'sprites', live_sprites)
            if new_db:
                os.replace(store.path, previous / 'rejected.db')
            if db_moved:
                os.replace(previous / 'app.db', store.path)
        except OSError:
            recoverable = False
        raise RestoreError(pre_restore, not recoverable) from exc
    finally:
        if recoverable:
            # Cleanup is best effort; it cannot invalidate a completed restore or rollback.
            shutil.rmtree(previous, ignore_errors=True)
    return {'restored': True, 'pre_restore_backup_id': pre_restore, 'login_required': True, 'resubscribe_required': True}
