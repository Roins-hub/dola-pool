"""BDMS URL 签名器（Node 长驻进程），源自 dola-pool-cookie 的 server/signer.py。

去掉对 server.metrics 的依赖（用 no-op 计时器替换），供 dola_pure_api 使用。
需要服务器安装 node（DOLA_NODE 可指向 node 路径，默认用 PATH 里的 node）。
"""
from __future__ import annotations

import contextvars
import collections
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("dola_pure.signer")
SIGN_RETRIES = 2
SIGN_FAIL_LOG_INTERVAL = 300
# 单次签名的等待上限：长驻 node 卡住时不能无限等（否则持锁不放，整个服务被拖死）
SIGN_TIMEOUT_SECONDS = float(os.getenv("DOLA_SIGN_TIMEOUT", "15"))
# 长驻进程签名这么多次后主动重启一次，避免长时间运行累积状态/内存
SIGN_RECYCLE_AFTER = int(os.getenv("DOLA_SIGN_RECYCLE_AFTER", "500"))
# 熔断窗口调小：以前 60~300 秒内所有任务都会直接失败，代价太大
SIGN_CIRCUIT_BASE_SECONDS = 30
SIGN_CIRCUIT_MAX_SECONDS = 180
# 签名是 CPU 密集：每次都要在 node 里新建一个 VM 上下文、加载 164KB 的 bdms SDK，
# 单个 node 进程吃满一个核也只有个位数签名/秒。号池规模上来后并发签名需求是几十/秒，
# 只能靠多进程横向扩，不能靠单进程排队。
DEFAULT_SIGNER_PROCESSES = max(2, min(8, (os.cpu_count() or 4) // 2))
SIGNER_PROCESSES = max(1, int(os.getenv("DOLA_SIGNER_PROCESSES", "") or DEFAULT_SIGNER_PROCESSES))
# 同一个进程上连续这么多次单请求超时 → 判它卡死，单独回收重建
SIGN_MAX_CONSECUTIVE_TIMEOUTS = max(1, int(os.getenv("DOLA_SIGN_MAX_CONSECUTIVE_TIMEOUTS", "3")))
# 一次性 node 兜底进程的并发上限：别让兜底本身变成 node 进程风暴
SIGN_FALLBACK_CONCURRENCY = max(1, int(os.getenv("DOLA_SIGN_FALLBACK_CONCURRENCY", "2")))
_FALLBACK_SEM = threading.Semaphore(SIGN_FALLBACK_CONCURRENCY)


def _extract_id(line: str) -> str:
    """从守护进程回包（或请求体）里取 id；没有就当历史通道处理。"""
    if not line or "\"id\"" not in line:
        return ""
    try:
        return str((json.loads(line) or {}).get("id") or "")
    except Exception:
        return ""

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
    def __init__(self, script: Path, name: str = "signer"):
        self.script = Path(script)
        self.name = name
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._generation = 0
        self._fail_streak = 0
        self._sign_count = 0
        self._timeouts = 0
        self._queue: queue.Queue[str | None] = queue.Queue()
        # 请求 id -> (回包队列, 进程代号)：并发下必须按 id 配对回包，
        # 否则 A 线程可能拿到 B 线程的签名（老实现就是共享队列，存在串线隐患）。
        self._pending: dict[str, tuple[queue.Queue, int]] = {}
        self._pending_lock = threading.Lock()
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=8)
        if not self.script.exists():
            raise FileNotFoundError(f"BDMS signer not found: {self.script}")
        self._start()

    def _start(self) -> None:
        env = os.environ.copy()
        env["DOLA_SIGNER_DAEMON"] = "1"
        self._queue = queue.Queue()
        self._stderr_tail.clear()
        self._generation += 1
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
        threading.Thread(target=self._read_stdout,
                         args=(self._proc, self._queue, self._generation),
                         daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True).start()

    def _read_stdout(self, proc: subprocess.Popen[str], sink: "queue.Queue[str | None]",
                     generation: int = 0) -> None:
        """把长驻进程的输出按请求 id 路由回各自的等待者；无 id 的走历史共享通道。

        调用方带超时取，避免 readline 卡死。进程结束时只失败属于它这一代的在途请求，
        不牵连其他代（老实现里一个进程死掉会把所有等待者一起打死）。
        """
        try:
            stream = proc.stdout
            if stream is None:
                return
            for line in stream:
                rid = _extract_id(line)
                if not rid:
                    sink.put(line)
                    continue
                with self._pending_lock:
                    slot = self._pending.get(rid)
                if slot is not None:
                    slot[0].put(line)
                # id 不在册（该请求已超时放弃/属于别的进程）：直接丢弃，绝不污染别人
        except Exception:
            pass
        finally:
            with self._pending_lock:
                stale = [(rid, q) for rid, (q, gen) in self._pending.items() if gen == generation]
                for rid, _q in stale:
                    self._pending.pop(rid, None)
            for _rid, q in stale:
                q.put(None)
            sink.put(None)

    @property
    def pending_count(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        try:
            stream = proc.stderr
            if stream is None:
                return
            for line in stream:
                text = str(line or "").strip()
                if text:
                    self._stderr_tail.append(text[:200])
                    log.warning("bdms stderr: %s", text[:500])
        except Exception:
            return

    def _restart(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.kill()
                    try:
                        self._proc.wait(timeout=5)   # 收尸，别留 zombie
                    except Exception:
                        pass
            except Exception:
                pass
            self._proc = None
        self._start()

    def close(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except Exception:
                    pass
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
                "id": uuid.uuid4().hex[:12],
                "url": url,
                "method": method,
                "headers": headers or {},
                "body": body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                "cookies": cookies or {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._parse(self._send_daemon(payload), url)

    def _recycle_if_idle(self) -> None:
        """签满 RECYCLE_AFTER 次就趁空闲重启一次，避免长期运行累积状态。"""
        if self._sign_count < SIGN_RECYCLE_AFTER:
            return
        with self._pending_lock:
            busy = bool(self._pending)
        if busy:
            return      # 有在途请求，别一边签一边把进程换掉
        log.info("[%s] 已签名 %d 次，空闲期主动重启", self.name, self._sign_count)
        self._sign_count = 0
        self._restart()

    def _send_daemon(self, payload: str) -> str:
        """把一行请求喂给长驻进程，按请求 id 取回自己那一行。

        关键点：超时只让**当前这一个**请求失败，不动进程、不牵连别的请求。
        真正卡死的判定交给 _recover_after_failure（连续超时若干次才回收）。
        """
        rid = _extract_id(payload)
        with self._lock:
            self._recycle_if_idle()
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            assert self._proc and self._proc.stdin
            generation = self._generation
            pending: queue.Queue | None = queue.Queue() if rid else None
            if pending is not None:
                with self._pending_lock:
                    self._pending[rid] = (pending, generation)
            try:
                self._proc.stdin.write(payload + "\n")
                self._proc.stdin.flush()
            except Exception as exc:
                if rid:
                    with self._pending_lock:
                        self._pending.pop(rid, None)
                raise RuntimeError(f"BDMS signer stdin broken: {exc}") from exc
        sink = pending if pending is not None else self._queue
        try:
            line = sink.get(timeout=SIGN_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            self._timeouts += 1
            raise RuntimeError(
                f"BDMS signer timeout after {SIGN_TIMEOUT_SECONDS:.0f}s "
                f"(stderr: {' | '.join(self._stderr_tail) or 'empty'})"
            ) from exc
        finally:
            if rid:
                with self._pending_lock:
                    self._pending.pop(rid, None)
        self._timeouts = 0
        if line is None:
            code = self._proc.poll() if self._proc else None
            raise RuntimeError(
                f"BDMS signer produced no output (exit={code}, "
                f"stderr: {' | '.join(self._stderr_tail) or 'empty'})"
            )
        self._sign_count += 1
        return str(line)

    def _recover_after_failure(self) -> None:
        """失败后只回收"确实坏了"的进程，不做无差别自杀。

        老实现是失败就 kill 共享进程：一次超时会把所有在途请求一起打死，
        每个失败者又各自重启，形成 SIGKILL 风暴（实测 32 路并发下 300 次签名失败 27 次）。
        """
        proc = self._proc
        dead = proc is None or proc.poll() is not None
        if dead or self._timeouts >= SIGN_MAX_CONSECUTIVE_TIMEOUTS:
            self._restart()

    def _send_subprocess(self, payload: str) -> str:
        """兜底：按请求起一次性 node（不依赖长驻进程的状态）。"""
        with _FALLBACK_SEM:
            return self._send_subprocess_locked(payload)

    def _send_subprocess_locked(self, payload: str) -> str:
        """真正的一次性 node 调用；由 _FALLBACK_SEM 限制并发（防进程风暴）。"""
        proc = subprocess.run(
            [resolve_node(), str(self.script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=SIGN_TIMEOUT_SECONDS * 2,
            cwd=str(self.script.parent),
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"BDMS signer one-shot failed: {proc.stderr[:300] or proc.stdout[:300]}"
            )
        return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""

    def _parse(self, line: str, url: str) -> str:
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
                    self._recover_after_failure()
                if attempt < SIGN_RETRIES:
                    continue
        # 长驻进程两轮都失败时兜底用一次性 node（实测这条通路最稳），
        # 别因为签名器状态坏了就让 60~180 秒内所有任务直接失败。
        try:
            payload = json.dumps(
                {
                    "id": uuid.uuid4().hex[:12],
                    "url": url,
                    "method": method,
                    "headers": headers or {},
                    "body": body if isinstance(body, str) else json.dumps(
                        body, ensure_ascii=False, separators=(",", ":")),
                    "cookies": cookies or {},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            signed = self._parse(self._send_subprocess(payload), url)
            log.warning("bdms signer 走了兜底一次性进程（长驻进程失败: %s）",
                        str(last_exc)[:200])
            self._fail_streak = 0
            _circuit_until = 0.0
            return signed
        except Exception as fallback_exc:
            last_exc = fallback_exc
        assert last_exc is not None
        self._trip_circuit(last_exc)
        assert last_exc is not None
        raise last_exc


class SignerPool:
    """N 个独立签名进程组成的小池子：取"当前在途最少"的那个来签。

    单进程签名是 CPU 密集的（每次签名都要新建 VM 上下文），一个核几百毫秒，
    所以并发能力靠进程数横向扩；每个进程内部仍然串行处理自己的请求。
    池子只做负载分配，签名语义与单进程完全一致。
    """

    def __init__(self, script: Path, size: int = SIGNER_PROCESSES):
        size = max(1, int(size))
        self._script = Path(script)
        self._signers = [
            PersistentSigner(self._script, name=f"signer-{i + 1}")
            for i in range(size)
        ]
        self._lock = threading.Lock()
        self._turn = 0

    def __len__(self) -> int:
        return len(self._signers)

    @property
    def size(self) -> int:
        return len(self._signers)

    def stats(self) -> list[dict]:
        return [
            {"name": s.name,
             "pending": s.pending_count,
             "signs": s._sign_count,
             "timeouts": s._timeouts,
             "alive": bool(s._proc is not None and s._proc.poll() is None)}
            for s in self._signers
        ]

    def _pick(self) -> PersistentSigner:
        """在途最少优先（同分时轮转），避免请求全压在一个进程上。"""
        with self._lock:
            n = len(self._signers)
            best = None
            best_load = None
            for i in range(n):
                idx = (self._turn + i) % n
                sig = self._signers[idx]
                load = sig.pending_count
                if best is None or load < best_load:
                    best, best_load, best_idx = sig, load, idx
            self._turn = (best_idx + 1) % n
            return best

    def __call__(self, url: str, *, method: str = "POST",
                 headers: dict[str, str] | None = None,
                 body: Any = "{}", cookies: dict[str, str] | None = None) -> str:
        return self._pick()(url, method=method, headers=headers, body=body, cookies=cookies)

    def close(self) -> None:
        for sig in self._signers:
            try:
                sig.close()
            except Exception:
                pass


_SIGNER: SignerPool | None = None
_SIGNER_LOCK = threading.Lock()


def get_signer() -> SignerPool:
    global _SIGNER
    with _SIGNER_LOCK:
        if _SIGNER is None:
            _SIGNER = SignerPool(signer_script())
            log.info("BDMS 签名进程池已就绪：%d 个进程", _SIGNER.size)
        return _SIGNER


def install() -> None:
    """把持久签名器装进 dola_pure_api（替换其每请求 subprocess）。"""
    from protocol import dola_pure_api
    signer = get_signer()
    dola_pure_api.set_sign_url_impl(signer)
    return signer
