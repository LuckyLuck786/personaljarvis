"""Backup & restore for the second brain.

Snapshots everything JARVIS knows into one portable archive:
  * SQLite DB — copied via the online backup API (consistent even while the
    hub is running; no need to stop the daemon)
  * LanceDB vector store — the on-disk directory
  * a manifest with version, timestamp, and SHA-256 of each part

The memory *content* is already Fernet-encrypted inside these files, so the
archive inherits that encryption — but it does NOT contain JARVIS_MASTER_KEY
(that lives in .env and must be backed up separately, once). Restore refuses
to clobber a non-empty data dir unless --force, and verifies checksums first.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

from jarvis.core.logging import get_logger

log = get_logger(__name__)

MANIFEST = "manifest.json"
DB_NAME = "jarvis.db"
LANCE_DIR = "lancedb"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _consistent_db_copy(db_path: Path, dest: Path) -> None:
    """Use SQLite's online backup API for a crash-consistent snapshot while
    the hub may be writing (WAL mode)."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def create_backup(data_dir: Path, out_path: Path | None = None) -> Path:
    data_dir = Path(data_dir)
    db_path = data_dir / DB_NAME
    if not db_path.exists():
        raise FileNotFoundError(f"no database at {db_path}")
    out_path = Path(out_path) if out_path else (
        data_dir / f"jarvis-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _consistent_db_copy(db_path, tmp / DB_NAME)
        manifest = {
            "created": time.time(),
            "version": 1,
            "files": {DB_NAME: _sha256(tmp / DB_NAME)},
        }
        lance = data_dir / LANCE_DIR
        if lance.is_dir():
            shutil.copytree(lance, tmp / LANCE_DIR)
            # checksum every lance file for integrity on restore
            manifest["lance_files"] = {
                str(p.relative_to(tmp)): _sha256(p)
                for p in sorted((tmp / LANCE_DIR).rglob("*")) if p.is_file()
            }
        (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2))

        with tarfile.open(out_path, "w:gz") as tar:
            for item in sorted(tmp.iterdir()):
                tar.add(item, arcname=item.name)
    log.info("backup_created", path=str(out_path))
    return out_path


def verify_backup(archive: Path) -> dict:
    archive = Path(archive)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with tarfile.open(archive) as tar:
            _safe_extract(tar, tmp)
        manifest = json.loads((tmp / MANIFEST).read_text())
        ok = _sha256(tmp / DB_NAME) == manifest["files"][DB_NAME]
        for rel, digest in manifest.get("lance_files", {}).items():
            if _sha256(tmp / rel) != digest:
                ok = False
                break
    return {"ok": ok, "created": manifest.get("created"),
            "version": manifest.get("version")}


def restore_backup(archive: Path, data_dir: Path, force: bool = False) -> dict:
    archive = Path(archive)
    data_dir = Path(data_dir)
    existing_db = data_dir / DB_NAME
    if existing_db.exists() and not force:
        raise FileExistsError(
            f"{existing_db} already exists — pass force=True to overwrite")

    check = verify_backup(archive)
    if not check["ok"]:
        raise ValueError("backup failed checksum verification — refusing to restore")

    data_dir.mkdir(parents=True, exist_ok=True)
    # move current data aside rather than deleting (safety)
    if existing_db.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for name in (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm"):
            p = data_dir / name
            if p.exists():
                p.rename(data_dir / f"{name}.pre-restore-{stamp}")
        lance = data_dir / LANCE_DIR
        if lance.exists():
            lance.rename(data_dir / f"{LANCE_DIR}.pre-restore-{stamp}")

    with tarfile.open(archive) as tar:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _safe_extract(tar, tmp)
            shutil.copy2(tmp / DB_NAME, data_dir / DB_NAME)
            if (tmp / LANCE_DIR).is_dir():
                shutil.copytree(tmp / LANCE_DIR, data_dir / LANCE_DIR,
                                dirs_exist_ok=True)
    log.info("backup_restored", archive=str(archive), data_dir=str(data_dir))
    return {"restored": True, "created": check["created"]}


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Guard against path-traversal in a malicious archive."""
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise ValueError(f"unsafe path in archive: {member.name}")
    # 'data' filter (py3.12+) also blocks absolute paths / device files; our
    # manual check above stays for older runtimes
    try:
        tar.extractall(dest, filter="data")
    except TypeError:
        tar.extractall(dest)  # Python < 3.12 has no filter kwarg
