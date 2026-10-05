"""Quản lý cấu hình AI Provider (Gemini / OpenAI-compatible) mã hóa bằng Fernet.

Đặc điểm:
- Mã hóa tệp ai_settings.enc bằng khóa riêng (DOLA_AI_KEY hoặc ai_settings.key).
- Ghi nguyên tử (atomic write qua tmp file + replace) chống hỏng tệp.
- Báo lỗi rõ ràng khi tệp bị hỏng hoặc sai khóa (AiSettingsDecryptError), không tự ghi đè.
- Không bao giờ trả API key thật ra ngoài API/log/localStorage.
- Giữ nguyên key khi để trống; xóa key khi có cờ clear_api_key rõ ràng.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional
from cryptography.fernet import Fernet, InvalidToken

_DEFAULT_ENCFILE = Path(__file__).with_name("ai_settings.enc")
_DEFAULT_KEYFILE = Path(__file__).with_name("ai_settings.key")


def get_keyfile() -> Path:
    env = os.getenv("DOLA_AI_KEY_FILE")
    if env:
        return Path(env)
    return _KEYFILE if "_KEYFILE" in globals() and _KEYFILE is not None else _DEFAULT_KEYFILE


def get_encfile() -> Path:
    env = os.getenv("DOLA_AI_ENC_FILE")
    if env:
        return Path(env)
    return _ENCFILE if "_ENCFILE" in globals() and _ENCFILE is not None else _DEFAULT_ENCFILE


_KEYFILE = _DEFAULT_KEYFILE
_ENCFILE = _DEFAULT_ENCFILE


class AiSettingsError(Exception):
    pass


class AiSettingsDecryptError(AiSettingsError):
    pass


def _get_key(keyfile: Optional[Path] = None) -> bytes:
    env = os.getenv("DOLA_AI_KEY")
    if env:
        return env.encode("utf-8")
    kf = keyfile or get_keyfile()
    if kf.exists():
        return kf.read_text(encoding="utf-8").strip().encode("utf-8")
    key = Fernet.generate_key()
    kf.write_text(key.decode("utf-8"), encoding="utf-8")
    try:
        os.chmod(kf, 0o600)
    except OSError:
        pass
    return key


def _get_fernet(keyfile: Optional[Path] = None) -> Fernet:
    return Fernet(_get_key(keyfile))


DEFAULT_IMAGE_MODEL = "antigravity/gemini-3.1-flash-image"


def _default_state() -> dict[str, Any]:
    return {
        "active_provider": None,  # "gemini" | "openai" | None
        "gemini": {
            "model": "gemini-2.5-flash",
            "api_key": "",
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "image_model": DEFAULT_IMAGE_MODEL,
            "api_key": "",
        },
    }


def load_raw_settings(encfile: Optional[Path] = None, keyfile: Optional[Path] = None) -> dict[str, Any]:
    """Tải toàn bộ cấu hình (bao gồm API key thật) để backend sử dụng."""
    ef = encfile or get_encfile()
    if not ef.exists():
        return _default_state()
    try:
        raw_bytes = ef.read_bytes()
        if not raw_bytes:
            return _default_state()
        f = _get_fernet(keyfile)
        decrypted = f.decrypt(raw_bytes)
        data = json.loads(decrypted.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Dữ liệu cấu hình không phải JSON object hợp lệ")
        # Merge với default để đảm bảo đủ schema
        base = _default_state()
        if data.get("active_provider") in ("gemini", "openai"):
            base["active_provider"] = data["active_provider"]
        else:
            base["active_provider"] = None

        if isinstance(data.get("gemini"), dict):
            base["gemini"]["model"] = str(data["gemini"].get("model") or base["gemini"]["model"])
            base["gemini"]["api_key"] = str(data["gemini"].get("api_key") or "")

        if isinstance(data.get("openai"), dict):
            base["openai"]["base_url"] = str(data["openai"].get("base_url") or base["openai"]["base_url"])
            base["openai"]["model"] = str(data["openai"].get("model") or base["openai"]["model"])
            base["openai"]["image_model"] = str(data["openai"].get("image_model") or DEFAULT_IMAGE_MODEL)
            base["openai"]["api_key"] = str(data["openai"].get("api_key") or "")

        return base
    except (InvalidToken, Exception) as exc:
        raise AiSettingsDecryptError(f"Không thể giải mã tệp cấu hình AI ({ef.name}): {exc}") from exc


def save_raw_settings(data: dict[str, Any], encfile: Optional[Path] = None, keyfile: Optional[Path] = None) -> None:
    """Lưu cấu hình an toàn qua atomic write."""
    ef = encfile or get_encfile()
    f = _get_fernet(keyfile)
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    encrypted = f.encrypt(payload)

    # Atomic write qua file tạm cùng thư mục
    target_dir = ef.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=f".{ef.name}.tmp")
    try:
        with os.fdopen(temp_fd, "wb") as tmp_file:
            tmp_file.write(encrypted)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(temp_path, ef)
        try:
            os.chmod(ef, 0o600)
        except OSError:
            pass
    except Exception:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


def get_public_settings(encfile: Optional[Path] = None, keyfile: Optional[Path] = None) -> dict[str, Any]:
    """Trả về cấu hình an toàn cho Web UI / Admin API, tuyệt đối không lộ API key thật."""
    raw = load_raw_settings(encfile, keyfile)
    return {
        "active_provider": raw["active_provider"],
        "gemini": {
            "model": raw["gemini"].get("model", "gemini-2.5-flash"),
            "has_api_key": bool(raw["gemini"].get("api_key")),
        },
        "openai": {
            "base_url": _public_base_url(raw["openai"].get("base_url", "https://api.openai.com/v1")),
            "model": raw["openai"].get("model", "gpt-4o-mini"),
            "image_model": raw["openai"].get("image_model") or DEFAULT_IMAGE_MODEL,
            "has_api_key": bool(raw["openai"].get("api_key")),
        },
    }


def _public_base_url(value):
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(value)
    host = parts.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    if parts.port:
        host += ":" + str(parts.port)
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def update_settings(
    update_data: dict[str, Any],
    encfile: Optional[Path] = None,
    keyfile: Optional[Path] = None
) -> dict[str, Any]:
    """Cập nhật cấu hình từ request Admin PUT.

    - Nếu api_key có giá trị: ghi đè key mới.
    - Nếu clear_api_key = True: xóa key.
    - Nếu api_key rỗng và clear_api_key != True: giữ nguyên key đã lưu trước đó.
    """
    base_url = (update_data.get("openai") or {}).get("base_url")
    if base_url:
        from urllib.parse import urlsplit
        parts = urlsplit(base_url)
        _ = parts.port  # Validate malformed port before persisting.
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Base URL phải là HTTP(S), không chứa mật khẩu hoặc tham số bí mật")
    current = load_raw_settings(encfile, keyfile)

    if "active_provider" in update_data:
        act = update_data["active_provider"]
        if act in ("gemini", "openai", None):
            current["active_provider"] = act
        else:
            raise ValueError(f"active_provider không hợp lệ: {act}")

    if "gemini" in update_data and isinstance(update_data["gemini"], dict):
        g = update_data["gemini"]
        if "model" in g and g["model"]:
            current["gemini"]["model"] = str(g["model"]).strip()
        if g.get("clear_api_key"):
            current["gemini"]["api_key"] = ""
        elif g.get("api_key") and str(g["api_key"]).strip():
            current["gemini"]["api_key"] = str(g["api_key"]).strip()

    if "openai" in update_data and isinstance(update_data["openai"], dict):
        o = update_data["openai"]
        if "base_url" in o and o["base_url"]:
            current["openai"]["base_url"] = str(o["base_url"]).strip().rstrip("/")
        if "model" in o and o["model"]:
            current["openai"]["model"] = str(o["model"]).strip()
        if o.get("image_model") and str(o["image_model"]).strip():
            current["openai"]["image_model"] = str(o["image_model"]).strip()
        if o.get("clear_api_key"):
            current["openai"]["api_key"] = ""
        elif o.get("api_key") and str(o["api_key"]).strip():
            current["openai"]["api_key"] = str(o["api_key"]).strip()

    save_raw_settings(current, encfile, keyfile)
    return get_public_settings(encfile, keyfile)


def get_active_provider_config(encfile: Optional[Path] = None, keyfile: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Trả về cấu hình của provider đang active, hoặc None nếu chưa cấu hình."""
    raw = load_raw_settings(encfile, keyfile)
    active = raw.get("active_provider")
    if not active or active not in ("gemini", "openai"):
        return None
    conf = raw[active]
    if not conf.get("api_key"):
        return None
    return {
        "provider": active,
        **conf
    }


def get_image_config(encfile: Optional[Path] = None, keyfile: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Cấu hình tạo ảnh: dùng khối OpenAI-compatible (9router...) dù provider chữ đang là Gemini."""
    conf = load_raw_settings(encfile, keyfile)["openai"]
    if not conf.get("api_key"):
        return None
    return {"base_url": conf["base_url"], "api_key": conf["api_key"], "model": conf.get("image_model") or DEFAULT_IMAGE_MODEL}


def compute_config_fingerprint(encfile: Optional[Path] = None, keyfile: Optional[Path] = None) -> str:
    """Tạo fingerprint cho cấu hình AI hiện tại để vô hiệu hóa token preflight cũ khi cấu hình đổi."""
    raw = load_raw_settings(encfile, keyfile)
    active = raw.get("active_provider")
    if not active or active not in ("gemini", "openai"):
        return "none"
    conf = raw[active]
    blob = f"{active}:{conf.get('base_url', '')}:{conf.get('model', '')}:{conf.get('api_key', '')}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
