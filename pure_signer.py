"""BDMS URL 签名器（Node 长驻进程），源自 dola-pool-cookie 的 server/signer.py。

去掉对 server.metrics 的依赖（用 no-op 计时器替换），供 dola_pure_api 使用。
需要服务器安装 node（DOLA_NODE 可指向 node 路径，默认用 PATH 里的 node）。
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("dola_pure.signer")
SIGN_RETRIES = 2
SIGN_FAIL_LOG_INTERVAL = 300
SIGN_CIRCUIT_BASE_SECONDS = 60
SIGN_CIRCUIT_MAX_SECONDS = 300

_ACCOUNT = contextvars.ContextVar("dola_signer_account", default="shared")
_circuit_until = 0.0
_fail_log_at = 0.0


class _NoopTimer:
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def time(self):
        return self


NOOP_TIMER = _NoopTimer()


def resolve_node() -> str:
    env = (os.environ.get("DOLA_NODE") or "").strip()
    if env and Path(env).exists():
        return env
    found = shutil.which("node") or shutil.which("node.exe")
    return found or "node"


def signer_script() -> Path:
    """返回协议目录下的 bdms 签名 JS。"""
    root = Path(__file__).resolve().parent / "protocol"
    for candidate in (
        root / "js" / "bdms_sign_url.js",
        root / "bdms_sign_url.js",
    ):
        if candidate.exists():
            return candidate
    return root / "js" / "bdms_sign_url.js"


@contextmanager
def use_account(account_id: str) -> Iterator[None]:
    token = _ACCOUNT.set(account_id or "shared")
    try:
        yield
    finally:
        _ACCOUNT.reset(token)


class PersistentSigner:
    def __init__(self, script: Path):
        self.script = Path(script)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._fail_streak = 0
        if not self.script.exists():
            raise FileNotFoundError(f"BDMS signer not found: {self.script}")
        self._start()

    def _start(self) -> None:
        env = os.environ.copy()
        env["DOLA_SIGNER_DAEMON"] = "1"
        self._proc = subprocess.Popen(
            [resolve_node(), str(self.script), "--daemon"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.script.parent),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True).start()

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        try:
            stream = proc.stderr
            if stream is None:
                return
            for line in stream:
                text = str(line or "").strip()
                if text:
                    log.warning("bdms stderr: %s", text[:500])
        except Exception:
            return

    def _restart(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.kill()
            except Exception:
                pass
            self._proc = None
        self._start()

    def close(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
            self._proc = None

    def _trip_circuit(self, exc: Exception) -> None:
        global _circuit_until, _fail_log_at
        self._fail_streak += 1
        wait = min(SIGN_CIRCUIT_MAX_SECONDS, SIGN_CIRCUIT_BASE_SECONDS * self._fail_streak)
        _circuit_until = time.time() + wait
        now = time.time()
        if now - _fail_log_at < SIGN_FAIL_LOG_INTERVAL:
            return
        _fail_log_at = now
        log.warning(
            "bdms signer unavailable for %ss: %s",
            wait,
            str(exc).replace("\n", " ")[:300],
        )

    def _sign_once(
        self,
        url: str,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body: Any = "{}",
        cookies: dict[str, str] | None = None,
    ) -> str:
        payload = json.dumps(
            {
                "url": url,
                "method": method,
                "headers": headers or {},
                "body": body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                "cookies": cookies or {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            assert self._proc and self._proc.stdin and self._proc.stdout
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError("BDMS signer produced no output")
        try:
            data = json.loads(line)
        except ValueError as exc:
            raise RuntimeError(f"BDMS signer returned non-JSON: {line[:500]}") from exc
        if data.get("error"):
            raise RuntimeError(str(data["error"])[:800])
        signed = str(data.get("signed_url") or url)
        if "a_bogus=" not in signed:
            raise RuntimeError(f"BDMS signer did not add a_bogus: {data}")
        return signed

    def __call__(
        self,
        url: str,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body: Any = "{}",
        cookies: dict[str, str] | None = None,
    ) -> str:
        global _circuit_until
        now = time.time()
        if now < _circuit_until:
            raise RuntimeError("BDMS signer cooling down after empty/failed output")
        last_exc: Exception | None = None
        for attempt in range(1, SIGN_RETRIES + 1):
            try:
                signed = self._sign_once(url, method=method, headers=headers, body=body, cookies=cookies)
                self._fail_streak = 0
                _circuit_until = 0.0
                return signed
            except Exception as exc:
                last_exc = exc
                with self._lock:
                    self._restart()
                if attempt < SIGN_RETRIES:
                    continue
                self._trip_circuit(exc)
        assert last_exc is not None
        raise last_exc


_SIGNER: PersistentSigner | None = None
_SIGNER_LOCK = threading.Lock()


def get_signer() -> PersistentSigner:
    global _SIGNER
    with _SIGNER_LOCK:
        if _SIGNER is None:
            _SIGNER = PersistentSigner(signer_script())
        return _SIGNER


def install() -> None:
    """把持久签名器装进 dola_pure_api（替换其每请求 subprocess）。"""
    from protocol import dola_pure_api
    signer = get_signer()
    dola_pure_api.set_sign_url_impl(signer)
    return signer
