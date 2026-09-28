"""代理池存储：代理记录 + 账号-代理映射，存 pool_usage.db。

支持两种出口类型（proxies.mode）：

- ``static``  静态IP：一条记录 = 一个固定出口，host:port 决定出口。
- ``dynamic`` 动态IP：隧道/旋转网关。host:port 固定，出口 IP 由 username 里的
  session 选择器决定（BrightData / Oxylabs / 快代理 / 芝麻等隧道代理都是这个形态）。
  每个号自动分配独立 sticky session，也就是独立出口 IP；session 到期或被上游风控
  时可以随时换一个 —— 换 session 即换 IP，不用换账号。

出口身份（egress key）：
- static  → ``scheme://host:port``（与历史行为一致）
- dynamic → ``scheme://host:port#<session>``，各号互不干扰，
  所以共用同一个旋转网关也不会被同出口隔离误伤。

浏览器每次启动/验证账号前通过 browser.py 查账号对应的代理；
未给账号指定代理时回落到 config.DOLA_PROXY（环境默认代理）。
"""
import os
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import config

DB_PATH = Path(config.POOL_DB_PATH if hasattr(config, "POOL_DB_PATH") else "pool_usage.db")

PROXY_MODES = ("static", "dynamic", "extract")
# username 的拼装模板；{user} = 记录里的基础用户名，{session} = 本次 sticky session
DEFAULT_SESSION_TEMPLATE = "{user}-{session}"


def _default_node_ttl() -> int:
    """提取型节点没有 sessiontime 时的默认保质期（秒）。"""
    return int(os.getenv("DOLA_EXTRACT_NODE_TTL", "300"))


def _extract_timeout() -> int:
    return int(os.getenv("DOLA_EXTRACT_TIMEOUT", "30"))


def _default_rotate_min_interval() -> int:
    return int(os.getenv("DOLA_DYNAMIC_ROTATE_MIN_INTERVAL", "60"))


def _default_sticky_ttl() -> int:
    """session 保质期（秒）；0 = 不按时间过期，仅在风控时轮换。"""
    return int(os.getenv("DOLA_DYNAMIC_STICKY_TTL", "0"))


