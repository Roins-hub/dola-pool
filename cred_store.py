"""账号 Google 登录凭据加密存储（用于自动刷新登录态）。"""
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

_KEYFILE = Path(__file__).with_name("cred.key")
_CREDFILE = Path(__file__).with_name("creds.json")


def _key() -> bytes:
    env = os.getenv("DOLA_CRED_KEY")
    if env:
        return env.encode()
    if _KEYFILE.exists():
        return _KEYFILE.read_text(encoding="utf-8").strip().encode()
    k = Fernet.generate_key()
    _KEYFILE.write_text(k.decode(), encoding="utf-8")
    try:
        os.chmod(_KEYFILE, 0o600)
    except OSError:
        pass
    return k


def _fernet() -> Fernet:
    return Fernet(_key())


def _load_all() -> dict:
    if not _CREDFILE.exists():
        return {}
    try:
        return json.loads(_fernet().decrypt(_CREDFILE.read_bytes()).decode("utf-8"))
    except Exception:
        return {}


def _save_all(data: dict) -> None:
    blob = _fernet().encrypt(json.dumps(data).encode("utf-8"))
    _CREDFILE.write_bytes(blob)
    try:
        os.chmod(_CREDFILE, 0o600)
    except OSError:
        pass


def store(name: str, email: str, password: str, totp: str) -> None:
    data = _load_all()
    data[name] = {"email": email, "password": password, "totp": totp}
    _save_all(data)


def load(name: str) -> dict | None:
    return _load_all().get(name)


def all_names() -> list[str]:
    return list(_load_all().keys())


def rename(old: str, new: str) -> None:
    """账号改名时把凭据一起搬（key 就是账号名）。"""
    data = _load_all()
    if old in data:
        data[new] = data.pop(old)
        _save_all(data)
