"""Scout 的应用内登录认证。

密码只保存 PBKDF2 哈希；登录后发一个 HMAC 签名、带过期时间的 HttpOnly Cookie。
这里不存会话表，单用户服务重启后已有 Cookie 仍然有效。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import time

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 600_000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_password_hash(
    password: str, *, iterations: int = _ITERATIONS, salt: bytes | None = None
) -> str:
    """生成可放进 `SCOUT_AUTH_PASSWORD_HASH` 的字符串。"""
    if not password:
        raise ValueError("密码不能为空")
    salt = salt or os.urandom(18)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return f"{_ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, expected = encoded.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        iterations = int(raw_iterations)
        if not 100_000 <= iterations <= 2_000_000:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _unb64(raw_salt), iterations
        )
        return hmac.compare_digest(_b64(digest), expected)
    except (TypeError, ValueError, binascii.Error):
        return False


def issue_session(username: str, secret: str, *, max_age: int, now: int | None = None) -> str:
    expires = int(now if now is not None else time.time()) + max_age
    payload = _b64(f"{username}\n{expires}".encode("utf-8"))
    signature = _b64(hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_session(
    token: str, secret: str, username: str, *, now: int | None = None
) -> bool:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(
            hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return False
        got_user, raw_expires = _unb64(payload).decode("utf-8").split("\n", 1)
        current = int(now if now is not None else time.time())
        return hmac.compare_digest(got_user, username) and int(raw_expires) >= current
    except (TypeError, ValueError, UnicodeDecodeError, binascii.Error):
        return False
