from sqlmodel import Field
from .p_base_model import PBaseModel

# RawMessage.flushed 的三种取值。
#
# 改成三态是因为 Hindsight 的 retain 支持异步：HTTP 200 只代表「已排队」，
# 事实要等后台抽取跑完才真正落地。两态的话，请求一返回就得写「已提交」，
# 而那时服务端可能正在失败，本地却再也不会回头看了。
FLUSHED_PENDING = 0  # 还没提交过，等下一轮投递
FLUSHED_ENQUEUED = 1  # 已入队，等服务端确认（对应一条 SyncOperation）
FLUSHED_DONE = 2  # 服务端确认抽取完成


# 记录原始对话的表
class RawMessage(PBaseModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    # 记录日期
    record_date: str
    document_id: str
    sender_id: str
    # 说话人显示名（昵称）。投递给 Hindsight 时用这个名字而不是 sender_id：
    # 数字 ID 进对话文本会被抽取成实体 "user"，人格化事实就挂不到具体的人身上。
    # 昵称拿不到时留空，渲染时回退到 sender_id。
    sender_name: str = Field(default="")
    persona_id: str
    role: str
    raw_message: str
    message_sent_time: str
    # 去重指纹：由 document_id + role + raw_message 计算，不含时间与人格。
    # 建唯一索引让数据库来挡重复，而不是靠"先查再插"（并发下仍会重复）。
    content_hash: str = Field(default="", index=True, unique=True)
    # 三态，取值见本模块顶部的 FLUSHED_* 常量。
    flushed: int = Field(default=FLUSHED_PENDING, index=True)
    # 这一行归属哪个提交批次（空串 = 不在任何批次里，即还没提交过）。
    #
    # 归属必须落在行上，不能靠 id 区间反推：一次认领拿到的 id 可能带洞
    # （低 id 的行被上一个失败批次退回过、中间又有新行插进来），
    # min/max 组成的闭区间会盖住别的批次仍处于「已入队」的行——对账完成时
    # 把别人的行一起标成完成，等于绕过 retry/release，静默丢记忆。
    #
    # 有了这一列，对账和撤回都是 WHERE operation_id = ?，判据就是归属本身。
    operation_id: str = Field(default="", index=True)


# 一次 retain 请求的凭据。
#
# 异步提交之后，「这批内容到底有没有落到服务端」只能靠 operation_id 回去问。
# 没有这张表，进程一重启就彻底失忆：那些已入队但失败的批会永远停在
# FLUSHED_ENQUEUED，既不会被重投，也不会被重试。
#
# 归属关系存在 RawMessage.operation_id 上，这张表只记「还没结论的批次」本身。
# 曾经用 (first_row_id, last_row_id) 闭区间来表示归属，v1 迁移时已废弃：
# 区间不是集合，认领带洞时会串到别的批次。这两列保留下来只作遥测（日志与
# 排错时看得到这批大致覆盖哪个 id 范围），不参与任何状态回写。
class SyncOperation(PBaseModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    # 服务端返回的 operation_id。它是幂等键：用同一个 id 重发不会产生第二份
    # 内容，所以提交超时后可以直接重发，不必先确认上一次到底成没成。
    #
    # 注意服务端是「父子两级」结构：我们提交时提供的 id 对应父操作
    # （type=batch_retain），父操作会为每个文档生成一个 type=retain 的子操作。
    # 父操作在全部子操作跑完之前一直停在 pending，之后一起变 completed，
    # 所以巡检只看这个父 id 就够了。
    operation_id: str = Field(index=True, unique=True)

    # 本批覆盖的 rawmessage.id 大致范围（min/max 的认领结果），**仅作遥测**。
    # 不要拿它们做状态回写：认领集合带洞时，区间会盖住别的批次的记录。
    first_row_id: int
    last_row_id: int
    # 本批认领到的行数。对账完成时拿回写的 rowcount 与它比对，少说明有行
    # 已不在「已入队」状态（被别的路径动过），值得留一条 warning。
    items_count: int = Field(default=0)

    # 对账用的本地状态：pending / completed / failed。
    # 与 rawmessage.flushed 的关系是「一个 SyncOperation 对应一批记录」，
    # 结论落在 flushed 上，这张表只负责「还没结论的批次」。
    status: str = Field(default="pending", index=True)

    # 见到 failed 后先调服务端 retry 而不是重投：append 模式下重投会把同样的
    # 内容再追加一遍，同一句话进两次库。retry 是让服务端用已收到的原文重跑。
    attempts: int = Field(default=0)
    error: str = Field(default="")

    created_at: str = Field(default="")
    updated_at: str = Field(default="")