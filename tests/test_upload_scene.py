"""prepare_upload 的 scene_id 按站点区分：dola "4"，豆包 "5"（豆包用 "4" 会回 710010703，参考图传不上）。"""
from protocol import dola_pure_api, doubao_site


def _body(module, monkeypatch):
    seen = {}
    def fake_post(session, ctx, path, body, **kw):
        seen.update(path=path, body=body)
        return {"data": {"ok": True}}
    monkeypatch.setattr(module, "dola_api_post_json", fake_post)
    module.prepare_upload(None, {}, {}, resource_type=2)
    return seen


def test_dola_and_doubao_use_their_own_scene(monkeypatch):
    doubao = doubao_site.load()
    assert _body(dola_pure_api, monkeypatch)["body"] == {"tenant_id": "5", "scene_id": "4", "resource_type": 2}
    assert _body(doubao, monkeypatch)["body"] == {"tenant_id": "5", "scene_id": "5", "resource_type": 2}
    assert dola_pure_api.UPLOAD_SCENE_ID == "4"      # 豆包模块的覆盖不会串到 dola
