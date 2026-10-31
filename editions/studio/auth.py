"""dola-pool 用户鉴权工具：邮箱规范化、密码哈希、会话 token。

只依赖标准库，不引入新依赖。
- 密码：PBKDF2-HMAC-SHA256，200k 迭代，每用户独立 salt
- 会话：随机 token，数据库只存 SHA-256 摘要（泄库也无法反推）
"""
import hashlib
import hmac
import re
import secrets

PBKDF2_ITERATIONS = 200_000
_HASH_ALGO = "pbkdf2_sha256"
SESSION_PREFIX = "sess-"
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{1,63}$")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(normalize_email(email)))


def validate_password(password: str) -> str | None:
    """返回错误信息；None 表示通过。"""
    if not password or len(password) < 8:
        return "密码至少 8 位"
    if len(password) > 200:
        return "密码过长"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{_HASH_ALGO}${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not password or not stored:
        return False
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != _HASH_ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def new_session_token() -> str:
    return SESSION_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def parse_bearer(authorization: str | None) -> str | None:
    """从 Authorization 头取出 Bearer token。"""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None
