"""身份、会话与权限（企划第 8.1 节）。

模块分工：

* ``passwords``  Argon2id 哈希
* ``permissions`` 内容与文案能力的集合语义
* ``sessions``    Cookie 会话（库里存哈希）
* ``tokens``      访问令牌（命令行与站点）
* ``throttle``    登录失败限速
* ``service``     被接口和命令行共用的业务操作
* ``deps``        FastAPI 依赖：解析身份、检查权限
"""
