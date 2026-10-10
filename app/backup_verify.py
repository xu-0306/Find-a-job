"""安全驗證並解出 M3 備份 ZIP 到呼叫者提供的暫存目錄。

這個模組只處理暫存目錄。它不碰 Store、不替換正式資料庫或素材，也不會
建立任何 live 檔案；真正的備份、替換與還原交易由主流程負責。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sqlite3
from typing import Iterable
import zipfile

from app.sprite_validation import STATES, validate_png


MAX_ARCHIVE_BYTES = 128 * 1024**2
MAX_TOTAL_BYTES = 256 * 1024**2
MAX_ENTRIES = 32

MAX_MANIFEST_BYTES = 64 * 1024
MAX_SPRITE_BYTES = 5 * 1024**2
MAX_SPRITES = 5
MAX_COMPRESSION_RATIO = 1000
SCHEMA_VERSION = 6
_PNG_SUFFIX = ".png"
_RE_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_SUPPORTED_COMPRESSION = {
    zipfile.ZIP_STORED,
    zipfile.ZIP_DEFLATED,
    zipfile.ZIP_BZIP2,
    zipfile.ZIP_LZMA,
}
_REQUIRED_TABLES = {"settings", "sessions", "login_attempts", "sprites"}
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class _ValidationFailure(ValueError):
    """A safe, already-localized validation failure."""


def _fail(message: str) -> None:
    raise _ValidationFailure(message)


def _is_reparse_or_symlink(path: Path, st: os.stat_result | None = None) -> bool:
    """Detect symlinks and Windows reparse points without resolving untrusted paths."""
    try:
        if path.is_symlink():
            return True
        if st is None:
            st = path.lstat()
        # FILE_ATTRIBUTE_REPARSE_POINT.  This covers Windows junctions as well
        # as ordinary symlinks when the platform exposes file attributes.
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)
    except (OSError, ValueError):
        return True


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except (OSError, ValueError):
        _fail("備份暫存路徑無效。")
        raise AssertionError  # pragma: no cover


def _require_empty_staging(staging_root: Path) -> None:
    st = _lstat(staging_root)
    if _is_reparse_or_symlink(staging_root, st) or not stat.S_ISDIR(st.st_mode):
        _fail("備份暫存目錄無效。")
    try:
        next(staging_root.iterdir())
    except StopIteration:
        return
    except (OSError, ValueError):
        _fail("備份暫存目錄無法讀取。")
    _fail("備份暫存目錄必須是空的。")


def _require_archive(archive_path: Path) -> None:
    st = _lstat(archive_path)
    if _is_reparse_or_symlink(archive_path, st) or not stat.S_ISREG(st.st_mode):
        _fail("備份檔案無效。")
    if st.st_size < 0 or st.st_size > MAX_ARCHIVE_BYTES:
        _fail("備份檔案大小超過限制。")


def _normalise_zip_name(raw_name: object) -> str:
    if not isinstance(raw_name, str) or not raw_name:
        _fail("備份檔名無效。")
    if "\x00" in raw_name or "\\" in raw_name:
        _fail("備份檔名含有不安全字元。")
    if raw_name.startswith("/") or raw_name.startswith("//"):
        _fail("備份檔名不可使用絕對路徑。")
    # Colon is rejected everywhere: besides drive letters it prevents NTFS
    # alternate data streams from reaching the staging directory.
    if ":" in raw_name or re.match(r"^[A-Za-z]:", raw_name):
        _fail("備份檔名不可使用磁碟或特殊路徑。")
    parts = raw_name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _fail("備份檔名含有不安全路徑。")
    if any(any(ord(char) < 32 for char in part) for part in parts):
        _fail("備份檔名含有不安全字元。")
    normalised = "/".join(parts)
    if normalised != raw_name:
        _fail("備份檔名含有不安全路徑。")
    return normalised


def _validate_single_sprite_filename(name: object) -> str:
    if not isinstance(name, str) or not name:
        _fail("素材檔名無效。")
    if (
        "\x00" in name
        or "/" in name
        or "\\" in name
        or ":" in name
        or name in {".", ".."}
        or any(ord(char) < 32 for char in name)
        or name != name.rstrip(" .")
        or not name.endswith(_PNG_SUFFIX)
        or len(name) > 255
    ):
        _fail("素材檔名無效。")
    stem = name.rsplit(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        _fail("素材檔名無效。")
    return name


def _entry_kind(path: str) -> str:
    if path == "manifest.json":
        return "manifest"
    if path == "app.db":
        return "database"
    if path.startswith("sprites/"):
        _validate_single_sprite_filename(path[len("sprites/") :])
        return "sprite"
    _fail("備份包含不允許的檔案。")
    raise AssertionError  # pragma: no cover


def _validate_zip_mode(info: zipfile.ZipInfo) -> None:
    if info.is_dir():
        _fail("備份不可包含目錄項目。")
    if info.flag_bits & 0x1:
        _fail("備份不可包含加密項目。")
    if info.compress_type not in _SUPPORTED_COMPRESSION:
        _fail("備份壓縮格式不受支援。")
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type and file_type != stat.S_IFREG:
        _fail("備份不可包含連結或特殊檔案。")
    if info.create_system == 0 and info.external_attr & 0x10:
        _fail("備份不可包含目錄項目。")
    if info.file_size < 0 or info.compress_size < 0:
        _fail("備份項目大小無效。")
    if info.file_size >= 1024 * 1024:
        compressed = max(info.compress_size, 1)
        if info.file_size > compressed * MAX_COMPRESSION_RATIO:
            _fail("備份壓縮比例超過限制。")


def _entry_limit(kind: str) -> int:
    if kind == "manifest":
        return MAX_MANIFEST_BYTES
    if kind == "sprite":
        return MAX_SPRITE_BYTES
    return MAX_TOTAL_BYTES


def _ensure_stage_directory(path: Path) -> None:
    try:
        if path.exists():
            st = path.lstat()
            if _is_reparse_or_symlink(path, st) or not stat.S_ISDIR(st.st_mode):
                _fail("備份暫存目錄無效。")
            return
        path.mkdir()
    except _ValidationFailure:
        raise
    except (OSError, ValueError):
        _fail("備份暫存目錄無法建立。")


def _extract_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    limit: int,
) -> tuple[int, str]:
    if info.file_size > limit:
        _fail("備份項目大小超過限制。")
    try:
        if destination.exists() or destination.is_symlink():
            _fail("備份暫存檔案重複。")
        _ensure_stage_directory(destination.parent)
        digest = hashlib.sha256()
        total = 0
        with archive.open(info, "r") as source, destination.open("xb") as output:
            while True:
                chunk = source.read(min(1024 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    _fail("備份項目大小超過限制。")
                digest.update(chunk)
                output.write(chunk)
        if total != info.file_size:
            _fail("備份項目大小不一致。")
        return total, digest.hexdigest()
    except _ValidationFailure:
        raise
    except (OSError, RuntimeError, EOFError, ValueError, zipfile.BadZipFile):
        _fail("備份項目無法安全讀取。")
        raise AssertionError  # pragma: no cover


def _object_without_duplicate_keys(pairs: list[tuple[object, object]]) -> dict[object, object]:
    result: dict[object, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise ValueError


def _parse_manifest(data: bytes) -> tuple[int, list[dict[str, object]]]:
    try:
        parsed = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        _fail("manifest.json 格式無效。")
    if not isinstance(parsed, dict) or set(parsed) != {
        "format_version",
        "schema_version",
        "files",
    }:
        _fail("manifest.json 欄位不符。")
    if type(parsed["format_version"]) is not int or parsed["format_version"] != 1:
        _fail("備份格式版本不支援。")
    if type(parsed["schema_version"]) is not int or parsed["schema_version"] not in (4, 5, SCHEMA_VERSION):
        _fail("備份資料庫版本不符。")
    descriptors = parsed["files"]
    if not isinstance(descriptors, list):
        _fail("manifest.json 檔案清單無效。")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or set(descriptor) != {
            "path",
            "size",
            "sha256",
        }:
            _fail("manifest.json 檔案欄位不符。")
        path = _normalise_zip_name(descriptor["path"])
        if _entry_kind(path) == "manifest":
            _fail("manifest.json 不可列入檔案清單。")
        folded = path.casefold()
        if folded in seen:
            _fail("manifest.json 檔案清單重複。")
        seen.add(folded)
        size = descriptor["size"]
        digest = descriptor["sha256"]
        if type(size) is not int or size < 0:
            _fail("manifest.json 檔案大小無效。")
        if not isinstance(digest, str) or not _RE_HEX64.fullmatch(digest):
            _fail("manifest.json 雜湊格式無效。")
        result.append({"path": path, "size": size, "sha256": digest.lower()})
    return parsed["schema_version"], result


def _normalise_sql(sql: object) -> str | None:
    if sql is None:
        return None
    if not isinstance(sql, str):
        _fail("資料庫 schema 資料無效。")
    return re.sub(r"\s+", " ", sql).strip()


def _schema_catalog(rows: Iterable[object]) -> list[tuple[str, str, str | None, str | None]]:
    result: list[tuple[str, str, str | None, str | None]] = []
    try:
        for row in rows:
            if isinstance(row, (str, bytes)) or not hasattr(row, "__len__") or len(row) != 4:
                _fail("資料庫 schema 資料無效。")
            kind, name, table_name, sql = tuple(row)
            if not isinstance(kind, str) or not isinstance(name, str):
                _fail("資料庫 schema 資料無效。")
            if table_name is not None and not isinstance(table_name, str):
                _fail("資料庫 schema 資料無效。")
            result.append((kind, name, table_name, _normalise_sql(sql)))
    except _ValidationFailure:
        raise
    except (TypeError, ValueError):
        _fail("資料庫 schema 資料無效。")
    if len({(kind, name, table_name) for kind, name, table_name, _ in result}) != len(result):
        _fail("資料庫 schema 含有重複項目。")
    return sorted(result, key=lambda row: (row[0], row[1], row[2] or "", row[3] or ""))


def _read_sprite(path: Path) -> bytes:
    st = _lstat(path)
    if _is_reparse_or_symlink(path, st) or not stat.S_ISREG(st.st_mode):
        _fail("素材檔案無效。")
    if st.st_size < 0 or st.st_size > MAX_SPRITE_BYTES:
        _fail("素材檔案大小超過限制。")
    try:
        with path.open("rb") as source:
            data = source.read(MAX_SPRITE_BYTES + 1)
    except (OSError, ValueError):
        _fail("素材檔案無法讀取。")
        raise AssertionError  # pragma: no cover
    if len(data) > MAX_SPRITE_BYTES:
        _fail("素材檔案大小超過限制。")
    return data


def _validate_database(
    database_path: Path,
    staging_root: Path,
    expected_schema: Iterable[object],
    archive_sprite_paths: set[str],
    schema_version: int,
) -> None:
    expected_catalog = _schema_catalog(expected_schema)
    if _lstat(database_path).st_size > MAX_TOTAL_BYTES:
        _fail("資料庫檔案大小超過限制。")
    connection: sqlite3.Connection | None = None
    try:
        try:
            connection = sqlite3.connect(
                database_path.resolve().as_uri() + "?mode=ro", uri=True
            )
            connection.execute("PRAGMA trusted_schema=OFF")
        except (OSError, sqlite3.Error, ValueError):
            _fail("資料庫無法以唯讀模式開啟。")

        try:
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if user_version != schema_version:
                _fail("資料庫版本不符。")
            actual_rows = connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        except (sqlite3.Error, TypeError, ValueError):
            _fail("資料庫 schema 無法讀取。")
        actual_catalog = _schema_catalog(actual_rows)
        if actual_catalog != expected_catalog:
            _fail("資料庫 schema 與目前版本不符。")
        actual_tables = {name for kind, name, _, _ in actual_catalog if kind == "table"}
        if not _REQUIRED_TABLES.issubset(actual_tables):
            _fail("資料庫缺少必要表格。")

        try:
            integrity_cursor = connection.execute("PRAGMA integrity_check")
        except sqlite3.Error:
            _fail("資料庫完整性檢查失敗。")
        integrity_row = integrity_cursor.fetchone()
        if integrity_row is None or integrity_row[0] != "ok":
            _fail("資料庫完整性檢查失敗。")
        try:
            foreign_rows = connection.execute("PRAGMA foreign_key_check").fetchone()
        except sqlite3.Error:
            _fail("資料庫外鍵檢查失敗。")
        if foreign_rows is not None:
            _fail("資料庫外鍵檢查失敗。")

        try:
            settings = connection.execute("SELECT value_json FROM settings").fetchall()
            for (value_json,) in settings:
                if not isinstance(value_json, str):
                    _fail("設定資料不是有效 JSON。")
                json.loads(value_json, parse_constant=_reject_json_constant)
            for table_name in ("sessions", "login_attempts"):
                if connection.execute(f"SELECT 1 FROM {table_name} LIMIT 1").fetchone() is not None:
                    _fail("備份不可包含登入或工作階段資料。")
            sprite_rows = connection.execute(
                "SELECT state,path,frame_width,frame_height,frame_count,fps FROM sprites"
            ).fetchall()
        except _ValidationFailure:
            raise
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            _fail("資料庫內容無法驗證。")

        if len(sprite_rows) > MAX_SPRITES:
            _fail("素材數量超過限制。")
        database_sprite_paths: set[str] = set()
        for row in sprite_rows:
            state, path, frame_width, frame_height, frame_count, fps = row
            if state not in STATES:
                _fail("素材狀態無效。")
            filename = _validate_single_sprite_filename(path)
            database_sprite_paths.add(filename)
            relative_path = f"sprites/{filename}"
            if relative_path not in archive_sprite_paths:
                _fail("資料庫引用的素材不存在。")
            sprite_path = staging_root / "sprites" / filename
            try:
                validate_png(
                    _read_sprite(sprite_path),
                    frame_width,
                    frame_height,
                    frame_count,
                    fps,
                )
            except _ValidationFailure:
                raise
            except Exception:
                _fail("素材內容無法驗證。")
        if {f"sprites/{filename}" for filename in database_sprite_paths} != archive_sprite_paths:
            _fail("備份素材與資料庫引用不一致。")
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def _check_stage_shape(staging_root: Path, archive_paths: set[str]) -> None:
    expected_root = {"manifest.json", "app.db"}
    if any(path.startswith("sprites/") for path in archive_paths):
        expected_root.add("sprites")
    try:
        children = list(staging_root.iterdir())
    except (OSError, ValueError):
        _fail("備份暫存目錄無法讀取。")
    child_names = {child.name.casefold() for child in children}
    if len(child_names) != len(children) or child_names != {name.casefold() for name in expected_root}:
        _fail("備份暫存內容不符。")
    for child in children:
        st = _lstat(child)
        if _is_reparse_or_symlink(child, st):
            _fail("備份暫存不可包含連結。")
        if child.name.casefold() == "sprites":
            if not stat.S_ISDIR(st.st_mode):
                _fail("備份素材暫存目錄無效。")
            try:
                sprite_children = list(child.iterdir())
            except (OSError, ValueError):
                _fail("備份素材暫存目錄無法讀取。")
            actual = set()
            for sprite in sprite_children:
                sprite_st = _lstat(sprite)
                if _is_reparse_or_symlink(sprite, sprite_st) or not stat.S_ISREG(sprite_st.st_mode):
                    _fail("備份素材暫存檔案無效。")
                _validate_single_sprite_filename(sprite.name)
                actual.add(f"sprites/{sprite.name}")
            if len(actual) != len(sprite_children) or actual != {
                path for path in archive_paths if path.startswith("sprites/")
            }:
                _fail("備份素材暫存內容不符。")
        elif child.name.casefold() in {"manifest.json", "app.db"}:
            if not stat.S_ISREG(st.st_mode):
                _fail("備份暫存檔案無效。")
        else:
            _fail("備份暫存包含不允許的檔案。")


def _validate_backup(zip_path: object, staging_root: object, expected_schema: Iterable[object] | dict[int, Iterable[object]]) -> dict[str, object]:
    if not isinstance(zip_path, (str, os.PathLike)) or not isinstance(
        staging_root, (str, os.PathLike)
    ):
        _fail("備份路徑參數無效。")
    archive_path = Path(zip_path)
    stage_path = Path(staging_root)
    _require_archive(archive_path)
    _require_empty_staging(stage_path)

    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, ValueError, zipfile.BadZipFile):
        _fail("備份 ZIP 無法開啟。")

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            _fail("備份項目數量超過限制。")
        entries: list[tuple[str, zipfile.ZipInfo, str]] = []
        names: set[str] = set()
        total_size = 0
        sprite_entry_paths: set[str] = set()
        for info in infos:
            if "\x00" in str(getattr(info, "orig_filename", "")):
                _fail("備份原始檔名含有不安全字元。")
            path = _normalise_zip_name(info.filename)
            folded = path.casefold()
            if folded in names:
                _fail("備份檔名重複。")
            names.add(folded)
            _validate_zip_mode(info)
            kind = _entry_kind(path)
            limit = _entry_limit(kind)
            if info.file_size > limit:
                _fail("備份項目大小超過限制。")
            total_size += info.file_size
            if total_size > MAX_TOTAL_BYTES:
                _fail("備份解壓總大小超過限制。")
            if kind == "sprite":
                sprite_entry_paths.add(path)
                if len(sprite_entry_paths) > MAX_SPRITES:
                    _fail("素材數量超過限制。")
            entries.append((path, info, kind))
        if "manifest.json" not in {path for path, _, _ in entries} or "app.db" not in {
            path for path, _, _ in entries
        }:
            _fail("備份缺少必要檔案。")

        observed: dict[str, tuple[int, str]] = {}
        for path, info, kind in entries:
            destination = stage_path / Path(path)
            size, digest = _extract_entry(archive, info, destination, _entry_limit(kind))
            observed[path] = (size, digest)
        _check_stage_shape(stage_path, {path for path, _, _ in entries})

        try:
            manifest_data = (stage_path / "manifest.json").read_bytes()
        except (OSError, ValueError):
            _fail("manifest.json 無法讀取。")
        schema_version, descriptors = _parse_manifest(manifest_data)
        archive_paths = {path for path, _, _ in entries if path != "manifest.json"}
        manifest_paths = [str(item["path"]) for item in descriptors]
        if set(manifest_paths) != archive_paths or len(manifest_paths) != len(archive_paths):
            _fail("manifest.json 檔案清單與備份不一致。")
        for descriptor in descriptors:
            path = str(descriptor["path"])
            actual_size, actual_digest = observed[path]
            if descriptor["size"] != actual_size or descriptor["sha256"] != actual_digest:
                _fail("備份檔案雜湊或大小不一致。")

        _validate_database(
            stage_path / "app.db",
            stage_path,
            expected_schema[schema_version] if isinstance(expected_schema, dict) else expected_schema,
            sprite_entry_paths,
            schema_version,
        )
        return {"schema_version": schema_version, "files": manifest_paths}


def validate_backup(
    zip_path: object,
    staging_root: object,
    expected_schema: Iterable[object] | dict[int, Iterable[object]],
) -> dict[str, object]:
    """驗證備份並解出到空的 staging_root。

    所有公開失敗都會轉成不含路徑、資料庫內容或秘密的繁中訊息。
    """
    try:
        return _validate_backup(zip_path, staging_root, expected_schema)
    except _ValidationFailure as exc:
        raise ValueError(str(exc)) from None
    except Exception:
        raise ValueError("備份驗證失敗。") from None
