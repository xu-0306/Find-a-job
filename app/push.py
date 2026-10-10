"""Small, synchronous Web Push transport.

This module deliberately owns no database, session, scheduler, or retry state.
The caller owns those concerns and supplies the VAPID subject and private key.
"""

import base64
import binascii
import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush


_B64URL = re.compile(r"^[A-Za-z0-9_-]*={0,2}$")
_MAX_ENDPOINT = 2048
_MAX_PAYLOAD = 3000
_MAX_PRIVATE_KEY = 4096
_MAX_SUBJECT = 2048
_MAX_TITLE = 120
_MAX_BODY = 2000
_RESOLVER_TIMEOUT = 5.0
_ALLOWED_SUBSCRIPTION_FIELDS = {"endpoint", "keys", "expirationTime"}
_ALLOWED_PAYLOAD_FIELDS = {"title", "body", "url"}


class _BlockedInput(ValueError):
    pass


class _EndpointChanged(_BlockedInput):
    pass


class _UnknownTransport(Exception):
    pass


def _no_controls(value, *, what):
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise _BlockedInput(f"invalid {what}")


def _decode_b64url(value, *, size, what):
    if not isinstance(value, str) or not value or len(value) > 256 or not _B64URL.fullmatch(value):
        raise _BlockedInput(f"invalid {what}")
    unpadded = value.rstrip("=")
    try:
        raw = base64.urlsafe_b64decode(unpadded + "=" * ((4 - len(unpadded) % 4) % 4))
    except (binascii.Error, ValueError):
        raise _BlockedInput(f"invalid {what}") from None
    if len(raw) != size:
        raise _BlockedInput(f"invalid {what}")
    return raw, base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _canonical_endpoint(value):
    if not isinstance(value, str) or not value or len(value) > _MAX_ENDPOINT:
        raise _BlockedInput("invalid endpoint")
    _no_controls(value, what="endpoint")
    if "\\" in value:
        raise _BlockedInput("invalid endpoint")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise _BlockedInput("invalid endpoint") from None
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise _BlockedInput("invalid endpoint")
    if "#" in value or parsed.fragment or port not in (None, 443):
        raise _BlockedInput("invalid endpoint")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise _BlockedInput("invalid endpoint") from None
    if not hostname:
        raise _BlockedInput("invalid endpoint")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        netloc = hostname
    else:
        netloc = f"[{hostname}]" if ":" in hostname else hostname
    canonical = urlunsplit(("https", netloc, parsed.path, parsed.query, ""))
    if len(canonical) > _MAX_ENDPOINT:
        raise _BlockedInput("invalid endpoint")
    return canonical


def validate_subscription(value):
    """Validate and canonicalize browser PushSubscription JSON.

    ``expirationTime`` is browser metadata and is intentionally ignored.  The
    returned mapping contains only the endpoint and the two Web Push keys.
    """

    if not isinstance(value, Mapping) or not set(value).issubset(_ALLOWED_SUBSCRIPTION_FIELDS):
        raise ValueError("invalid push subscription")
    try:
        endpoint = _canonical_endpoint(value.get("endpoint"))
        keys = value.get("keys")
        if not isinstance(keys, Mapping) or set(keys) != {"p256dh", "auth"}:
            raise _BlockedInput("invalid subscription keys")
        p256dh, p256dh_text = _decode_b64url(keys.get("p256dh"), size=65, what="p256dh")
        auth, auth_text = _decode_b64url(keys.get("auth"), size=16, what="auth")
        if p256dh[0] != 4:
            raise _BlockedInput("invalid p256dh")
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
        except ValueError:
            raise _BlockedInput("invalid p256dh") from None
    except (AttributeError, TypeError):
        raise ValueError("invalid push subscription") from None
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh_text, "auth": auth_text}}


def generate_keys():
    """Generate the persisted VAPID pair used by the Web Push caller."""

    private = ec.generate_private_key(ec.SECP256R1())
    private_der = private.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    encode = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return {"private_key": encode(private_der), "public_key": encode(public_raw)}


