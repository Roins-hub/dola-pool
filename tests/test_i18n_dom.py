"""管理面板多语言：用户 / 上游数据不能被词典改写（PR #12 审查 P2）。

动态 DOM 部分用 node 跑 tests/i18n_dom_harness.js（极简 DOM + 真实的 i18n.js、vi.json 和面板渲染函数）；
没装 node 时跳过。静态部分扫 web/index.html，防止以后新加的渲染点忘了用 noTr / translate="no"。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / "i18n_dom_harness.js"
NAME = "代理(默认)"


@pytest.fixture(scope="module")
def dom():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 node")
    r = subprocess.run([node, str(HARNESS), str(ROOT)], capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_dictionary_would_rewrite_the_name(dom):
    # 前提：词典里真有这一条，否则下面的用例测不出东西
    assert dom["dict_has_name"]
    assert dom["default_button"] == "Proxy (mặc định)"


def test_proxy_names_kept(dom):
    assert dom["proxy_button"] == NAME
    assert dom["proxy_option"] == NAME
    assert dom["egress_proxy"] == NAME + " · 1.2.3.4:8080"


def test_panel_text_around_data_still_translated(dom):
    assert dom["dialog_title"] == "Đặt proxy tài khoản: acc1"
    assert dom["default_option"] == "Proxy mặc định (DOLA_PROXY)"
    assert dom["egress_direct"] == "Máy chủ kết nối trực tiếp (chưa cấu hình proxy)"
    assert dom["note_title"] == "Nhấn để sửa ghi chú"


def test_notes_and_upstream_text_kept(dom):
    assert dom["note_text"] == NAME
    assert dom["upstream_note"] == "acc1 · dola：" + NAME


def test_rerender_and_text_change_kept(dom):
    assert dom["rerender"] == NAME


# 面板里会显示的用户 / 上游数据字段
DATA_FIELD = re.compile(
    r"esc\((x\.proxy_name|x\.email|p\.name|p\?p\.name:id|p\.remark|p\.username|n\.username|k\.name"
    r"|t\.api_key_name|u\.username|note|cur\.account|n\.account|t\.account|v\.account|name)\b")
# 这些位置是属性值 / JS 参数，不会被翻译
ATTR_CONTEXT = re.compile(r"""(value="\$\{(p\?)?|\('\$\{)$""")


def test_panel_renders_user_data_with_notr():
    bad = []
    for no, line in enumerate((ROOT / "web" / "index.html").read_text(encoding="utf-8").splitlines(), 1):
        for m in DATA_FIELD.finditer(line):
            before = line[:m.start()]
            if ATTR_CONTEXT.search(before) or 'translate="no"' in before:
                continue
            bad.append(f"{no}: {line.strip()[:120]}")
    assert not bad, "这些地方把用户数据当普通文字插入，换语言时会被翻译，请改用 noTr()：\n" + "\n".join(bad)
    # 不转义直接插入（如 t.api_key_name||'—'）同样不行
    raw = re.findall(r"^\s*(?:t\.api_key_name|v\.account|k\.name)\|\|", (ROOT / "web" / "index.html")
                     .read_text(encoding="utf-8"), flags=re.M)
    assert not raw


@pytest.mark.parametrize("lang", ["zh", "vi", "vi-zh"])
def test_native_dialogs_keep_dynamic_values(lang):
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 node")
    r = subprocess.run([node, str(HARNESS), str(ROOT), lang], capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout.strip().splitlines()[-1])
    for key in ("delete_account_confirm", "delete_user_confirm", "recover_confirm"):
        assert "风控账号" in result[key]
        assert "Rủi roTài khoản" not in result[key]
    for key in ("delete_proxy_confirm", "rotate_confirm"):
        assert "代理(默认)" in result[key]
        assert "Proxy (mặc định)" not in result[key]
    for key in ("opaque_alert", "opaque_prompt"):
        assert result["opaque_value"] in result[key]
    if lang == "zh":
        assert result["delete_account_confirm"] == "确定删除账号 风控账号？将同时删除其 profile。"
    else:
        assert result["delete_account_confirm"].startswith("Xác nhận xóa tài khoản 风控账号?")
        if lang == "vi-zh":
            assert "确定删除账号 风控账号？将同时删除其 profile。" in result["delete_account_confirm"]