# 已建表/迁移过的库路径。热路径不能碰 DDL：
# CREATE TABLE / ALTER TABLE 都要写锁，而这个库被服务进程里多条长连接共用，
# 一旦取连接时做 DDL 撞上别处的锁就会抛 "database is locked"；
# 异常里残留的 traceback 又会让那条连接（连同一个未释放的写事务）不被回收，
# 于是锁不释放、后续每一次取连接都失败，级联成整片 500。
_ready_paths: set[str] = set()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        if str(DB_PATH) not in _ready_paths:
            _init_schema(conn)
            _ready_paths.add(str(DB_PATH))
    except Exception:
        conn.close()
        raise
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """建表 + 迁移，每个库路径在一个进程里只做一次。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proxies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            protocol TEXT NOT NULL DEFAULT 'http',
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            username TEXT DEFAULT '',
            password TEXT DEFAULT '',
            remark TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_proxy (
            account TEXT PRIMARY KEY,
            proxy_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_sessions (
            account TEXT PRIMARY KEY,
            proxy_id TEXT,
            session TEXT NOT NULL,
            rotated_at REAL NOT NULL,
            last_exit_ip TEXT DEFAULT '',
            last_probe_at REAL DEFAULT 0,
            last_probe_ok INTEGER DEFAULT 0,
            last_probe_detail TEXT DEFAULT ''
        )
        """
    )
    # 提取型（mode=extract）拿到的节点池：一条记录 = 一个可直接使用的 host:port:user:pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy_id TEXT NOT NULL,
            protocol TEXT NOT NULL DEFAULT 'http',
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            username TEXT DEFAULT '',
            password TEXT DEFAULT '',
            exit_hint TEXT DEFAULT '',
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL DEFAULT 0,
            dead INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0,
            last_ok_at REAL NOT NULL DEFAULT 0,
            last_exit_ip TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proxy_nodes_pid ON proxy_nodes(proxy_id, dead)"
    )
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """给已有库补上动态代理需要的列（幂等）。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(proxies)")}
    wanted = {
        "mode": "TEXT NOT NULL DEFAULT 'static'",
        "session_template": "TEXT DEFAULT ''",
        "sticky_ttl": "INTEGER NOT NULL DEFAULT 0",
        "rotate_on_risk": "INTEGER NOT NULL DEFAULT 1",
        "rotate_min_interval": "INTEGER NOT NULL DEFAULT 0",
        "extract_url": "TEXT DEFAULT ''",
        "extract_interval": "INTEGER NOT NULL DEFAULT 0",
        "extract_last_at": "REAL NOT NULL DEFAULT 0",
        "extract_last_count": "INTEGER NOT NULL DEFAULT 0",
        "extract_last_error": "TEXT DEFAULT ''",
    }
    for name, ddl in wanted.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE proxies ADD COLUMN {name} {ddl}")
    scols = {row["name"] for row in conn.execute("PRAGMA table_info(proxy_sessions)")}
    if "node_id" not in scols:
        conn.execute("ALTER TABLE proxy_sessions ADD COLUMN node_id INTEGER DEFAULT 0")


def _safe_dict(row: sqlite3.Row, with_password: bool = False) -> dict:
    data = dict(row)
    data.setdefault("mode", "static")
    data["is_dynamic"] = data.get("mode") == "dynamic"
    if not with_password:
        data["has_password"] = bool(data.get("password"))
        data.pop("password", None)
    return data


def list_proxies() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM proxies ORDER BY enabled DESC, created_at DESC"
        ).fetchall()
        out = []
        for row in rows:
            item = _safe_dict(row)
            used = conn.execute(
                "SELECT COUNT(*) AS c FROM account_proxy WHERE proxy_id=?",
                (row["id"],),
            ).fetchone()["c"]
            item["account_count"] = used
            out.append(item)
        return out
    finally:
        conn.close()


def get_proxy(proxy_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM proxies WHERE id=?", (proxy_id,)
        ).fetchone()
        return _safe_dict(row, with_password=True) if row else None
    finally:
        conn.close()


def create_proxy(name: str, protocol: str, host: str, port: int,
                 username: str = "", password: str = "",
                 remark: str = "", mode: str = "static",
                 session_template: str = "", sticky_ttl: int | None = None,
                 rotate_on_risk: bool = True,
                 rotate_min_interval: int | None = None,
                 extract_url: str = "",
                 extract_interval: int = 0,
                 nodes_text: str = "") -> dict:
    name = (name or "").strip()
    protocol = (protocol or "http").strip().lower()
    host = (host or "").strip()
    mode = (mode or "static").strip().lower()
    nodes_text = (nodes_text or "").strip()
    if not name:
        raise ValueError("代理名称不能为空")
    if protocol not in ("http", "https", "socks4", "socks5"):
        raise ValueError("协议仅支持 http/https/socks4/socks5")
    if mode not in PROXY_MODES:
        raise ValueError("mode 仅支持 static/dynamic/extract")
    if mode == "extract":
        # 动态IP：出口由节点池提供 —— 要么有提取链接，要么直接粘贴节点行。
        if not (extract_url or "").strip() and not nodes_text:
            raise ValueError(
                "动态IP 代理要么填提取链接，要么粘贴节点列表（host:port:user:pass 一行一个）")
        if nodes_text and not parse_extract_lines(nodes_text):
            raise ValueError(
                "粘贴的节点没解析出合法行，格式应为 host:port:user:pass（一行一个）")
        if not host or not int(port or 0):
            # 没填网关地址时，用粘贴的第一行作为记录的 host/port
            first = parse_extract_lines(nodes_text)[:1]
            if first:
                host = first[0]["host"]
                port = int(first[0]["port"])
    if mode != "extract" and (not host or not int(port or 0)):
        # extract 型不需要网关地址：出口由节点池里的节点决定
        raise ValueError("代理名称/地址/端口不能为空")
    ttl = _default_sticky_ttl() if sticky_ttl is None else int(sticky_ttl)
    min_interval = (_default_rotate_min_interval() if rotate_min_interval is None
                    else int(rotate_min_interval))
    proxy_id = "px_" + uuid.uuid4().hex[:12]
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO proxies
                (id, name, protocol, host, port, username, password, remark, enabled,
                 created_at, mode, session_template, sticky_ttl, rotate_on_risk,
                 rotate_min_interval, extract_url, extract_interval)
                VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)""",
            (proxy_id, name, protocol, host, int(port or 0),
              username or "", password or "", remark or "", time.time(),
              mode, (session_template or "").strip(), ttl,
              1 if rotate_on_risk else 0, min_interval,
              (extract_url or "").strip(), int(extract_interval or 0)),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.close()
        raise ValueError(f"代理名称已存在: {name}") from exc
    if nodes_text:
        add_nodes_from_text(proxy_id, nodes_text)
    row = None
    try:
        row = conn.execute(
            "SELECT * FROM proxies WHERE id=?", (proxy_id,)
        ).fetchone()
    finally:
        conn.close()
    return _safe_dict(row, with_password=False) if row else {}