def _validate_private_key(value):
    if not isinstance(value, str) or not value or len(value) > _MAX_PRIVATE_KEY or not _B64URL.fullmatch(value):
        raise _BlockedInput("invalid VAPID key")
    try:
        der = base64.urlsafe_b64decode(value.rstrip("=") + "=" * ((4 - len(value.rstrip("=")) % 4) % 4))
        key = serialization.load_der_private_key(der, password=None)
    except (ValueError, TypeError, binascii.Error):
        raise _BlockedInput("invalid VAPID key") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise _BlockedInput("invalid VAPID key")


def _validate_subject(value):
    if not isinstance(value, str) or not value or len(value) > _MAX_SUBJECT:
        raise _BlockedInput("invalid VAPID subject")
    _no_controls(value, what="VAPID subject")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise _BlockedInput("invalid VAPID subject") from None
    if parsed.scheme == "mailto":
        address = parsed.path
        if (
            not address
            or address.count("@") != 1
            or not address.split("@", 1)[0]
            or not address.rsplit("@", 1)[1]
            or parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or parsed.fragment
        ):
            raise _BlockedInput("invalid VAPID subject")
    elif parsed.scheme == "https":
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or "#" in value or parsed.fragment or (port is not None and not 1 <= port <= 65535)):
            raise _BlockedInput("invalid VAPID subject")
    else:
        raise _BlockedInput("invalid VAPID subject")


def valid_subject(value):
    """Return whether a configured VAPID subject has an allowed URI shape."""

    try:
        _validate_subject(value)
    except Exception:
        return False
    return True


