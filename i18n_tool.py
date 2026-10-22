"""管理面板多语言工具：从 web/index.html 抽取中文文案，同步 / 检查 web/i18n/<lang>.json。

面板源码里的中文就是「源语言」，翻译文件是「中文原文 -> 译文」的映射；
运行时由 web/i18n/i18n.js 在页面上替换文字，不需要改动面板代码里的任何字符串。

用法：
    python i18n_tool.py extract            # 打印抽取到的全部中文文案（JSON 数组）
    python i18n_tool.py sync vi            # 把新文案补进 web/i18n/vi.json（译文留空），去掉已不存在的
    python i18n_tool.py check [vi ...]     # 报告缺译、占位符不一致；有占位符错误时退出码 1
    python i18n_tool.py missing vi         # 只打印 vi 里缺译的原文（JSON 数组），方便分批交给翻译

文案里的 {0} {1} … 是占位符（对应面板代码 ${...} 拼进去的数字、账号名等），译文必须原样保留，顺序可调。
"""
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "web" / "index.html"
I18N_DIR = ROOT / "web" / "i18n"

CJK = re.compile(r"[一-鿿]")
PLACEHOLDER = re.compile(r"\{(\d+)\}")
# 运行时会翻译的属性；其余属性（value、data-* 等）是数据，不翻
TEXT_ATTRS = ("title", "placeholder", "alt", "aria-label")
ATTR_RE = re.compile(r"""\b(%s)\s*=\s*(?:"([^"]*)"|'([^']*)')""" % "|".join(TEXT_ATTRS), re.I)
TAG_RE = re.compile(r"<[^<>]*>")
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "", "b": "", "f": "", "v": "", "0": ""}


def normalize(text: str) -> str:
    """与 i18n.js 的 normalize 一致：所有空白折叠成一个空格并去掉首尾。"""
    return re.sub(r"\s+", " ", text).strip()


def renumber(text: str) -> str:
    """把一段文案里的占位符重新从 {0} 起编号（同一模板切成几段后各段独立编号）。"""
    counter = iter(range(1000))
    return PLACEHOLDER.sub(lambda m: "{%d}" % next(counter), text)


def unescape_js(raw: str) -> str:
    out, i = [], 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            n = raw[i + 1]
            if n == "u" and re.match(r"[0-9a-fA-F]{4}", raw[i + 2:i + 6]):
                out.append(chr(int(raw[i + 2:i + 6], 16)))
                i += 6
                continue
            if n == "x" and re.match(r"[0-9a-fA-F]{2}", raw[i + 2:i + 4]):
                out.append(chr(int(raw[i + 2:i + 4], 16)))
                i += 4
                continue
            out.append(_JS_ESCAPES.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def js_literals(code: str):
    """极简 JS 词法：按出现顺序产出字符串 / 模板字面量的原始内容（模板里的 ${...} 换成 {n}）。

    跳过注释和正则字面量；模板里 ${...} 内部的代码会递归扫描，嵌套的字面量同样产出。
    """
    i, n = 0, len(code)
    prev = ""  # 上一个非空白代码字符，用来判断 / 是除号还是正则开头

    def read_quoted(j, quote):
        k = j + 1
        while k < n and code[k] != quote:
            if code[k] == "\\":
                k += 1
            elif code[k] == "\n":
                break
            k += 1
        return code[j + 1:k], k + 1

    def read_template(j):
        """j 指向 `；返回 (带 {n} 的原文, 嵌套字面量列表, 结束位置)。"""
        parts, nested, k, idx = [], [], j + 1, 0
        while k < n and code[k] != "`":
            if code[k] == "\\":
                parts.append(code[k:k + 2])
                k += 2
                continue
            if code[k] == "$" and k + 1 < n and code[k + 1] == "{":
                depth, m = 1, k + 2
                start = m
                while m < n and depth:
                    ch = code[m]
                    if ch in "'\"":
                        _, m = read_quoted(m, ch)
                        continue
                    if ch == "`":
                        _, _, m = read_template(m)
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    m += 1
                expr = code[start:m - 1]
                nested.append(expr)
                k = m
                # ${noTr(...)} 渲染成 <span translate="no">：运行时前后文字是两个文本节点，这里也按标签切开
                if expr.strip().startswith("noTr("):
                    parts.append("<noTr>")
                    continue
                parts.append("{%d}" % idx)
                idx += 1
                continue
            parts.append(code[k])
            k += 1
        return "".join(parts), nested, k + 1

    while i < n:
        c = code[i]
        if c == "/" and i + 1 < n and code[i + 1] == "/":
            i = code.find("\n", i)
            i = n if i < 0 else i
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "*":
            i = code.find("*/", i + 2)
            i = n if i < 0 else i + 2
            continue
        if c == "/" and (prev == "" or prev in "(,=:[!&|?{};+-*%<>~^"):
            k, in_class = i + 1, False
            while k < n and code[k] != "\n":
                ch = code[k]
                if ch == "\\":
                    k += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                k += 1
            i = k + 1
            prev = "/"
            continue
        if c in "'\"":
            raw, i = read_quoted(i, c)
            yield raw
            prev = c
            continue
        if c == "`":
            raw, nested, i = read_template(i)
            yield raw
            for inner in nested:
                yield from js_literals(inner)
            prev = "`"
            continue
        if not c.isspace():
            prev = c
        i += 1


def keys_from_markup(text: str, is_js: bool):
    """一段可能含 HTML 标签的文案 -> 若干条 key（标签之间的文字 + 可翻译属性值）。"""
    keys = []
    for tag in TAG_RE.findall(text):
        for m in ATTR_RE.finditer(tag):
            # 属性值（多为 placeholder）里的换行会原样显示：运行时按行翻译，这里也按行切
            value = m.group(2) if m.group(2) is not None else m.group(3)
            if is_js:
                value = unescape_js(value)
            keys.extend(html.unescape(value).split("\n"))
        # 标签里内联的 onclick="..." 等事件代码
        for m in re.finditer(r"""\bon\w+\s*=\s*"([^"]*)\"""", tag):
            keys.extend(keys_from_js(m.group(1).replace("&quot;", '"').replace("&#39;", "'")))
    for seg in TAG_RE.split(text):
        # 字符串拼接出来的半截标签：'<option'+x+'>启用' 里的 '>启用'、'说明<span' 里的 '说明<span'
        if is_js:
            # 拼接出来的标签片段 '" title="点击放大" onclick="…'：先取出可翻译属性值，其余丢掉
            for m in ATTR_RE.finditer(seg):
                keys.append(m.group(2) if m.group(2) is not None else m.group(3))
            # 属性值被 + 拆成两段：'title="前半；' + '后半">正文'，两半各算一条（运行时按片段拼）
            head = re.search(r"""\b(?:%s)\s*=\s*"([^"]*)$""" % "|".join(TEXT_ATTRS), seg)
            if head:
                keys.append(head.group(1))
            tail = re.match(r"""^([^"<>=]*)"[^<>]*>""", seg)
            if tail:
                keys.append(tail.group(1))
            seg = seg[seg.rfind(">") + 1:]
            if "<" in seg:
                seg = seg[:seg.find("<")]
            seg = re.sub(r"""[\w-]+\s*=\s*(?:"[^"]*"|'[^']*')""", "", seg)
            if re.search(r"""[\w-]+\s*=\s*["']""", seg):
                seg = ""
        # JS 里的 \n 是 alert/confirm 换行：运行时按行翻译，这里也按行切
        pieces = seg.split("\\n") if is_js else [seg]
        for piece in pieces:
            keys.append(unescape_js(piece) if is_js else piece)
    out = []
    for k in keys:
        k = renumber(normalize(html.unescape(k)))
        if CJK.search(k):
            out.append(k)
    return out


def keys_from_js(code: str):
    out = []
    for raw in js_literals(code):
        if CJK.search(raw):
            out.extend(keys_from_markup(raw, is_js=True))
    return out


def extract(html: str):
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    keys = []
    pos = 0
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S | re.I):
        keys.extend(keys_from_markup(html[pos:m.start()], is_js=False))
        keys.extend(keys_from_js(m.group(1)))
        pos = m.end()
    keys.extend(keys_from_markup(html[pos:], is_js=False))
    seen, ordered = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            ordered.append(k)
    return ordered


