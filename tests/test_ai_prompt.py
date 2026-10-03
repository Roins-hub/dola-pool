"""Unit test cho ai_provider.py (Rewrite & Preflight) với mock HTTP transport."""
import json
import pytest
import httpx

from ai_provider import (
    rewrite_prompt_text,
    preflight_check_prompt,
    AiProviderCallError,
    AiProviderNotConfiguredError,
)


@pytest.fixture
def mock_gemini_config():
    return {
        "provider": "gemini",
        "api_key": "dummy-gemini-key",
        "model": "gemini-2.5-flash",
    }


@pytest.fixture
def mock_openai_config():
    return {
        "provider": "openai",
        "api_key": "dummy-openai-key",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    }


@pytest.mark.asyncio
async def test_rewrite_success_openai(mock_openai_config):
    rewritten_text = "Góc quay cinematic tracking shot theo bước chân kiếm khách trong mưa tuyết, ánh sáng xanh mờ ảo."

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert "Bearer dummy-openai-key" in request.headers["authorization"]
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "gpt-4o-mini"
        assert len(body["messages"]) >= 2
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": rewritten_text}}
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await rewrite_prompt_text(
            original_prompt="Kiếm khách đi dưới tuyết",
            duration=30,
            ratio="16:9",
            override_config=mock_openai_config,
            client=client,
        )
        assert res == rewritten_text


@pytest.mark.asyncio
async def test_rewrite_success_gemini(mock_gemini_config):
    rewritten_text = "Slow pan shot hoa anh đào rơi trong gió xuân, ánh sáng vàng lúc chiều tà."

    def handler(request: httpx.Request) -> httpx.Response:
        assert "generativelanguage.googleapis.com" in request.url.host
        # API key được truyền qua header x-goog-api-key, KHÔNG để trong URL query string
        assert request.headers.get("x-goog-api-key") == "dummy-gemini-key"
        assert "key" not in request.url.params
        body = json.loads(request.content.decode("utf-8"))
        assert "contents" in body
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": rewritten_text}]}}
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await rewrite_prompt_text(
            original_prompt="Hoa anh đào rơi",
            override_config=mock_gemini_config,
            client=client,
        )
        assert res == rewritten_text


@pytest.mark.asyncio
async def test_preflight_allow(mock_openai_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "allow",
                                    "reason": "",
                                    "suggestions": "",
                                }
                            )
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await preflight_check_prompt(
            prompt="Cảnh núi Phú Sĩ tuyết phủ mùa đông",
            override_config=mock_openai_config,
            client=client,
        )
        assert res["status"] == "allow"
        assert res["reason"] == ""


@pytest.mark.asyncio
async def test_preflight_review_for_fantasy_combat(mock_openai_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "review",
                                    "reason": "Cảnh combat kiếm hiệp giả tưởng, cần người dùng xác nhận mức độ va chạm",
                                    "suggestions": "",
                                }
                            )
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await preflight_check_prompt(
            prompt="Hai nhân vật combat trên mái ngói",
            override_config=mock_openai_config,
            client=client,
        )
        assert res["status"] == "review"
        assert "combat" in res["reason"]


@pytest.mark.asyncio
async def test_preflight_block_with_suggestions(mock_openai_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "block",
                                    "reason": "Chứa nội dung bạo lực đẫm máu",
                                    "suggestions": "Bỏ từ ngữ miêu tả thương tích đẫm máu",
                                }
                            )
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await preflight_check_prompt(
            prompt="Cảnh bạo lực đẫm máu",
            override_config=mock_openai_config,
            client=client,
        )
        assert res["status"] == "block"
        assert res["reason"] != ""
        assert res["suggestions"] != ""


@pytest.mark.asyncio
async def test_provider_401_or_500_raises_error(mock_openai_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Incorrect API key provided")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AiProviderCallError) as exc_info:
            await preflight_check_prompt(
                prompt="Test prompt",
                override_config=mock_openai_config,
                client=client,
            )
        assert "401" in str(exc_info.value)
        assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_provider_unconfigured(monkeypatch):
    import ai_provider
    monkeypatch.setattr(ai_provider, "get_active_provider_config", lambda: None)
    with pytest.raises(AiProviderNotConfiguredError):
        await preflight_check_prompt(prompt="Test", override_config=None)
