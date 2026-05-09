import sqlite3
import json
from datetime import datetime

from config import CHECKPOINTER_DB_PATH


def _get_conn():
    conn = sqlite3.connect(CHECKPOINTER_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            thread_id TEXT NOT NULL,
            checkpoint_key TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (thread_id, checkpoint_key)
        )
    """)
    conn.commit()
    return conn


def save_checkpoint(thread_id: str, key: str, state: dict):
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO checkpoints (thread_id, checkpoint_key, state, created_at)
               VALUES (?, ?, ?, ?)""",
            (thread_id, key, json.dumps(state, ensure_ascii=False),
             datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def load_checkpoint(thread_id: str, key: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT state FROM checkpoints WHERE thread_id = ? AND checkpoint_key = ?",
            (thread_id, key),
        ).fetchone()
        return json.loads(row["state"]) if row else None
    finally:
        conn.close()


def delete_thread_checkpoints(thread_id: str):
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
        conn.commit()
    finally:
        conn.close()


def list_threads() -> list[str]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints ORDER BY created_at DESC"
        ).fetchall()
        return [r["thread_id"] for r in rows]
    finally:
        conn.close()
