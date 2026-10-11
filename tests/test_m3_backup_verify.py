import hashlib
from io import BytesIO
import json
from pathlib import Path
import stat
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from PIL import Image

from app.backup_verify import (
    MAX_ARCHIVE_BYTES,
    MAX_ENTRIES,
    MAX_TOTAL_BYTES,
    validate_backup,
)


SCHEMA_SQL = """
CREATE TABLE settings(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE sessions(id_hash TEXT PRIMARY KEY, role TEXT NOT NULL, expires_at TEXT NOT NULL, device_id TEXT);
CREATE TABLE login_attempts(id_hash TEXT PRIMARY KEY, attempted_at TEXT NOT NULL);
CREATE TABLE sprites(
    state TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    frame_width INTEGER NOT NULL,
    frame_height INTEGER NOT NULL,
    frame_count INTEGER NOT NULL,
    fps INTEGER NOT NULL
);
"""


def png_bytes(width=1, height=1, color="red"):
    image = Image.new("RGBA", (width, height), color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def schema_catalog(db_path):
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        connection.close()


class BackupVerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stage_counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def make_database(self, *, sprite=None, setting='{"theme":"A"}', user_version=4):
        path = self.root / f"source_{len(list(self.root.glob('source_*.db')))}.db"
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version = {int(user_version)}")
            connection.execute("INSERT INTO settings(key,value_json) VALUES(?,?)", ("config", setting))
            if sprite is not None:
                connection.execute(
                    "INSERT INTO sprites(state,path,frame_width,frame_height,frame_count,fps) "
                    "VALUES(?,?,?,?,?,?)",
                    sprite,
                )
            connection.commit()
        finally:
            connection.close()
        return path, schema_catalog(path)

    def make_archive(self, db_path, sprite_files=None, manifest=None, extra_entries=None):
        sprite_files = sprite_files or {}
        payloads = {"app.db": db_path.read_bytes()}
        payloads.update({f"sprites/{name}": data for name, data in sprite_files.items()})
        if manifest is None:
            manifest = {
                "format_version": 1,
                "schema_version": 4,
                "files": [
                    {
                        "path": path,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    for path, data in payloads.items()
                ],
            }
        archive_path = self.root / "backup.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest).encode("utf-8"))
            for path, data in payloads.items():
                archive.writestr(path, data)
            for path, data in (extra_entries or {}).items():
                archive.writestr(path, data)
        return archive_path

    def new_stage(self, name="stage"):
        if name == "stage":
            name = f"stage_{self.stage_counter}"
            self.stage_counter += 1
        path = self.root / name
        path.mkdir()
        return path

    def assert_invalid(self, archive_path, schema, stage=None):
        if stage is None:
            stage = self.new_stage()
        with self.assertRaises(ValueError) as raised:
            validate_backup(archive_path, stage, schema)
        self.assertNotIn("secret-value", str(raised.exception))
        self.assertNotIn("source.db", str(raised.exception))

    def test_valid_backup_extracts_only_to_empty_staging_and_returns_manifest_files(self):
        db_path, schema = self.make_database(
            sprite=("idle", "idle.png", 1, 1, 1, 10),
            setting='{"vapid_private_key":"secret-value","theme":"A"}',
        )
        archive_path = self.make_archive(db_path, {"idle.png": png_bytes()})
        stage = self.new_stage()

        result = validate_backup(archive_path, stage, schema)

        self.assertEqual(result, {"schema_version": 4, "files": ["app.db", "sprites/idle.png"]})
        self.assertTrue((stage / "manifest.json").is_file())
        self.assertTrue((stage / "app.db").is_file())
        self.assertTrue((stage / "sprites/idle.png").is_file())

    def test_manifest_requires_exact_fields_entries_sizes_and_hashes(self):
        db_path, schema = self.make_database()
        valid_archive = self.make_archive(db_path)
        valid_manifest = {
            "format_version": 1,
            "schema_version": 4,
            "files": [
                {
                    "path": "app.db",
                    "size": db_path.stat().st_size,
                    "sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
                }
            ],
        }
        bad_manifests = [
            {**valid_manifest, "extra": True},
            {
                "format_version": 1,
                "schema_version": 4,
                "files": [{"path": "app.db", "size": 0, "sha256": "0" * 64}],
            },
            {
                "format_version": 1,
                "schema_version": 4,
                "files": [
                    valid_manifest["files"][0],
                    valid_manifest["files"][0],
                ],
            },
            {
                "format_version": 1,
                "schema_version": 4,
                "files": [{"path": "unknown.bin", "size": 0, "sha256": "0" * 64}],
            },
        ]
        for index, manifest in enumerate(bad_manifests):
            with self.subTest(index=index):
                archive_path = self.make_archive(db_path, manifest=manifest)
                self.assert_invalid(archive_path, schema)

    def test_rejects_unsafe_paths_duplicates_and_unknown_entries_before_extract(self):
        db_path, schema = self.make_database()
        unsafe_names = [
            "sprites/../escape.png",
            "sprites\\escape.png",
            "C:/escape.png",
            "//server/share/escape.png",
            "sprites/escape.png",
        ]
        for index, unsafe_name in enumerate(unsafe_names):
            with self.subTest(index=index):
                archive_path = self.make_archive(
                    db_path,
                    extra_entries={unsafe_name: b"bad"},
                )
                self.assert_invalid(archive_path, schema)

        duplicate_path = self.root / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate_path, "w") as archive:
                archive.writestr("manifest.json", b"{}")
                archive.writestr("app.db", b"bad")
                archive.writestr("sprites/one.png", b"x")
                archive.writestr("sprites/one.png", b"x")
        self.assert_invalid(duplicate_path, schema)

    def test_rejects_entry_modes_crc_encryption_and_truncated_zip(self):
        db_path, schema = self.make_database()
        symlink_archive = self.root / "symlink.zip"
        link_info = zipfile.ZipInfo("sprites/link.png")
        link_info.create_system = 3
        link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(symlink_archive, "w") as archive:
            archive.writestr("manifest.json", b"{}")
            archive.writestr("app.db", db_path.read_bytes())
            archive.writestr(link_info, b"target")
        self.assert_invalid(symlink_archive, schema)

        valid_archive = self.make_archive(db_path)
        encrypted_raw = bytearray(valid_archive.read_bytes())
        local_signature = b"PK\x03\x04"
        local_offset = encrypted_raw.find(local_signature)
        central_offset = encrypted_raw.find(b"PK\x01\x02")
        self.assertGreaterEqual(local_offset, 0)
        self.assertGreaterEqual(central_offset, 0)
        encrypted_raw[local_offset + 6] |= 1
        encrypted_raw[central_offset + 8] |= 1
        encrypted_archive = self.root / "encrypted.zip"
        encrypted_archive.write_bytes(encrypted_raw)
        self.assert_invalid(encrypted_archive, schema)

        raw = bytearray(valid_archive.read_bytes())
        central_offset = raw.find(b"PK\x01\x02")
        self.assertGreaterEqual(central_offset, 0)
        raw[central_offset + 16] ^= 1
        crc_archive = self.root / "crc.zip"
        crc_archive.write_bytes(raw)
        self.assert_invalid(crc_archive, schema)

        truncated_archive = self.root / "truncated.zip"
        truncated_archive.write_bytes(valid_archive.read_bytes()[:-12])
        self.assert_invalid(truncated_archive, schema)

    def test_rejects_archive_entry_and_total_limits(self):
        db_path, schema = self.make_database()
        archive_path = self.make_archive(db_path)
        with patch("app.backup_verify.MAX_ARCHIVE_BYTES", 1):
            self.assert_invalid(archive_path, schema)
        with patch("app.backup_verify.MAX_ENTRIES", 1):
            self.assert_invalid(archive_path, schema)
        with patch("app.backup_verify.MAX_TOTAL_BYTES", 1):
            self.assert_invalid(archive_path, schema)

        self.assertEqual(MAX_ARCHIVE_BYTES, 128 * 1024**2)
        self.assertEqual(MAX_TOTAL_BYTES, 256 * 1024**2)
        self.assertEqual(MAX_ENTRIES, 32)

    def test_schema_user_version_and_required_table_checks_happen_before_integrity(self):
        db_path, schema = self.make_database()
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("CREATE TABLE extra_table(value TEXT)")
            connection.commit()
        finally:
            connection.close()
        self.assert_invalid(self.make_archive(db_path), schema)

        db_path, schema = self.make_database(user_version=5)
        self.assert_invalid(self.make_archive(db_path), schema)

        db_path, schema = self.make_database()
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "INSERT INTO sessions(id_hash,role,expires_at,device_id) VALUES('x','user','now','d')"
            )
            connection.commit()
        finally:
            connection.close()
        self.assert_invalid(self.make_archive(db_path), schema)

        db_path, schema = self.make_database(setting="not-json")
        self.assert_invalid(self.make_archive(db_path), schema)

    def test_sprite_rows_must_match_archive_and_pass_real_png_validation(self):
        db_path, schema = self.make_database(
            sprite=("idle", "idle.png", 1, 1, 1, 10)
        )
        self.assert_invalid(self.make_archive(db_path), schema)

        db_path, schema = self.make_database()
        extra_archive = self.make_archive(db_path, {"extra.png": png_bytes()})
        self.assert_invalid(extra_archive, schema)

        db_path, schema = self.make_database(
            sprite=("idle", "idle.png", 1, 1, 1, 10)
        )
        self.assert_invalid(
            self.make_archive(db_path, {"idle.png": b"not a png"}),
            schema,
        )

        db_path, schema = self.make_database(
            sprite=("idle", "../idle.png", 1, 1, 1, 10)
        )
        self.assert_invalid(
            self.make_archive(db_path, {"idle.png": png_bytes()}),
            schema,
        )

    def test_rejects_nonempty_and_symlink_staging_roots(self):
        db_path, schema = self.make_database()
        archive_path = self.make_archive(db_path)

        nonempty = self.new_stage("nonempty")
        (nonempty / "leftover").write_bytes(b"x")
        self.assert_invalid(archive_path, schema, nonempty)

        link_root = self.root / "stage-link"
        target = self.new_stage("stage-target")
        try:
            link_root.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("此環境不允許建立 symlink")
        self.assert_invalid(archive_path, schema, link_root)


if __name__ == "__main__":
    unittest.main()
