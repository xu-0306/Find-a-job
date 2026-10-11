from base64 import b64encode
import asyncio
import json
import secrets
import sqlite3
import os
import ipaddress
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError, field_validator, model_validator
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser

from . import security
from .store import Criteria, Input, Job, Store, encode, now
from . import chat, llm, sources, reviews, backups, notifications, push, resume_pdf, chat_files, web
from .scheduler import DailyRunner, TAIPEI, effective_preferences, run_view, user_run_view
from .sprite_validation import STATES, MAX_FILE_BYTES, TEMPLATE_COLUMNS, validate_png, split_template
from .sharing import Sharing
from .ports import Ports

STATIC = Path(__file__).parent / "static"
SECRET_FIELDS = ("llm_api_key", "brave_api_key", "tavily_api_key")
DEFAULT_SETTINGS = {"llm_base_url": "", "llm_model": "", "llm_protocol": "auto", "admin_local_only": True, "source_518_enabled": True, "source_1111_enabled": False, "vapid_subject": ""}


async def finish_file_operation(function, *args, **kwargs):
    """Keep maintenance/staging alive until its filesystem thread has stopped."""
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(operation)
            break
        except asyncio.CancelledError:
            cancelled = True
            if operation.cancelled():
                raise
    if cancelled:
        raise asyncio.CancelledError()
    return result


class Login(Input):
    # Passwords are exact text, including intentional whitespace.
    model_config = {"extra": "forbid", "str_strip_whitespace": False}
    password: str = Field(min_length=4)


USERNAME_PATTERN = r'^[A-Za-z0-9_.-]{3,32}$'


class UserCreate(Input):
    model_config = {"extra": "forbid", "str_strip_whitespace": False}
    username: str = Field(pattern=USERNAME_PATTERN)


class UserUpdate(Input):
    model_config = {"extra": "forbid", "str_strip_whitespace": False}
    username: str | None = Field(default=None, pattern=USERNAME_PATTERN)
    disabled: bool | None = None


class SharingAction(Input):
    action: Literal['install', 'login', 'enable', 'disable', 'check']


class Settings(Input):
    llm_base_url: str | None = Field(default=None, max_length=2000)
    llm_model: str | None = Field(default=None, max_length=200)
    llm_protocol: Literal["auto", "responses", "chat_completions", "anthropic_messages"] | None = None
    admin_local_only: bool | None = None
    source_518_enabled: bool | None = None
    source_1111_enabled: bool | None = None
    vapid_subject: str | None = Field(default=None, max_length=2000)
    llm_api_key: str = Field(default="", max_length=4096)
    brave_api_key: str = Field(default="", max_length=4096)
    tavily_api_key: str = Field(default="", max_length=4096)
    search_provider: Literal['auto', 'Brave', 'Tavily'] | None = None
    clear_secrets: list[Literal["llm_api_key", "brave_api_key", "tavily_api_key"]] = Field(default_factory=list)

    @field_validator("llm_base_url")
    @classmethod
    def valid_url(cls, value):
        if value:
            try:
                parsed = urlsplit(value)
                valid = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
                parsed.port
            except ValueError:
                valid = False
            if not valid or any(char.isspace() for char in value):
                raise ValueError("需為不含帳密的 HTTP 或 HTTPS 網址")
        return value

    @model_validator(mode="after")
    def no_conflicting_secret(self):
        if any(getattr(self, key) for key in self.clear_secrets):
            raise ValueError("同一金鑰不能同時設定與清除")
        return self

    @field_validator('vapid_subject')
    @classmethod
    def valid_subject(cls, value):
        if value and not push.valid_subject(value):
            raise ValueError('推播聯絡 URI 須為 mailto 或 HTTPS')
        return value


class Defaults(Input):
    preferences: list[Criteria] = Field(max_length=20)


class Schedule(Input):
    time: str = Field(pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')


class Quotes(Input):
    texts: list[str] = Field(max_length=100)

    @field_validator('texts')
    @classmethod
    def valid_texts(cls, values):
        if any(not text.strip() or len(text) > 500 for text in values):
            raise ValueError('語錄須為1～500字純文字')
        return [text.strip() for text in values]


class PushSubscription(Input):
    endpoint: str = Field(max_length=2048)
    keys: dict[str, str]
    expirationTime: int | None = None


class ChatAction(Input):
    type: Literal['continue_search']
    search_id: str = Field(min_length=1, max_length=100)


class ChatInput(Input):
    text: str = Field(min_length=1, max_length=10000)
    request_id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    conversation_id: str = Field(default='legacy', min_length=1, max_length=100)
    edit_message_id: str | None = Field(default=None, min_length=1, max_length=100)
    edit_request_id: str | None = Field(default=None, min_length=1, max_length=100)
    action: ChatAction | None = None


class SearchDismiss(Input):
    conversation_id: str = Field(min_length=1, max_length=100)
    job_id: str = Field(min_length=1, max_length=100)
    dismissed: bool


class Recommendation(Input):
    job_id: str = Field(min_length=1, max_length=100)
    reasons: list[str] = Field(default_factory=list, max_length=5)
    concerns: list[str] = Field(default_factory=list, max_length=5)
    unknowns: list[str] = Field(default_factory=list, max_length=5)

    @field_validator('reasons', 'concerns', 'unknowns')
    @classmethod
    def bounded_reasons(cls, values):
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError('說明須為1～500字')
        return values


class Recommendations(Input):
    items: list[Recommendation] = Field(min_length=1, max_length=20)


class AreaLookup(Input):
    query: str = Field(min_length=1, max_length=100)


class ConversationTitle(Input):
    title: str = Field(min_length=1, max_length=80)


class ConversationInput(ConversationTitle):
    title: str = Field(default='新對話', min_length=1, max_length=80)


class ProposalInput(Input):
    operation: Literal["create", "update", "delete"]
    preference_id: str | None = Field(default=None, max_length=100)
    base_revision: int | None = Field(default=None, ge=1)
    after: Criteria | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.operation == "create":
            if self.preference_id is not None or self.base_revision is not None or self.after is None:
                raise ValueError("新增需要條件，不接受既有版本")
        elif self.preference_id is None or self.base_revision is None:
            raise ValueError("修改與刪除需要條件 ID 和版本")
        if (self.operation == "delete") != (self.after is None):
            raise ValueError("刪除不得帶新條件；新增或修改需帶新條件")
        return self


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


async def read_chat_request(request):
    """Keep the existing JSON and multipart contracts for one transient attachment."""
    content_type = request.headers.get('content-type', '').split(';')[0].lower()
    limit = resume_pdf.MAX_BYTES + 65536 if content_type == 'multipart/form-data' else 65536

    async def bounded_stream():
        total = 0
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise MultiPartException('上傳內容過大，附件最多 20 MiB。')
                yield chunk

    try:
        if content_type == 'application/json':
            raw = b''.join([chunk async for chunk in bounded_stream()])
            return ChatInput.model_validate(json.loads(raw)), None
        if content_type != 'multipart/form-data':
            raise APIError(415, 'invalid_content_type', '請送出文字訊息或附上一份檔案。')
        parser = MultiPartParser(request.headers, bounded_stream(), max_files=1, max_fields=5, max_part_size=40000)
        # Private attachment bytes stay in memory, rather than spooling to disk.
        parser.spool_max_size = limit
        form = await parser.parse()
        try:
            fields = set(form.keys())
            if not {'text', 'request_id', 'resume'} <= fields or not fields <= {'text', 'request_id', 'resume', 'conversation_id', 'edit_message_id', 'edit_request_id'} or len(form.multi_items()) != len(fields):
                raise APIError(422, 'invalid_input', '請送出文字訊息與一份附件。')
            body = ChatInput.model_validate({key: form[key] for key in fields if key != 'resume'})
            file = form['resume']
            if not hasattr(file, 'read') or not file.filename:
                raise APIError(422, 'invalid_resume', '請附上一份 PDF、圖片、Markdown 或文字檔。')
            filename = file.filename.replace('\\', '/').split('/')[-1]
            extension = Path(filename).suffix.lower()
            if extension not in chat_files.EXTENSIONS:
                raise APIError(422, 'invalid_resume', '支援 PDF、PNG／JPEG／WebP、Markdown 與 TXT。')
            data = await file.read(resume_pdf.MAX_BYTES + 1)
            if not data or len(data) > resume_pdf.MAX_BYTES:
                raise APIError(422, 'invalid_resume', '附件大小須介於 1 byte 與 20 MiB。')
            filename = ''.join(c for c in filename if c.isprintable())[:120]
            return body, {'data': data, 'filename': filename, 'extension': extension}
        finally:
            await form.close()
    except (ValidationError, ValueError, UnicodeError):
        raise APIError(422, 'invalid_input', '欄位格式不符，請檢查輸入。') from None
    except MultiPartException:
        raise APIError(422, 'invalid_resume', '上傳格式不符或內容過大；請只附上一份最多 20 MiB 的檔案。') from None
    except TimeoutError:
        raise APIError(408, 'upload_timeout', '上傳等候時間已到，請稍後重試。') from None


def error(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message, "retryable": status in (409, 429, 503)}}, status_code=status)


def settings_view(store, db):
    settings = DEFAULT_SETTINGS | store.get(db, "services", {})
    for key in SECRET_FIELDS:
        settings[key] = {"configured": bool(store.get(db, key)), "mask": "••••••••" if store.get(db, key) else ""}
    settings["search_provider"] = store.get(db, 'search_provider', 'auto')
    settings['active_search_provider'] = reviews.active_provider(store, db)
    search_test = store.get(db, 'search_test')
    settings['search_connection_test'] = search_test if search_test and search_test.get('provider') == settings['active_search_provider'] else None
    test = store.get(db, 'llm_test', {})
    current = test.get('signature') == llm.signature(settings, store.get(db, 'llm_api_key', ''))
    settings['connection_test_status'] = 'passed' if current and test.get('tools') and test.get('reply') else 'pending'
    settings['detected_protocol'] = test.get('protocol') if current else None
    return settings


