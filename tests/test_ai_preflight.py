"""Unit test cho ai_preflight.py."""
import time
from ai_preflight import (
    record_preflight_result,
    validate_preflight_gate,
    compute_prompt_hash,
    normalize_prompt,
)


def test_normalize_and_hash():
    p1 = "  Một   tiên nhân  bay trên mây  "
    p2 = "Một tiên nhân bay trên mây"
    assert normalize_prompt(p1) == normalize_prompt(p2)
    assert compute_prompt_hash(p1) == compute_prompt_hash(p2)


def test_allow_flow():
    prompt = "Cảnh bình minh trên cánh đồng hoa tam giác mạch Hà Giang"
    session_id = "sess_user_1"
    fp = "fp_test_123"

    res = record_preflight_result(
        session_id=session_id,
        prompt=prompt,
        result={"status": "allow", "reason": "", "suggestions": ""},
        custom_fingerprint=fp,
    )
    token = res["token"]
    assert token.startswith("pf_")

    # Xác thực thành công
    ok, err, st = validate_preflight_gate(
        session_id=session_id,
        prompt=prompt,
        token=token,
        custom_fingerprint=fp,
    )
    assert ok is True
    assert err == ""
    assert st == "allow"


def test_block_flow():
    prompt = "Nội dung bạo lực đẫm máu cực đoan"
    session_id = "sess_user_1"
    fp = "fp_test_123"

    res = record_preflight_result(
        session_id=session_id,
        prompt=prompt,
        result={
            "status": "block",
            "reason": "Vi phạm chính sách bạo lực",
            "suggestions": "Giảm bớt yếu tố đổ máu, chuyển sang giao đấu kiếm thuật giả tưởng",
        },
        custom_fingerprint=fp,
    )
    token = res["token"]

    ok, err, st = validate_preflight_gate(
        session_id=session_id,
        prompt=prompt,
        token=token,
        custom_fingerprint=fp,
    )
    assert ok is False
    assert "Nội dung bị chặn (Block)" in err
    assert "Vi phạm chính sách bạo lực" in err
    assert st == "block"


def test_review_flow_requires_confirmation():
    prompt = "Hai hiệp khách quyết chiến đỉnh núi, kiếm quang lấp lánh"
    session_id = "sess_user_1"
    fp = "fp_test_123"

    res = record_preflight_result(
        session_id=session_id,
        prompt=prompt,
        result={
            "status": "review",
            "reason": "Cảnh chiến đấu kiếm hiệp, cần kiểm tra mức độ va chạm",
            "suggestions": "",
        },
        custom_fingerprint=fp,
    )
    token = res["token"]

    # 1. Chưa confirm -> Bị chặn
    ok, err, st = validate_preflight_gate(
        session_id=session_id,
        prompt=prompt,
        token=token,
        confirm_review=False,
        custom_fingerprint=fp,
    )
    assert ok is False
    assert "Nội dung thuộc diện cần xác nhận (Review)" in err
    assert st == "review"

    # 2. Đã confirm -> Cho qua
    ok2, err2, st2 = validate_preflight_gate(
        session_id=session_id,
        prompt=prompt,
        token=token,
        confirm_review=True,
        custom_fingerprint=fp,
    )
    assert ok2 is True
    assert err2 == ""
    assert st2 == "review"


def test_prompt_modification_invalidates_preflight():
    prompt1 = "Cảnh hoàng hôn trên biển Đà Nẵng"
    prompt2 = "Cảnh hoàng hôn trên biển Nha Trang"
    session_id = "sess_user_1"
    fp = "fp_test_123"

    res = record_preflight_result(
        session_id=session_id,
        prompt=prompt1,
        result={"status": "allow", "reason": ""},
        custom_fingerprint=fp,
    )
    token = res["token"]

    # Dùng token cho prompt2 -> Bị từ chối
    ok, err, _ = validate_preflight_gate(
        session_id=session_id,
        prompt=prompt2,
        token=token,
        custom_fingerprint=fp,
    )
    assert ok is False
    assert "sửa đổi" in err or "chưa qua bước kiểm tra" in err


def test_config_change_invalidates_preflight():
    prompt = "Cảnh bình minh tuyệt đẹp"
    session_id = "sess_user_1"
    fp_old = "fp_old"
    fp_new = "fp_new"

    res = record_preflight_result(
        session_id=session_id,
        prompt=prompt,
        result={"status": "allow", "reason": ""},
        custom_fingerprint=fp_old,
    )
    token = res["token"]

    # Khi config fingerprint đổi sang fp_new
    ok, err, _ = validate_preflight_gate(
        session_id=session_id,
        prompt=prompt,
        token=token,
        custom_fingerprint=fp_new,
    )
    assert ok is False
    assert "Cấu hình AI Provider đã thay đổi" in err
