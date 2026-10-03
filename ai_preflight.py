"""Cổng kiểm soát Preflight ở tầng Backend trước khi gửi task tạo video."""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any, Optional

from ai_settings import compute_config_fingerprint

# Thời gian hiệu lực của kết quả preflight (15 phút)
PREFLIGHT_EXPIRY_SECONDS = 900.0

# Lưu trữ cache preflight trong bộ nhớ: {token: Record}
_PREFLIGHT_TOKENS: dict[str, dict[str, Any]] = {}


def normalize_prompt(text: str) -> str:
    """Chuẩn hóa prompt để hash không bị lệch bởi khoảng trắng thừa."""
    clean = re.sub(r"\s+", " ", (text or "").strip())
    return clean


def compute_prompt_hash(text: str) -> str:
    norm = normalize_prompt(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _purge_expired() -> None:
    now = time.time()
    expired = [k for k, v in _PREFLIGHT_TOKENS.items() if v.get("expires_at", 0) < now]
    for k in expired:
        _PREFLIGHT_TOKENS.pop(k, None)


def record_preflight_result(
    session_id: str,
    prompt: str,
    result: dict[str, Any],
    custom_fingerprint: Optional[str] = None,
) -> dict[str, Any]:
    """Lưu kết quả preflight và phát hành verification token gắn với session + prompt + config."""
    _purge_expired()
    now = time.time()
    token = "pf_" + uuid.uuid4().hex
    p_hash = compute_prompt_hash(prompt)
    cfg_fp = custom_fingerprint if custom_fingerprint is not None else compute_config_fingerprint()

    record = {
        "token": token,
        "session_id": session_id,
        "prompt_hash": p_hash,
        "config_fingerprint": cfg_fp,
        "status": result.get("status"),
        "reason": result.get("reason", ""),
        "suggestions": result.get("suggestions", ""),
        "created_at": now,
        "expires_at": now + PREFLIGHT_EXPIRY_SECONDS,
    }
    _PREFLIGHT_TOKENS[token] = record
    return {
        "token": token,
        "status": result.get("status"),
        "reason": result.get("reason", ""),
        "suggestions": result.get("suggestions", ""),
        "expires_at": record["expires_at"],
    }


def validate_preflight_gate(
    session_id: str,
    prompt: str,
    token: Optional[str] = None,
    confirm_review: bool = False,
    custom_fingerprint: Optional[str] = None,
) -> tuple[bool, str, Optional[str]]:
    """Xác thực gate preflight ở backend trước khi tạo video.

    Yêu cầu:
    - Token bắt buộc phải có, hợp lệ và thuộc đúng session_id xác thực.
    - Prompt phải khớp hash đã kiểm tra.
    - Cấu hình provider không được đổi kể từ lúc cấp token.
    - 'block' tuyệt đối không thể bỏ qua bằng confirm_review.
    - 'review' yêu cầu confirm_review = True.
    - Mọi trạng thái bất thường khác đều fail closed.

    Trả về: (ok: bool, error_message: str, status: Optional[str])
    """
    _purge_expired()
    now = time.time()
    p_hash = compute_prompt_hash(prompt)
    cur_fp = custom_fingerprint if custom_fingerprint is not None else compute_config_fingerprint()

    if not token or not str(token).strip():
        return (
            False,
            "Thiếu mã xác thực kiểm tra (Preflight token). Vui lòng bấm 'Kiểm tra prompt' trước khi gửi.",
            None,
        )

    clean_token = str(token).strip()
    rec = _PREFLIGHT_TOKENS.get(clean_token)
    if not rec:
        return (
            False,
            "Mã xác thực kiểm tra (Preflight token) không hợp lệ hoặc đã hết hạn. Vui lòng kiểm tra lại prompt.",
            None,
        )

    if rec["session_id"] != session_id:
        return (
            False,
            "Mã xác thực kiểm tra không thuộc phiên đăng nhập này. Vui lòng kiểm tra lại prompt.",
            None,
        )

    if rec["prompt_hash"] != p_hash:
        return (
            False,
            "Prompt đã bị sửa đổi sau khi kiểm tra. Vui lòng bấm 'Kiểm tra prompt' lại.",
            None,
        )

    if rec["expires_at"] <= now:
        return (
            False,
            "Kết quả kiểm tra prompt đã hết hạn. Vui lòng kiểm tra lại trước khi gửi.",
            None,
        )

    if rec["config_fingerprint"] != cur_fp:
        return (
            False,
            "Cấu hình AI Provider đã thay đổi kể từ lần kiểm tra trước. Vui lòng kiểm tra lại prompt.",
            None,
        )

    status = rec.get("status")
    if status == "block":
        reason = rec.get("reason") or "Nội dung vi phạm chính sách kiểm duyệt"
        suggestions = rec.get("suggestions") or ""
        msg = f"Nội dung bị chặn (Block): {reason}."
        if suggestions:
            msg += f" Gợi ý: {suggestions}"
        return (False, msg, status)

    if status == "review":
        if not confirm_review:
            reason = rec.get("reason") or "Nội dung cần người dùng xem xét"
            return (
                False,
                f"Nội dung thuộc diện cần xác nhận (Review): {reason}. Vui lòng xác nhận để tiếp tục gửi.",
                status,
            )
        return (True, "", status)

    if status == "allow":
        return (True, "", status)

    return (False, f"Trạng thái kiểm tra prompt không hợp lệ ({status}). Vui lòng kiểm tra lại.", None)
