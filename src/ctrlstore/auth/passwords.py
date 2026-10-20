"""密码哈希。

用 Argon2id（argon2-cffi 的默认类型）。参数取库的默认档：内存 64 MiB、时间 3 轮，
在 4 核 8 GB 的机器上单次约几十毫秒，足够挡住离线爆破又不影响登录。
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: 新建账号的密码最短长度。企划未规定数值，这里按“口令而非验证码”定 12。
MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()


class WeakPasswordError(ValueError):
    """密码不符合最低要求。"""


def validate_password(password: str) -> None:
    """不合规就抛 :class:`WeakPasswordError`。"""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"密码至少 {MIN_PASSWORD_LENGTH} 个字符")


def hash_password(password: str) -> str:
    """生成哈希。调用方应先校验强度。"""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """校验密码。哈希损坏或算法不匹配都按失败处理，不向调用方抛异常。"""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """参数升级后，用户下次登录成功可以顺手重算哈希。"""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False