def preferences_view(store, db, user_id):
    items = [{"id": row["id"], "revision": row["revision"], "criteria": json.loads(row["criteria_json"]),
              "enabled": bool(row["enabled"]), "is_default": False}
             for row in db.execute("SELECT * FROM preferences WHERE user_id=? ORDER BY id", (user_id,))]
    chosen = user_has_chosen(db, user_id)
    effective = items if chosen else [
        {"id": None, "revision": None, "criteria": value, "enabled": True, "is_default": True}
        for value in store.get(db, "default_preferences", [])]
    pending = [proposal_view(row) for row in db.execute(
        "SELECT * FROM preference_proposals WHERE status='pending' AND user_id=? ORDER BY rowid", (user_id,))]
    return {"items": items, "effective": effective, "proposals": pending,
            "user_has_chosen": chosen, "paused": not any(item["enabled"] for item in effective)}


def user_has_chosen(db, user_id):
    row = db.execute("SELECT has_chosen FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row[0]) if row else True


def proposal_view(row):
    result = dict(row)
    result.pop("user_id", None)
    result["before"] = json.loads(result.pop("before_json")) if row["before_json"] else None
    result["after"] = json.loads(result.pop("after_json")) if row["after_json"] else None
    return result


def create_app(data_dir=None, *, automatic_daily=True, native_sharing=False, admin_port=None):
    public_origin = os.environ.get('JOB_COMPANION_PUBLIC_ORIGIN', '').strip()
    public_hosts = set()
    if public_origin:
        origin = urlsplit(public_origin)
        if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
            or origin.path or origin.query or origin.fragment or origin.port == 0
            or any(char.isspace() for char in public_origin)):
            raise ValueError('JOB_COMPANION_PUBLIC_ORIGIN 須為不含路徑的 HTTPS 網站 origin。')
        host = '['+origin.hostname.lower()+']' if ':' in origin.hostname else origin.hostname.lower()
        public_hosts = {host, host+':443'} if origin.port in (None,443) else {host+':'+str(origin.port)}
    local_peers = tuple(str(ipaddress.ip_address(value.strip())) for value in
                        os.environ.get('JOB_COMPANION_LOCAL_PEERS', '').split(',') if value.strip())
    store = Store(data_dir)

    @asynccontextmanager
    async def lifespan(app):
        store.recover_messages()
        notifications.ensure_keys(store)
        await app.state.daily.open()
        await app.state.sharing.open()
        yield
        await app.state.sharing.close()
        await app.state.daily.close()
        for active in list(app.state.active_chats.values()):
            active['task'].cancel()
            await active['task']

    app = FastAPI(title="求職陪伴小幫手", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.public_origin = public_origin
    app.state.sharing = Sharing(app, native=native_sharing, admin_port=admin_port)
    app.state.active_chats = {}  # one running reply per user: user_id -> {request_id, conversation_id, task, auth_hash}
    app.state.resume_lock = asyncio.Lock()
    app.state.llm_transport = None  # httpx transport injection for offline checks
    app.state.llm_test_busy = False
    app.state.sources = sources.Sources()
    app.state.daily = DailyRunner(store, app.state.sources, automatic=automatic_daily)
    app.state.reviews = reviews.Reviews(store, app.state.sources)
    app.state.web_reader = web.WebReader()
    app.state.comparison_busy = False
    app.state.search_test_busy = False
    app.state.maintenance = False
    app.state.recovery_required = False
    app.state.active_requests = 0
    app.state.notifications = notifications.Notifications(store)
    app.state.daily.notify = app.state.notifications.deliver
    app.state.restore_checkpoint = lambda stage: None

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def input_error(request, exc):
        # Pydantic's input/error context may contain submitted passwords or API keys.
        return error(422, "invalid_input", "欄位格式不符，請檢查輸入。")

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(exc.status_code, "http_error", "找不到此項目。" if exc.status_code == 404 else "無法處理請求。")

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request, exc):
        return error(503, "storage_unavailable", "資料暫時無法保存，請稍後再試。")

    @app.exception_handler(OSError)
    async def file_error(request, exc):
        return error(503, 'storage_unavailable', '檔案暫時無法保存，原資料仍保留。')

    @app.middleware("http")
    async def protect(request, call_next):
        # Explicit deployment origin decides cookie security; forwarded headers never decide peers.
        sharing_host = urlsplit(app.state.sharing.origin).netloc if app.state.sharing.origin else None
        if request.headers.get('host', '').lower() in public_hosts | ({sharing_host} if sharing_host else set()):
            request.scope['scheme'] = 'https'
        path = request.url.path
        is_admin = path == "/admin" or path.startswith("/admin/") or path == "/api/admin" or path.startswith("/api/admin/")
        if app.state.maintenance:
            response = error(409, 'maintenance_busy', '資料維護正在進行，請稍後重新操作。')
        else:
            app.state.active_requests += 1
            try:
                with store.connect() as db:
                    only_local = (DEFAULT_SETTINGS | store.get(db, "services", {}))["admin_local_only"]
                if is_admin and only_local and not security.local_request(request, local_peers):
                    response = error(403, "local_only", "後台目前只允許本機存取。")
                elif request.method not in ("GET", "HEAD", "OPTIONS") and not security.origin_matches(request):
                    response = error(403, "origin_rejected", "請由同一個網站送出操作。")
                else:
                    response = await call_next(request)
            finally:
                app.state.active_requests -= 1
        response.headers.update({
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "X-Frame-Options": "DENY",
        })
        return response

    def session(role):
        def authenticate(request: Request, response: Response):
            secure = request.url.scheme == "https"
            token = request.cookies.get(security.cookie_name(role, secure), "")
            with store.connect(write=True) as db:
                row = db.execute("""SELECT s.*, u.disabled AS user_disabled FROM sessions s LEFT JOIN users u ON u.id=s.user_id
                    WHERE s.id_hash=? AND s.role=?""", (security.digest(token), role)).fetchone()
                if not row or datetime.fromisoformat(row["expires_at"]) <= now() or (
                        role == 'user' and (row['user_id'] is None or row['user_disabled'] is None or row['user_disabled'])):
                    if row:
                        if role == 'user':
                            db.execute('DELETE FROM push_subscriptions WHERE device_id=?', (row['device_id'],))
                        db.execute("DELETE FROM sessions WHERE id_hash=?", (row["id_hash"],))
                    raise APIError(401, "login_required", "請先登入。")
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    if not security.csrf_matches(request.headers.get("x-csrf-token", ""), token):
                        raise APIError(403, "csrf_rejected", "頁面驗證已失效，請重新整理。")
                lifetime = timedelta(days=30) if role == "user" else timedelta(hours=8)
                expiry = now() + lifetime
                db.execute("UPDATE sessions SET expires_at=? WHERE id_hash=?", (expiry.isoformat(), row["id_hash"]))
            response.set_cookie(security.cookie_name(role, secure), token, httponly=True, secure=secure,
                                samesite="lax", path="/", max_age=int(lifetime.total_seconds()))
            return {"hash": row["id_hash"], "csrf_token": security.csrf(token), "device_id": row["device_id"], "user_id": row["user_id"]}
        return authenticate

    user = session("user")
    admin = session("admin")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(STATIC / "index.html")

    @app.get('/sw.js', include_in_schema=False)
    def service_worker():
        return FileResponse(STATIC / 'sw.js', media_type='application/javascript', headers={'Service-Worker-Allowed': '/'})

    @app.get("/admin", include_in_schema=False)
    def admin_page():
        # The public shell contains no configuration; every data endpoint authenticates.
        return FileResponse(STATIC / "admin.html")

    @app.get("/access")
    def access(request: Request, token: str = Query(min_length=40, max_length=100)):
        response = RedirectResponse("/", status_code=303)
        with store.connect(write=True) as db:
            # Every account owns one random link; a missing hash (no link yet) can never match.
            account = db.execute("SELECT id FROM users WHERE access_token_hash=? AND disabled=0",
                                 (security.digest(token),)).fetchone()
            if not account:
                raise APIError(401, "invalid_link", "專屬連結已失效，請向管理者取得新連結。")
            old = request.cookies.get(security.cookie_name("user", request.url.scheme == "https"), "")
            db.execute("DELETE FROM sessions WHERE id_hash=? AND role='user'", (security.digest(old),))
            security.new_session(db, response, "user", request.url.scheme == "https", account["id"])
        return response

    @app.get("/api/admin/login-context")
    def login_context(request: Request):
        token = secrets.token_urlsafe(32)
        with store.connect() as db:
            configured = bool(store.get(db, "admin_password_hash"))
        response = JSONResponse({"csrf_token": security.csrf(token), "initialized": configured})
        response.set_cookie(security.cookie_name("login", request.url.scheme == "https"), token,
                            httponly=True, secure=request.url.scheme == "https", samesite="lax", max_age=600, path="/")
        return response

    @app.post("/api/admin/login")
    def login(body: Login, request: Request):
        secure = request.url.scheme == "https"
        challenge = request.cookies.get(security.cookie_name("login", secure), "")
        if not security.csrf_matches(request.headers.get("x-csrf-token", ""), challenge):
            raise APIError(403, "csrf_rejected", "請重新載入登入頁。")
        peer = request.client.host if request.client else "unknown"
        with store.connect(write=True) as db:
            attempt = db.execute("SELECT * FROM login_attempts WHERE peer=?", (peer,)).fetchone()
            stored = store.get(db, "admin_password_hash")
            if not stored:
                return error(503, "admin_not_initialized", "請先在本機執行 python -m app init-admin。")
            if attempt and attempt["blocked_until"] and datetime.fromisoformat(attempt["blocked_until"]) > now():
                return error(429, "login_cooldown", "登入失敗次數過多，請在 15 分鐘後再試。")
            if not security.password_matches(body.password, stored):
                failures = attempt["failures"] + 1 if attempt and not attempt["blocked_until"] else 1
                blocked = (now() + timedelta(minutes=15)).isoformat() if failures >= 5 else None
                db.execute("INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)", (peer, failures, blocked))
                return error(429 if blocked else 401, "login_failed", "密碼不正確。" if not blocked else "請在 15 分鐘後再試。")
            db.execute("DELETE FROM login_attempts WHERE peer=?", (peer,))
            old = request.cookies.get(security.cookie_name("admin", secure), "")
            db.execute("DELETE FROM sessions WHERE id_hash=? AND role='admin'", (security.digest(old),))
            response = Response()
            csrf_token = security.new_session(db, response, "admin", secure)
            response.delete_cookie(security.cookie_name("login", secure), path="/", secure=secure, httponly=True, samesite="lax")
            result = JSONResponse({"ok": True, "csrf_token": csrf_token})
            result.raw_headers.extend(header for header in response.raw_headers if header[0] == b"set-cookie")
            return result

    @app.get("/api/admin/session")
    def admin_session(auth=Depends(admin)):
        return {"csrf_token": auth["csrf_token"]}

    @app.get('/api/admin/sharing')
    async def sharing_status(request: Request, auth=Depends(admin)):
        if not security.local_request(request):
            raise APIError(403, 'local_only', '外部連線設定只允許在本機管理。')
        return await app.state.sharing.status()

    @app.get('/api/admin/ports')
    def ports_status(request: Request, auth=Depends(admin)):
        if not security.local_request(request):
            raise APIError(403, 'local_only', '連接埠設定只允許在本機管理。')
        return app.state.sharing.ports_view()

    @app.put('/api/admin/ports')
    async def ports_save(body: Ports, request: Request, auth=Depends(admin)):
        if not security.local_request(request):
            raise APIError(403, 'local_only', '連接埠設定只允許在本機管理。')
        with store.connect() as db:
            ensure_active(db, auth['hash'])
        try:
            return await app.state.sharing.save_ports(body.model_dump())
        except ValueError as error:
            raise APIError(409, 'ports_conflict', str(error)) from error
        except OSError as error:
            raise APIError(500, 'ports_save_failed', '連接埠設定無法保存，請檢查資料目錄權限後重試。') from error

    @app.post('/api/admin/sharing')
    async def sharing_action(body: SharingAction, request: Request, auth=Depends(admin)):
        if not security.local_request(request):
            raise APIError(403, 'local_only', '外部連線設定只允許在本機管理。')
        with store.connect() as db:
            ensure_active(db, auth['hash'])
        return await app.state.sharing.action(body.action)

    async def logout(request, role, auth):
        app.state.notifications.paused = True
        try:
            while app.state.notifications.active:
                await asyncio.sleep(.05)
            with store.connect(write=True) as db:
                if role == 'user':
                    db.execute('DELETE FROM push_subscriptions WHERE device_id=?', (auth['device_id'],))
                db.execute("DELETE FROM sessions WHERE id_hash=?", (auth["hash"],))
        finally:
            app.state.notifications.paused = app.state.maintenance
        active = app.state.active_chats.get(auth['user_id']) if role == 'user' else None
        if active and active['auth_hash'] == auth['hash']:
            task = active['task']
            task.get_loop().call_soon_threadsafe(task.cancel)
        response = Response(status_code=204)
        response.delete_cookie(security.cookie_name(role, request.url.scheme == "https"), path="/",
                               secure=request.url.scheme == "https", httponly=True, samesite="lax")
        return response

    @app.post("/api/logout")
    async def user_logout(request: Request, auth=Depends(user)):
        return await logout(request, "user", auth)

    @app.post("/api/admin/logout")
    async def admin_logout(request: Request, auth=Depends(admin)):
        return await logout(request, "admin", auth)

    @app.get("/api/admin/settings")
    def get_settings(auth=Depends(admin)):
        with store.connect() as db:
            return settings_view(store, db)

    @app.put("/api/admin/settings")
    def put_settings(body: Settings, auth=Depends(admin)):
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            previous_provider = reviews.active_provider(store, db)
            previous_search_key = store.get(db, (previous_provider or '').lower() + '_api_key', '')
            services = DEFAULT_SETTINGS | store.get(db, "services", {})
            for key in DEFAULT_SETTINGS:
                if getattr(body, key) is not None:
                    services[key] = getattr(body, key)
            store.put(db, "services", services)
            for key in SECRET_FIELDS:
                if key in body.clear_secrets:
                    db.execute("DELETE FROM settings WHERE key=?", (key,))
                elif getattr(body, key):
                    store.put(db, key, getattr(body, key))
            if any(getattr(body, key) or key in body.clear_secrets for key in ('brave_api_key', 'tavily_api_key')):
                db.execute("DELETE FROM settings WHERE key='search_comparison'")
            if 'search_provider' in body.model_fields_set:
                store.put(db, 'search_provider', body.search_provider)
            provider = reviews.active_provider(store, db)
            if (provider, store.get(db, (provider or '').lower() + '_api_key', '')) != (previous_provider, previous_search_key):
                db.execute("DELETE FROM settings WHERE key='search_test'")
            return settings_view(store, db)

    @app.post('/api/admin/search-test')
    async def search_test(auth=Depends(admin)):
        if app.state.search_test_busy:
            raise APIError(409, 'test_busy', '搜尋連線測試正在進行，請稍候。')
        with store.connect() as db:
            provider = reviews.active_provider(store, db)
            key = store.get(db, (provider or '').lower() + '_api_key', '')
        if provider not in ('Brave', 'Tavily') or not key:
            raise APIError(422, 'search_unconfigured', '請先選擇搜尋引擎、設定金鑰並保存服務設定。')
        app.state.search_test_busy = True
        started = time.monotonic()
        try:
            result = {'provider': provider, 'passed': False}
            try:
                async with asyncio.timeout(45):
                    parsed = await app.state.reviews.web_search(provider, key, '台灣 公司評價 工作心得')
                if parsed['status'] not in ('success', 'partial', 'empty'):
                    raise sources.SourceError('invalid_response', '搜尋回應格式無法使用，請稍後再試。')
                result.update(passed=True, result_count=len(parsed['evidence']))
            except sources.SourceError as exc:
                result['message'] = exc.message
            except TimeoutError:
                result['message'] = '搜尋連線等候時間已到，請稍後再試。'
            result.update(tested_at=now().isoformat(), duration_ms=round((time.monotonic() - started) * 1000))
            with store.connect(write=True) as db:
                ensure_active(db, auth['hash'])
                if reviews.active_provider(store, db) != provider or store.get(db, provider.lower() + '_api_key', '') != key:
                    raise APIError(409, 'settings_changed', '搜尋設定已變更，請重新測試。')
                store.put(db, 'search_test', result)
            if not result['passed']:
                raise APIError(503, 'search_test_failed', result['message'])
            return result
        finally:
            app.state.search_test_busy = False

    @app.post('/api/admin/llm-test')
    async def llm_test(auth=Depends(admin)):
        if app.state.llm_test_busy:
            raise APIError(409, 'test_busy', '連線測試正在進行。')
        app.state.llm_test_busy = True
        try:
            with store.connect(write=True) as db:
                ensure_active(db, auth['hash'])
                settings = DEFAULT_SETTINGS | store.get(db, 'services', {})
                key = store.get(db, 'llm_api_key', '')
                store.put(db, 'llm_test', {})
            async with asyncio.timeout(180):
                result = await llm.test_connection(settings, key, app.state.llm_transport)
            with store.connect(write=True) as db:
                ensure_active(db, auth['hash'])
                current = DEFAULT_SETTINGS | store.get(db, 'services', {})
                if llm.signature(current, store.get(db, 'llm_api_key', '')) != llm.signature(settings, key):
                    raise APIError(409, 'settings_changed', '設定已變更，請重新測試。')
                store.put(db, 'llm_test', result | {'signature': llm.signature(settings, key), 'tested_at': now().isoformat()})
            return result
        except llm.LLMError as exc:
            raise APIError(503, exc.code, exc.message) from None
        except TimeoutError:
            raise APIError(503, 'test_timeout', '連線測試等候時間已到。') from None
        finally:
            app.state.llm_test_busy = False

    @app.get("/api/admin/default-preferences")
    def get_defaults(auth=Depends(admin)):
        with store.connect() as db:
            return {"preferences": store.get(db, "default_preferences", [])}

    @app.put("/api/admin/default-preferences")
    async def put_defaults(body: Defaults, auth=Depends(admin)):
        values = [value.model_dump() for value in body.preferences]
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            previous = store.get(db, 'default_preferences', [])
            changed = values != previous
            if changed:
                store.put(db, 'default_preferences_revision', store.get(db, 'default_preferences_revision', 1 if previous else 0) + 1)
            store.put(db, "default_preferences", values)
            chosen = user_has_chosen(db, security.DEFAULT_USER_ID)
        if changed and not chosen:
            app.state.daily.request_baseline()
        return {"preferences": values}

    async def revoke_user_sessions(user_id):
        """Sign one account out on all devices and stop its running reply."""
        app.state.notifications.paused = True
        try:
            # Drain bounded sends before revocation, so none can begin after it returns.
            while app.state.notifications.active:
                await asyncio.sleep(.05)
            with store.connect(write=True) as db:
                db.execute("DELETE FROM sessions WHERE role='user' AND user_id=?", (user_id,))
                db.execute("DELETE FROM push_subscriptions WHERE user_id=?", (user_id,))
        finally:
            app.state.notifications.paused = app.state.maintenance
        active = app.state.active_chats.get(user_id)
        if active:
            task = active['task']
            task.get_loop().call_soon_threadsafe(task.cancel)

    def issue_link(db, user_id):
        """Replace the account's link; the token is returned once and only its hash is stored."""
        token = secrets.token_urlsafe(32)
        db.execute("UPDATE users SET access_token_hash=? WHERE id=?", (security.digest(token), user_id))
        return {"access_path": f"/access?token={token}", "access_origin": app.state.public_origin or None}

    def user_summary(db, row):
        return {"id": row["id"], "username": row["username"], "has_link": bool(row["access_token_hash"]),
                "disabled": bool(row["disabled"]), "created_at": row["created_at"],
                "devices": db.execute("SELECT COUNT(*) FROM sessions WHERE role='user' AND user_id=? AND expires_at>?",
                                      (row["id"], now().isoformat())).fetchone()[0]}

    @app.get("/api/admin/users")
    def list_users(auth=Depends(admin)):
        with store.connect() as db:
            return {"items": [user_summary(db, row) for row in db.execute("SELECT * FROM users ORDER BY created_at,id")]}

    @app.post("/api/admin/users", status_code=201)
    def create_user(body: UserCreate, auth=Depends(admin)):
        ident = str(uuid4())
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            if db.execute("SELECT 1 FROM users WHERE username=?", (body.username,)).fetchone():
                raise APIError(409, "username_taken", "這個帳號名稱已被使用。")
            # A new account starts with no conditions (daily search paused) until its owner confirms some.
            db.execute("INSERT INTO users VALUES(?,?,NULL,0,1,?)", (ident, body.username, now().isoformat()))
            link = issue_link(db, ident)
            return user_summary(db, db.execute("SELECT * FROM users WHERE id=?", (ident,)).fetchone()) | link

    @app.patch("/api/admin/users/{user_id}")
    async def update_user(user_id: str, body: UserUpdate, auth=Depends(admin)):
        if body.username is None and body.disabled is None:
            raise APIError(422, "invalid_input", "請至少修改一項。")
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            account = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not account:
                raise APIError(404, "user_missing", "找不到這個帳號。")
            if body.username is not None and body.username.lower() != account["username"].lower() and db.execute(
                    "SELECT 1 FROM users WHERE username=?", (body.username,)).fetchone():
                raise APIError(409, "username_taken", "這個帳號名稱已被使用。")
            if body.username is not None:
                db.execute("UPDATE users SET username=? WHERE id=?", (body.username, user_id))
            if body.disabled is not None:
                db.execute("UPDATE users SET disabled=? WHERE id=?", (int(body.disabled), user_id))
        if body.disabled:
            await revoke_user_sessions(user_id)
        with store.connect() as db:
            return user_summary(db, db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    @app.post("/api/admin/users/{user_id}/access/reset")
    async def reset_user_access(user_id: str, auth=Depends(admin)):
        with store.connect() as db:
            ensure_active(db, auth['hash'])
            account = db.execute("SELECT disabled FROM users WHERE id=?", (user_id,)).fetchone()
            if not account:
                raise APIError(404, "user_missing", "找不到這個帳號。")
            if account["disabled"]:
                raise APIError(409, "user_disabled", "這個帳號已停用，請先啟用再重設連結。")
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            link = issue_link(db, user_id)
        await revoke_user_sessions(user_id)
        return link

    @app.delete("/api/admin/users/{user_id}/access", status_code=204)
    async def disable_user_access(user_id: str, auth=Depends(admin)):
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            if not db.execute("UPDATE users SET access_token_hash=NULL WHERE id=?", (user_id,)).rowcount:
                raise APIError(404, "user_missing", "找不到這個帳號。")
        await revoke_user_sessions(user_id)
        return Response(status_code=204)

    @app.post("/api/admin/users/{user_id}/sign-out")
    async def sign_out_user(user_id: str, auth=Depends(admin)):
        with store.connect() as db:
            ensure_active(db, auth['hash'])
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise APIError(404, "user_missing", "找不到這個帳號。")
        await revoke_user_sessions(user_id)
        return {"signed_out": True}

    @app.delete("/api/admin/users/{user_id}", status_code=204)
    async def delete_user(user_id: str, confirm: bool = Query(default=False), auth=Depends(admin)):
        if not confirm:
            raise APIError(422, "confirmation_required", "刪除帳號會一併刪除對話、收藏、條件與每日職缺，請先確認。")
        with store.connect() as db:
            ensure_active(db, auth['hash'])
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise APIError(404, "user_missing", "找不到這個帳號。")
        await revoke_user_sessions(user_id)
        active = app.state.active_chats.get(user_id)
        if active:
            await asyncio.gather(active['task'], return_exceptions=True)
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            db.execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id=?)", (user_id,))
            db.execute("DELETE FROM conversations WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM favorites WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM daily_jobs WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM seen_jobs WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM preference_baselines WHERE preference_id IN (SELECT id FROM preferences WHERE user_id=?)", (user_id,))
            if user_id == security.DEFAULT_USER_ID:
                db.execute("DELETE FROM preference_baselines WHERE preference_id LIKE 'default:%'")
            db.execute("DELETE FROM preference_proposals WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM preferences WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM push_subscriptions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM users WHERE id=?", (user_id,))
        return Response(status_code=204)

    @app.get("/api/bootstrap")
    def bootstrap(auth=Depends(user)):
        with store.connect() as db:
            preferences = preferences_view(store, db, auth['user_id'])
            account = db.execute("SELECT username FROM users WHERE id=?", (auth['user_id'],)).fetchone()
            recent = db.execute('SELECT * FROM runs ORDER BY started_at DESC,rowid DESC LIMIT 1').fetchone()
            vapid = store.get(db, 'vapid_keys', {})
            push_ready = bool(vapid and store.get(db, 'services', {}).get('vapid_subject'))
            subscription = db.execute('SELECT id FROM push_subscriptions WHERE device_id=?', (auth['device_id'],)).fetchone()
            sprites = [{k: row[k] for k in ('state','frame_width','frame_height','frame_count','fps')} for row in db.execute('SELECT * FROM sprites')]
            quotes = [row[0] for row in db.execute('SELECT text FROM quotes ORDER BY sort_order,id')]
        return {"csrf_token": auth["csrf_token"], "display": {"title": "找工作小幫手", "default_theme": "a"},
                "account": {"username": account["username"]},
                "preferences": preferences, "push_configured": push_ready, 'push_public_key': vapid.get('public_key') if push_ready else None,
                'push_subscription_id': subscription[0] if subscription else None, 'sprites': sprites, 'quotes': quotes,
                "recent_run": user_run_view(recent, auth['user_id']) if recent else None,
                "local_date": now().astimezone(TAIPEI).date().isoformat(), "stage": "M3"}

    @app.post('/api/push-subscriptions', status_code=201)
    def subscribe(body: PushSubscription, auth=Depends(user)):
        try:
            subscription = push.validate_subscription({'endpoint': body.endpoint, 'keys': body.keys})
        except ValueError:
            raise APIError(422, 'invalid_subscription', '通知訂閱格式不符，請重新啟用。') from None
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            if not store.get(db, 'vapid_keys') or not store.get(db, 'services', {}).get('vapid_subject'):
                raise APIError(503, 'push_unconfigured', '通知服務尚未設定，請聯絡管理者。')
            previous = db.execute('SELECT id FROM push_subscriptions WHERE endpoint=?', (subscription['endpoint'],)).fetchone()
            ident = previous[0] if previous else uuid4().hex
            db.execute('''INSERT INTO push_subscriptions VALUES(?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET
                device_id=excluded.device_id,keys_json=excluded.keys_json,user_id=excluded.user_id''',
                (ident, auth['device_id'], subscription['endpoint'], encode(subscription['keys']), now().isoformat(), auth['user_id']))
            db.execute('DELETE FROM push_subscriptions WHERE device_id=? AND id!=?', (auth['device_id'], ident))
        return {'id': ident, 'enabled': True}

    @app.delete('/api/push-subscriptions/{subscription_id}', status_code=204)
    def unsubscribe(subscription_id: str, auth=Depends(user)):
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            db.execute('DELETE FROM push_subscriptions WHERE id=? AND device_id=?', (subscription_id, auth['device_id']))
        return Response(status_code=204)

    @app.post('/api/admin/push-test')
    async def push_test(auth=Depends(admin)):
        if app.state.notifications.active:
            raise APIError(409, 'push_busy', '正在傳送通知，請稍後再測試。')
        with store.connect() as db:
            if not store.get(db, 'services', {}).get('vapid_subject'):
                raise APIError(503, 'push_unconfigured', '請先設定推播聯絡 URI。')
        return {'test': True, 'items': await app.state.notifications.test(auth['hash'])}

    @app.get('/api/admin/schedule')
    def get_schedule(auth=Depends(admin)):
        with store.connect() as db:
            return {'time': store.get(db, 'schedule_time', '09:00'), 'timezone': 'Asia/Taipei'}

    @app.put('/api/admin/schedule')
    async def put_schedule(body: Schedule, auth=Depends(admin)):
        with store.connect(write=True) as db:
            store.put(db, 'schedule_time', body.time)
        return get_schedule()

    @app.post('/api/admin/search-runs', status_code=202)
    async def start_daily(auth=Depends(admin)):
        try:
            ident = app.state.daily.start('manual', auth['hash'])
        except RuntimeError:
            raise APIError(409, 'daily_busy', '搜尋正在進行，請等待這輪完成。') from None
        except PermissionError:
            raise APIError(401, 'session_revoked', '登入已失效。') from None
        return {'id': ident}

    @app.get('/api/admin/search-runs')
    def list_runs(limit: int = Query(20, ge=1, le=100), cursor: str | None = None, auth=Depends(admin)):
        with store.connect() as db:
            rows = db.execute('SELECT rowid AS seq,* FROM runs WHERE rowid<? ORDER BY rowid DESC LIMIT ?',
                              (parse_cursor(cursor) if cursor else 9223372036854775807, limit + 1)).fetchall()
        return {'items': [run_view(r) for r in rows[:limit]],
                'next_cursor': str(rows[limit-1]['seq']) if len(rows) > limit else None}

    @app.get('/api/admin/search-runs/{run_id}')
    def get_run(run_id: str, auth=Depends(admin)):
        with store.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row: raise APIError(404, 'run_missing', '找不到這輪執行。')
        return run_view(row)

    def selected_daily_date(date):
        today = now().astimezone(TAIPEI).date()
        try:
            wanted = datetime.strptime(date, '%Y-%m-%d').date() if date else today
            if date and wanted.isoformat() != date: raise ValueError()
            if not today - timedelta(days=6) <= wanted <= today: raise ValueError()
        except ValueError:
            raise APIError(422, 'invalid_date', '請選今天或最近7天的日期。') from None
        return wanted.isoformat()

    @app.get('/api/daily-jobs')
    def daily_jobs(date: str | None = None, limit: int = Query(20, ge=1, le=100), cursor: str | None = None, auth=Depends(user)):
        wanted = selected_daily_date(date)
        with store.connect() as db:
            rows = db.execute('''SELECT jobs.*,daily_jobs.rowid AS seq,daily_jobs.is_initial,daily_jobs.read_at,
                EXISTS(SELECT 1 FROM favorites WHERE favorites.job_id=jobs.id AND favorites.user_id=daily_jobs.user_id) AS favorite
                FROM daily_jobs JOIN jobs ON jobs.id=daily_jobs.job_id
                WHERE daily_jobs.user_id=? AND daily_jobs.local_date=? AND daily_jobs.rowid>? ORDER BY daily_jobs.rowid LIMIT ?''',
                (auth['user_id'], wanted, parse_cursor(cursor), limit+1)).fetchall()
            recent = db.execute('SELECT * FROM runs WHERE local_date=? ORDER BY started_at DESC,rowid DESC LIMIT 1', (wanted,)).fetchone()
            success = db.execute("SELECT finished_at FROM runs WHERE status IN ('success','partial') AND json_extract(result_json,'$.paused') IS NOT 1 ORDER BY started_at DESC LIMIT 1").fetchone()
        return {'items': [dict(json.loads(r['payload_json']), id=r['id'], is_favorite=bool(r['favorite']),
                               is_initial=bool(r['is_initial']), read_at=r['read_at']) for r in rows[:limit]],
                'next_cursor': str(rows[limit-1]['seq']) if len(rows)>limit else None,
                'local_date': wanted, 'run': user_run_view(recent, auth['user_id']) if recent else None,
                'last_success_at': success[0] if success else None}

    @app.post('/api/jobs/{job_id}/read')
    def read_job(job_id: str, date: str | None = None, auth=Depends(user)):
        wanted = selected_daily_date(date)
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            if not db.execute('UPDATE daily_jobs SET read_at=COALESCE(read_at,?) WHERE user_id=? AND local_date=? AND job_id=?',
                              (now().isoformat(), auth['user_id'], wanted, job_id)).rowcount:
                raise APIError(404, 'job_missing', '這天的每日清單沒有這筆職缺。')
        return {'job_id': job_id, 'local_date': wanted, 'read': True}

    def sprite_rows():
        with store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM sprites ORDER BY state')]

    @app.get('/api/admin/sprites')
    def get_sprites(auth=Depends(admin)):
        return {'items': [{k: v for k, v in r.items() if k != 'path'} for r in sprite_rows()]}

    def save_sprites(sheets, frame_width, frame_height, frame_count, fps, auth):
        """Write all new files before replacing any references in one transaction."""
        directory = store.path.parent / 'sprites'
        directory.mkdir(exist_ok=True)
        paths = {}
        old_paths = []
        try:
            for state, data in sheets.items():
                path = directory / (uuid4().hex + '.png')
                paths[state] = path
                path.write_bytes(data)
            with store.connect(write=True) as db:
                ensure_active(db, auth['hash'])
                for state, path in paths.items():
                    row = db.execute('SELECT path FROM sprites WHERE state=?', (state,)).fetchone()
                    if row: old_paths.append(row['path'])
                    db.execute('''INSERT INTO sprites VALUES(?,?,?,?,?,?) ON CONFLICT(state) DO UPDATE SET
                        path=excluded.path,frame_width=excluded.frame_width,frame_height=excluded.frame_height,
                        frame_count=excluded.frame_count,fps=excluded.fps''', (state, path.name, frame_width, frame_height, frame_count, fps))
        except BaseException:
            for path in paths.values():
                try: path.unlink(missing_ok=True)
                except OSError: pass
            raise
        for old in old_paths:
            if Path(old).name == old:
                try: (directory / old).unlink(missing_ok=True)
                except OSError: pass  # An unused old file is safer than undoing a committed upload.
        return [{'state': state, 'frame_width': frame_width, 'frame_height': frame_height,
                 'frame_count': frame_count, 'fps': fps} for state in sheets]

    @app.put('/api/admin/sprites/{state}')
    async def put_sprite(state: Literal['idle','thinking','happy','mail','encourage'], file: UploadFile = File(...),
                         frame_width: int = Form(...), frame_height: int = Form(...), frame_count: int = Form(...), fps: int = Form(...), auth=Depends(admin)):
        data = await file.read(MAX_FILE_BYTES + 1)
        try: validate_png(data, frame_width, frame_height, frame_count, fps)
        except ValueError as exc: raise APIError(422, 'invalid_sprite', str(exc)) from None
        return save_sprites({state: data}, frame_width, frame_height, frame_count, fps, auth)[0]

    @app.post('/api/admin/sprite-template/preview')
    async def preview_sprite_template(file: UploadFile = File(...), fps: int = Form(8), columns: int = Form(TEMPLATE_COLUMNS), clean_edges: bool = Form(False), auth=Depends(admin)):
        try: sheets = split_template(await file.read(MAX_FILE_BYTES + 1), fps, columns, clean_edges)
        except ValueError as exc: raise APIError(422, 'invalid_sprite', str(exc)) from None
        return {'items': [{'state': state, 'frame_width': 64, 'frame_height': 64, 'frame_count': columns, 'fps': fps,
                           'png_base64': b64encode(data).decode('ascii')}
                          for state, data in sheets.items()]}

    @app.put('/api/admin/sprite-template')
    async def put_sprite_template(file: UploadFile = File(...), fps: int = Form(8), columns: int = Form(TEMPLATE_COLUMNS), clean_edges: bool = Form(False), confirm: bool = Form(False), auth=Depends(admin)):
        if not confirm: raise APIError(422, 'confirmation_required', '請先預覽並確認五種狀態的對應。')
        try: sheets = split_template(await file.read(MAX_FILE_BYTES + 1), fps, columns, clean_edges)
        except ValueError as exc: raise APIError(422, 'invalid_sprite', str(exc)) from None
        return {'items': save_sprites(sheets, 64, 64, columns, fps, auth)}

    def sprite_image(state):
        with store.connect() as db:
            row = db.execute('SELECT path FROM sprites WHERE state=?', (state,)).fetchone()
        if not row or Path(row['path']).name != row['path']:
            raise APIError(404, 'sprite_missing', '尚未上傳這個狀態的素材。')
        path = store.path.parent / 'sprites' / row['path']
        if not path.is_file(): raise APIError(404, 'sprite_missing', '素材檔案不存在。')
        return FileResponse(path, media_type='image/png')

    @app.get('/api/admin/sprites/{state}/image')
    def admin_sprite_image(state: Literal['idle','thinking','happy','mail','encourage'], auth=Depends(admin)):
        return sprite_image(state)

    @app.get('/api/sprites/{state}/image')
    def user_sprite_image(state: Literal['idle','thinking','happy','mail','encourage'], auth=Depends(user)):
        return sprite_image(state)

    @app.get('/api/admin/quotes')
    def get_quotes(auth=Depends(admin)):
        with store.connect() as db:
            return {'texts': [r[0] for r in db.execute('SELECT text FROM quotes ORDER BY sort_order')]}

    @app.put('/api/admin/quotes')
    def put_quotes(body: Quotes, auth=Depends(admin)):
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            db.execute('DELETE FROM quotes')
            db.executemany('INSERT INTO quotes VALUES(?,?,?)', [(str(uuid4()), text, i) for i,text in enumerate(body.texts)])
        return {'texts': body.texts}

    @app.get('/api/areas')
    def areas(auth=Depends(user)):
        try:
            return {'items': sources.area_catalog()}
        except ImportError:
            raise APIError(503, 'source_dependency', '來源依賴尚未安裝，請管理者檢查。') from None

    @app.get('/api/admin/areas')
    def admin_areas(auth=Depends(admin)):
        return areas()

    def ensure_active(db, auth_hash):
        try:
            store.require_session(db, auth_hash)
        except PermissionError:
            raise APIError(401, 'session_revoked', '登入已失效，請重新登入。')

    @asynccontextmanager
    async def maintenance(auth_hash):
        if app.state.maintenance:
            raise APIError(409, 'maintenance_busy', '另一項資料維護正在進行。')
        app.state.maintenance = True
        app.state.daily.paused = app.state.notifications.paused = True
        deadline = asyncio.get_running_loop().time() + 30
        try:
            while (app.state.active_requests > 1 or app.state.active_chats or app.state.daily.task
                   or app.state.notifications.active):
                if asyncio.get_running_loop().time() >= deadline:
                    raise APIError(409, 'work_busy', '仍有工作執行中，原資料未替換，請稍後重試。')
                await asyncio.sleep(.05)
            with store.connect() as db:
                ensure_active(db, auth_hash)
            yield
        finally:
            if not app.state.recovery_required:
                app.state.maintenance = False
                app.state.daily.paused = app.state.notifications.paused = False
                if app.state.daily.pending_baseline:
                    app.state.daily.request_baseline()

    @app.post('/api/admin/backups', status_code=201)
    async def make_backup(auth=Depends(admin)):
        async with maintenance(auth['hash']):
            try:
                return await finish_file_operation(backups.create_backup, store)
            except ValueError as exc:
                raise APIError(503, 'backup_failed', str(exc)) from None

    @app.get('/api/admin/backups')
    def list_backups(auth=Depends(admin)):
        directory = store.path.parent / 'backups'
        paths = sorted(directory.glob('*.zip'), key=lambda path: path.stat().st_mtime, reverse=True) if directory.exists() else []
        return {'items': [{'id': path.stem, 'size': path.stat().st_size,
                          'created_at': datetime.fromtimestamp(path.stat().st_mtime, now().tzinfo).isoformat()} for path in paths
                          if len(path.stem) == 32 and all(c in '0123456789abcdef' for c in path.stem) and not path.is_symlink()]}

    @app.get('/api/admin/backups/{backup_id}')
    def download_backup(backup_id: str, auth=Depends(admin)):
        if len(backup_id) != 32 or any(c not in '0123456789abcdef' for c in backup_id):
            raise APIError(404, 'backup_missing', '找不到這份備份。')
        path = store.path.parent / 'backups' / (backup_id + '.zip')
        if not path.is_file() or path.is_symlink():
            raise APIError(404, 'backup_missing', '找不到這份備份。')
        return FileResponse(path, filename='job-companion-' + backup_id + '.zip', media_type='application/zip')

    @app.post('/api/admin/restore')
    async def restore(file: UploadFile = File(...), confirm: bool = Form(False), auth=Depends(admin)):
        from .backup_verify import MAX_ARCHIVE_BYTES, validate_backup
        if not confirm:
            raise APIError(422, 'confirmation_required', '請先確認還原會替換資料並撤銷登入。')
        data = await file.read(MAX_ARCHIVE_BYTES + 1)
        if len(data) > MAX_ARCHIVE_BYTES:
            raise APIError(422, 'backup_too_large', '備份超過128 MiB上限。')
        with tempfile.TemporaryDirectory(prefix='job-companion-restore-', dir=store.path.parent) as directory:
            root = Path(directory)
            archive = root / 'upload.zip'
            archive.write_bytes(data)
            staging = root / 'staging'
            staging.mkdir()
            with store.connect() as db:
                ensure_active(db, auth['hash'])
                schema = backups.schema_catalog(db)
            try:
                catalogs = {7: schema}
                for old_version in (4, 5, 6):
                    reference = Store(root / f'schema{old_version}', target_version=old_version)
                    with reference.connect() as db: catalogs[old_version] = backups.schema_catalog(db)
                await finish_file_operation(validate_backup, archive, staging, catalogs)
                backups.validate_settings(staging / 'app.db', Settings, Defaults, Schedule)
                # Upgrade only the verified staging copy, before replacing live data.
                Store(staging)
            except ValueError as exc:
                raise APIError(422, 'invalid_backup', str(exc)) from None
            async with maintenance(auth['hash']):
                try:
                    return await finish_file_operation(backups.restore_backup, store, staging, checkpoint=app.state.restore_checkpoint)
                except backups.RestoreError as exc:
                    app.state.recovery_required = exc.recovery_required
                    message = '還原失敗，原資料已恢復。' if not exc.recovery_required else '還原補償未完成，請先停止服務並使用保留的還原前備份復原。'
                    raise APIError(503, 'restore_failed', message + ' 還原前備份：' + exc.backup_id) from None
                except ValueError as exc:
                    raise APIError(503, 'backup_failed', str(exc)) from None

    def stored_job(job_id):
        with store.connect() as db:
            row = db.execute('SELECT payload_json FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise APIError(404, 'job_missing', '找不到這筆已保存職缺。')
        return Job.model_validate_json(row[0])

    async def company_reviews(body, auth_hash):
        company104 = None
        if body.job_id:
            job = stored_job(body.job_id)
            if reviews.name_key(body.company_name) != reviews.name_key(job.company_name):
                raise APIError(422, 'company_mismatch', '公司名稱與職缺不一致。')
            if job.source == '104': company104 = job.company_source_id
        result, updates = await app.state.reviews.get(body, company104)
        await asyncio.sleep(0)
        with store.connect(write=True) as db:
            ensure_active(db, auth_hash)
            for key, cached, expires in updates:
                db.execute('INSERT INTO review_cache VALUES(?,?,?) ON CONFLICT(company_key) DO UPDATE SET payload_json=excluded.payload_json,expires_at=excluded.expires_at', (key, encode(cached), expires))
        return result

    @app.post('/api/reviews')
    async def review_query(body: reviews.ReviewInput, auth=Depends(user)):
        return await company_reviews(body, auth['hash'])

    @app.get('/api/admin/search-comparison')
    def get_comparison(auth=Depends(admin)):
        with store.connect() as db: return store.get(db, 'search_comparison')

    @app.post('/api/admin/search-comparison')
    async def run_comparison(auth=Depends(admin)):
        if app.state.comparison_busy:
            raise APIError(409, 'comparison_busy', '比較正在進行，請稍候。')
        with store.connect() as db:
            companies = []
            for row in db.execute('SELECT payload_json FROM jobs ORDER BY rowid DESC'):
                name = json.loads(row[0])['company_name']
                if reviews.name_key(name) not in {reviews.name_key(x) for x in companies}: companies.append(name)
                if len(companies) == 4: break
            keys = {p: store.get(db, p.lower() + '_api_key', '') for p in ('Brave', 'Tavily')}
        if len(companies) != 4:
            raise APIError(422, 'companies_required', '請先在前台搜尋並取得至少四家不同公司的職缺，再執行比較。')
        app.state.comparison_busy = True
        try:
            async with asyncio.timeout(300):
                result = await app.state.reviews.compare(companies, keys)
            with store.connect(write=True) as db:
                ensure_active(db, auth['hash'])
                if any(store.get(db, p.lower() + '_api_key', '') != key for p, key in keys.items()):
                    raise APIError(409, 'settings_changed', '金鑰已變更，這次比較未保存。')
                store.put(db, 'search_comparison', result)
            return result
        except TimeoutError:
            raise APIError(503, 'comparison_timeout', '比較等候時間已到，請稍後再試。') from None
        finally:
            app.state.comparison_busy = False

    @app.post('/api/admin/search-selection')
    def save_selection(body: reviews.SelectionInput, auth=Depends(admin)):
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            comparison = store.get(db, 'search_comparison')
            if not comparison or comparison['id'] != body.comparison_id:
                raise APIError(409, 'comparison_changed', '比較結果已變更，請重新載入。')
            try: provider, scores = reviews.select_provider(comparison, body.assessments)
            except ValueError as exc: raise APIError(422, 'assessment_invalid', str(exc)) from None
            comparison.update(selection=provider, scores=scores, status='selected' if provider else 'no_qualified_results', assessments=[x.model_dump() for x in body.assessments])
            store.put(db, 'search_comparison', comparison)
            store.put(db, 'search_provider', provider)
            return {'selection': provider, 'scores': scores}

    async def read_job_details(job_id, auth_hash, user_id):
        job = stored_job(job_id)
        try:
            with store.connect() as db:
                enabled = sources.enabled_sources(store.get(db, 'services', {}))
            if job.source not in enabled:
                raise sources.SourceError('unsupported', f'{job.source} 已在後台停用，本次未查詢詳細頁。')
            result = await app.state.sources.detail(job)
        except sources.SourceError as exc:
            result = {'status': exc.status, 'detail': None, 'identity_verified': False, 'limitations': [exc.message]}
        result.update(job_id=job_id, item=None)
        with store.connect(write=True) as db:
            ensure_active(db, auth_hash)
            if result.get('identity_verified') and result.get('detail'):
                detail = result['detail']
                changes = {name: detail[name] for name in ('summary', 'title', 'company_name', 'location', 'salary_text') if detail.get(name)}
                updated = Job.model_validate(job.model_dump() | changes)
                store.save_job(updated, db)
                result['item'] = dict(updated.model_dump(mode='json'), id=job_id,
                    is_favorite=bool(db.execute('SELECT 1 FROM favorites WHERE job_id=? AND user_id=?', (job_id, user_id)).fetchone()))
        return result

    @app.post('/api/jobs/{job_id}/details')
    async def job_details(job_id: str, auth=Depends(user)):
        return await read_job_details(job_id, auth['hash'], auth['user_id'])

    def conversation_view(db, conversation_id, user_id):
        # Another account's conversation answers exactly like a missing one.
        row = db.execute('SELECT *, (SELECT COUNT(*) FROM messages WHERE conversation_id=c.id) AS message_count FROM conversations c WHERE id=? AND user_id=?', (conversation_id, user_id)).fetchone()
        if not row:
            raise APIError(404, 'conversation_missing', '找不到這段對話，可能已被刪除。')
        return dict(row)

    @app.get('/api/conversations')
    def conversations(q: str = Query(default='', max_length=200), cursor: str | None = None,
                      limit: int = Query(default=20, ge=1, le=100), auth=Depends(user)):
        # Stable creation order keeps renamed/continued conversations from skipping pages.
        with store.connect() as db:
            rows = db.execute('''SELECT rowid AS seq,*, (SELECT COUNT(*) FROM messages WHERE conversation_id=c.id) AS message_count
                FROM conversations c WHERE rowid<? AND user_id=? AND (instr(lower(title),lower(?))>0 OR EXISTS(
                SELECT 1 FROM messages m WHERE m.conversation_id=c.id AND instr(lower(json_extract(m.content_json,'$.text')),lower(?))>0))
                ORDER BY rowid DESC LIMIT ?''', (parse_cursor(cursor) if cursor is not None else 9223372036854775807, auth['user_id'], q.strip(), q.strip(), limit+1)).fetchall()
            return {'items': [{key: row[key] for key in ('id','title','created_at','updated_at','message_count')} for row in rows[:limit]],
                    'next_cursor': str(rows[limit-1]['seq']) if len(rows)>limit else None}

    @app.post('/api/conversations', status_code=201)
    async def create_conversation(body: ConversationInput, auth=Depends(user)):
        if app.state.active_chats.get(auth['user_id']):
            raise APIError(409, 'chat_busy', '請先等待回覆完成或停止回覆，再開始新對話。')
        ident, timestamp = str(uuid4()), now().isoformat()
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            db.execute('INSERT INTO conversations VALUES(?,?,?,?,?)', (ident, body.title, timestamp, timestamp, auth['user_id']))
            return conversation_view(db, ident, auth['user_id'])

    @app.patch('/api/conversations/{conversation_id}')
    def rename_conversation(conversation_id: str, body: ConversationTitle, auth=Depends(user)):
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash']); conversation_view(db, conversation_id, auth['user_id'])
            db.execute('UPDATE conversations SET title=?,updated_at=? WHERE id=?', (body.title, now().isoformat(), conversation_id))
            return conversation_view(db, conversation_id, auth['user_id'])

    @app.delete('/api/conversations/{conversation_id}', status_code=204)
    async def delete_conversation(conversation_id: str, auth=Depends(user)):
        active = app.state.active_chats.get(auth['user_id'])
        if active and active.get('conversation_id') == conversation_id:
            raise APIError(409, 'chat_busy', '這段對話正在回覆，請先停止回覆再刪除。')
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash']); conversation_view(db, conversation_id, auth['user_id'])
            db.execute('DELETE FROM messages WHERE conversation_id=?', (conversation_id,))
            db.execute('DELETE FROM conversations WHERE id=?', (conversation_id,))
        return Response(status_code=204)

    @app.get('/api/messages')
    def messages(cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100),
                 conversation_id: str | None = Query(default=None, min_length=1, max_length=100), auth=Depends(user)):
        if conversation_id is not None:
            with store.connect() as db: conversation_view(db, conversation_id, auth['user_id'])
        return store.messages(parse_cursor(cursor) if cursor is not None else None, limit, conversation_id, user_id=auth['user_id'])

    @app.patch('/api/searches/{search_id}')
    def dismiss_search_job(search_id: str, body: SearchDismiss, auth=Depends(user)):
        with store.connect() as db:
            conversation_view(db, body.conversation_id, auth['user_id'])
        try:
            return store.dismiss_search_job(body.conversation_id, search_id, body.job_id, body.dismissed, auth['hash'])
        except LookupError:
            raise APIError(404, 'search_missing', '找不到這次搜尋中的職缺，請重新整理。') from None
        except PermissionError:
            raise APIError(401, 'session_revoked', '登入已失效，請重新登入。') from None

    tool_specs = [
        {'name': 'list_preferences', 'description': '查看已確認的條件與等待確認的提案。',
         'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        {'name': 'propose_preference_change', 'description': '提出條件變更，須由使用者檢視確認後才生效。',
         'parameters': ProposalInput.model_json_schema()},
        {'name': 'search_jobs', 'description': '依使用者條件或履歷中的技能與經驗搜尋104／518／1111職缺（1111 為預設停用的實驗來源，依後台開關可用）；只傳求職條件，不傳履歷全文或聯絡資料。來源失敗及薪資篩選限制須如實告知。', 'parameters': sources.SearchInput.model_json_schema()},
        {'name': 'lookup_job_areas', 'description': '由地區名稱查官方地區代碼；搜尋前先查碼，不猜數字。', 'parameters': AreaLookup.model_json_schema()},
        {'name': 'present_job_recommendations', 'description': '以本次對話已取得的職缺job_id呈現精選或比較卡片；reasons/concerns/unknowns只寫有資料支持的適配理由、注意點與未知條件。不查外網或修改每日条件。', 'parameters': Recommendations.model_json_schema()},
        {'name': 'get_job_details', 'description': '讀取已保存職缺的詳細內容；未驗證的詳細資料不能聲稱身份已核對。', 'parameters': sources.DetailInput.model_json_schema()},
        {'name': 'get_company_reviews', 'description': '使用者詢問或要求時才查公司評價；同名不明時請使用者選擇。', 'parameters': reviews.ReviewInput.model_json_schema()},
        {'name': 'read_web_page', 'description': '讀取使用者提供或與其要求直接相關的公開網頁，例如 GitHub 個人頁、專案 README、個人網站及作品集。一次一個完整 HTTP／HTTPS 網址；回傳文字及来源網址。不能登入或搜尋，失敗、截斷與未讀連結須如實說明。', 'parameters': web.ReadInput.model_json_schema()},
    ]

    def validate_tool(call):
        try:
            arguments = json.loads(call['arguments'])
            if call['name'] == 'list_preferences':
                if arguments != {}:
                    raise ValueError()
                return arguments
            if call['name'] == 'propose_preference_change':
                return ProposalInput.model_validate(arguments)
            if call['name'] == 'search_jobs': return sources.SearchInput.model_validate(arguments)
            if call['name'] == 'lookup_job_areas': return AreaLookup.model_validate(arguments)
            if call['name'] == 'present_job_recommendations': return Recommendations.model_validate(arguments)
            if call['name'] == 'get_job_details': return sources.DetailInput.model_validate(arguments)
            if call['name'] == 'get_company_reviews': return reviews.ReviewInput.model_validate(arguments)
            if call['name'] == 'read_web_page': return web.ReadInput.model_validate(arguments)
        except (ValueError, TypeError):
            pass
        raise llm.LLMError('invalid_tool', '模型提出的工具或參數不符，請調整後重試。')

    async def invoke_tool(name, arguments, auth_hash, conversation_id, request_id, user_id):
        if name == 'lookup_job_areas':
            normalized = arguments.query.replace('臺', '台')
            catalog = sources.area_catalog()
            exact = [area for area in catalog if area['name'].replace('臺', '台') == normalized]
            return {'items': exact or [area for area in catalog if normalized in area['name'].replace('臺', '台')][:100]}, None
        if name == 'present_job_recommendations':
            with store.connect() as db:
                ensure_active(db, auth_hash)
                allowed = store.conversation_job_ids(db, conversation_id)
                if any(item.job_id not in allowed for item in arguments.items):
                    raise llm.LLMError('unknown_job', '只能推薦這段對話已取得的職缺，請先搜尋。')
                items, seen = [], set()
                for item in arguments.items:
                    if item.job_id in seen:
                        continue
                    seen.add(item.job_id)
                    job = stored_job(item.job_id)
                    items.append(item.model_dump() | {'job': dict(job.model_dump(mode='json'), id=item.job_id,
                        citation_number=allowed.index(item.job_id) + 1,
                        is_favorite=bool(db.execute('SELECT 1 FROM favorites WHERE job_id=? AND user_id=?', (item.job_id, user_id)).fetchone()))})
            result = {'items': items}
            return result, ('recommendations', result)
        if name == 'read_web_page':
            with store.connect() as db:
                ensure_active(db, auth_hash)
                key = store.get(db, 'tavily_api_key', '') if reviews.active_provider(store, db) == 'Tavily' else ''
            result = await app.state.web_reader.read(arguments, key)
            with store.connect() as db: ensure_active(db, auth_hash)
            return result, None
        if name == 'search_jobs':
            search_id = getattr(arguments, 'search_id', None)
            previous = None
            if search_id:
                with store.connect() as db:
                    ensure_active(db, auth_hash)
                    try:
                        previous = store.search_snapshot(db, conversation_id, search_id)
                    except LookupError:
                        raise llm.LLMError('search_missing', '找不到這段對話的搜尋，請重新搜尋。') from None
                if not previous.get('can_continue'):
                    raise llm.LLMError('search_exhausted', '這次搜尋已沒有下一批，請調整條件後重新搜尋。')
                arguments = sources.SearchInput.model_validate(previous['criteria_input'] | {'search_id': search_id})
            with store.connect() as db:
                enabled = sources.enabled_sources(store.get(db, 'services', {}))
            requested = list(arguments.sources)
            if not previous and 'sources' not in arguments.model_fields_set and '1111' in enabled:
                requested.append('1111')
            kept = [source for source in requested if source in enabled]
            disabled_sources = [source for source in requested if source not in enabled]
            disabled_names = '、'.join(disabled_sources)
            if not kept:
                raise llm.LLMError('source_disabled', f'{disabled_names} 已在後台停用，本次未查詢；可改查 104，或請管理者在後台重新啟用來源。')
            disabled = bool(disabled_sources)
            arguments = arguments.model_copy(update={'sources': kept})
            if previous:
                excluded = {f'{job.source}:{job.source_job_id}' for job in map(stored_job, previous['dismissed_ids'])}
                result = await app.state.sources.search(arguments, continuation=previous.get('_continuation'), exclude_ids=excluded)
            else:
                search_id = str(uuid4())
                result = await app.state.sources.search(arguments)
            if disabled:
                result['filter_warnings'].append(f'{disabled_names} 已在後台停用，本次未查詢。')
            if previous and disabled and not result['items'] and not result['can_continue']:
                raise llm.LLMError('search_exhausted', f'{disabled_names} 已在後台停用，其餘來源也已沒有下一批，請調整條件後重新搜尋。')
            await asyncio.sleep(0)
            with store.connect(write=True) as db:
                ensure_active(db, auth_hash)
                items = []
                known_job_ids = store.conversation_job_ids(db, conversation_id)
                for job in result['items']:
                    ident = store.save_job(job, db)
                    if ident not in known_job_ids:
                        known_job_ids.append(ident)
                    items.append(dict(job.model_dump(mode='json'), id=ident,
                        citation_number=known_job_ids.index(ident) + 1,
                        is_favorite=bool(db.execute('SELECT 1 FROM favorites WHERE job_id=? AND user_id=?', (ident, user_id)).fetchone())))
            result['items'] = items
            old_ids = previous.get('candidate_ids', []) if previous else []
            new_ids = [item['id'] for item in items if item['id'] not in set(old_ids)]
            candidate_ids = list(dict.fromkeys([*old_ids, *(item['id'] for item in items)]))
            continuation = result.pop('continuation', None)
            result.update(search_id=search_id, origin_request_id=previous['origin_request_id'] if previous else request_id,
                criteria=arguments.criteria.model_dump(), sources=arguments.sources,
                criteria_input=arguments.model_dump(exclude={'search_id'}), candidate_ids=candidate_ids,
                new_ids=new_ids, new_count=len(new_ids), total_count=len(candidate_ids),
                dismissed_ids=previous['dismissed_ids'] if previous else [], _continuation=continuation)
            result.setdefault('duplicate_count', len(items) - len(new_ids))
            result.setdefault('can_continue', bool(continuation))
            return {key: value for key, value in result.items() if key != '_continuation'}, ('jobs', result)
        if name == 'get_company_reviews':
            result = await company_reviews(arguments, auth_hash)
            return result, ('review', result)
        if name == 'get_job_details':
            result = await read_job_details(arguments.job_id, auth_hash, user_id)
            return result, ('job_detail', result)
        if name == 'list_preferences':
            with store.connect() as db:
                return preferences_view(store, db, user_id), None
        try:
            proposal = propose_change(store, arguments, auth_hash, user_id)
        except APIError as exc:
            raise llm.LLMError(exc.code, exc.message) from None
        return proposal, ('preference_proposal', proposal)

    @app.post('/api/chat')
    async def start_chat(request: Request, auth=Depends(user)):
        body, upload = await read_chat_request(request)
        initial_search = None
        if body.action:
            if body.edit_message_id or upload:
                raise APIError(422, 'invalid_input', '續查請單獨送出，不與訊息編輯或附件混用。')
            with store.connect() as db:
                conversation_view(db, body.conversation_id, auth['user_id'])
                try:
                    previous = store.search_snapshot(db, body.conversation_id, body.action.search_id)
                except LookupError:
                    raise APIError(404, 'search_missing', '找不到這段對話的搜尋，請重新搜尋。') from None
                if not previous.get('can_continue'):
                    raise APIError(409, 'search_finished', '這次搜尋已沒有可接續的結果，請調整條件。')
            initial_search = previous['criteria_input'] | {'search_id': body.action.search_id}
        if (body.edit_message_id is None) != (body.edit_request_id is None):
            raise APIError(422, 'invalid_input', '編輯訊息需提供原訊息及原請求識別。')
        with store.connect() as db:
            conversation_view(db, body.conversation_id, auth['user_id'])
            if db.execute('SELECT 1 FROM messages WHERE request_id=?', (body.request_id,)).fetchone():
                raise APIError(409, 'duplicate_request', '這則訊息已送出，請查看歷史。')
            if body.edit_message_id:
                original = db.execute("SELECT request_id,content_json FROM messages WHERE id=? AND role='user' AND conversation_id=?",
                                      (body.edit_message_id, body.conversation_id)).fetchone()
                if not original:
                    raise APIError(404, 'message_missing', '找不到這則使用者訊息，請重新整理對話。')
                if original['request_id'] != body.edit_request_id:
                    raise APIError(409, 'message_changed', '這則訊息已被修改，請重新整理後再編輯。')
                if json.loads(original['content_json']).get('attachments') and not upload:
                    raise APIError(422, 'attachment_required', '原附件未保存，請重新附上檔案再生成回覆。')
            settings = DEFAULT_SETTINGS | store.get(db, 'services', {})
            key = store.get(db, 'llm_api_key', '')
            test = store.get(db, 'llm_test', {})
        if app.state.active_chats.get(auth['user_id']):
            raise APIError(409, 'chat_busy', '另一則回覆正在進行，請稍候或停止目前回覆。')
        if not key or not settings['llm_base_url'] or not settings['llm_model']:
            raise APIError(503, 'not_configured', '模型服務尚未設定，請聯絡管理者。')
        protocol = settings['llm_protocol']
        if protocol == 'auto':
            if test.get('signature') != llm.signature(settings, key) or not test.get('tools') or not test.get('reply'):
                raise APIError(503, 'test_required', '請先由管理者完成模型連線與工具測試。')
            protocol = test['protocol']
        resume_context = None
        attachments = []
        if upload:
            if app.state.resume_lock.locked():
                raise APIError(429, 'resume_busy', '另一份附件正在解析，請稍候再送出。')
            try:
                async with app.state.resume_lock:
                    resume_context, metadata = await chat_files.prepare(upload)
            except resume_pdf.ResumeError as exc:
                raise APIError(422, 'invalid_resume', str(exc)) from None
            attachments = [metadata]
            # Parsing yields: another chat can have started, or this client disconnected.
            if await request.is_disconnected():
                raise APIError(400, 'upload_cancelled', '附件上傳已取消，訊息尚未保存。')
            if app.state.active_chats.get(auth['user_id']):
                raise APIError(409, 'chat_busy', '另一則回覆正在進行，請稍候或停止目前回覆。')
        try:
            store.start_chat(body.request_id, body.text, auth['hash'], attachments, body.conversation_id,
                             body.edit_message_id, body.edit_request_id, user_id=auth['user_id'])
        except LookupError as exc:
            if exc.args[0] == 'message_missing':
                raise APIError(404, 'message_missing', '找不到這則使用者訊息，請重新整理對話。') from None
            raise APIError(404, 'conversation_missing', '這段對話已被刪除，請開始新對話。') from None
        except ValueError as exc:
            if exc.args[0] == 'message_changed':
                raise APIError(409, 'message_changed', '這則訊息已被修改，請重新整理後再編輯。') from None
            raise APIError(422, 'attachment_required', '原附件未保存，請重新附上檔案再生成回覆。') from None
        except PermissionError:
            raise APIError(401, 'session_revoked', '登入已失效，請重新登入。') from None
        except sqlite3.IntegrityError:
            with store.connect() as db:
                duplicate = db.execute('SELECT 1 FROM messages WHERE request_id=?', (body.request_id,)).fetchone()
            if duplicate:
                raise APIError(409, 'duplicate_request', '這則訊息已送出，請查看歷史。') from None
            raise APIError(503, 'storage_unavailable', '訊息未能保存，請稍後再試。') from None
        queue = asyncio.Queue()
        task = asyncio.create_task(chat.produce_reply(app, body.request_id, settings, key, protocol,
                                                    tool_specs, validate_tool,
                                                    lambda name, arguments: invoke_tool(name, arguments, auth['hash'], body.conversation_id, body.request_id, auth['user_id']),
                                                    auth['hash'], queue, resume_context, initial_search))
        app.state.active_chats[auth['user_id']] = {'request_id': body.request_id, 'conversation_id': body.conversation_id, 'task': task, 'auth_hash': auth['hash']}
        # Start the producer before any cancel endpoint can cancel an unstarted task.
        await asyncio.sleep(0)
        return StreamingResponse(chat.reply_stream(task, queue, body.request_id), media_type='text/event-stream',
                                 headers={'X-Accel-Buffering': 'no'})

    @app.get('/api/chat/{request_id}')
    def chat_progress(request_id: str, auth=Depends(user)):
        with store.connect() as db:
            row = db.execute("""SELECT m.conversation_id FROM messages m JOIN conversations c ON c.id=m.conversation_id
                WHERE m.request_id=? AND m.role='user' AND c.user_id=?""", (request_id, auth['user_id'])).fetchone()
        if not row:
            raise APIError(404, 'message_missing', '找不到這次訊息，可能尚未送達。')
        return {'request_id': request_id, 'conversation_id': row['conversation_id'],
                'items': store.messages(limit=2, request_id=request_id, user_id=auth['user_id'])['items']}

    @app.post('/api/chat/{request_id}/cancel')
    async def cancel_chat(request_id: str, auth=Depends(user)):
        active = app.state.active_chats.get(auth['user_id'])
        if active and active['request_id'] == request_id:
            active['task'].cancel()
            await active['task']
        with store.connect() as db:
            row = db.execute("""SELECT m.content_json FROM messages m JOIN conversations c ON c.id=m.conversation_id
                WHERE m.request_id=? AND m.role='assistant' AND c.user_id=?""", (request_id, auth['user_id'])).fetchone()
        if not row:
            raise APIError(404, 'message_missing', '找不到這次回覆。')
        return {'request_id': request_id, 'status': json.loads(row[0])['status']}

    @app.get("/api/preferences")
    def preferences(auth=Depends(user)):
        with store.connect() as db:
            return preferences_view(store, db, auth['user_id'])

    @app.post("/api/preference-proposals", status_code=201)
    def propose(body: ProposalInput, auth=Depends(user)):
        return propose_change(store, body, auth['hash'], auth['user_id'])

    @app.post("/api/preference-proposals/{proposal_id}/{action}")
    async def resolve(proposal_id: str, action: Literal["confirm", "cancel"], auth=Depends(user)):
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            row = db.execute("SELECT * FROM preference_proposals WHERE id=? AND user_id=?", (proposal_id, auth['user_id'])).fetchone()
            if not row:
                raise APIError(404, "proposal_missing", "找不到這份提案。")
            desired = "confirmed" if action == "confirm" else "cancelled"
            if row["status"] == desired:
                return proposal_view(row)
            if row["status"] != "pending":
                raise APIError(409, "proposal_resolved", "提案已處理，請重新檢視。")
            if action == "confirm":
                if row["operation"] == "create":
                    db.execute("INSERT INTO preferences VALUES(?,1,?,1,?)", (row["preference_id"], row["after_json"], auth['user_id']))
                else:
                    current = db.execute("SELECT revision FROM preferences WHERE id=? AND user_id=?", (row["preference_id"], auth['user_id'])).fetchone()
                    if not current or current["revision"] != row["base_revision"]:
                        raise APIError(409, "stale_revision", "條件已變更，這份提案已過期，請取消後重新提出。")
                    if row["operation"] == "update":
                        db.execute("UPDATE preferences SET revision=revision+1,criteria_json=? WHERE id=? AND user_id=?",
                                   (row["after_json"], row["preference_id"], auth['user_id']))
                    else:
                        db.execute("DELETE FROM preferences WHERE id=? AND user_id=?", (row["preference_id"], auth['user_id']))
                db.execute("UPDATE users SET has_chosen=1 WHERE id=?", (auth['user_id'],))
            db.execute("UPDATE preference_proposals SET status=? WHERE id=?", (desired, proposal_id))
            result = proposal_view(db.execute("SELECT * FROM preference_proposals WHERE id=?", (proposal_id,)).fetchone())
        if action == 'confirm': app.state.daily.request_baseline()
        return result

    @app.get("/api/favorites")
    def favorites(limit: int = Query(default=20, ge=1, le=100), cursor: str | None = Query(default=None, max_length=100), auth=Depends(user)):
        with store.connect() as db:
            rows = db.execute("""SELECT jobs.*, favorites.rowid AS favorite_cursor FROM favorites JOIN jobs ON jobs.id=favorites.job_id
                WHERE favorites.user_id=? AND favorites.rowid > ? ORDER BY favorites.rowid LIMIT ?""", (auth['user_id'], parse_cursor(cursor), limit + 1)).fetchall()
        items = [{"id": row["id"], **json.loads(row["payload_json"]), "is_favorite": True} for row in rows[:limit]]
        return {"items": items, "next_cursor": str(rows[limit - 1]["favorite_cursor"]) if len(rows) > limit else None}

    @app.put("/api/favorites/{job_id}")
    def add_favorite(job_id: str, auth=Depends(user)):
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            if not db.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone():
                raise APIError(404, "job_missing", "找不到這份職缺。")
            db.execute("INSERT OR IGNORE INTO favorites VALUES(?,?,?)", (job_id, auth['user_id'], now().isoformat()))
        return {"job_id": job_id, "is_favorite": True}

    @app.delete("/api/favorites/{job_id}", status_code=204)
    def remove_favorite(job_id: str, auth=Depends(user)):
        with store.connect(write=True) as db:
            ensure_active(db, auth['hash'])
            db.execute("DELETE FROM favorites WHERE job_id=? AND user_id=?", (job_id, auth['user_id']))
        return Response(status_code=204)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def parse_cursor(cursor):
    if cursor is None:
        return 0
    if not cursor.isascii() or not cursor.isdigit() or len(cursor) > 18:
        raise APIError(422, "invalid_cursor", "分頁位置不正確。")
    return int(cursor)


