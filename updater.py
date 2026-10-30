"""面板一键更新（app 侧）：查 GitHub 最新版本、排空任务、和外部更新程序交接。

app 自己不能替换自己（源码部署重启服务需要 root，且重启会杀掉自己起的子进程；Docker 容器
不能替换自己的镜像），所以真正的「换代码 / 换镜像 → 重启 → 验证 → 失败回滚」由外部更新程序做：
  - 源码部署：deploy/updater.sh，由 systemd path 单元监听 ready 文件触发（deploy/install-updater.sh 安装）；
  - Docker：docker/updater.sh，compose 里的 updater 容器（挂 docker.sock）轮询 ready 文件。

交接文件都在 UPDATE_DIR（默认数据库所在目录下的 update/）：
  request.json       app 写：{id, version, tag, state, requested_at, requested_by, ready_at, message}
                     state：draining（暂停派新单，等进行中的任务完成）→ ready（可以开始）
  ready              app 在空闲后写，源码部署的 systemd path 单元以它为触发条件
  agent-status.json  更新程序写：{id, state, message, from_version, to_version, updated_at}
                     state：started / backing_up / downloading / installing / restarting / verifying /
                            done / failed / rolled_back
  agent.json         更新程序安装时写：{mode: source|docker, heartbeat_at}（Docker 版持续刷新心跳）
更新程序结束（成功或失败）后删除 request.json 和 ready；app 看到它们消失就恢复派单。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

import aiohttp

import config

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
TERMINAL = ("done", "failed", "rolled_back")
_LATEST_CACHE: dict = {"at": 0.0, "data": None, "error": ""}


class UpdateError(RuntimeError):
    """给管理员看的更新错误。"""


def update_dir() -> Path:
    return Path(config.UPDATE_DIR)


def _read(name: str) -> dict | None:
    try:
        return json.loads((update_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(name: str, data: dict) -> None:
    root = update_dir()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{name}.{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, root / name)


def _remove(*names: str) -> None:
    for name in names:
        try:
            (update_dir() / name).unlink()
        except FileNotFoundError:
            pass


def parse_version(text: str) -> tuple[int, ...] | None:
    text = (text or "").strip().lstrip("vV")
    return tuple(int(x) for x in text.split(".")) if VERSION_RE.match(text) else None


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


# ---------------------------------------------------------------- 版本信息

async def latest_release(force: bool = False) -> dict:
    """GitHub 最新正式版（缓存 UPDATE_CHECK_TTL 秒）。失败时返回上次结果并带 error。"""
    now = time.time()
    if not force and _LATEST_CACHE["data"] and now - _LATEST_CACHE["at"] < config.UPDATE_CHECK_TTL:
        return {**_LATEST_CACHE["data"], "error": _LATEST_CACHE["error"]}
    url = f"https://api.github.com/repos/{config.UPDATE_REPO}/releases/latest"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url, headers={"Accept": "application/vnd.github+json",
                                                 "User-Agent": "dola-pool-updater"}) as resp:
                if resp.status != 200:
                    raise UpdateError(f"GitHub 返回 HTTP {resp.status}")
                data = await resp.json()
        tag = str(data.get("tag_name") or "")
        if not parse_version(tag):
            raise UpdateError(f"最新版本标签不是 vX.Y.Z：{tag[:40]}")
        _LATEST_CACHE.update(at=now, error="", data={
            "version": tag.lstrip("vV"), "tag": tag, "name": data.get("name") or tag,
            "notes": (data.get("body") or "")[:20000], "url": data.get("html_url") or "",
            "published_at": data.get("published_at") or ""})
    except Exception as exc:  # noqa: BLE001 - 网络问题不影响面板其它功能
        _LATEST_CACHE.update(at=now, error=str(exc)[:200] or type(exc).__name__)
    return {**(_LATEST_CACHE["data"] or {}), "error": _LATEST_CACHE["error"]}


def deploy_mode() -> str:
    forced = (config.DEPLOY_MODE or "").strip().lower()
    if forced in ("source", "docker"):
        return forced
    return "docker" if os.path.exists("/.dockerenv") else "source"


def agent_info() -> dict:
    """更新程序是否就绪。Docker 版要有 2 分钟内的心跳；源码版装过即可（systemd 按需拉起）。"""
    agent = _read("agent.json") or {}
    mode = deploy_mode()
    if not agent:
        return {"ready": False, "mode": mode, "reason": "没有安装更新程序"}
    if agent.get("mode") != mode:
        return {"ready": False, "mode": mode, "reason": f"更新程序是 {agent.get('mode')} 版，当前是 {mode} 部署"}
    if mode == "docker":
        beat = float(agent.get("heartbeat_at") or 0)
        if time.time() - beat > 120:
            return {"ready": False, "mode": mode, "reason": "updater 容器没有在运行（2 分钟内没有心跳）"}
    if agent.get("error"):
        return {"ready": False, "mode": mode, "reason": str(agent["error"])[:200]}
    return {"ready": True, "mode": mode, "reason": ""}


# ---------------------------------------------------------------- 请求与排空

def current_request() -> dict | None:
    return _read("request.json")


def draining() -> bool:
    """有未完成的更新请求时暂停派新单（已受理的任务排队等，更新后自动继续）。"""
    request = current_request()
    return bool(request and request.get("state") in ("draining", "ready"))


def request_update(version: str, current_version: str, user: str, latest: dict) -> dict:
    if not parse_version(version):
        raise UpdateError("版本号格式应为 X.Y.Z")
    if version != latest.get("version"):
        raise UpdateError(f"只能更新到 GitHub 上的最新正式版 {latest.get('version') or '（未获取到）'}")
    if not is_newer(version, current_version):
        raise UpdateError(f"当前已是 {current_version}，不需要更新到 {version}")
    agent = agent_info()
    if not agent["ready"]:
        raise UpdateError(f"不能自动更新：{agent['reason']}")
    if draining():
        raise UpdateError("已经有一个更新在进行")
    request = {"id": uuid.uuid4().hex[:12], "version": version, "tag": f"v{version}", "state": "draining",
               "from_version": current_version, "requested_at": time.time(), "requested_by": user,
               "ready_at": None, "message": "暂停派新单，等正在生成的任务完成"}
    _remove("ready")
    _write("request.json", request)
    return request


def cancel_update() -> dict:
    request = current_request()
    if not request:
        raise UpdateError("没有进行中的更新")
    status = _read("agent-status.json") or {}
    if status.get("id") == request.get("id") and status.get("state") not in TERMINAL:
        raise UpdateError("更新程序已经开始执行，不能取消；请等它完成或自动回滚")
    _remove("ready", "request.json")
    _write("last-result.json", {"id": request["id"], "state": "cancelled", "version": request.get("version"),
                                "message": "管理员取消了更新", "at": time.time()})
    return request


def tick(busy: dict) -> dict | None:
    """后台每几秒调一次：空闲就放行，等太久就放弃。busy = {"running": n, "jobs": n}。返回最新 request。"""
    request = current_request()
    if not request:
        return None
    now = time.time()
    state = request.get("state")
    if state == "draining":
        if busy.get("running", 0) == 0 and busy.get("jobs", 0) == 0:
            request.update(state="ready", ready_at=now, message="任务已清空，等待更新程序开始")
            _write("request.json", request)
            _write("ready", {"id": request["id"]})
        elif now - float(request.get("requested_at") or now) > config.UPDATE_IDLE_TIMEOUT:
            _give_up(request, f"等进行中的任务完成超过 {config.UPDATE_IDLE_TIMEOUT // 60} 分钟，已放弃本次更新")
            return None
        else:
            message = f"暂停派新单，等 {busy.get('running', 0)} 个生成中的任务、{busy.get('jobs', 0)} 个后台作业完成"
            if request.get("message") != message:
                request["message"] = message
                _write("request.json", request)
    elif state == "ready":
        status = _read("agent-status.json") or {}
        started = status.get("id") == request.get("id")
        if not started and now - float(request.get("ready_at") or now) > config.UPDATE_AGENT_TIMEOUT:
            _give_up(request, "更新程序没有响应（检查 dola-pool-updater 是否安装 / updater 容器是否在运行）")
            return None
    return request


def _give_up(request: dict, message: str) -> None:
    _remove("ready", "request.json")
    _write("last-result.json", {"id": request.get("id"), "state": "failed", "version": request.get("version"),
                                "message": message, "at": time.time()})


def status() -> dict:
    request = current_request()
    agent_status = _read("agent-status.json") or {}
    last = _read("last-result.json") or {}
    if request and agent_status.get("id") == request.get("id"):
        progress = agent_status
    elif request:
        progress = {"id": request["id"], "state": request.get("state"), "message": request.get("message")}
    else:
        newest = max((agent_status, last), key=lambda x: float(x.get("updated_at") or x.get("at") or 0))
        progress = newest if newest else {}
    return {"request": request, "progress": progress, "agent": agent_info(), "mode": deploy_mode()}
