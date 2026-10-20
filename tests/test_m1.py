"""Offline M1 checks: no real credentials, network, jobs, or conversations."""
import json
import io
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.__main__ import main as cli_main
from app.security import digest, password_hash
from app.store import Job, now

PASSWORD = "offline-test-password-only"
ORIGIN = "http://localhost"
CRITERIA = {"keyword": "測試條件", "area_codes": ["3001008001"], "min_monthly_salary": 30000}


class M1Checks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(self.directory.name, automatic_daily=False)
        self.store = self.app.state.store
        self.admin = self.client()
        with self.store.connect(write=True) as db:
            self.store.put(db, "admin_password_hash", password_hash(PASSWORD))
        context = self.admin.get("/api/admin/login-context").json()
        result = self.admin.post("/api/admin/login", json={"password": PASSWORD},
                                 headers={"Origin": ORIGIN, "X-CSRF-Token": context["csrf_token"]})
        self.assertEqual(result.status_code, 200, result.text)
        self.admin.headers.update({"Origin": ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]})
        self.access_path = self.admin.post("/api/admin/users/default/access/reset").json()["access_path"]
        self.user = self.user_client()

    def tearDown(self):
        self.admin.close()
        self.user.close()
        self.directory.cleanup()

    def client(self, app=None, base_url=ORIGIN, peer="127.0.0.1"):
        return TestClient(app or self.app, base_url=base_url, client=(peer, 50000))

    def user_client(self):
        client = self.client()
        response = client.get(self.access_path, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        bootstrap = client.get("/api/bootstrap").json()
        client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": bootstrap["csrf_token"]})
        return client

    def propose(self, operation="create", preference=None, after=CRITERIA, client=None):
        body = {"operation": operation}
        if preference:
            body.update(preference_id=preference["id"], base_revision=preference["revision"])
        if operation != "delete":
            body["after"] = after
        result = (client or self.user).post("/api/preference-proposals", json=body)
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def resolve(self, proposal, action="confirm", client=None):
        return (client or self.user).post(f"/api/preference-proposals/{proposal['id']}/{action}")

    def preferences(self):
        return self.user.get("/api/preferences").json()

    def test_isolation_origin_csrf_and_private_headers(self):
        with self.client() as anonymous:
            self.assertEqual(anonymous.get("/api/bootstrap").status_code, 401)
            self.assertEqual(anonymous.get("/api/admin/settings").status_code, 401)
        self.assertEqual(self.user.get("/api/admin/settings").status_code, 401)
        self.assertEqual(self.admin.get("/api/bootstrap").status_code, 401)
        self.assertEqual(self.admin.post("/api/preference-proposals", json={"operation": "create", "after": CRITERIA}).status_code, 401)
        self.assertEqual(self.user.post("/api/preference-proposals", json={"operation": "create", "after": CRITERIA},
                                        headers={"Origin": "https://attacker.invalid"}).status_code, 403)
        self.assertEqual(self.user.post("/api/logout", headers={"X-CSRF-Token": "wrong"}).status_code, 403)
        self.assertEqual(self.user.post("/api/logout", headers={b"X-CSRF-Token": b"\xe9"}).status_code, 403)
        self.assertEqual(self.user.post("/api/logout", headers={"Origin": ""}).status_code, 403)
        response = self.user.get("/api/bootstrap")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        cookie = self.user.cookies.get("jc_user")
        with self.store.connect() as db:
            hashes = [row[0] for row in db.execute("SELECT id_hash FROM sessions")]
        self.assertIn(digest(cookie), hashes)
        self.assertNotIn(cookie, hashes)
        self.assertNotIn(PASSWORD, self.store.path.read_bytes().decode("latin1"))

    def test_loopback_tunnel_host_and_forwarded_headers(self):
        with self.client(base_url="http://public.example") as tunnel:
            self.assertEqual(tunnel.get("/admin", headers={"X-Forwarded-For": "127.0.0.1"}).status_code, 403)
            self.assertEqual(tunnel.get("/api/admin/login-context").status_code, 403)
        with self.client(peer="192.0.2.1") as remote:
            self.assertEqual(remote.get("/admin", headers={"X-Forwarded-For": "127.0.0.1"}).status_code, 403)
        self.assertEqual(self.admin.get("/admin").status_code, 200)
        self.admin.put("/api/admin/settings", json={"admin_local_only": False})
        with self.client(base_url="http://public.example", peer="192.0.2.1") as remote:
            self.assertEqual(remote.get("/admin").status_code, 200)
            self.assertEqual(remote.get("/api/admin/settings").status_code, 401)

    def test_revocation_every_device_old_link_and_push(self):
        other = self.user_client()
        try:
            with self.store.connect(write=True) as db:
                db.execute("INSERT INTO push_subscriptions VALUES(?,?,?,?,?,?)",
                           ('offline-subscription', 'offline-device', 'https://push.invalid/test', '{}', now().isoformat(), 'default'))
            replacement = self.admin.post("/api/admin/users/default/access/reset").json()["access_path"]
            self.assertNotEqual(replacement, self.access_path)
            self.assertEqual(self.user.get("/api/bootstrap").status_code, 401)
            self.assertEqual(other.get("/api/bootstrap").status_code, 401)
            self.assertEqual(other.get(self.access_path, follow_redirects=False).status_code, 401)
            self.assertEqual(other.get(replacement, follow_redirects=False).status_code, 303)
            with self.store.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0], 0)
                stored = db.execute("SELECT access_token_hash FROM users WHERE id='default'").fetchone()[0]
                self.assertNotEqual(stored, replacement.split("token=")[1])
                self.assertEqual(stored, digest(replacement.split("token=")[1]))
            self.assertEqual(self.admin.get("/api/admin/settings").status_code, 200)
        finally:
            other.close()

    def test_https_cookies_idle_expiry_and_logout(self):
        with self.client(base_url="https://localhost") as secure:
            response = secure.get(self.access_path, follow_redirects=False)
            self.assertIn("__Host-jc_user", response.headers["set-cookie"])
            for attribute in ["HttpOnly", "Secure", "SameSite=lax", "Path=/"]:
                self.assertIn(attribute, response.headers["set-cookie"])
            self.assertEqual(secure.get("/api/bootstrap").status_code, 200)
        with self.store.connect(write=True) as db:
            old_expiry = (now() + timedelta(minutes=1)).isoformat()
            db.execute("UPDATE sessions SET expires_at=? WHERE role='user'", (old_expiry,))
        self.user.get("/api/bootstrap")
        with self.store.connect() as db:
            extended = db.execute("SELECT expires_at FROM sessions WHERE id_hash=?", (digest(self.user.cookies.get("jc_user")),)).fetchone()[0]
        self.assertGreater(extended, old_expiry)
        self.assertEqual(self.user.post("/api/logout").status_code, 204)
        self.assertEqual(self.user.get("/api/bootstrap").status_code, 401)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE sessions SET expires_at=? WHERE role='admin'", ((now() - timedelta(seconds=1)).isoformat(),))
        self.assertEqual(self.admin.get("/api/admin/session").status_code, 401)

    def test_password_cooldown_and_uninitialized(self):
        with self.client() as client:
            context = client.get("/api/admin/login-context").json()
            headers = {"Origin": ORIGIN, "X-CSRF-Token": context["csrf_token"]}
            for index in range(5):
                result = client.post("/api/admin/login", json={"password": "wrong"}, headers=headers)
                self.assertEqual(result.status_code, 429 if index == 4 else 401)
            self.assertEqual(client.post("/api/admin/login", json={"password": PASSWORD}, headers=headers).status_code, 429)
            with patch("app.main.now", return_value=now() + timedelta(minutes=16)):
                self.assertEqual(client.post("/api/admin/login", json={"password": PASSWORD}, headers=headers).status_code, 200)
        with tempfile.TemporaryDirectory() as blank:
            app = create_app(blank, automatic_daily=False)
            with self.client(app) as client:
                context = client.get("/api/admin/login-context").json()
                self.assertFalse(context["initialized"])
                self.assertEqual(client.post("/api/admin/login", json={"password": PASSWORD},
                    headers={"Origin": ORIGIN, "X-CSRF-Token": context["csrf_token"]}).status_code, 503)

    def test_password_minimum_four_without_other_length_or_format_limits(self):
        with patch.dict(os.environ, {"JOB_COMPANION_DATA": self.directory.name}), patch("sys.argv", ["app", "init-admin"]):
            with patch("app.__main__.getpass.getpass", return_value="abc"), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as rejected:
                    cli_main()
                self.assertEqual(rejected.exception.code, 2)
            for password in ["1234", "aaaa", "    ", "測試密碼", "x" * 201]:
                with self.subTest(length=len(password)), patch("app.__main__.getpass.getpass", side_effect=[password, password]), patch("sys.stdout", new_callable=io.StringIO):
                    cli_main()
                    with self.client() as client:
                        context = client.get("/api/admin/login-context").json()
                        result = client.post("/api/admin/login", json={"password": password},
                            headers={"Origin": ORIGIN, "X-CSRF-Token": context["csrf_token"]})
                        self.assertEqual(result.status_code, 200, result.text)

    def test_settings_mask_preserve_clear_restart_and_validation(self):
        key = "offline-secret-sentinel"
        self.assertEqual(self.admin.put("/api/admin/settings", json={"llm_api_key": key, "llm_model": "test-model"}).status_code, 200)
        response = self.admin.get("/api/admin/settings")
        self.assertNotIn(key, response.text)
        self.assertTrue(response.json()["llm_api_key"]["configured"])
        self.assertEqual(self.admin.put("/api/admin/settings", json={"llm_api_key": ""}).status_code, 200)
        with self.store.connect() as db:
            self.assertEqual(self.store.get(db, "llm_api_key"), key)
        bootstrap = self.user.get("/api/bootstrap").text
        for forbidden in [key, "llm_api_key", "password_hash", "access_token", "endpoint"]:
            self.assertNotIn(forbidden, bootstrap)
        restarted = create_app(self.directory.name, automatic_daily=False)
        with self.client(restarted) as client:
            client.cookies.update(self.admin.cookies)
            self.assertEqual(client.get("/api/admin/settings").json()["llm_model"], "test-model")
        for body in [{"llm_base_url": "javascript:alert(1)"}, {"llm_base_url": "https://user:password@example.com"},
                     {"llm_api_key": key, "clear_secrets": ["llm_api_key"]}, {"llm_api_key": key, "unknown": True}]:
            result = self.admin.put("/api/admin/settings", json=body)
            self.assertEqual(result.status_code, 422)
            self.assertNotIn(key, result.text)
        self.admin.put("/api/admin/settings", json={"clear_secrets": ["llm_api_key"]})
        self.assertFalse(self.admin.get("/api/admin/settings").json()["llm_api_key"]["configured"])
        self.assertEqual(self.user.get("/data/app.db").status_code, 404)
        self.assertEqual(self.user.get("/static/../store.py").status_code, 404)

    def test_source_518_switch_defaults_on_and_survives_restart(self):
        self.assertTrue(self.admin.get("/api/admin/settings").json()["source_518_enabled"])
        self.assertEqual(self.admin.put("/api/admin/settings", json={"source_518_enabled": False}).status_code, 200)
        self.assertFalse(self.admin.get("/api/admin/settings").json()["source_518_enabled"])
        restarted = create_app(self.directory.name, automatic_daily=False)
        with self.client(restarted) as client:
            client.cookies.update(self.admin.cookies)
            self.assertFalse(client.get("/api/admin/settings").json()["source_518_enabled"])
        self.assertEqual(self.admin.put("/api/admin/settings", json={"source_518_enabled": True}).status_code, 200)
        self.assertTrue(self.admin.get("/api/admin/settings").json()["source_518_enabled"])

    def test_confirm_cancel_stale_versions_and_empty_defaults(self):
        self.admin.put("/api/admin/default-preferences", json={"preferences": [CRITERIA]})
        self.assertTrue(self.preferences()["effective"][0]["is_default"])
        pending = self.propose()
        self.assertFalse(self.preferences()["user_has_chosen"])
        self.assertEqual(self.resolve(pending, "cancel").status_code, 200)
        self.assertFalse(self.preferences()["user_has_chosen"])
        proposal = self.propose()
        self.assertEqual(self.resolve(proposal).status_code, 200)
        self.assertEqual(self.resolve(proposal).status_code, 200)
        preference = self.preferences()["items"][0]
        self.assertEqual(len(self.preferences()["items"]), 1)
        newer = self.propose("update", preference, {**CRITERIA, "keyword": "更新條件"})
        stale = self.propose("delete", preference)
        self.assertEqual(self.resolve(newer).status_code, 200)
        self.assertEqual(self.resolve(stale).status_code, 409)
        current = self.preferences()["items"][0]
        self.assertEqual(current["revision"], 2)
        self.admin.put("/api/admin/default-preferences", json={"preferences": [{**CRITERIA, "keyword": "新預設"}]})
        self.assertEqual(self.preferences()["effective"][0]["criteria"]["keyword"], "更新條件")
        self.assertEqual(self.resolve(self.propose("delete", current)).status_code, 200)
        self.assertTrue(self.preferences()["paused"])
        self.assertEqual(self.preferences()["effective"], [])
        self.assertEqual(self.preferences()["items"], [])

    def test_admin_default_preferences_normalize_legacy_remote_field_before_change_check(self):
        with self.store.connect(write=True) as db:
            self.store.put(db, "default_preferences", [dict(CRITERIA)])
            self.store.put(db, "default_preferences_revision", 7)
        daily = self.app.state.daily
        with patch.object(daily, "request_baseline") as request_baseline:
            response = self.admin.put("/api/admin/default-preferences", json={"preferences": [CRITERIA]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["preferences"][0]["remote_only"])
            with self.store.connect() as db:
                self.assertEqual(self.store.get(db, "default_preferences_revision"), 7)
            request_baseline.assert_not_called()

        with patch.object(daily, "request_baseline") as request_baseline:
            changed = self.admin.put("/api/admin/default-preferences", json={
                "preferences": [{**CRITERIA, "remote_only": True}]})
            self.assertEqual(changed.status_code, 200, changed.text)
            with self.store.connect() as db:
                self.assertEqual(self.store.get(db, "default_preferences_revision"), 8)
            request_baseline.assert_called_once_with()

    def test_concurrent_confirmation_writes_once(self):
        proposal = self.propose()
        other = self.user_client()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.resolve, proposal, client=client) for client in [self.user, other]]
                self.assertEqual([future.result().status_code for future in futures], [200, 200])
            self.assertEqual(len(self.preferences()["items"]), 1)
        finally:
            other.close()

    def test_favorites_identity_pagination_shared_and_retained_snapshot(self):
        self.assertEqual(self.user.put("/api/favorites/unknown").status_code, 404)
        self.assertEqual(self.user.post("/api/jobs", json={"title": "fake"}).status_code, 404)
        self.assertEqual(self.user.get("/api/favorites").json()["items"], [])
        job = Job(source="104", source_job_id="offline-test", title="測試職缺", company_name="測試公司",
                  url="https://example.invalid/job", summary="<script>not executable</script>")
        job_id = self.store.save_job(job)
        self.assertEqual(self.store.save_job(job), job_id)
        shifted = Job(**{**job.model_dump(), "published_at": "2026-10-05T09:00:00+08:00"})
        self.assertEqual(shifted.published_at, datetime(2026, 10, 5, 1, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            Job(**{**job.model_dump(), "published_at": "2026-10-05T09:00:00"})
        other_id = self.store.save_job(job.model_copy(update={"source": "518"}))
        for identifier in [job_id, job_id, other_id]:
            self.assertEqual(self.user.put(f"/api/favorites/{identifier}").status_code, 200)
        with self.user_client() as other:
            first = other.get("/api/favorites?limit=1").json()
            self.assertTrue(first["items"][0]["is_favorite"])
            second = other.get("/api/favorites", params={"cursor": first["next_cursor"], "limit": 1}).json()
            self.assertIsNone(second["next_cursor"])
            self.assertEqual(len({first["items"][0]["id"], second["items"][0]["id"]}), 2)
            self.assertEqual(first["items"][0]["summary"], job.summary)
        restarted = create_app(self.directory.name, automatic_daily=False)
        with self.client(restarted) as client:
            client.cookies.update(self.user.cookies)
            self.assertEqual(len(client.get("/api/favorites").json()["items"]), 2)
        for _ in range(2):
            self.assertEqual(self.user.delete(f"/api/favorites/{job_id}").status_code, 204)
        self.assertEqual(len(self.user.get("/api/favorites").json()["items"]), 1)
        self.assertEqual(self.user.get("/api/favorites?cursor=bad").status_code, 422)
        self.assertEqual(self.user.get("/api/favorites?limit=101").status_code, 422)

    def test_invalid_proposals_leave_state_unchanged(self):
        for body in [{"operation": "confirm", "after": CRITERIA}, {"operation": "create"},
                     {"operation": "delete", "preference_id": "missing", "base_revision": 1, "after": CRITERIA},
                     {"operation": "create", "after": {**CRITERIA, "area_codes": ["not-a-code"]}},
                     {"operation": "create", "after": {**CRITERIA, "min_monthly_salary": -1}}]:
            self.assertEqual(self.user.post("/api/preference-proposals", json=body).status_code, 422)
        self.assertEqual(self.preferences()["items"], [])
        self.assertEqual(self.preferences()["proposals"], [])


if __name__ == "__main__":
    unittest.main()
