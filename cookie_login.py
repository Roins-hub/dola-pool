"""通过 Cookie / 完整存储 bundle 导入 dola 账号。

参考 DolaMultiBrowser（方悦多开）的实现：真正让 Dola 接受导入会话的，
不只是 cookie，还包括 localStorage/sessionStorage/indexedDB。

支持的输入：
1. Cookie-Editor / EditThisCookie 扩展导出（对象含 cookies 数组）；
2. 直接 cookie 数组；
3. 完整 bundle：{ cookies, localStorage, sessionStorage, indexedDB }。

流程：
1. 在 accounts/<name>/ 创建 persistent profile；
2. 用 WebView2/Playwright CookieManager 同等方式写 cookies；
3. 若 bundle 含 web storage，打开 dola.com 后恢复 localStorage/
   sessionStorage/indexedDB（与 DolaMultiBrowser 注入脚本同思路）；
4. require_login 时再做真实验证；验证失败清理 profile。
"""
import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

from patchright.async_api import async_playwright

import config
from browser import EDGE_MARKER, LAUNCH_ARGS, check_login_state, proxy_kwargs_for


def normalize_cookies(data) -> list[dict]:
    """把扩展导出 JSON / cookie 数组统一成普通 dict 列表。"""
    if isinstance(data, dict):
        data = data.get("cookies") or data.get("cookie") or [data]
    if not isinstance(data, list):
        raise ValueError("cookie 格式无法识别：需要数组，或含 cookies 数组的对象")
    out: list[dict] = []
    for c in data:
        if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
            raise ValueError("cookie 缺少 name/value 字段")
        out.append(c)
    return out


def normalize_storage_bundle(data) -> dict:
    """从导入数据中提取完整 web storage。

    兼容两种形态：
    - 根对象直接带 localStorage/sessionStorage/indexedDB；
    - 或根对象带 storage: { localStorage:..., sessionStorage:..., indexedDB:... }。
    """
    if not isinstance(data, dict):
        return {}
    root = data.get("storage") if isinstance(data.get("storage"), dict) else data
    bundle = {}
    for key in ("localStorage", "sessionStorage", "indexedDB"):
        value = root.get(key)
        if isinstance(value, dict) or isinstance(value, list):
            bundle[key] = value
    return bundle


def to_playwright_cookies(cookies: list[dict]) -> list[dict]:
    """转为 playwright add_cookies 可接受的格式。"""
    out: list[dict] = []
    for c in cookies:
        domain = str(c.get("domain") or "").strip().lstrip(".")
        if not domain:
            raise ValueError("cookie 缺少 domain 字段")
        item = {
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": domain,
            "path": str(c.get("path") or "/"),
        }
        # 会话 cookie 不写 expires 时 Chromium 不会落盘到 profile，
        # 因此统一转成“本地 90 天过期”的持久 cookie（服务端有效期不受影响）。
        exp = None
        if not c.get("isSession"):
            exp = c.get("expiresUtc") or c.get("expires")
        if exp is None or not str(exp).strip():
            exp = time.time() + 90 * 86400
        else:
            try:
                exp = float(exp)
                if exp > 1e12:
                    exp = exp / 1000
            except (TypeError, ValueError):
                exp = time.time() + 90 * 86400
        item["expires"] = exp
        if "isHttpOnly" in c:
            item["httpOnly"] = bool(c["isHttpOnly"])
        if "isSecure" in c:
            item["secure"] = bool(c["isSecure"])
        if c.get("sameSite"):
            ss = str(c["sameSite"]).lower()
            item["sameSite"] = {
                "strict": "Strict",
                "lax": "Lax",
                "none": "None",
                "no_restriction": "None",
            }.get(ss, "Lax")
        out.append(item)
    return out