def update_proxy(proxy_id: str, *, name=None, protocol=None, host=None,
                 port=None, username=None, password=None, remark=None,
                 enabled=None, mode=None, session_template=None,
                 sticky_ttl=None, rotate_on_risk=None,
                 rotate_min_interval=None, extract_url=None,
                 extract_interval=None) -> dict:
    fields = []
    values = []
    if name is not None:
        fields.append("name=?")
        values.append((name or "").strip())
    if protocol is not None:
        fields.append("protocol=?")
        values.append((protocol or "http").strip().lower())
    if host is not None:
        fields.append("host=?")
        values.append((host or "").strip())
    if port is not None:
        fields.append("port=?")
        values.append(int(port))
    if username is not None:
        fields.append("username=?")
        values.append(username or "")
    if password is not None:
        fields.append("password=?")
        values.append(password or "")
    if remark is not None:
        fields.append("remark=?")
        values.append(remark or "")
    if enabled is not None:
        fields.append("enabled=?")
        values.append(1 if enabled else 0)
    if mode is not None:
        mode = (mode or "static").strip().lower()
        if mode not in PROXY_MODES:
            raise ValueError("mode 仅支持 static/dynamic/extract")
        fields.append("mode=?")
        values.append(mode)
    if extract_url is not None:
        fields.append("extract_url=?")
        values.append((extract_url or "").strip())
    if extract_interval is not None:
        fields.append("extract_interval=?")
        values.append(int(extract_interval))
    if session_template is not None:
        fields.append("session_template=?")
        values.append((session_template or "").strip())
    if sticky_ttl is not None:
        fields.append("sticky_ttl=?")
        values.append(int(sticky_ttl))
    if rotate_on_risk is not None:
        fields.append("rotate_on_risk=?")
        values.append(1 if rotate_on_risk else 0)
    if rotate_min_interval is not None:
        fields.append("rotate_min_interval=?")
        values.append(int(rotate_min_interval))
    if not fields:
        return get_proxy(proxy_id) or {}
    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE proxies SET {', '.join(fields)} WHERE id=?",
            (*values, proxy_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            raise ValueError("代理不存在")
    except ValueError:
        conn.close()
        raise
    row = None
    try:
        row = conn.execute(
            "SELECT * FROM proxies WHERE id=?", (proxy_id,)
        ).fetchone()
    finally:
        conn.close()
    return _safe_dict(row, with_password=False) if row else {}


def delete_proxy(proxy_id: str):
    conn = _connect()
    try:
        conn.execute("DELETE FROM account_proxy WHERE proxy_id=?", (proxy_id,))
        conn.execute("DELETE FROM proxy_sessions WHERE proxy_id=?", (proxy_id,))
        conn.execute("DELETE FROM proxies WHERE id=?", (proxy_id,))
        conn.commit()
    finally:
        conn.close()


def get_account_proxy(account: str) -> dict | None:
    """返回账号绑定的代理（含密码）；未绑定返回 None。"""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT p.* FROM account_proxy ap
               JOIN proxies p ON p.id = ap.proxy_id
               WHERE ap.account=? AND p.enabled=1""",
            (account,),
        ).fetchone()
        return _safe_dict(row, with_password=True) if row else None
    finally:
        conn.close()


def forget_account(account: str) -> None:
    """账号删除时清掉它的代理绑定与出口会话，避免同名新号继承旧状态。"""
    conn = _connect()
    try:
        conn.execute("DELETE FROM account_proxy WHERE account=?", (account,))
        conn.execute("DELETE FROM proxy_sessions WHERE account=?", (account,))
        conn.commit()
    finally:
        conn.close()


def rename_account(old: str, new: str) -> None:
    """账号改名时搬走以账号名为主键的代理记录（绑定 + 出口会话）。"""
    conn = _connect()
    try:
        conn.execute("UPDATE account_proxy SET account=? WHERE account=?", (new, old))
        conn.execute("UPDATE proxy_sessions SET account=? WHERE account=?", (new, old))
        conn.commit()
    finally:
        conn.close()


def set_account_proxy(account: str, proxy_id: str | None):
    conn = _connect()
    try:
        if proxy_id:
            row = conn.execute(
                "SELECT id FROM proxies WHERE id=?", (proxy_id,)
            ).fetchone()
            if not row:
                raise ValueError("代理不存在")
            conn.execute(
                """INSERT INTO account_proxy(account, proxy_id) VALUES (?,?)
                   ON CONFLICT(account) DO UPDATE SET proxy_id=excluded.proxy_id""",
                (account, proxy_id),
            )
            # 换绑代理后旧 session 作废，下次取用时按新代理重新分配
            conn.execute(
                "DELETE FROM proxy_sessions WHERE account=? AND proxy_id<>?",
                (account, proxy_id),
            )
        else:
            conn.execute(
                "DELETE FROM account_proxy WHERE account=?", (account,)
            )
            conn.execute(
                "DELETE FROM proxy_sessions WHERE account=?", (account,)
            )
        conn.commit()
    finally:
        conn.close()


# ===== 动态代理：session（=出口 IP）管理 =====


def _new_session_id() -> str:
    return secrets.token_hex(4)


def _session_row(conn: sqlite3.Connection, account: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM proxy_sessions WHERE account=?", (account,)
    ).fetchone()


def get_session(account: str) -> dict | None:
    conn = _connect()
    try:
        row = _session_row(conn, account)
        return dict(row) if row else None
    finally:
        conn.close()


def rotate_session(account: str, proxy_id: str | None = None) -> str:
    """给账号换一个 sticky session（=换一个出口 IP），返回新的 session id。"""
    session = _new_session_id()
    conn = _connect()
    try:
        if not proxy_id:
            row = conn.execute(
                "SELECT proxy_id FROM account_proxy WHERE account=?", (account,)
            ).fetchone()
            proxy_id = row["proxy_id"] if row else ""
        conn.execute(
            """INSERT INTO proxy_sessions
               (account, proxy_id, session, rotated_at, last_exit_ip,
                last_probe_at, last_probe_ok, last_probe_detail)
               VALUES (?,?,?,?,'',0,0,'')
               ON CONFLICT(account) DO UPDATE SET
                 proxy_id=excluded.proxy_id,
                 session=excluded.session,
                 rotated_at=excluded.rotated_at,
                 last_exit_ip='',
                 last_probe_at=0,
                 last_probe_ok=0,
                 last_probe_detail=''""",
            (account, proxy_id or "", session, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return session


def _session_expired(row: sqlite3.Row, sticky_ttl: int) -> bool:
    if not sticky_ttl or sticky_ttl <= 0:
        return False
    return time.time() - float(row["rotated_at"] or 0) > sticky_ttl


def _ensure_session(account: str, info: dict, rotate: bool = False) -> str:
    """拿到该号当前有效的 session；缺失/过期/显式轮换时换一个。"""
    conn = _connect()
    try:
        row = _session_row(conn, account)
    finally:
        conn.close()
    ttl = int(info.get("sticky_ttl") or 0)
    if row and row["proxy_id"] == info["id"]:
        if rotate or _session_expired(row, ttl):
            return rotate_session(account, info["id"])
        return row["session"]
    return rotate_session(account, info["id"])


def render_session_username(template: str, base_user: str, session: str) -> str:
    """把 session 拼进用户名。模板里可用 {user} 和 {session}。"""
    tpl = (template or DEFAULT_SESSION_TEMPLATE).strip()
    try:
        out = tpl.format(user=base_user or "", session=session)
    except (KeyError, IndexError, ValueError):
        out = f"{base_user}-{session}" if base_user else session
    # {user} 为空时模板会留下前导/尾随分隔符
    return out.strip("-_. ")


def _url_from_record(info: dict, username: str | None = None) -> str:
    user = info.get("username") if username is None else username
    auth = ""
    if user:
        auth = quote(user, safe="")
        if info.get("password"):
            auth += ":" + quote(info["password"], safe="")
        auth += "@"
    return f"{info['protocol']}://{auth}{info['host']}:{info['port']}"


def _egress_of_url(proxy_url: str) -> str:
    """归一化出口为 scheme://host:port（去掉凭据）；空/直连 => 'direct'。"""
    proxy_url = (proxy_url or "").strip()
    if not proxy_url or proxy_url == "direct":
        return "direct"
    if "://" not in proxy_url:
        proxy_url = "http://" + proxy_url
    parts = urlsplit(proxy_url)
    if not parts.hostname:
        return "direct"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{parts.hostname}:{port}"


def proxy_url_for(account: str, *, rotate: bool = False) -> str:
    """账号实际使用的代理 URL（动态代理会带上该号自己的 session）。

    这是全项目构造代理 URL 的唯一入口 —— browser.py / browser_pool.py /
    aiohttp 下载路径都走这里，避免各处重复拼 username。
    """
    info = get_account_proxy(account)
    if not info:
        return config.PROXY or ""
    mode = info.get("mode") or "static"
    if mode == "extract":
        node = _ensure_node(account, info, rotate=rotate)
        return _url_from_node(node) if node else (config.PROXY or "")
    if mode != "dynamic":
        return _url_from_record(info)
    session = _ensure_session(account, info, rotate=rotate)
    username = render_session_username(
        info.get("session_template") or "", info.get("username") or "", session
    )
    return _url_from_record(info, username)


def _upgrade_socks_dns(url: str) -> str:
    """socks5:// -> socks5h://（其余 scheme 原样返回）。"""
    if url.startswith("socks5://"):
        return "socks5h://" + url[len("socks5://"):]
    return url


def requests_proxy_url(account: str, *, rotate: bool = False) -> str:
    """给 requests / aiohttp 用的代理 URL：socks5 升级为 socks5h（域名交代理远端解析）。

    socks5:// 会让 requests 先在本机解析域名、再把解析出的 IP 交给代理。dola 的 CDN 与
    对象存储走 Akamai，会同时返回 IPv4 和 IPv6，本机挑中的地址代理侧经常连不上，表现为
    「参考图上传失败 ... SOCKSHTTPSConnectionPool(host='tos-...vodupload.com', port=443)
    ... NewConnectionError」。同一个代理换成 socks5h:// 立刻可通（实测 204）。

    注意：只给 requests 这类路径用。Playwright / Chromium 不认 socks5h 这个 scheme，
    浏览器路径必须继续用 proxy_url_for() 返回的 socks5://。
    """
    return _upgrade_socks_dns(proxy_url_for(account, rotate=rotate))


def egress_key_for(account: str) -> str:
    """账号的出口身份。动态代理按 session 区分，静态代理按 host:port。"""
    info = get_account_proxy(account)
    if not info:
        return _egress_of_url(config.PROXY or "")
    mode = info.get("mode") or "static"
    if mode == "extract":
        node = _ensure_node(account, info)
        if not node:
            return _egress_of_url(config.PROXY or "")
        # 每个节点是独立出口，按节点 id 区分
        return f"{_egress_of_url(_url_from_node(node))}#node{node['id']}"
    base = _egress_of_url(_url_from_record(info, username=""))
    if mode != "dynamic":
        return base
    session = _ensure_session(account, info)
    return f"{base}#{session}"


def rotate_account_session(account: str, *, reason: str = "",
                           min_interval: int | None = None,
                           force: bool = False) -> str | None:
    """风控/异常时给动态代理账号换出口 IP。

    min_interval 用来防止**自动**轮换短时间反复换（把 IP 刷光）：
    距上次轮换不足该秒数则跳过。
    force=True 用于人工操作（面板点「换IP」）—— 操作员是明确意图，不该被间隔挡住。
    返回新 session；跳过或该号不是动态代理时返回 None。
    """
    info = get_account_proxy(account)
    if not info:
        return None
    mode = info.get("mode") or "static"
    if mode == "extract":
        if not info.get("rotate_on_risk", 1):
            return None
        return _rotate_extract_node(account, info, min_interval, force=force)
    if mode != "dynamic":
        return None
    if not info.get("rotate_on_risk", 1):
        return None
    interval = (info.get("rotate_min_interval") if min_interval is None
                else min_interval)
    try:
        interval = int(interval or 0)
    except (TypeError, ValueError):
        interval = 0
    if not force:
        cur = get_session(account)
        if cur and interval > 0 and (time.time() - float(cur["rotated_at"] or 0)) < interval:
            return None
    return rotate_session(account, info["id"])


def _rotate_extract_node(account: str, info: dict,
                         min_interval: int | None,
                         force: bool = False) -> str | None:
    """提取型：换到池子里另一个节点（=换一个出口 IP）。"""
    interval = info.get("rotate_min_interval") if min_interval is None else min_interval
    try:
        interval = int(interval or 0)
    except (TypeError, ValueError):
        interval = 0
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT node_id, rotated_at FROM proxy_sessions WHERE account=?",
            (account,),
        ).fetchone()
    finally:
        conn.close()
    if (not force and row and interval > 0
            and (time.time() - float(row["rotated_at"] or 0)) < interval):
        return None
    old_id = row["node_id"] if row else 0
    node = _pick_node(info["id"], account, exclude_node=old_id)
    if node is None and _should_extract(info):
        fetch_extract_nodes(info["id"], info)
        node = _pick_node(info["id"], account, exclude_node=old_id)
    if node is None:
        return None
    _bind_node(account, info["id"], node)
    return node.get("exit_hint") or node.get("username") or f"node{node['id']}"


