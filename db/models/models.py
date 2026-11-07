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
    flushed: int = Field(default=0)