def catalog():
    return extract(INDEX_HTML.read_text(encoding="utf-8"))


def locale_path(lang: str) -> Path:
    return I18N_DIR / f"{lang}.json"


def load_locale(lang: str) -> dict:
    path = locale_path(lang)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_locale(lang: str, data: dict):
    locale_path(lang).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def placeholder_errors(data: dict):
    """译文里的占位符集合必须与原文一致。"""
    errors = []
    for src, dst in data.items():
        if dst and sorted(PLACEHOLDER.findall(src)) != sorted(PLACEHOLDER.findall(dst)):
            errors.append((src, dst))
    return errors


def sync(lang: str):
    keys = catalog()
    old = load_locale(lang)
    new = {k: old.get(k, "") for k in keys}
    save_locale(lang, new)
    added = sum(1 for k in keys if k not in old)
    removed = sum(1 for k in old if k not in new)
    print(f"{lang}: {len(new)} 条（新增 {added}，移除 {removed}，缺译 {sum(1 for v in new.values() if not v)}）")


def check(langs):
    keys = set(catalog())
    bad = False
    for lang in langs:
        data = load_locale(lang)
        missing = [k for k in keys if not data.get(k)]
        stale = [k for k in data if k not in keys]
        errs = placeholder_errors(data)
        print(f"{lang}: 共 {len(keys)} 条，缺译 {len(missing)}，多余 {len(stale)}，占位符错误 {len(errs)}")
        for src, dst in errs:
            print(f"  占位符不一致: {src!r} -> {dst!r}")
        bad = bad or bool(errs)
    return 1 if bad else 0


def available_langs():
    return sorted(p.stem for p in I18N_DIR.glob("*.json"))


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK / cp1252
    cmd = argv[1] if len(argv) > 1 else "check"
    args = argv[2:]
    if cmd == "extract":
        print(json.dumps(catalog(), ensure_ascii=False, indent=1))
    elif cmd == "sync":
        for lang in args or available_langs():
            sync(lang)
    elif cmd == "check":
        return check(args or available_langs())
    elif cmd == "missing":
        data = load_locale(args[0])
        print(json.dumps([k for k in catalog() if not data.get(k)], ensure_ascii=False, indent=1))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