def propose_change(store, body: ProposalInput, auth_hash=None, user_id=security.DEFAULT_USER_ID):
    """Shared by the authenticated UI and future LLM tool; never confirms a change."""
    proposal_id = str(uuid4())
    with store.connect(write=True) as db:
        if auth_hash:
            try: store.require_session(db, auth_hash)
            except PermissionError: raise APIError(401, 'session_revoked', '登入已失效，請重新登入。') from None
        before = None
        if body.operation != "create":
            row = db.execute("SELECT * FROM preferences WHERE id=? AND user_id=?", (body.preference_id, user_id)).fetchone()
            if not row:
                raise APIError(404, "preference_missing", "這組條件已不存在。")
            if row["revision"] != body.base_revision:
                raise APIError(409, "stale_revision", "條件已更新，請重新檢視。")
            before = row["criteria_json"]
        preference_id = str(uuid4()) if body.operation == "create" else body.preference_id
        db.execute("INSERT INTO preference_proposals VALUES(?,?,?,?,?,?,'pending',?)", (
            proposal_id, body.operation, preference_id, body.base_revision, before,
            encode(body.after.model_dump()) if body.after else None, user_id))
        return proposal_view(db.execute("SELECT * FROM preference_proposals WHERE id=?", (proposal_id,)).fetchone())