def _write_cookie_state(profile_dir: Path, cookies: list[dict], storage_bundle: dict) -> None:
    """把导入的 cookie 落盘成 cookie_state.json（对齐 dola-pool-cookie 的账号状态文件）。

    供后续「纯 API」出片路径与面板复核复用；cookie 来源账号才有此文件。
    """
    state = {
        "source": "cookie",
        "cookies_list": [
            {
                "name": c.get("name"),
                "value": c.get("value", ""),
                "domain": c.get("domain") or ".dola.com",
                "path": c.get("path") or "/",
            }
            for c in cookies
            if c.get("name")
        ],
        "cookies": {
            c["name"]: {
                "value": c.get("value", ""),
                "domain": c.get("domain", ".dola.com"),
                "path": c.get("path", "/"),
            }
            for c in cookies
            if c.get("name")
        },
        "storage": storage_bundle or {},
    }
    (profile_dir / "cookie_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def _restore_web_storage(page, bundle: dict) -> dict:
    """在已打开 dola.com 的页面里恢复 localStorage/sessionStorage/indexedDB。"""
    if not bundle:
        return {"local": 0, "session": 0, "indexedDB": 0, "errors": []}
    return await page.evaluate(
        """async (bundle) => {
            const result = { local: 0, session: 0, indexedDB: 0, errors: [] };
            function restoreWebStorage(storage, data) {
                if (!data || typeof data !== "object" || Array.isArray(data)) return 0;
                storage.clear();
                let count = 0;
                for (const [key, value] of Object.entries(data)) {
                    try { storage.setItem(key, String(value)); count += 1; }
                    catch (e) { result.errors.push(`storage:${key}:${e.message || e}`); }
                }
                return count;
            }
            try {
                if (Object.prototype.hasOwnProperty.call(bundle, "localStorage"))
                    result.local = restoreWebStorage(localStorage, bundle.localStorage);
            } catch (e) { result.errors.push(`localStorage:${e.message || e}`); }
            try {
                if (Object.prototype.hasOwnProperty.call(bundle, "sessionStorage"))
                    result.session = restoreWebStorage(sessionStorage, bundle.sessionStorage);
            } catch (e) { result.errors.push(`sessionStorage:${e.message || e}`); }
            try {
                const openDb = (name, version, stores) => new Promise((resolve, reject) => {
                    const req = indexedDB.open(name, version || 1);
                    req.onupgradeneeded = () => {
                        const db = req.result;
                        for (const store of stores || []) {
                            if (!db.objectStoreNames.contains(store.name)) {
                                const options = {};
                                if (store.keyPath) options.keyPath = store.keyPath;
                                if (store.autoIncrement) options.autoIncrement = true;
                                db.createObjectStore(store.name, options);
                            }
                        }
                    };
                    req.onsuccess = () => resolve(req.result);
                    req.onerror = () => reject(req.error);
                });
                const databases = Array.isArray(bundle.indexedDB?.databases) ? bundle.indexedDB.databases : [];
                for (const spec of databases) {
                    if (!spec || !spec.name) continue;
                    await new Promise((resolve) => {
                        const req = indexedDB.deleteDatabase(spec.name);
                        req.onsuccess = resolve; req.onerror = resolve; req.onblocked = resolve;
                    });
                    const db = await openDb(spec.name, spec.version || 1, spec.stores || []);
                    for (const storeSpec of spec.stores || []) {
                        const records = Array.isArray(storeSpec.records) ? storeSpec.records : [];
                        for (const record of records) {
                            await new Promise((resolve, reject) => {
                                const tx = db.transaction(storeSpec.name, "readwrite");
                                const store = tx.objectStore(storeSpec.name);
                                const value = Array.isArray(record)
                                    ? { key: record[0], value: record[1] }
                                    : record;
                                store.put(value.value ?? value, value.key ?? undefined);
                                tx.oncomplete = resolve;
                                tx.onerror = () => reject(tx.error);
                            });
                        }
                        result.indexedDB += records.length;
                    }
                    db.close();
                }
            } catch (e) { result.errors.push(`indexedDB:${e.message || e}`); }
            return result;
        }""",
        bundle,
    )


def _patchright_driver_node() -> Path | None:
    """patchright 自带的 driver/node 路径；拿不到返回 None。"""
    try:
        from patchright._impl._driver import compute_driver_executable

        result = compute_driver_executable()
        path = Path(str(result[0] if isinstance(result, (tuple, list)) else result))
        if path.exists():
            return path
    except Exception:
        pass
    try:
        import patchright

        path = Path(patchright.__file__).resolve().parent / "driver" / "node"
        if path.exists():
            return path
    except Exception:
        pass
    return None


def browser_backend_available() -> tuple[bool, str]:
    """浏览器后端能不能用，返回 (可用, 不可用原因)。

    CentOS 7（glibc 2.17）上 patchright 的 driver/node 需要 glibc>=2.28，
    启动只会抛「Connection closed while reading from the driver」。
    这里先跑一次 --version 探明，探不通就让导入落到纯 API 路径
    （cookie 来源账号出片/验证本来就只走纯 API，不需要浏览器）。
    """
    node = _patchright_driver_node()
    if node is None:
        return False, "patchright 驱动不存在（未安装或包被裁剪）"
    try:
        proc = subprocess.run(
            [str(node), "--version"], capture_output=True, text=True, timeout=30
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"驱动无法执行: {type(exc).__name__}: {exc}"[:200]
    if proc.returncode != 0:
        first = ((proc.stderr or proc.stdout or "").strip().splitlines() or [""])[0]
        return False, f"驱动无法执行: {first.strip()[:180]}"
    return True, ""


async def _apply_browser_profile(name: str, profile_dir: Path, cookies: list[dict],
                                 storage_bundle: dict) -> tuple[bool, dict | None, str]:
    """把 cookie 写进浏览器 persistent profile（仅当浏览器可用时走这条）。"""
    pw_cookies = to_playwright_cookies(cookies)
    use_edge = shutil.which("microsoft-edge-stable") is not None
    if use_edge:
        (profile_dir / EDGE_MARKER).write_text("edge", encoding="utf-8")
    session_cookie_present = False
    storage_result = None
    page_error = ""
    async with async_playwright() as p:
        kwargs = {
            "headless": config.HEADLESS,
            "args": list(LAUNCH_ARGS),
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
        }
        if use_edge:
            kwargs["channel"] = "msedge"
        proxy_kwargs = proxy_kwargs_for(name)
        if proxy_kwargs:
            kwargs["proxy"] = proxy_kwargs
        ctx = await p.chromium.launch_persistent_context(
            str(profile_dir), **kwargs
        )
        try:
            await ctx.add_cookies(pw_cookies)
            # 完整 bundle 需要打开 origin 后恢复 localStorage/sessionStorage/indexedDB。
            # 打开后再补写一次 cookies，避免 dola 页面首次加载覆盖导入的会话 cookie。
            if storage_bundle:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                try:
                    await page.goto(
                        "https://www.dola.com/chat",
                        timeout=60000,
                        wait_until="domcontentloaded",
                    )
                    await page.wait_for_timeout(1500)
                except Exception as exc:
                    page_error = str(exc)[:300]
                await ctx.add_cookies(pw_cookies)
                try:
                    storage_result = await _restore_web_storage(page, storage_bundle)
                except Exception as exc:
                    storage_result = {"errors": [str(exc)[:300]]}
            stored = await ctx.cookies("https://www.dola.com")
            session_cookie_present = any(
                c["name"] == "sessionid" and c.get("value")
                for c in stored
            )
        finally:
            await ctx.close()
    return session_cookie_present, storage_result, page_error


async def import_cookie_account(name: str, cookie_data,
                                require_login: bool = True,
                                browser: bool | None = None) -> dict:
    """把 cookie 导入 accounts/<name>/ 并验证登录态。

    两条路：
    - 浏览器可用（browser=True 或自动探测可用）：写 persistent profile + add_cookies，
      有 web storage bundle 时一并恢复，与旧行为一致；
    - 浏览器不可用（例如 CentOS7 上 patchright 驱动 node 缺 glibc>=2.28）：只落盘
      cookie_state.json，登录态改走纯 API 探活。cookie 来源账号的出片本来就只走
      纯 API，所以少了 profile 不影响出片，也不会再抛
      「写入 cookie profile 失败: Connection closed while reading from the driver」。

    验证失败不再删除 profile（非破坏，对齐 dola-pool-cookie）：账号入池并标记「未验证」，
    配好代理后可到面板点“验证”复核。
    require_login=False 时只写入不验证。
    """
    name = (name or "").strip()
    if not name or any(ch in name for ch in "/\\"):
        raise ValueError("账号名不合法")
    profile_dir = Path("accounts") / name
    if profile_dir.exists():
        raise FileExistsError(f"账号已存在: {name}")
    cookies = normalize_cookies(cookie_data)
    if not cookies:
        raise ValueError("cookie 列表为空")
    storage_bundle = normalize_storage_bundle(cookie_data)

    profile_dir.mkdir(parents=True, exist_ok=False)
    _write_cookie_state(profile_dir, cookies, storage_bundle)
    state_file = profile_dir / "cookie_state.json"

    ok, reason = browser_backend_available()
    use_browser = ok if browser is None else bool(browser)
    engine = "browser" if use_browser else "pure"
    browser_error = "" if use_browser else reason
    session_cookie_present = any(
        str(c.get("name")) == "sessionid" and c.get("value") for c in cookies
    )
    storage_result = None
    page_error = ""

    if use_browser:
        try:
            session_cookie_present, storage_result, page_error = \
                await _apply_browser_profile(name, profile_dir, cookies, storage_bundle)
        except Exception as exc:
            # 浏览器路径挂了（驱动/内核/代理）不该让整批导入失败：
            # cookie_state.json 已落盘，账号照样能走纯 API。
            browser_error = f"{type(exc).__name__}: {exc}"[:200]
            engine = "pure"
            page_error = browser_error
            for item in list(profile_dir.iterdir()):
                if item.name == "cookie_state.json":
                    continue
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    try:
                        item.unlink()
                    except OSError:
                        pass

    verify_error = ""
    verified = False
    if require_login:
        if engine == "browser" and not config.PURE_API_ENABLED:
            # 只有明确关掉纯 API 时才需要浏览器验证
            try:
                verified = await check_login_state(name)
            except Exception as exc:
                page_error = page_error or str(exc)[:300]
            if not verified:
                verify_error = page_error or "dola.com 未返回已登录状态"
        else:
            # cookie 账号走纯 API 探活：不开浏览器，也不会占 profile
            from pure_api_gen import probe_login

            valid, detail = await asyncio.to_thread(probe_login, state_file)
            verified = bool(valid)
            if not verified:
                verify_error = detail or "纯 API 探活未通过"

    return {
        "ok": True,
        "account": name,
        "cookie_count": len(cookies),
        "cookie_state_file": str(state_file),
        "session_cookie_present": session_cookie_present,
        "storage_result": storage_result,
        "profile": str(profile_dir),
        "engine": engine,
        "browser_error": browser_error,
        "verified": verified,
        "verify_error": verify_error,
    }


async def import_cookie_accounts_from_text(pool, raw_text: str,
                                           name_prefix: str = "acc",
                                           require_login: bool = True) -> dict:
    """Cookie 头文本 / Cookie JSON 批量导入（对齐 api-pool 的 import-cookies）。

    - 按 sessionid 去重：已存在则记入 updated（不重复建号）；
    - 新 sessionid 自动命名 acc<N>（跳过已占用名）；
    - 每个账号写浏览器 profile + add_cookies，require_login=True 时做真实验证；
      require_login=False 时只写 cookie 不验证（供无 JP/KR 代理时先导入、之后面板验证）。
    """
    from cookie_import import parse_cookie_header_text, sessionid_from_state

    states = parse_cookie_header_text(raw_text)
    existing = set(pool.accounts)
    created: list[str] = []
    updated: list[str] = []
    errors: list[str] = []
    unverified: list[str] = []
    counter = 1
    for state in states:
        sid = sessionid_from_state(state)
        if sid:
            dup = pool.find_by_sessionid(sid)
            if dup:
                updated.append(dup[0])
                continue
        name = f"{name_prefix}{counter}"
        counter += 1
        while name in existing:
            name = f"{name_prefix}{counter}"
            counter += 1
        existing.add(name)
        bundle = {"cookies": state["cookies_list"]}
        try:
            result = await import_cookie_account(name, bundle, require_login=require_login)
            pool.ensure_account(name)
            pool.set_sessionid(name, sid)
            pool.set_source(name, "cookie")
            verified = bool(result.get("verified"))
            pool.set_login_status(name, verified)
            if not verified:
                unverified.append(name)
            pool.set_email(name, "")
            created.append(name)
        except Exception as exc:  # 仅格式/写入失败会报错；登录态验证失败不再清 profile（非破坏）
            errors.append(f"{name}: {str(exc)[:120]}")
    return {"created": created, "updated": updated, "errors": errors,
            "unverified": unverified}
