"""上传。

* ``inspection`` 只看文件头判断类型，不信浏览器申报；
* ``service``    会话、分片、合并、落盘，接口与命令行共用；
* ``router``     管理接口 ``/api/admin/v1/uploads``。

第 3 部分到此为止：字节进了 ``originals/``，会话里留下 ``asset_id`` 与
``storage_key``，但还没有 ``assets`` 行——登记是第 4 部分的事。
"""
