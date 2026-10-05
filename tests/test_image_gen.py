"""Tạo ảnh qua gateway OpenAI-compatible; mock transport, không gọi mạng thật."""
import base64
import asyncio
import httpx
import pytest
import image_gen
from ai_provider import AiProviderCallError, AiProviderNotConfiguredError

import io
from PIL import Image
_buffer = io.BytesIO()
Image.new("RGB", (2, 2), "blue").save(_buffer, format="PNG")
PNG = _buffer.getvalue()
CFG = {"base_url": "http://gw.invalid/v1", "api_key": "sk-test-secret-key-123456", "model": "m"}


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(request):
    return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})


def test_generate_image_returns_bytes_and_hits_images_endpoint():
    seen = {}

    def h(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return ok(request)

    data = asyncio.run(image_gen.generate_image(CFG, "cat", "1024x1024", client(h)))
    assert data == PNG and seen["url"] == "http://gw.invalid/v1/images/generations"
    assert seen["auth"] == "Bearer " + CFG["api_key"]


def test_missing_key_is_not_configured():
    with pytest.raises(AiProviderNotConfiguredError):
        asyncio.run(image_gen.generate_image({**CFG, "api_key": ""}, "cat"))


def test_error_hides_api_key():
    def h(request):
        return httpx.Response(500, text="boom " + CFG["api_key"])

    with pytest.raises(AiProviderCallError) as e:
        asyncio.run(image_gen.generate_image(CFG, "cat", client=client(h)))
    assert CFG["api_key"] not in str(e.value)


def test_generate_many_isolates_failures_and_keeps_order():
    def h(request):
        import json
        if "bad" in json.loads(request.content)["prompt"]:
            return httpx.Response(500, text="x")
        return ok(request)

    out = asyncio.run(image_gen.generate_many(CFG, ["a", "bad", "c"], "1024x1024", client(h)))
    assert [("data" in r) for r in out] == [True, False, True]
    assert [r["prompt"] for r in out] == ["a", "bad", "c"]


def test_settings_image_model_roundtrip_and_no_key_leak(tmp_path):
    import ai_settings
    enc, key = tmp_path / "a.enc", tmp_path / "a.key"
    assert ai_settings.get_image_config(enc, key) is None
    pub = ai_settings.update_settings({"openai": {"base_url": "http://127.0.0.1:20128/v1", "api_key": "k-123456", "image_model": "x/y"}}, enc, key)
    assert pub["openai"]["image_model"] == "x/y" and "k-123456" not in str(pub)
    cfg = ai_settings.get_image_config(enc, key)
    assert cfg == {"base_url": "http://127.0.0.1:20128/v1", "api_key": "k-123456", "model": "x/y"}


def test_gateway_non_image_bytes_are_rejected():
    def h(request):
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b'<html>gateway error</html>').decode()}]})
    with pytest.raises(AiProviderCallError, match='ảnh'):
        asyncio.run(image_gen.generate_image(CFG, 'cat', client=client(h)))


def test_gateway_malformed_response_is_safe():
    def h(request):
        return httpx.Response(200, text='gateway failed '+CFG['api_key'])
    with pytest.raises(AiProviderCallError) as e:
        asyncio.run(image_gen.generate_image(CFG, 'cat', client=client(h)))
    assert CFG['api_key'] not in str(e.value)
