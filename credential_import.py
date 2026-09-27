"""账号凭据批量解析 + 分配（对齐 api-pool 的 server.login）。

支持一行一个账号，多种分隔符 & 字段，以及 JSON 数组：
  a@gmail.com----password          (----)
  a@gmail.com|password             (|)
  a@gmail.com,password             (,)
  a@gmail.com:password             (:)
  a@gmail.com----password----totp  (---- + TOTP)
  [{"email":"x@gmail.com","password":"p","totp":"...","id":"acc3"}]

allocate_credentials 按 email 去重，把凭据分配到已有账号名或自动 acc<N>。
纯解析层，不依赖浏览器，可直接单测。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class AccountCred:
    email: str
    password: str
    totp: str = ""
    account_id: str = ""


def parse_account_line(line: str) -> AccountCred | None:
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    # 分隔符优先级：---- > | > , > :
    sep = None
    for candidate in ("----", "|", ",", ":"):
        if candidate in line:
            sep = candidate
            break
    if sep is None:
        return None
    parts = [p.strip() for p in line.split(sep)]
    if len(parts) < 2 or not EMAIL_RE.match(parts[0]):
        return None
    return AccountCred(
        email=parts[0],
        password=parts[1],
        totp=parts[2] if len(parts) > 2 else "",
    )


def _parse_json_records(text: str) -> list[AccountCred]:
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        email = (item.get("email") or "").strip()
        if not EMAIL_RE.match(email):
            continue
        out.append(AccountCred(
            email=email,
            password=str(item.get("password") or ""),
            totp=str(item.get("totp") or ""),
            account_id=str(item.get("id") or item.get("name") or ""),
        ))
    return out


def parse_account_text(raw: str) -> list[AccountCred]:
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.lstrip().startswith("["):
        return _parse_json_records(raw)
    out = []
    for line in raw.splitlines():
        cred = parse_account_line(line)
        if cred:
            out.append(cred)
    return out


def allocate_credentials(records: list[AccountCred],
                         existing_accounts: list[str],
                         name_prefix: str = "acc") -> list[tuple[str, AccountCred]]:
    """按 email 去重；自动取名 acc<N> 或复用记录自带的 id。"""
    existing = set(existing_accounts)
    seen_email: set[str] = set()
    used_names: set[str] = set()
    next_num = 1
    out: list[tuple[str, AccountCred]] = []
    for cred in records:
        norm = cred.email.lower()
        if norm in seen_email:
            continue  # 同邮箱重复导入 → 跳过
        seen_email.add(norm)
        if cred.account_id and cred.account_id not in existing and cred.account_id not in used_names:
            name = cred.account_id
        else:
            while f"{name_prefix}{next_num}" in existing or f"{name_prefix}{next_num}" in used_names:
                next_num += 1
            name = f"{name_prefix}{next_num}"
        used_names.add(name)
        out.append((name, cred))
    return out
