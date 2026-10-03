"""Kiểm thử tích hợp các endpoint Admin: AI Settings, Rewrite, Preflight, Video Generations."""
import asyncio
import hashlib
import io
import json
import pytest
from pathlib import Path
from httpx import AsyncClient, ASGITransport

import server
from ai_preflight import record_preflight_result
import ai_settings


@pytest.fixture
def test_app(tmp_path, monkeypatch):
    import ai_provider
    import httpx
    client_class=httpx.AsyncClient
    monkeypatch.setattr(ai_provider,'get_active_provider_config',lambda:{'provider':'openai','api_key':'fixture','model':'fixture','base_url':'https://ai.invalid/v1'})
    def ai_reply(request):
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'status':'allow','reason':'','suggestions':''})}}]})
    monkeypatch.setattr(ai_provider.httpx,'AsyncClient',lambda *args,**kwargs:client_class(transport=httpx.MockTransport(ai_reply)))
    # Dùng database và thư mục tạm
    encfile = tmp_path / "ai_settings.enc"
    keyfile = tmp_path / "ai_settings.key"
    monkeypatch.setattr(ai_settings, "_ENCFILE", encfile)
    monkeypatch.setattr(ai_settings, "_KEYFILE", keyfile)

    db_path = str(tmp_path / "test_tasks.db")
    from store import TaskStore
    store = TaskStore(db_path)
    monkeypatch.setattr(server, "store", store)

    # Đăng nhập giả lập để tạo admin token
    token = "test_admin_token_xyz"
    server.ADMIN_SESSIONS[token] = {
        "username": "admin",
        "role": "owner",
        "expires_at": 9999999999.0,
    }

    # Đảm bảo pool có tài khoản để _submit_task không báo no account
    from browser_pool import BrowserPool
    monkeypatch.setattr(BrowserPool, "available", property(lambda self: True))
    monkeypatch.setattr(BrowserPool, "all_accounts_limited", property(lambda self: False))
    monkeypatch.setattr(BrowserPool, "all_accounts_quota_blocked", property(lambda self: False))
    monkeypatch.setattr(BrowserPool, "accounts", property(lambda self: ["acc1"]))

    # Ngăn _run_task chạy thật
    async def fake_run_task(*args, **kwargs):
        pass

    monkeypatch.setattr(server, "_run_task", fake_run_task)

    session_id = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
    return server.app, token, session_id, encfile, keyfile, store


@pytest.mark.asyncio
async def test_admin_ai_settings_crud(test_app):
    app, token, session_id, encfile, keyfile, _ = test_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Chưa đăng nhập -> 401
        r = await ac.get("/api/admin/ai-settings")
        assert r.status_code == 401

        # 2. Đã đăng nhập -> Lấy cấu hình mặc định (chưa có key)
        r = await ac.get("/api/admin/ai-settings", headers={"x-admin-key": token})
        assert r.status_code == 200
        data = r.json()
        assert data["active_provider"] is None
        assert data["gemini"]["has_api_key"] is False
        assert data["openai"]["has_api_key"] is False

        # 3. Cập nhật OpenAI-compatible
        r = await ac.put(
            "/api/admin/ai-settings",
            headers={"x-admin-key": token},
            json={
                "active_provider": "openai",
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini",
                    "api_key": "sk-secret-12345",
                },
            },
        )
        assert r.status_code == 200
        res = r.json()
        assert res["active_provider"] == "openai"
        assert res["openai"]["has_api_key"] is True
        assert "sk-secret-12345" not in json.dumps(res)

        # 4. GET lại -> Xác nhận không lộ key
        r_get = await ac.get("/api/admin/ai-settings", headers={"x-admin-key": token})
        assert r_get.status_code == 200
        assert r_get.json()["openai"]["has_api_key"] is True
        assert "sk-secret" not in r_get.text


@pytest.mark.asyncio
async def test_admin_preflight_gate_enforcement(test_app, monkeypatch):
    app, token, session_id, encfile, keyfile, store = test_app
    transport = ASGITransport(app=app)

    prompt = "Một kiếm khách luyện kiếm dưới chân thác nước hùng vĩ"

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # A. Cố tình gửi tạo video KHI CHƯA CHẠY PREFLIGHT -> Bị Backend từ chối 422!
        r_no_pf = await ac.post(
            "/api/admin/videos/generations",
            headers={"x-admin-key": token},
            data={"prompt": prompt, "model": "seedance-2.5", "duration": 30},
        )
        assert r_no_pf.status_code == 422
        assert "Thiếu mã xác thực kiểm tra" in r_no_pf.json()["detail"] or "chưa qua bước kiểm tra" in r_no_pf.json()["detail"]

        # B. Chạy preflight giả lập trả về ALLOW và nhận token
        rec = record_preflight_result(session_id, prompt, {"status": "allow", "reason": ""})
        pf_token = rec["token"]

        # Gửi lại với đúng prompt và token -> Được chấp thuận 200, tạo task queued!
        r_ok = await ac.post(
            "/api/admin/videos/generations",
            headers={"x-admin-key": token},
            data={"prompt": prompt, "model": "seedance-2.5", "duration": 30, "preflight_token": pf_token},
        )
        assert r_ok.status_code == 200
        task_data = r_ok.json()
        assert task_data["status"] == "queued"
        task_id = task_data["id"]

        # Xác nhận task đã được lưu trong DB
        db_task = store.get(task_id)
        assert db_task is not None
        assert db_task["prompt"] == prompt

        # C. Sửa prompt 1 chữ so với bản đã preflight -> Bị Backend từ chối 422!
        modified_prompt = prompt + " vào buổi tối"
        r_tampered = await ac.post(
            "/api/admin/videos/generations",
            headers={"x-admin-key": token},
            data={"prompt": modified_prompt, "model": "seedance-2.5", "duration": 30, "preflight_token": pf_token},
        )
        assert r_tampered.status_code == 422
        assert "sửa đổi" in r_tampered.json()["detail"] or "chưa qua" in r_tampered.json()["detail"]


