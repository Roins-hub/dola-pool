"""管理面板多语言：文案抽取 + 翻译文件的格式检查（缺译不算失败，占位符错了才算）。"""
import json

import i18n_tool


def test_extract_template_placeholders_and_tags():
    html = """<div title="清除面板密码">刷新</div>
<script>
// 注释里的中文不抽
q('#a').innerHTML=`<div class="l">可用号 · 剩余 ${st.total} 点</div><b title="出片 ${d.n}">x</b>`;
alert('失败：'+e.message+'\\n请重试');
const re=/[，、]/g;
</script>"""
    keys = i18n_tool.extract(html)
    assert "清除面板密码" in keys
    assert "刷新" in keys
    assert "可用号 · 剩余 {0} 点" in keys
    assert "出片 {0}" in keys
    assert "失败：" in keys and "请重试" in keys
    assert not any("注释" in k for k in keys)


def test_extract_concatenated_tag_fragments():
    html = r"""<script>
const a='<span class="x" title="点击放大" onclick="show(\''+id+'\')">';
const b='<option value="1"'+(on?' selected':'')+'>启用该账号</option>';
</script>"""
    keys = i18n_tool.extract(html)
    assert "点击放大" in keys
    assert "启用该账号" in keys
    assert not any("=" in k or ">" in k for k in keys)


def test_renumber_per_segment():
    html = "<script>x=`${a}<i>剩余 ${b} 点，共 ${c} 条</i>`;</script>"
    assert "剩余 {0} 点，共 {1} 条" in i18n_tool.extract(html)


def test_panel_catalog_covers_main_ui():
    keys = set(i18n_tool.catalog())
    for k in ("仪表盘", "账号管理", "代理管理", "任务列表", "登录"):
        assert k in keys


def test_locale_files_are_valid():
    langs = i18n_tool.available_langs()
    assert "vi" in langs
    for lang in langs:
        data = i18n_tool.load_locale(lang)
        assert isinstance(data, dict)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in data.items())
        assert i18n_tool.placeholder_errors(data) == [], lang
