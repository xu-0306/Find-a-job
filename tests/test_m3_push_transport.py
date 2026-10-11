import base64
import json
import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from app import push


def encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def subscription(endpoint="https://Push.Example.test:443/v1/notify?x=1"):
    key = ec.generate_private_key(ec.SECP256R1()).public_key()
    public = key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": endpoint, "keys": {"p256dh": encode(public), "auth": encode(b"a" * 16)}}


def public_resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port))]


class PushTransportChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = push.generate_keys()
        cls.payload = {"title": "今日新增", "body": "有 3 筆新職缺", "url": "/?page=daily"}

    def test_generate_keys_are_der_p256_and_raw_public_key(self):
        private_der = base64.urlsafe_b64decode(self.keys["private_key"] + "==")
        private = serialization.load_der_private_key(private_der, password=None)
        public_raw = base64.urlsafe_b64decode(self.keys["public_key"] + "==")
        self.assertIsInstance(private, ec.EllipticCurvePrivateKey)
        self.assertIsInstance(private.curve, ec.SECP256R1)
        self.assertEqual(len(public_raw), 65)
        self.assertEqual(public_raw[0], 4)
        self.assertEqual(
            private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint),
            public_raw,
        )

    def test_validate_subscription_canonicalizes_endpoint_and_ignores_expiration(self):
        value = subscription()
        value["expirationTime"] = None
        normalized = push.validate_subscription(value)
        self.assertEqual(normalized["endpoint"], "https://push.example.test/v1/notify?x=1")
        self.assertEqual(set(normalized), {"endpoint", "keys"})
        self.assertNotIn("=", normalized["keys"]["auth"])
        for endpoint in [
            "http://push.example.test/v1",
            "https://push.example.test:444/v1",
            "https://user:password@push.example.test/v1",
            "https://push.example.test/v1#fragment",
            "https://push.example.test/v1#",
            "https://push.example.test/v1/ bad",
        ]:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                push.validate_subscription({**value, "endpoint": endpoint})
        with self.assertRaises(ValueError):
            push.validate_subscription({**value, "unexpected": True})
        with self.assertRaises(ValueError):
            push.validate_subscription({**value, "keys": {**value["keys"], "auth": encode(b"short")}})
        with self.assertRaises(ValueError):
            push.validate_subscription({**value, "keys": {"p256dh": encode(b"\x04" + b"\x00" * 64), "auth": value["keys"]["auth"]}})

    def test_dns_requires_every_resolved_address_to_be_global(self):
        calls = []

        def sender(*args, **kwargs):
            self.fail("blocked endpoint must never call sender")

        def resolver(host, port, **kwargs):
            calls.append((host, port))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))]

        result = push.send(subscription(), self.keys["private_key"], "mailto:owner@example.com", self.payload,
                           resolver=resolver, sender=sender)
        self.assertEqual(result, {"result": "blocked", "status_code": None})
        self.assertEqual(calls, [("push.example.test", 443)])
        for address in ["127.0.0.1", "::1", "192.168.1.5", "2001:db8::1", "224.0.0.1", "::ffff:224.0.0.1"]:
            with self.subTest(address=address):
                result = push.send(
                    subscription(), self.keys["private_key"], "https://example.com", self.payload,
                    resolver=lambda host, port, **kwargs: [(socket.AF_INET6 if ":" in address else socket.AF_INET,
                                                            socket.SOCK_STREAM, 6, "", (address, port))],
                    sender=sender,
                )
                self.assertEqual(result["result"], "blocked")
        accepted = push.send(
            subscription(), self.keys["private_key"], "mailto:owner@example.com", self.payload,
            resolver=lambda host, port, **kwargs: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:4860:4860::8888", port))],
            sender=lambda *args, **kwargs: SimpleNamespace(status_code=201),
        )
        self.assertEqual(accepted["result"], "accepted")

        started = threading.Event()
        release = threading.Event()

        def blocking_resolver(host, port, **kwargs):
            started.set()
            release.wait(1)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

        with patch.object(push, "_RESOLVER_TIMEOUT", 0.01):
            timeout = push.send(
                subscription(), self.keys["private_key"], "mailto:owner@example.com", self.payload,
                resolver=blocking_resolver, sender=sender,
            )
        self.assertEqual(timeout, {"result": "unknown", "status_code": None})
        self.assertTrue(started.wait(0.2))
        release.set()


    def test_status_mapping_payload_boundary_and_unknown_errors(self):
        observed = []

        def sender(info, **kwargs):
            observed.append((info, kwargs))
            return SimpleNamespace(status_code=201)

        result = push.send(subscription(), self.keys["private_key"], "mailto:owner@example.com", self.payload,
                           resolver=public_resolver, sender=sender)
        self.assertEqual(result, {"result": "accepted", "status_code": 201})
        info, kwargs = observed[-1]
        self.assertEqual(info["endpoint"], "https://push.example.test/v1/notify?x=1")
        decoded = json.loads(kwargs["data"])
        self.assertEqual(decoded, self.payload)
        self.assertNotIn("endpoint", decoded)

        for status, expected in [(202, "accepted"), (404, "expired"), (410, "expired"), (500, "failed")]:
            with self.subTest(status=status):
                result = push.send(subscription(), self.keys["private_key"], "https://example.com", self.payload,
                                   resolver=public_resolver,
                                   sender=lambda *args, status=status, **kwargs: SimpleNamespace(status_code=status))
                self.assertEqual(result, {"result": expected, "status_code": status})

        changed_url = push.send(
            subscription(), self.keys["private_key"], "https://example.com", self.payload,
            resolver=public_resolver,
            sender=lambda *args, **kwargs: SimpleNamespace(status_code=201, url="https://other.example/"),
        )
        self.assertEqual(changed_url, {"result": "blocked", "status_code": None})

        for error, expected in [(WebPushException("gone", response=SimpleNamespace(status_code=410)), "expired"),
                                (requests.exceptions.Timeout("timeout"), "unknown"),
                                (RuntimeError("raw secret must not escape"), "unknown")]:
            with self.subTest(error=type(error).__name__):
                result = push.send(subscription(), self.keys["private_key"], "https://example.com", self.payload,
                                   resolver=public_resolver,
                                   sender=lambda *args, error=error, **kwargs: (_ for _ in ()).throw(error))
                self.assertEqual(result["result"], expected)
                self.assertNotIn("raw secret", json.dumps(result))

        for invalid_payload in [
            {**self.payload, "endpoint": "https://private.invalid"},
            {**self.payload, "body": "x" * 2001},
            {**self.payload, "url": "/?page=daily&token=secret"},
        ]:
            with self.subTest(invalid_payload=invalid_payload):
                result = push.send(subscription(), self.keys["private_key"], "https://example.com", invalid_payload,
                                   resolver=public_resolver, sender=sender)
                self.assertEqual(result["result"], "blocked")

        self.assertEqual(push.send(subscription(), self.keys["private_key"], "ftp://example.com", self.payload,
                                   resolver=public_resolver, sender=sender)["result"], "blocked")
        for subject in ["", "ftp://example.com", "mailto:", "https://user:pass@example.com"]:
            with self.subTest(subject=subject):
                self.assertEqual(push.send(subscription(), self.keys["private_key"], subject, self.payload,
                                           resolver=public_resolver, sender=sender)["result"], "blocked")
        for subject in [
            "mailto:owner@example.com",
            "https://example.com/contact",
            "https://example.com:443",
            "https://203.0.113.10:8443/contact",
            "https://[2001:db8::10]:65535/contact",
        ]:
            with self.subTest(subject=subject):
                self.assertTrue(push.valid_subject(subject))
        for subject in [
            None,
            "mailto:",
            "mailto:owner",
            "mailto:owner@example.com?subject=x",
            "https://example.com#",
            "https://user:pass@example.com",
            "https://203.0.113.10:0/contact",
            "https://[2001:db8::10]:65536/contact",
            "ftp://example.com",
        ]:
            with self.subTest(subject=subject):
                self.assertFalse(push.valid_subject(subject))

    def test_pywebpush_performs_real_encryption_before_mock_session_post(self):
        response = requests.Response()
        response.status_code = 202
        response._content = b""
        with patch.object(push._PinnedSession, "post", return_value=response) as post:
            result = push.send(subscription(), self.keys["private_key"], "mailto:owner@example.com", self.payload,
                               resolver=public_resolver, sender=webpush)
        self.assertEqual(result, {"result": "accepted", "status_code": 202})
        self.assertEqual(post.call_count, 1)
        self.assertGreater(len(post.call_args.kwargs["data"]), 0)

    def test_pinned_connection_uses_numeric_peer_and_original_sni(self):
        class RawSocket:
            def __init__(self):
                self.closed = False

            def setsockopt(self, *args):
                self.sockopt = args

            def close(self):
                self.closed = True

        class TLSSocket(RawSocket):
            pass

        raw = RawSocket()
        tls = TLSSocket()
        context = SimpleNamespace(wrap_socket=lambda sock, server_hostname: (setattr(context, "sni", server_hostname) or tls))
        with (patch("app.push.socket.create_connection", return_value=raw) as connect,
              patch("app.push.ssl.create_default_context", return_value=context)):
            connection = push._PinnedHTTPSConnection("push.example.test", "8.8.8.8", 10)
            connection.connect()
            connection.close()
        connect.assert_called_once_with(("8.8.8.8", 443), timeout=10)
        self.assertEqual(context.sni, "push.example.test")
        self.assertTrue(tls.closed)

    def test_pinned_session_preserves_path_host_and_never_follows_redirect(self):
        calls = []

        class Reply:
            status = 302
            reason = "Found"

            def getheaders(self):
                return [("Location", "https://other.example/")]

            def close(self):
                calls.append("reply-close")

        class Connection:
            def __init__(self, host, peer_ip, timeout):
                calls.append(("connect", host, peer_ip, timeout))

            def request(self, method, path, body, headers):
                calls.append(("request", method, path, body, headers))

            def getresponse(self):
                return Reply()

            def close(self):
                calls.append("connection-close")

        endpoint = "https://push.example.test/path/to/push?token=abc"
        with patch("app.push._PinnedHTTPSConnection", Connection):
            result = push._PinnedSession(endpoint, "8.8.8.8").post(
                endpoint, data=b"encrypted", headers={"Authorization": "vapid"}, timeout=10)
        self.assertEqual(result.status_code, 302)
        request_call = next(call for call in calls if call[0] == "request")
        self.assertEqual(request_call[1:4], ("POST", "/path/to/push?token=abc", b"encrypted"))
        self.assertEqual(request_call[4]["Host"], "push.example.test")
        self.assertEqual([call[0] for call in calls if isinstance(call, tuple) and call[0] == "request"].count("request"), 1)
        with patch("app.push._PinnedHTTPSConnection", Connection):
            with self.assertRaises(ValueError):
                push._PinnedSession(endpoint, "8.8.8.8").post(endpoint, data=b"x", timeout=10, allow_redirects=True)


if __name__ == "__main__":
    unittest.main()
