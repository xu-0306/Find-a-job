import hashlib
import hmac
import ipaddress
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from .store import now


DEFAULT_USER_ID = 'default'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${derived.hex()}"


def password_matches(password, stored):
    if not stored:
        return False
    _, salt, expected = stored.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return hmac.compare_digest(actual.hex(), expected)


def loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_request(request, local_peers=()):
    try:
        host = urlsplit("//" + request.headers.get("host", "")).hostname or ""
    except ValueError:
        return False
    return bool(request.client and (loopback(request.client.host) or request.client.host in local_peers) and loopback(host))


def origin_matches(request):
    # Forwarded headers never decide local access or the expected Origin.
    origin = request.headers.get("origin", "")
    return origin == f"{request.url.scheme}://{request.headers.get('host', '')}"


def cookie_name(role, secure=False):
    return ("__Host-" if secure else "") + f"jc_{role}"


def csrf(token):
    return digest("job-companion:csrf:" + token)


def csrf_matches(candidate, token):
    return bool(token) and hmac.compare_digest(candidate.encode(), csrf(token).encode())


def new_session(db, response, role, secure=False, user_id=None):
    if (role == "user") != bool(user_id):
        raise ValueError("user sessions need a user id; admin sessions must not have one")
    token = secrets.token_urlsafe(32)
    lifetime = timedelta(days=30) if role == "user" else timedelta(hours=8)
    expiry = now() + lifetime
    device = secrets.token_hex(16) if role == "user" else None
    db.execute("DELETE FROM sessions WHERE expires_at<=?", (now().isoformat(),))
    db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (digest(token), role, expiry.isoformat(), device, user_id))
    response.set_cookie(cookie_name(role, secure), token, httponly=True, secure=secure,
                        samesite="lax", path="/", max_age=int(lifetime.total_seconds()))
    return csrf(token)