def _payload_json(payload):
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            raise _BlockedInput("invalid push payload") from None
    if not isinstance(payload, Mapping) or set(payload) != _ALLOWED_PAYLOAD_FIELDS:
        raise _BlockedInput("invalid push payload")
    title, body, target = payload.get("title"), payload.get("body"), payload.get("url")
    if not isinstance(title, str) or not title.strip() or len(title) > _MAX_TITLE:
        raise _BlockedInput("invalid push payload")
    if not isinstance(body, str) or not body.strip() or len(body) > _MAX_BODY:
        raise _BlockedInput("invalid push payload")
    if target not in ("/", "/?page=daily"):
        raise _BlockedInput("invalid push payload")
    try:
        encoded = json.dumps({"title": title, "body": body, "url": target}, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise _BlockedInput("invalid push payload") from None
    if len(encoded.encode("utf-8")) > _MAX_PAYLOAD:
        raise _BlockedInput("invalid push payload")
    return encoded


def _is_global(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_global and not ip.is_private and not ip.is_loopback and not ip.is_link_local and not ip.is_multicast and not ip.is_unspecified


def _resolve_peer(endpoint, resolver):
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    result_queue = queue.Queue(maxsize=1)

    def resolve_in_background():
        try:
            records = resolver(host, 443, type=socket.SOCK_STREAM)
            records = tuple(records)
        except BaseException as error:
            try:
                result_queue.put_nowait(("error", error))
            except queue.Full:
                pass
            return
        try:
            result_queue.put_nowait(("records", records))
        except queue.Full:
            pass

    worker = threading.Thread(target=resolve_in_background, name="push-dns", daemon=True)
    try:
        worker.start()
        result_kind, result = result_queue.get(timeout=_RESOLVER_TIMEOUT)
    except queue.Empty:
        raise _UnknownTransport("endpoint DNS timeout") from None
    except Exception:
        raise _UnknownTransport("endpoint DNS unavailable") from None
    if result_kind == "error":
        raise _UnknownTransport("endpoint DNS unavailable") from None
    records = result
    addresses = []
    try:
        for record in records:
            sockaddr = record[4]
            address = sockaddr[0]
            if not _is_global(address):
                raise _BlockedInput("endpoint address is not public")
            addresses.append(address)
    except (IndexError, KeyError, TypeError):
        raise _BlockedInput("endpoint DNS invalid") from None
    except _BlockedInput:
        raise
    except Exception:
        raise _UnknownTransport("endpoint DNS unavailable") from None
    if not addresses:
        raise _BlockedInput("endpoint DNS empty")
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that pins the already-validated peer address."""

    def __init__(self, hostname, peer_ip, timeout):
        self.peer_ip = peer_ip
        super().__init__(hostname, port=443, timeout=timeout, context=ssl.create_default_context())

    def connect(self):
        raw = socket.create_connection((self.peer_ip, self.port), timeout=self.timeout)
        try:
            try:
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except (AttributeError, OSError):
                pass
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class _PinnedSession:
    """The small ``requests_session.post`` protocol used by pywebpush."""

    def __init__(self, endpoint, peer_ip):
        parsed = urlsplit(endpoint)
        self.endpoint = endpoint
        self.hostname = parsed.hostname
        self.host_header = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        self.peer_ip = peer_ip

    def post(self, url, *, data=None, headers=None, timeout=None, **kwargs):
        if url != self.endpoint:
            raise _EndpointChanged("push library changed endpoint")
        if kwargs or timeout is None or isinstance(timeout, tuple):
            raise _BlockedInput("unsupported HTTP options")
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        request_headers = {str(key): str(value) for key, value in (headers or {}).items()}
        request_headers["Host"] = self.host_header
        request_headers.setdefault("Connection", "close")
        connection = _PinnedHTTPSConnection(self.hostname, self.peer_ip, float(timeout))
        try:
            connection.request("POST", path, body=data or b"", headers=request_headers)
            reply = connection.getresponse()
            status_code = int(reply.status)
            reason = reply.reason or ""
            response_headers = dict(reply.getheaders())
            # Do not read provider bodies: status and headers are enough, then close.
            reply.close()
        finally:
            connection.close()
        result = requests.Response()
        result.status_code = status_code
        result.reason = reason
        result.url = url
        result.headers = requests.structures.CaseInsensitiveDict(response_headers)
        result._content = b""
        result.encoding = "utf-8"
        return result


def _status_from_exception(error):
    try:
        status = error.status_code
    except AttributeError:
        status = getattr(getattr(error, "response", None), "status_code", None)
    return status if type(status) is int else None


def _result(status_code):
    if status_code in (201, 202):
        result = "accepted"
    elif status_code in (404, 410):
        result = "expired"
    elif type(status_code) is int:
        result = "failed"
    else:
        result = "unknown"
    return {"result": result, "status_code": status_code}


def send(subscription, private_key, subject, payload, *, resolver=socket.getaddrinfo, sender=webpush):
    """Encrypt and deliver one notification without retrying or persisting state."""

    try:
        normalized = validate_subscription(subscription)
        _validate_private_key(private_key)
        _validate_subject(subject)
        serialized = _payload_json(payload)
        peer_ip = _resolve_peer(normalized["endpoint"], resolver)
    except _UnknownTransport:
        return {"result": "unknown", "status_code": None}
    except _BlockedInput:
        return {"result": "blocked", "status_code": None}
    except Exception:
        return {"result": "blocked", "status_code": None}

    session = _PinnedSession(normalized["endpoint"], peer_ip)
    try:
        response = sender(
            normalized,
            data=serialized,
            vapid_private_key=private_key,
            vapid_claims={"sub": subject},
            timeout=10,
            requests_session=session,
        )
        response_url = getattr(response, "url", None)
        if response_url not in (None, normalized["endpoint"]):
            return {"result": "blocked", "status_code": None}
        return _result(getattr(response, "status_code", None))
    except WebPushException as error:
        return _result(_status_from_exception(error))
    except _BlockedInput:
        return {"result": "blocked", "status_code": None}
    except (TimeoutError, socket.timeout, requests.exceptions.Timeout):
        return {"result": "unknown", "status_code": None}
    except Exception:
        return {"result": "unknown", "status_code": None}
