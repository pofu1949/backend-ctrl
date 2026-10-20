"""存储层。

第一期只有本地文件系统实现，但对外只暴露「键 → 文件」的语义，
以后换对象存储时替换实现即可（企划第 16.2 节）。
"""

from . import keys
from .local import LocalStorage, PartWriter, TooLarge

__all__ = ["LocalStorage", "PartWriter", "TooLarge", "keys"]