def account_proxy_url(account: str) -> str | None:
    """给 aiohttp/下载用：返回可直接使用的代理 URL（带账号密码）。"""
    url = proxy_url_for(account)
    return url or None


def record_probe_result(account: str, ip: str, ok: bool, detail: str = "") -> None:
    conn = _connect()
    try:
        conn.execute(
            """UPDATE proxy_sessions
               SET last_exit_ip=?, last_probe_at=?, last_probe_ok=?,
                   last_probe_detail=?
               WHERE account=?""",
            (ip or "", time.time(), 1 if ok else 0, (detail or "")[:300], account),
        )
        conn.commit()
    finally:
        conn.close()


def sessions_snapshot() -> dict[str, dict]:
    """account -> session 记录，供面板展示当前出口 IP。"""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM proxy_sessions").fetchall()
        return {row["account"]: dict(row) for row in rows}
    finally:
        conn.close()


def probe_exit_ip(proxy_url: str, timeout: int = 15) -> dict:
    """通过代理查真实出口 IP 与归属地（用于验证动态代理是否真的在换 IP）。"""
    if not proxy_url or proxy_url == "direct":
        return {"ok": False, "ip": "", "error": "未配置代理（直连）"}
    import requests

    kwargs = {"timeout": timeout}
    if proxy_url.startswith("socks"):
        # requests 需要 PySocks 才支持 socks；缺失时明确报错而不是静默失败
        try:
            import socks  # noqa: F401
        except ImportError:
            return {"ok": False, "ip": "",
                    "error": "socks 代理需要 PySocks（pip install PySocks）"}
    kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
    try:
        resp = requests.get(
            "http://ip-api.com/json/?fields=status,country,countryCode,regionName,isp,query",
            **kwargs,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return {"ok": False, "ip": "", "error": f"探测失败: {data}"}
        return {
            "ok": True,
            "ip": data.get("query", ""),
            "country": data.get("country", ""),
            "country_code": data.get("countryCode", ""),
            "region": data.get("regionName", ""),
            "isp": data.get("isp", ""),
        }
    except Exception as exc:  # noqa: BLE001 - 探测失败原因要原样回报给面板
        return {"ok": False, "ip": "", "error": str(exc)[:200]}


def probe_proxy_record(proxy_id: str, session: str | None = None) -> dict:
    """探测一条代理记录的出口 IP。

    动态代理用一个临时 session 探测（等于"这个网关现在能出什么 IP"），
    不影响任何账号已经占用的 sticky session。
    """
    info = get_proxy(proxy_id)
    if not info:
        return {"ok": False, "ip": "", "error": "代理不存在"}
    if info.get("mode") == "extract":
        nodes = list_nodes(proxy_id, alive_only=True)
        if not nodes:
            fetched = fetch_extract_nodes(proxy_id, info)
            if not fetched.get("ok"):
                return {"ok": False, "ip": "", "error": fetched.get("error", "节点池为空")}
            nodes = list_nodes(proxy_id, alive_only=True)
        if not nodes:
            return {"ok": False, "ip": "", "error": "节点池为空"}
        result = probe_exit_ip(_url_from_node(nodes[0]))
        result["node_id"] = nodes[0]["id"]
        if result.get("ok"):
            mark_node_ok(nodes[0]["id"], result.get("ip", ""))
        return result
    if info.get("mode") == "dynamic":
        sess = session or _new_session_id()
        user = render_session_username(
            info.get("session_template") or "", info.get("username") or "", sess
        )
        result = probe_exit_ip(_url_from_record(info, user))
        result["session"] = sess
        return result
    return probe_exit_ip(_url_from_record(info))


def probe_account_exit_ip(account: str) -> dict:
    """探测某个号当前真实出口 IP，并记录到 proxy_sessions。"""
    url = proxy_url_for(account)
    result = probe_exit_ip(url)
    record_probe_result(
        account,
        result.get("ip", ""),
        bool(result.get("ok")),
        result.get("error", "") or result.get("isp", ""),
    )
    return result


# ===== 提取型（mode=extract）：调提取接口拿一批可用节点 =====

# 提取接口返回的常见形态：
#   host:port:user:pass      <- ipdeep 等（user 里已编码 session/country）
#   host:port@user:pass
#   user:pass@host:port
#   host:port                <- 无鉴权
_SESSION_TTL_RE = re.compile(r"sessiontime[_-](\d+)", re.I)


def parse_extract_lines(text: str) -> list[dict]:
    """把提取接口的返回文本解析成节点列表。"""
    nodes: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        # 有些接口用逗号/分号分隔多条
        for chunk in re.split(r"[;,\s]+", line):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            node = _parse_node_token(chunk)
            if node:
                nodes.append(node)
    return nodes


def _parse_node_token(token: str) -> dict | None:
    user = password = ""
    hostpart = token
    if "@" in token:
        left, right = token.split("@", 1)
        # user:pass@host:port  还是  host:port@user:pass
        if left.count(":") >= 1 and right.count(":") == 1 and _is_host_port(right):
            user, password = left.split(":", 1)
            hostpart = right
        else:
            hostpart, cred = left, right
            if ":" in cred:
                user, password = cred.split(":", 1)
            else:
                user = cred
    parts = hostpart.split(":")
    if len(parts) < 2:
        return None
    host, port = parts[0].strip(), parts[1].strip()
    if not host or not port.isdigit():
        return None
    rest = parts[2:]
    if not user and rest:
        user = rest[0].strip()
        if len(rest) > 1:
            password = rest[1].strip()
    if not host:
        return None
    ttl = _default_node_ttl()
    m = _SESSION_TTL_RE.search(user)
    if m:
        ttl = int(m.group(1)) * 60
    return {
        "host": host,
        "port": int(port),
        "username": user,
        "password": password,
        "exit_hint": user,
        "ttl": ttl,
    }


def _is_host_port(text: str) -> bool:
    parts = text.split(":")
    return len(parts) == 2 and parts[1].strip().isdigit()


def fetch_extract_nodes(proxy_id: str, info: dict | None = None) -> dict:
    """调用提取接口，把新节点写进 proxy_nodes（旧节点保留，过期自然淘汰）。

    注意：GenerateLink 类接口通常**按调用次数计费/配额**，所以只在
    需要补节点、且距上次提取超过 extract_interval 时才调用。
    """
    import requests

    info = info or get_proxy(proxy_id)
    if not info:
        return {"ok": False, "added": 0, "error": "代理不存在"}
    url = (info.get("extract_url") or "").strip()
    if not url:
        return {"ok": False, "added": 0, "error": "该代理未配置提取链接"}
    try:
        resp = requests.get(url, timeout=_extract_timeout())
        resp.raise_for_status()
        text = resp.text
        nodes = parse_extract_lines(text)
        if not nodes:
            _record_extract_result(proxy_id, 0, f"提取结果为空: {text[:120]}")
            return {"ok": False, "added": 0, "error": f"没解析出节点: {text[:120]}"}
        added = _insert_nodes(proxy_id, nodes, info.get("protocol") or "http")
        _record_extract_result(proxy_id, added, "")
        return {"ok": True, "added": added, "total_lines": len(nodes)}
    except Exception as exc:  # noqa: BLE001 - 失败原因要回报给面板
        _record_extract_result(proxy_id, 0, str(exc)[:200])
        return {"ok": False, "added": 0, "error": str(exc)[:200]}


def _record_extract_result(proxy_id: str, count: int, error: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE proxies SET extract_last_at=?, extract_last_count=?, "
            "extract_last_error=? WHERE id=?",
            (time.time(), count, (error or "")[:200], proxy_id),
        )
        conn.commit()
    finally:
        conn.close()


def add_nodes_from_text(proxy_id: str, text: str, ttl: int | None = None) -> dict:
    """手工粘贴节点：一行一个 host:port:user:pass（也认 user:pass@host:port 等形态）。

    ttl=None 时：行里带 sessiontime-N 的按 N 分钟算有效期；
    其余（手工粘贴的固定凭据）默认**不过期**，由操作员自己清理/替换。
    """
    info = get_proxy(proxy_id)
    if not info:
        return {"ok": False, "added": 0, "parsed": 0, "error": "代理不存在"}
    nodes = parse_extract_lines(text)
    if not nodes:
        return {"ok": False, "added": 0, "parsed": 0,
                "error": "没解析出节点，格式应为 host:port:user:pass（一行一个）"}
    for n in nodes:
        if ttl is not None:
            n["ttl"] = int(ttl)
        elif not _SESSION_TTL_RE.search(n.get("username") or ""):
            n["ttl"] = 0
    added = _insert_nodes(proxy_id, nodes, info.get("protocol") or "http")
    return {"ok": True, "added": added, "parsed": len(nodes)}


def _expiry_of(now: float, ttl) -> float:
    """ttl=None 用默认有效期；ttl<=0 表示不过期（0 在 _pick_node 里等价于永不过期）。"""
    ttl = _default_node_ttl() if ttl is None else int(ttl)
    return 0 if ttl <= 0 else now + ttl


def _insert_nodes(proxy_id: str, nodes: list[dict], protocol: str) -> int:
    """写入节点，按 (proxy_id, host, port, username) 去重。"""
    conn = _connect()
    now = time.time()
    added = 0
    try:
        for n in nodes:
            exists = conn.execute(
                "SELECT id FROM proxy_nodes WHERE proxy_id=? AND host=? AND port=? "
                "AND username=?",
                (proxy_id, n["host"], n["port"], n["username"]),
            ).fetchone()
            if exists:
                # 厂商会把同一批凭据再发一次。过期的节点要顺带续期，
                # 否则它会一直躺在表里且永远挑不中（dead 标记保持不动）。
                conn.execute(
                    "UPDATE proxy_nodes SET expires_at=? WHERE id=? AND dead=0",
                    (_expiry_of(time.time(), n.get("ttl")), exists["id"]),
                )
                continue
            conn.execute(
                """INSERT INTO proxy_nodes
                   (proxy_id, protocol, host, port, username, password, exit_hint,
                    created_at, expires_at, dead, fail_count)
                   VALUES (?,?,?,?,?,?,?,?,?,0,0)""",
                (proxy_id, protocol, n["host"], n["port"], n["username"],
                 n["password"], n.get("exit_hint", ""), now,
                 _expiry_of(now, n.get("ttl"))),
            )
            added += 1
        conn.commit()
    finally:
        conn.close()
    return added


def _node_row_to_dict(row) -> dict:
    return dict(row) if row else {}


def list_nodes(proxy_id: str | None = None, alive_only: bool = False) -> list[dict]:
    conn = _connect()
    try:
        sql = "SELECT * FROM proxy_nodes"
        args: list = []
        where = []
        if proxy_id:
            where.append("proxy_id=?")
            args.append(proxy_id)
        if alive_only:
            where.append("dead=0 AND (expires_at=0 OR expires_at>?)")
            args.append(time.time())
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY last_ok_at DESC, created_at DESC"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _url_from_node(node: dict) -> str:
    auth = ""
    if node.get("username"):
        auth = quote(node["username"], safe="")
        if node.get("password"):
            auth += ":" + quote(node["password"], safe="")
        auth += "@"
    return f"{node.get('protocol') or 'http'}://{auth}{node['host']}:{node['port']}"


def _claimed_node_ids(conn: sqlite3.Connection, except_account: str) -> set:
    rows = conn.execute(
        "SELECT node_id FROM proxy_sessions WHERE node_id>0 AND account<>?",
        (except_account,),
    ).fetchall()
    return {r["node_id"] for r in rows}


def _pick_node(proxy_id: str, account: str, exclude_node: int = 0) -> dict | None:
    """挑一个没用过、没过期、没标记死的节点；优先最近成功过的。"""
    now = time.time()
    conn = _connect()
    try:
        claimed = _claimed_node_ids(conn, account)
        rows = conn.execute(
            "SELECT * FROM proxy_nodes WHERE proxy_id=? AND dead=0 "
            "AND (expires_at=0 OR expires_at>?) ORDER BY last_ok_at DESC, created_at ASC",
            (proxy_id, now),
        ).fetchall()
        for row in rows:
            if row["id"] == exclude_node or row["id"] in claimed:
                continue
            return _node_row_to_dict(row)
        return None
    finally:
        conn.close()


def _node_by_id(node_id: int) -> dict | None:
    if not node_id:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM proxy_nodes WHERE id=?", (node_id,)
        ).fetchone()
        return _node_row_to_dict(row)
    finally:
        conn.close()


