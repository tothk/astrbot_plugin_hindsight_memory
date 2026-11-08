from sqlmodel import Field, SQLModel, create_engine
from .p_base_model import PBaseModel


# 记录原始对话的表
class RawMessage(PBaseModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    # 记录日期
    record_date: str
    document_id: str
    sender_id: str
    persona_id: str
    role:str
    raw_message: str
    message_sent_time: str
    # 去重指纹：由 document_id + role + raw_message 计算，不含时间与人格。
    # 建唯一索引让数据库来挡重复，而不是靠"先查再插"（并发下仍会重复）。
    content_hash: str = Field(default="", index=True, unique=True)
    flushed: int = Field(default=0)