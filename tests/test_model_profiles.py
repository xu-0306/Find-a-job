"""Saved connections: synthetic credentials and temporary databases only."""
import tempfile
import unittest
import zipfile
from datetime import timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient

from app import backups, security, llm
from app.main import create_app, Settings, Defaults, Schedule
from app.model_profiles import MAX_PROFILES
from app.store import now

BASE = "/api/admin/model-profiles"
CONNECTION = dict(name="Provider A", llm_base_url="https://a.example.test/v1",
                  llm_model="same-model", llm_protocol="responses", llm_api_key="synthetic-key-a")


class ModelProfileChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(self.directory.name, automatic_daily=False)
        self.store = self.app.state.store
        self.token = "synthetic-admin-session"
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO sessions(id_hash,role,expires_at,device_id) VALUES(?,?,?,?)",
                       (security.digest(self.token), "admin", (now() + timedelta(days=1)).isoformat(), "test"))
        self.client = TestClient(self.app, base_url="http://localhost", client=("127.0.0.1", 1234))
        self.client.cookies.set(security.cookie_name("admin", False), self.token)
        self.client.headers.update({"Origin": "http://localhost", "X-CSRF-Token": security.csrf(self.token)})

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def create(self, **changes):
        response = self.client.post(BASE, json=CONNECTION | changes)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn((CONNECTION | changes)["llm_api_key"], response.text)
        return response.json()

    def stored(self, key, default=None):
        with self.store.connect() as db:
            return self.store.get(db, key, default)

    def test_same_model_and_alias_have_independent_ids_and_keys(self):
        first = self.create()
        second = self.create(llm_base_url="https://b.example.test/api", llm_api_key="synthetic-key-b")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["llm_model"], second["llm_model"])
        self.assertEqual(first["name"], second["name"])
        changed = self.client.put(BASE + "/" + first["id"], json=CONNECTION | {
            "name": "Renamed", "llm_model": "unseen-model-name", "llm_api_key": ""})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["id"], first["id"])
        profiles = self.stored("model_profiles")
        self.assertEqual(profiles[0]["llm_api_key"], CONNECTION["llm_api_key"])
        self.assertEqual(profiles[1]["llm_api_key"], "synthetic-key-b")
        self.assertEqual(profiles[1]["llm_model"], "same-model")
        view = self.client.get(BASE)
        self.assertNotIn("synthetic-key", view.text)
        self.assertEqual(len(view.json()["items"]), 2)
        reopened = create_app(self.directory.name, automatic_daily=False)
        with reopened.state.store.connect() as db:
            self.assertEqual(reopened.state.store.get(db, "model_profiles"), profiles)

    def test_apply_preserves_other_settings_and_edit_delete_do_not_change_active(self):
        services = dict(source_1111_enabled=False, source_518_enabled=True,
                        vapid_subject="mailto:test@example.test", admin_local_only=True)
        with self.store.connect(write=True) as db:
            self.store.put(db, "services", services)
            self.store.put(db, "search_provider", "Brave")
            self.store.put(db, "brave_api_key", "synthetic-brave")
            self.store.put(db, "tavily_api_key", "synthetic-tavily")
        first = self.create()
        second = self.create(llm_base_url="https://b.example.test/api", llm_api_key="synthetic-key-b",
                             llm_protocol="anthropic_messages")
        applied = self.client.post(BASE + "/" + first["id"] + "/apply")
        self.assertEqual(applied.status_code, 200)
        self.assertNotIn(CONNECTION["llm_api_key"], applied.text)
        self.assertEqual(self.stored("llm_api_key"), CONNECTION["llm_api_key"])
        with self.store.connect(write=True) as db:
            self.store.put(db, "llm_test", dict(signature=llm.signature(self.stored("services"), CONNECTION["llm_api_key"]),
                                              tools=True, reply=True, protocol="responses"))
        self.assertEqual(self.client.get("/api/admin/settings").json()["connection_test_status"], "passed")
        result = self.client.post(BASE + "/" + second["id"] + "/apply").json()
        self.assertEqual(result["connection_test_status"], "pending")
        self.assertEqual(self.stored("llm_api_key"), "synthetic-key-b")
        for key, value in services.items():
            self.assertEqual(self.stored("services")[key], value)
        for key, value in [("search_provider", "Brave"), ("brave_api_key", "synthetic-brave"),
                           ("tavily_api_key", "synthetic-tavily")]:
            self.assertEqual(self.stored(key), value)
        self.assertEqual([p["is_active"] for p in self.client.get(BASE).json()["items"]], [False, True])
        before = self.stored("services")
        self.assertEqual(self.client.put(BASE + "/" + second["id"], json=CONNECTION | {"llm_api_key": "replacement"}).status_code, 200)
        self.assertEqual(self.stored("services"), before)
        self.assertEqual(self.stored("llm_api_key"), "synthetic-key-b")
        self.assertEqual(self.client.delete(BASE + "/" + second["id"]).status_code, 204)
        self.assertEqual(self.stored("services"), before)
        self.assertEqual(self.stored("llm_api_key"), "synthetic-key-b")
        self.assertEqual(self.client.get(BASE).json()["items"][0]["id"], first["id"])
        for method, suffix in [("delete", ""), ("post", "/apply"), ("put", "")]:
            kwargs = {"json": CONNECTION} if method == "put" else {}
            self.assertEqual(getattr(self.client, method)(BASE + "/" + second["id"] + suffix, **kwargs).status_code, 404)

    def test_context_window_is_validated_saved_and_applied_without_retesting(self):
        for window in (1000, 31_999, 2_000_001, -1):
            with self.subTest(window=window):
                self.assertEqual(self.client.post(BASE, json=CONNECTION | {"llm_context_window": window}).status_code, 422)
                self.assertEqual(self.client.put("/api/admin/settings", json={"llm_context_window": window}).status_code, 422)
        large = self.create(llm_context_window=1_000_000)
        self.assertEqual(large["llm_context_window"], 1_000_000)
        default = self.create(llm_api_key="synthetic-key-b")
        self.assertEqual(default["llm_context_window"], 0, "0 means the built-in default budget")
        self.client.post(BASE + "/" + large["id"] + "/apply")
        self.assertEqual(self.stored("services")["llm_context_window"], 1_000_000)
        signature = llm.signature(self.stored("services"), CONNECTION["llm_api_key"])
        self.assertEqual(self.client.put("/api/admin/settings", json={"llm_context_window": 500_000}).json()["llm_context_window"], 500_000)
        self.assertEqual(llm.signature(self.stored("services"), CONNECTION["llm_api_key"]), signature,
                         "changing the window does not invalidate the connection test")
        # A profile saved before this setting existed applies the default.
        with self.store.connect(write=True) as db:
            profiles = self.store.get(db, "model_profiles")
            del profiles[1]["llm_context_window"]
            self.store.put(db, "model_profiles", profiles)
        self.assertEqual(self.client.get(BASE).json()["items"][1]["llm_context_window"], 0)
        self.assertEqual(self.client.post(BASE + "/" + default["id"] + "/apply").status_code, 200)
        self.assertEqual(self.stored("services")["llm_context_window"], 0)

    def test_active_state_includes_key_even_with_identical_model_url_and_protocol(self):
        a, b = self.create(), self.create(llm_api_key="synthetic-key-b")
        self.client.post(BASE + "/" + b["id"] + "/apply")
        self.assertEqual([p["is_active"] for p in self.client.get(BASE).json()["items"]], [False, True])

    def test_validation_auth_and_csrf(self):
        for change in [{"llm_api_key": ""}, {"name": " "}, {"llm_model": ""},
                       {"llm_base_url": "https://user:secret@example.test"}, {"llm_base_url": "file:///tmp"},
                       {"llm_protocol": "invalid"}, {"id": "client-supplied"}, {"llm_api_key": "x" * 4097}]:
            response = self.client.post(BASE, json=CONNECTION | change)
            self.assertEqual(response.status_code, 422, change)
            self.assertNotIn(CONNECTION["llm_api_key"], response.text)
        self.assertEqual(self.client.get(BASE).json(), {"items": []})
        profile = self.create()
        routes = [("get", BASE, {}), ("post", BASE, {"json": CONNECTION}),
                  ("put", BASE + "/" + profile["id"], {"json": CONNECTION}),
                  ("delete", BASE + "/" + profile["id"], {}),
                  ("post", BASE + "/" + profile["id"] + "/apply", {})]
        for method, route, kwargs in routes:
            with TestClient(self.app, base_url="http://localhost", client=("127.0.0.1", 1234)) as anonymous:
                self.assertEqual(getattr(anonymous, method)(route, headers={"Origin": "http://localhost"}, **kwargs).status_code, 401)
            if method != "get":
                self.assertEqual(getattr(self.client, method)(route, headers={"X-CSRF-Token": "wrong"}, **kwargs).status_code, 403)
                self.assertEqual(getattr(self.client, method)(route, headers={"Origin": "https://outside.test"}, **kwargs).status_code, 403)
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM sessions")
        self.assertEqual(self.client.post(BASE + "/" + profile["id"] + "/apply").status_code, 401)

    def test_parallel_create_does_not_lose_profiles_and_limit(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.client.post(BASE, json=CONNECTION), range(8)))
        self.assertTrue(all(response.status_code == 201 for response in responses))
        self.assertEqual(len({p["id"] for p in self.stored("model_profiles")}), 8)
        template = self.stored("model_profiles")[0]
        with self.store.connect(write=True) as db:
            self.store.put(db, "model_profiles", [template | {"id": format(i, "032x")} for i in range(MAX_PROFILES)])
        self.assertEqual(self.client.post(BASE, json=CONNECTION).status_code, 409)

    def test_backup_roundtrip_and_invalid_profiles_rejected(self):
        first, second = self.create(), self.create(llm_base_url="https://b.example.test/api", llm_api_key="synthetic-key-b")
        original = self.stored("model_profiles")
        backup = backups.create_backup(self.store)
        staging = Path(self.directory.name) / "staging"
        staging.mkdir()
        with zipfile.ZipFile(Path(self.directory.name) / "backups" / (backup["id"] + ".zip")) as archive:
            archive.extractall(staging)
        backups.validate_settings(staging / "app.db", Settings, Defaults, Schedule)
        self.client.delete(BASE + "/" + first["id"])
        backups.restore_backup(self.store, staging)
        self.assertEqual(self.stored("model_profiles"), original)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        for invalid in [original + [original[0]], [original[0] | {"llm_api_key": ""}],
                        [original[0] | {"llm_base_url": "file:///tmp"}]]:
            with self.store.connect(write=True) as db:
                self.store.put(db, "model_profiles", invalid)
            with self.assertRaises(ValueError):
                backups.validate_settings(self.store.path, Settings, Defaults, Schedule)
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM settings WHERE key='model_profiles'")
        backups.validate_settings(self.store.path, Settings, Defaults, Schedule)


if __name__ == "__main__":
    unittest.main()