def _bind_node(account: str, proxy_id: str, node: dict) -> None:
    """把账号钉到某个节点（session 字段存节点标识，便于面板显示）。"""
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO proxy_sessions
               (account, proxy_id, session, rotated_at, last_exit_ip,
                last_probe_at, last_probe_ok, last_probe_detail, node_id)
               VALUES (?,?,?,?,'',0,0,'',?)
               ON CONFLICT(account) DO UPDATE SET
                 proxy_id=excluded.proxy_id,
                 session=excluded.session,
                 rotated_at=excluded.rotated_at,
                 last_exit_ip='',
                 last_probe_at=0,
                 last_probe_ok=0,
                 last_probe_detail='',
                 node_id=excluded.node_id""",
            (account, proxy_id, node.get("exit_hint") or node.get("username") or "",
             time.time(), node["id"]),
        )
        conn.commit()
    finally:
        conn.close()


def _ensure_node(account: str, info: dict, rotate: bool = False) -> dict | None:
    """给账号拿到一个可用节点；池子空了就调提取接口补，仍不行返回 None。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT node_id, rotated_at FROM proxy_sessions WHERE account=?",
            (account,),
        ).fetchone()
    finally:
        conn.close()
    cur_node = _node_by_id(row["node_id"]) if row and row["node_id"] else None
    expired = bool(cur_node) and cur_node.get("expires_at") and \
        cur_node["expires_at"] <= time.time()
    if cur_node and not rotate and not expired and not cur_node.get("dead"):
        return cur_node

    node = _pick_node(info["id"], account, exclude_node=(cur_node or {}).get("id", 0))
    if node is None and _should_extract(info):
        fetch_extract_nodes(info["id"], info)
        node = _pick_node(info["id"], account, exclude_node=(cur_node or {}).get("id", 0))
    if node is None:
        return None
    _bind_node(account, info["id"], node)
    return node


