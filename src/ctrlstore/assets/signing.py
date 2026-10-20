"""二进制文件的短时签名地址（企划场景 E）。

「仅站点」内容不进入 ``/p/``；网站后端拿一个短时签名地址，再把字节取回去。
签名绑定 **内容 ID、已发布版本 ID 和过期时间**，所以撤回或替换之后旧签名自动失效——
不需要维护吊销列表。

密钥从 ``pageview_hmac_secret`` 派生，而不是再加一个配置项：
同一台机器多一个密钥就多一处部署要改的地方，而派生足以把两种用途隔开。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

#: 派生标签。改动它等于让所有旧签名失效。
_DERIVATION_LABEL = b"ctrlstore/file-signing/v1"

#: 默认有效期（秒）。够网站取一次文件，又不至于被长期转发。
DEFAULT_TTL = 300

#: 允许的最长有效期，避免调用方签出一个"永久"地址。
MAX_TTL = 3600


@dataclass(frozen=True)
class Signed:
    """一个签名地址的参数。"""

    asset_id: str
    version_id: str
    expires: int
    signature: str

    def query(self) -> dict[str, str]:
        return {
            "version": self.version_id,
            "expires": str(self.expires),
            "sig": self.signature,
        }


def _key(secret: str) -> bytes:
    """由一个主密钥派生出文件签名专用密钥。"""
    return hmac.new(secret.encode("utf-8"), _DERIVATION_LABEL, hashlib.sha256).digest()


def _digest(secret: str, asset_id: str, version_id: str, expires: int) -> str:
    message = f"{asset_id}|{version_id}|{expires}".encode()
    return hmac.new(_key(secret), message, hashlib.sha256).hexdigest()


def sign(
    secret: str, *, asset_id: str, version_id: str, ttl: int = DEFAULT_TTL, now: int | None = None
) -> Signed:
    """给一个已发布版本签一个短时地址。"""
    lifetime = max(1, min(ttl, MAX_TTL))
    expires = int(now if now is not None else time.time()) + lifetime
    return Signed(
        asset_id=asset_id,
        version_id=version_id,
        expires=expires,
        signature=_digest(secret, asset_id, version_id, expires),
    )


def verify(
    secret: str,
    *,
    asset_id: str,
    version_id: str,
    expires: int,
    signature: str,
    now: int | None = None,
) -> bool:
    """校验签名与有效期。调用方还要自己确认内容当前的发布状态。"""
    if expires <= int(now if now is not None else time.time()):
        return False
    expected = _digest(secret, asset_id, version_id, expires)
    return hmac.compare_digest(expected, signature)