@pytest.mark.asyncio
async def test_admin_preflight_review_and_block_flow(test_app):
    app, token, session_id, encfile, keyfile, store = test_app
    transport = ASGITransport(app=app)

    prompt_review = "Hai cao thủ combat ác liệt trên đỉnh Tử Cấm Thành"
    prompt_block = "Cảnh bạo lực đẫm máu tàn sát"

    # Lưu kết quả preflight
    rec_review = record_preflight_result(
        session_id,
        prompt_review,
        {"status": "review", "reason": "Cảnh giao đấu kiếm thuật giả tưởng"},
    )
    rec_block = record_preflight_result(
        session_id,
        prompt_block,
        {"status": "block", "reason": "Bạo lực máu me", "suggestions": "Bỏ từ ngữ đổ máu"},
    )

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Review mà KHÔNG có confirm_review -> 422
        r1 = await ac.post(
            "/api/admin/videos/generations",
            headers={"x-admin-key": token},
            data={"prompt": prompt_review, "model": "seedance-2.5", "duration": 30, "preflight_token": rec_review["token"]},
        )
        assert r1.status_code == 422
        assert "cần xác nhận (Review)" in r1.json()["detail"]

        # 2. Review CÓ confirm_review=true -> 200 thành công
        r2 = await ac.post(
            "/api/admin/videos/generations",
            headers={"x-admin-key": token},
            data={
                "prompt": prompt_review,
                "model": "seedance-2.5",
                "duration": 30,
                "preflight_token": rec_review["token"],
                "confirm_review": "true",
            },
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "queued"

        # 3. Block -> Bị từ chối dù có confirm hay không
        r3 = await ac.post(
            "/api/admin/videos/generations",
            headers={"x-admin-key": token},
            data={
                "prompt": prompt_block,
                "model": "seedance-2.5",
                "duration": 30,
                "preflight_token": rec_block["token"],
                "confirm_review": "true",
            },
        )
        assert r3.status_code == 422
        assert "Nội dung bị chặn (Block)" in r3.json()["detail"]
        assert "Bạo lực máu me" in r3.json()["detail"]


@pytest.mark.asyncio
async def test_admin_reference_image_upload(test_app):
    app, token, session_id, encfile, keyfile, store = test_app
    transport = ASGITransport(app=app)
    prompt = "Cảnh cô gái áo xanh nhìn về phía chân trời"
    rec = record_preflight_result(session_id, prompt, {"status": "allow"})
    pf_token = rec["token"]

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Tải lên file sai đuôi (.txt) -> 422
        bad_file = ("bad.txt", io.BytesIO(b"not an image"), "text/plain")
        r_bad = await ac.post(
            "/api/admin/videos/generations",
            headers={"x-admin-key": token},
            data={"prompt": prompt, "model": "seedance-2.5", "duration": 30, "preflight_token": pf_token},
            files=[("images", bad_file)],
        )
        assert r_bad.status_code == 422
        assert "Định dạng ảnh không được hỗ trợ" in r_bad.json()["detail"]

        # 2. Tải lên file đuôi .png nhưng nội dung là chuỗi text giả mạo -> 422 (Magic bytes check)
        fake_content_file = ("fake.png", io.BytesIO(b"this is plain text not a png image file"), "image/png")
        r_fake = await ac.post(
            "/api/admin/videos/generations",
            headers={"x-admin-key": token},
            data={"prompt": prompt, "model": "seedance-2.5", "duration": 30, "preflight_token": pf_token},
            files=[("images", fake_content_file)],
        )
        assert r_fake.status_code == 422
        assert "không phải định dạng" in r_fake.json()["detail"] or "không thể giải mã" in r_fake.json()["detail"]

        # 3. Tải lên ảnh hợp lệ (.png) -> 200
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (100, 100), color="blue").save(buf, format="PNG")
        png_bytes = buf.getvalue()
        good_file = ("ref1.png", io.BytesIO(png_bytes), "image/png")
        r_good = await ac.post(
            "/api/admin/videos/generations",
            headers={"x-admin-key": token},
            data={"prompt": prompt, "model": "seedance-2.5", "duration": 30, "preflight_token": pf_token},
            files=[("images", good_file)],
        )
        assert r_good.status_code == 200
        task_id = r_good.json()["id"]
        task = store.get(task_id)
        assert task is not None
        refs = json.loads(task["reference_images"])
        assert len(refs) == 1
        # Data url đã được materialize thành đường dẫn file hợp lệ
        assert refs[0].startswith("refs/") or refs[0].endswith(".png")
