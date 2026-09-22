from database import checkpointer


def test_load_checkpoint_returns_none_for_corrupt_state(monkeypatch, tmp_path):
    db_path = tmp_path / "checkpointer.db"
    monkeypatch.setattr(checkpointer, "CHECKPOINTER_DB_PATH", str(db_path))

    conn = checkpointer._get_conn()
    try:
        conn.execute(
            """INSERT INTO checkpoints (thread_id, checkpoint_key, state, created_at)
               VALUES (?, ?, ?, ?)""",
            ("thread-1", "state", "{bad-json", "2026-06-02T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    assert checkpointer.load_checkpoint("thread-1", "state") is None


def test_load_checkpoint_keeps_valid_state(monkeypatch, tmp_path):
    db_path = tmp_path / "checkpointer.db"
    monkeypatch.setattr(checkpointer, "CHECKPOINTER_DB_PATH", str(db_path))

    checkpointer.save_checkpoint("thread-1", "state", {"step": 1})

    assert checkpointer.load_checkpoint("thread-1", "state") == {"step": 1}
