from sqlmodel import SQLModel as _SQLModel, Field
from sqlalchemy import MetaData


# 用独立 MetaData 隔离本插件的表：SQLModel.metadata 是进程级全局对象，
# astrbot.core.db.po 的 19 个模型早已注册进去，直接 create_all 会把它们一并建进本库。
# 注意：本方案依赖 SQLModel 未把 registry 写入 model_config
# （sqlmodel/main.py:653-660 会把 metadata 覆盖为 registry.metadata）。
# 若某天表突然不建了，先查这里，兜底可改用：
# RawMessageSqlite.__table__.create(self._engine, checkfirst=True)

# 上述兜底方案也有问题 2026-11-06
class PBaseModel(_SQLModel, table=False):
    metadata = MetaData()
