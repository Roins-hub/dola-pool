"""2.4.0：豆包号生图（Seedream，只接豆包）。请求体与回复结构取自 2026-10-19 网页抓包。"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import sites


@pytest.fixture(autouse=True)
def _stubs():
    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError",
                "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))

    async def _gen(*_a, **_k):
        raise RuntimeError("stub")

    v.generate_video = _gen
    v.resume_video = _gen
    sys.modules["video_worker_ui"] = v
    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d
    yield


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _creation(key, *, final=True):
    image = {"status": 1, "placeholder": {}}
    if final:
        image.update({
            "key": key,
            "image_thumb": {"url": f"https://cdn/{key}~thumb", "width": 384, "height": 384},
            "image_ori": {"url": f"https://cdn/{key}~dld_wm", "width": 2048, "height": 2048},
            "image_preview": {"url": f"https://cdn/{key}~pre_wm", "width": 2048, "height": 2048},
            "image_ori_raw": {"url": f"https://cdn/{key}~raw", "width": 2048, "height": 2048},
        })
    return {"block_type": 2074, "content": {"creation_block": {"creations": [{"type": 1, "image": image}]}}}


def test_image_models_and_ratio_mapping():
    assert sites.image_spec("DOUBAO_SEEDREAM_5.0_FLASH") == ("doubao", "Seedream 5.0 Flash", sites.DOUBAO_IMAGE_RATIOS)
    assert set(sites.IMAGE_MODELS) == {"doubao-seedream-5.0-flash", "doubao-seedream-5.0-pro",
                                       "doubao-seedream-4.5", "doubao-seedream-4.0"}
    # 不接 dola 生图
    assert not sites.is_image_model("dola-seedream-5.0-pro")
    assert not sites.is_image_model("seedance-2.5")
    # OpenAI size → 最接近的比例；都没给 → 自动
    assert sites.pick_image_ratio(size="1536x1024") == "3:2"
    assert sites.pick_image_ratio(size="1024x1792") == "9:16"
    assert sites.pick_image_ratio("9:16") == "9:16"
    assert sites.pick_image_ratio(None, None) == "auto"
    assert sites.pick_image_ratio(None, "auto") == "auto"


def test_payload_hook_matches_web_request():
    import image_gen
    from protocol import dola_pure_api as dp
    payload, _, _ = dp.build_payload([], "一只猫", {"fp": "x"}, 5, "", None, video_ability=False)
    image_gen.image_payload_hook("一只猫", "Seedream 5.0 Pro", "16:9")(payload)
    text = payload["messages"][-1]["content_block"][0]["content"]["text_block"]["text"]
    assert text == "生成图片：一只猫，16:9"
    ability = payload["chat_ability"]
    assert ability["ability_type"] == 3
    inner = json.loads(ability["ability_param"])
    assert inner["ability_type"] == 1
    assert inner["ability_param"]["model"] == "Seedream 5.0 Pro" and inner["ability_param"]["ratio"] == "16:9"
    assert inner["ability_param"]["input_box_content"] == {"user_input_content": "一只猫",
                                                           "reply_message_format": "生成图片：%s"}
    assert "use_deep_think" not in payload["ext"]
    # 不给比例：文字里不带、参数里也不带（dola 网页默认）
    payload, _, _ = dp.build_payload([], "一只猫", {"fp": "x"}, 5, "", None, video_ability=False)
    image_gen.image_payload_hook("一只猫", "Seedream 4.5", None)(payload)
    assert payload["messages"][-1]["content_block"][0]["content"]["text_block"]["text"] == "生成图片：一只猫"
    assert "ratio" not in json.loads(payload["chat_ability"]["ability_param"])["ability_param"]


def test_find_images_skips_placeholders_and_dedupes():
    import image_gen
    events = [{"data": {"patch_op": [{"patch_value": {"content_block": [_creation("a", final=False)]}}]}},
              {"data": {"patch_op": [{"patch_value": {"content_block": [_creation("a"), _creation("b")]}}]}},
              {"data": {"patch_op": [{"patch_value": {"content_block": [_creation("a")]}}]}}]
    imgs = image_gen.find_images([e["data"] for e in events])
    assert [i["key"] for i in imgs] == ["a", "b"]
    assert imgs[0]["urls"]["image_ori"].endswith("~dld_wm") and imgs[0]["width"] == 2048
    # 会话里的 content 是 JSON 字符串，也要能找到
    chain = {"messages": [{"content": json.dumps([_creation("c")])}]}
    assert [i["key"] for i in image_gen.find_images(chain)] == ["c"]


class _FakeDP:
    BASE = "https://www.doubao.com"

    def __init__(self, events, *, reply=""):
        self.events = events
        self.reply = reply
        self.hooked = None
        self.wm_calls = []

    def normalize_image_paths(self, paths):
        return [Path(p) for p in paths]

    def upload_image(self, *a):
        return {"name": "x.png", "uri": "tos/x", "width": 10, "height": 10, "format": "png"}

    def submit_chat_completion(self, session, ctx, headers, **k):
        payload = {"messages": [{"content_block": [{"content": {"text_block": {"text": k["prompt"]}}}]}], "ext": {}}
        k["payload_hook"](payload)
        self.hooked = payload
        return types.SimpleNamespace(error="", error_code="", conversation_id="conv",
                                     events=[{"data": d} for d in self.events],
                                     reply_texts=[self.reply] if self.reply else [], final_reply_text=self.reply)

    def fetch_single_chain(self, *a, **k):
        return {}

    def dola_api_post_json(self, session, ctx, endpoint, body, **k):
        assert endpoint == "/creativity/resource/get_without_watermark"
        self.wm_calls.append(body)
        return {"data": {"without_watermark": True,
                         "download_image": {key: {"url": f"https://cdn/{key}~cgen_wm0"} for key in body["uri"]}}}


def _patch(monkeypatch, tmp_path, dp, *, no_watermark=True):
    import image_gen
    import doubao_gen
    state = tmp_path / "cookie_state.json"
    state.write_text(json.dumps({"cookies": {"sessionid": {"value": "s", "domain": ".doubao.com"}}}))
    monkeypatch.setattr(image_gen, "_site_module", lambda site: dp)
    monkeypatch.setattr(image_gen, "_session", lambda *a, **k: (types.SimpleNamespace(close=lambda: None), {}, {}))
    monkeypatch.setattr(doubao_gen, "ensure_no_watermark", lambda *a, **k: no_watermark)
    got = []

    def fake_download(url, out_dir, stem):
        path = Path(out_dir) / f"{stem}.png"
        path.write_bytes(b"png")
        got.append(url)
        return path

    monkeypatch.setattr(image_gen, "_download", fake_download)
    monkeypatch.setattr(image_gen.time, "sleep", lambda *_a: None)
    return state, got


def test_doubao_image_uses_official_clean_url(monkeypatch, tmp_path):
    import image_gen
    dp = _FakeDP([{"content_block": [_creation("k1")]}])
    state, got = _patch(monkeypatch, tmp_path, dp)
    res = image_gen.generate_image(account="d1", prompt="狐狸", model="doubao-seedream-5.0-flash",
                                   cookie_state_path=state, ratio="16:9", output_dir=tmp_path)
    assert dp.wm_calls == [{"uri": ["k1"]}]
    assert got == ["https://cdn/k1~cgen_wm0"]
    assert res["unwatermarked"] is True and res["ratio"] == "16:9" and len(res["images"]) == 1


def test_clean_url_failure_falls_back_to_official_download(monkeypatch, tmp_path):
    import image_gen
    dp = _FakeDP([{"content_block": [_creation("a"), _creation("b")]}])
    state, got = _patch(monkeypatch, tmp_path, dp)

    def broken(*a, **k):
        raise RuntimeError("无水印接口挂了")

    dp.dola_api_post_json = broken
    res = image_gen.generate_image(account="d1", prompt="猫", model="doubao-seedream-4.5",
                                   cookie_state_path=state, n=1, output_dir=tmp_path)
    # 退回平台的正式下载原图（带标识），绝不拿预览图 / raw 版本
    assert got == ["https://cdn/a~dld_wm"]
    assert len(res["images"]) == 1 and res["unwatermarked"] is False


def test_non_doubao_site_rejected():
    import image_gen
    with pytest.raises(image_gen.PureApiError):
        image_gen._site_module("dola")


def test_no_image_with_reply_is_refusal(monkeypatch, tmp_path):
    import image_gen
    dp = _FakeDP([], reply="这个请求涉及真实人物，无法生成")
    state, _ = _patch(monkeypatch, tmp_path, dp)
    with pytest.raises(image_gen.ImageRefusedError):
        image_gen.generate_image(account="a1", prompt="x", model="doubao-seedream-4.5",
                                 cookie_state_path=state, output_dir=tmp_path, timeout=1)


def _pool(tmp_path, monkeypatch, dola=("acc1",), doubao=("db1", "db2")):
    import browser_pool
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    accounts_dir = tmp_path / "accounts"
    for name, domain in [(n, ".dola.com") for n in dola] + [(n, ".doubao.com") for n in doubao]:
        (accounts_dir / name).mkdir(parents=True, exist_ok=True)
        (accounts_dir / name / "cookie_state.json").write_text(json.dumps(
            {"cookies_list": [{"name": "sessionid", "value": name, "domain": domain}]}))
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir), db_path=str(tmp_path / "pool.db"))
    for name in list(dola) + list(doubao):
        bp.set_login_status(name, True)
        bp.set_source(name, "cookie")

    async def ok_probe(name, **_k):
        return {"ok": True, "status": "ok"}

    bp.probe_hello_async = ok_probe
    return bp


def test_pool_routes_image_by_site_and_retries(tmp_path, monkeypatch):
    import image_gen
    bp = _pool(tmp_path, monkeypatch)
    calls = []

    def fake(**k):
        calls.append(k["account"])
        if k["account"] == "db1":
            raise image_gen.PureApiError("db1 生图提交失败: 网络抖动")
        return {"images": [{"local_path": "x.png"}], "account": k["account"], "model": k["model"]}

    monkeypatch.setattr(image_gen, "generate_image", fake)
    res = _run(bp.generate_image("猫", "doubao-seedream-4.0"))
    # 只用豆包号，dola 号 acc1 不参与
    assert res["account"] == "db2" and calls == ["db1", "db2"]
    # 拒稿不换号
    def refuse(**k):
        calls.append(k["account"])
        raise image_gen.ImageRefusedError("没出图：涉及真实人物")
    monkeypatch.setattr(image_gen, "generate_image", refuse)
    calls.clear()
    with pytest.raises(image_gen.ImageRefusedError):
        _run(bp.generate_image("x", "doubao-seedream-4.5"))
    assert len(calls) == 1


def test_pool_image_without_site_accounts(tmp_path, monkeypatch):
    bp = _pool(tmp_path, monkeypatch, doubao=())
    with pytest.raises(RuntimeError, match="没有能生图的豆包号"):
        _run(bp.generate_image("猫", "doubao-seedream-5.0-pro"))
