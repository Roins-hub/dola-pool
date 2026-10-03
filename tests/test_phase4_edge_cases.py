"""Kiểm thử Phase 4: Các trường hợp biên, lỗi ngầm, migration DB và an toàn dữ liệu."""
import io
import json
import os
import sqlite3
import pytest
from pathlib import Path
from PIL import Image

import ai_settings
from ai_preflight import record_preflight_result, validate_preflight_gate
from store import TaskStore


def test_ai_settings_atomic_write_failure_safety(tmp_path):
    """Khi ghi cấu hình bị lỗi giữa chừng (ví dụ giả lập exception), tệp gốc không bị hỏng."""
    encfile = tmp_path / "ai_settings.enc"
    keyfile = tmp_path / "ai_settings.key"

    # 1. Ghi dữ liệu ban đầu
    ai_settings.update_settings(
        {
            "active_provider": "gemini",
            "gemini": {"model": "gemini-1.5-pro", "api_key": "original-key-123"},
        },
        encfile=encfile,
        keyfile=keyfile,
    )
    original_bytes = encfile.read_bytes()

    # 2. Giả lập lỗi khi ghi tệp (ví dụ os.replace ném PermissionError)
    def broken_replace(src, dst):
        raise PermissionError("Disk write simulated failure")

    orig_replace = os.replace
    os.replace = broken_replace
    try:
        with pytest.raises(PermissionError):
            ai_settings.update_settings(
                {
                    "active_provider": "openai",
                    "openai": {"api_key": "broken-attempt"},
                },
                encfile=encfile,
                keyfile=keyfile,
            )
    finally:
        os.replace = orig_replace

    # 3. Tệp gốc phải còn nguyên vẹn 100%, không bị file rác đè
    assert encfile.read_bytes() == original_bytes
    loaded = ai_settings.load_raw_settings(encfile=encfile, keyfile=keyfile)
    assert loaded["active_provider"] == "gemini"
    assert loaded["gemini"]["api_key"] == "original-key-123"


def test_db_migration_on_old_database(tmp_path):
    """Database phiên bản cũ không có failure_group và upstream_request_id được tự động migrate."""
    db_file = tmp_path / "legacy_tasks.db"
    conn = sqlite3.connect(str(db_file))
    # Tạo schema cũ thiếu 2 cột
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            model TEXT,
            prompt TEXT,
            ratio TEXT,
            duration INTEGER,
            status TEXT,
            video_url TEXT,
            error TEXT,
            created_at REAL,
            updated_at REAL,
            conversation_id TEXT,
            deadline_at REAL,
            last_poll_at REAL,
            failure_code TEXT,
            reference_images TEXT
        )
    """
    )
    conn.execute(
        """
        INSERT INTO tasks (id, model, prompt, status, created_at, updated_at)
        VALUES ('legacy_task_1', 'seedance-2.5', 'Prompt cũ', 'completed', 1000.0, 1005.0)
    """
    )
    conn.commit()
    conn.close()

    # Khởi tạo TaskStore trỏ vào DB cũ -> Phải chạy migration an toàn
    store = TaskStore(str(db_file))
    old_task = store.get("legacy_task_1")
    assert old_task is not None
    assert old_task["prompt"] == "Prompt cũ"

    # Cập nhật các trường mới được bổ sung
    store.update(
        "legacy_task_1",
        failure_group="copyright",
        upstream_request_id="req_migrated_123",
    )
    updated = store.get("legacy_task_1")
    assert updated is not None
    assert updated["failure_group"] == "copyright"
    assert updated["upstream_request_id"] == "req_migrated_123"


def test_direct_bypass_without_preflight_is_strictly_blocked():
    """Gọi trực tiếp không qua UI hoặc giả mạo token sai session/hash."""
    # Session user_A kiểm tra prompt A
    res = record_preflight_result(
        session_id="user_A",
        prompt="Prompt hợp lệ",
        result={"status": "allow"},
        custom_fingerprint="fp1",
    )

    # 1. Kẻ tấn công gọi không truyền token -> Bị từ chối (Thiếu token)
    ok_no_token, err_no_token, _ = validate_preflight_gate(
        session_id="user_B",
        prompt="Prompt hợp lệ",
        custom_fingerprint="fp1",
    )
    assert ok_no_token is False
    assert "Thiếu mã xác thực kiểm tra" in err_no_token

    # 2. Kẻ tấn công dùng session user_B với token của user_A -> Bị từ chối
    ok, err, _ = validate_preflight_gate(
        session_id="user_B",
        prompt="Prompt hợp lệ",
        token=res["token"],
        custom_fingerprint="fp1",
    )
    assert ok is False
    assert "không thuộc phiên đăng nhập" in err

    # 3. Kẻ tấn công dùng session user_A nhưng đổi prompt khác với token cũ -> Bị từ chối
    ok2, err2, _ = validate_preflight_gate(
        session_id="user_A",
        prompt="Prompt khác hoàn toàn",
        token=res["token"],
        custom_fingerprint="fp1",
    )
    assert ok2 is False
    assert "sửa đổi" in err2
