"""Cổng kiểm soát Preflight ở tầng Backend trước khi gửi task tạo video."""
from __future__ import annotations

import hashlib
import json
import copy
from prompt_rules import RULES_VERSION
import re
import time
import uuid
from typing import Any, Optional

from ai_settings import compute_config_fingerprint

# Thời gian hiệu lực của kết quả preflight (15 phút)
PREFLIGHT_EXPIRY_SECONDS = 900.0

# Lưu trữ cache preflight trong bộ nhớ: {token: Record}
_PREFLIGHT_TOKENS: dict[str, dict[str, Any]] = {}


def context_fingerprint(context: dict) -> str:
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_issue_anchors(prompt: str, issues: list[dict]) -> list[dict]:
    checked = copy.deepcopy(issues)
    ranges = []
    for issue in checked:
        issue.update(applicable=False, anchor_error="")
        quote = issue.get("quote") or ""
        if not quote or not isinstance(issue.get("replacement"), str):
            issue["anchor_error"] = "Nhận xét không có đoạn thay thế trực tiếp."
            continue
        positions = []
        start = 0
        while True:
            pos = prompt.find(quote, start)
            if pos < 0:
                break
            positions.append(pos)
            start = pos + 1
        occurrence = issue.get("occurrence")
        if not positions or (len(positions) > 1 and occurrence is None):
            issue["anchor_error"] = "Đoạn trích không tồn tại hoặc xuất hiện nhiều lần; cần chọn vị trí."
            continue
        index = (occurrence or 1) - 1
        if not isinstance(index, int) or index < 0 or index >= len(positions):
            issue["anchor_error"] = "Vị trí đoạn trích không hợp lệ."
            continue
        issue.update(applicable=True, occurrence=index + 1)
        ranges.append((positions[index], positions[index] + len(quote), issue))
    for i, (start, end, issue) in enumerate(ranges):
        for other_start, other_end, other in ranges[i + 1:]:
            if start < other_end and other_start < end:
                issue.update(applicable=False, anchor_error="Các gợi ý chồng lấn; hãy sửa trực tiếp.")
                other.update(applicable=False, anchor_error="Các gợi ý chồng lấn; hãy sửa trực tiếp.")
    return checked


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
    *, context: Optional[dict] = None,
) -> dict[str, Any]:
    """Lưu kết quả preflight và phát hành verification token gắn với session + prompt + config."""
    _purge_expired()
    now = time.time()
    token = "pf_" + uuid.uuid4().hex
    p_hash = compute_prompt_hash(prompt)
    cfg_fp = custom_fingerprint if custom_fingerprint is not None else compute_config_fingerprint()

    analysis = {key: result[key] for key in ("quality_summary", "issues", "rules_version", "sources", "context_fingerprint") if key in result}
    if "issues" in analysis:
        analysis["issues"] = validate_issue_anchors(prompt, analysis["issues"])
    record = {
        **analysis,
        "exact_prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
        "context": copy.deepcopy(context),
        "context_fingerprint": context_fingerprint(context) if context is not None else None,
        "rules_version": RULES_VERSION,
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
        **analysis,
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
    *, context: Optional[dict] = None, rules_version: Optional[str] = None,
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

    if rec.get("rules_version") != (rules_version or RULES_VERSION):
        return False, "Quy tắc kiểm tra đã đổi; hãy kiểm tra lại prompt.", None
    if rec.get("context") is not None:
        if context is None or rec.get("context_fingerprint") != context_fingerprint(context):
            return False, "Cấu hình video hoặc ảnh đã đổi; hãy kiểm tra lại prompt.", None
        if rec.get("exact_prompt_hash") != hashlib.sha256(prompt.encode()).hexdigest():
            return False, "Nguyên văn prompt đã đổi; hãy kiểm tra lại trước khi áp dụng gợi ý.", None
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
