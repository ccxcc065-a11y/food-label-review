"""Minimal task persistence for P5 resume, restart, and failure honesty.

This is not a full stage machine or a commercial workflow database. It stores
enough of a task (status, DecisionSet pointer, frozen bundle) so a same-taskId
resume can apply human rulings without rerunning AI, and so a restart can
reload a saved draft without re-rendering it.

SQLite holds the index; bundles and reports stay as files. API keys are never
written.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


ROOT = Path(__file__).resolve().parent
DEFAULT_DIR = ROOT / ".runtime" / "tasks"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_SECRET_KEYS = {
    "LABEL_REVIEW_API_KEY", "apiKey", "api_key", "key", "authorization",
    "Authorization", "token", "secret", "password",
}
_LOCK = threading.Lock()


def tasks_dir() -> Path:
    override = os.environ.get("LABEL_REVIEW_TASKS")
    return Path(override) if override else DEFAULT_DIR


def _db_path() -> Path:
    return tasks_dir() / "tasks.sqlite"


def _safe_id(task_id: str) -> str:
    token = str(task_id or "").strip()
    if not _TASK_ID_RE.match(token):
        raise ValueError("invalid task id")
    return token


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            decision_version TEXT,
            report_id TEXT,
            content_hash TEXT,
            snapshot_id TEXT,
            model_request_issued INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            at TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT
        )"""
    )
    conn.commit()
    return conn


def _strip_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _strip_secrets(v) for k, v in value.items() if str(k) not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_strip_secrets(item) for item in value]
    return value


def _bundle_path(task_id: str) -> Path:
    return tasks_dir() / _safe_id(task_id) / "bundle.json"


def get(task_id: str) -> Optional[dict]:
    token = _safe_id(task_id)
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (token,)).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    data = dict(row)
    bundle = load_bundle(token)
    data["bundle"] = bundle
    return data


def upsert(task_id: str, *, status: str, decision_version: str = "",
           report_id: str = "", content_hash: str = "", snapshot_id: str = "",
           model_request_issued: bool = False) -> dict:
    token = _safe_id(task_id)
    now = _now()
    with _LOCK:
        conn = _connect()
        try:
            existing = conn.execute("SELECT created_at, model_request_issued FROM tasks WHERE task_id = ?",
                                    (token,)).fetchone()
            issued = int(model_request_issued or (existing["model_request_issued"] if existing else 0))
            if existing:
                conn.execute(
                    """UPDATE tasks SET status=?, decision_version=?, report_id=?, content_hash=?,
                       snapshot_id=?, model_request_issued=?, updated_at=? WHERE task_id=?""",
                    (status, decision_version or "", report_id or "", content_hash or "",
                     snapshot_id or "", issued, now, token),
                )
                created = existing["created_at"]
            else:
                conn.execute(
                    """INSERT INTO tasks (task_id, status, decision_version, report_id, content_hash,
                       snapshot_id, model_request_issued, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (token, status, decision_version or "", report_id or "", content_hash or "",
                     snapshot_id or "", issued, now, now),
                )
                created = now
            conn.commit()
        finally:
            conn.close()
    return {"task_id": token, "status": status, "created_at": created, "updated_at": now}


def record_event(task_id: str, kind: str, payload: Optional[Mapping[str, Any]] = None) -> None:
    token = _safe_id(task_id)
    safe = _strip_secrets(payload or {})
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO events (task_id, at, kind, payload) VALUES (?, ?, ?, ?)",
                (token, _now(), str(kind), json.dumps(safe, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()


def events(task_id: str) -> list:
    token = _safe_id(task_id)
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT at, kind, payload FROM events WHERE task_id = ? ORDER BY id", (token,)
            ).fetchall()
        finally:
            conn.close()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        out.append({"at": row["at"], "kind": row["kind"], "payload": payload})
    return out


def save_bundle(task_id: str, bundle: Mapping[str, Any]) -> Path:
    token = _safe_id(task_id)
    path = _bundle_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _strip_secrets(dict(bundle))
    payload["taskId"] = token
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)
    return path


def load_bundle(task_id: str) -> Optional[dict]:
    path = _bundle_path(_safe_id(task_id))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def reset_for_tests() -> None:
    """Drop the in-directory store. Only for tests that set LABEL_REVIEW_TASKS."""
    override = os.environ.get("LABEL_REVIEW_TASKS")
    if not override:
        return
    import shutil
    path = Path(override)
    if path.exists() and path.resolve() != DEFAULT_DIR.resolve():
        shutil.rmtree(path, ignore_errors=True)