def _should_extract(info: dict) -> bool:
    """是否允许再调一次提取接口（按 extract_interval 限流，保护配额）。

    没配提取链接的（节点靠手工粘贴的池子）永远不去调。
    """
    if not (info.get("extract_url") or "").strip():
        return False
    interval = int(info.get("extract_interval") or 0)
    last = float(info.get("extract_last_at") or 0)
    if interval <= 0:
        return True
    return (time.time() - last) >= interval


def mark_node_dead(node_id: int, reason: str = "") -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE proxy_nodes SET dead=1, fail_count=fail_count+1 WHERE id=?",
            (node_id,),
        )
        conn.commit()
    finally:
        conn.close()


def mark_node_ok(node_id: int, exit_ip: str = "") -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE proxy_nodes SET last_ok_at=?, last_exit_ip=?, fail_count=0 WHERE id=?",
            (time.time(), exit_ip or "", node_id),
        )
        conn.commit()
    finally:
        conn.close()


def prune_nodes(proxy_id: str | None = None) -> int:
    """清掉过期和连续失败的节点，避免池子无限膨胀。"""
    conn = _connect()
    try:
        now = time.time()
        # 注意括号：AND proxy_id=? 必须作用在整个 OR 组上。
        # 少了这层括号时 AND 的优先级更高，条件会变成
        #   dead=1 OR expired OR (fail_count>=3 AND proxy_id=?)
        # 于是 prune 一个代理会把别的代理的节点池一起清掉。
        sql = ("DELETE FROM proxy_nodes WHERE (dead=1 "
               "OR (expires_at>0 AND expires_at<=?) OR fail_count>=3)")
        args: list = [now]
        if proxy_id:
            sql += " AND proxy_id=?"
            args.append(proxy_id)
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
