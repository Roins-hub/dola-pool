"""Unit test cho ai_settings.py."""
import pytest
from pathlib import Path
from ai_settings import (
    load_raw_settings,
    save_raw_settings,
    get_public_settings,
    update_settings,
    get_active_provider_config,
    compute_config_fingerprint,
    AiSettingsDecryptError,
)


@pytest.fixture
def temp_ai_files(tmp_path):
    encfile = tmp_path / "ai_settings.enc"
    keyfile = tmp_path / "ai_settings.key"
    return encfile, keyfile


def test_default_settings_when_empty(temp_ai_files):
    encfile, keyfile = temp_ai_files
    data = load_raw_settings(encfile, keyfile)
    assert data["active_provider"] is None
    assert data["gemini"]["api_key"] == ""
    assert data["openai"]["api_key"] == ""

    pub = get_public_settings(encfile, keyfile)
    assert pub["active_provider"] is None
    assert pub["gemini"]["has_api_key"] is False
    assert pub["openai"]["has_api_key"] is False


def test_update_settings_and_redaction(temp_ai_files):
    encfile, keyfile = temp_ai_files

    # 1. Cập nhật Gemini
    res = update_settings(
        {
            "active_provider": "gemini",
            "gemini": {
                "model": "gemini-1.5-pro",
                "api_key": "secret-gemini-key-12345",
            },
        },
        encfile,
        keyfile,
    )

    assert res["active_provider"] == "gemini"
    assert res["gemini"]["model"] == "gemini-1.5-pro"
    assert res["gemini"]["has_api_key"] is True
    assert "secret-gemini-key" not in str(res)

    # Đọc lại raw xem có đúng key không
    raw = load_raw_settings(encfile, keyfile)
    assert raw["gemini"]["api_key"] == "secret-gemini-key-12345"

    # 2. Cập nhật model mà không gửi lại key -> key cũ phải được bảo toàn
    res2 = update_settings(
        {
            "gemini": {
                "model": "gemini-2.5-flash",
                "api_key": "",  # bỏ trống
            }
        },
        encfile,
        keyfile,
    )
    assert res2["gemini"]["model"] == "gemini-2.5-flash"
    assert res2["gemini"]["has_api_key"] is True

    raw2 = load_raw_settings(encfile, keyfile)
    assert raw2["gemini"]["api_key"] == "secret-gemini-key-12345"

    # 3. Xóa key rõ ràng qua clear_api_key
    res3 = update_settings(
        {
            "gemini": {
                "clear_api_key": True,
            }
        },
        encfile,
        keyfile,
    )
    assert res3["gemini"]["has_api_key"] is False
    raw3 = load_raw_settings(encfile, keyfile)
    assert raw3["gemini"]["api_key"] == ""


def test_openai_compatible_settings(temp_ai_files):
    encfile, keyfile = temp_ai_files

    update_settings(
        {
            "active_provider": "openai",
            "openai": {
                "base_url": "https://api.groq.com/openai/v1",
                "model": "llama-3.3-70b",
                "api_key": "gsk-secret-key-999",
            },
        },
        encfile,
        keyfile,
    )

    pub = get_public_settings(encfile, keyfile)
    assert pub["active_provider"] == "openai"
    assert pub["openai"]["base_url"] == "https://api.groq.com/openai/v1"
    assert pub["openai"]["model"] == "llama-3.3-70b"
    assert pub["openai"]["has_api_key"] is True
    assert "gsk-secret-key" not in str(pub)

    active_conf = get_active_provider_config(encfile, keyfile)
    assert active_conf is not None
    assert active_conf["provider"] == "openai"
    assert active_conf["api_key"] == "gsk-secret-key-999"


def test_corrupt_file_raises_decrypt_error(temp_ai_files):
    encfile, keyfile = temp_ai_files

    # Ghi dữ liệu hợp lệ trước
    update_settings(
        {
            "active_provider": "gemini",
            "gemini": {"api_key": "valid-key"},
        },
        encfile,
        keyfile,
    )

    # Làm hỏng tệp
    encfile.write_bytes(b"corrupt-non-fernet-data")

    # Khi load phải ném AiSettingsDecryptError, không được ghi đè hay nuốt lỗi
    with pytest.raises(AiSettingsDecryptError):
        load_raw_settings(encfile, keyfile)


def test_fingerprint_changes_on_config_change(temp_ai_files):
    encfile, keyfile = temp_ai_files

    fp1 = compute_config_fingerprint(encfile, keyfile)

    update_settings(
        {
            "active_provider": "gemini",
            "gemini": {"model": "gemini-1.5-flash", "api_key": "k1"},
        },
        encfile,
        keyfile,
    )
    fp2 = compute_config_fingerprint(encfile, keyfile)
    assert fp1 != fp2

    # Đổi key -> fingerprint đổi
    update_settings(
        {
            "gemini": {"api_key": "k2"},
        },
        encfile,
        keyfile,
    )
    fp3 = compute_config_fingerprint(encfile, keyfile)
    assert fp2 != fp3
