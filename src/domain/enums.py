"""领域枚举与受控词表。

标识约定（见 docs/domain.md）：
- 对外编号不含真实身份信息，统一使用稳定引用号；
- 争议类型与程序节点均为受控词表，新增类型必须在此登记并给出路径规则。
"""

from enum import StrEnum


class DisputeType(StrEnum):
    MARITIME_ACCIDENT = "maritime_accident"          # 水上交通事故
    CREW_LABOR = "crew_labor"                        # 船员劳资
    VESSEL_POLLUTION = "vessel_pollution"            # 船舶污染
    CARGO_TRANSPORT = "cargo_transport"              # 货物运输争议


class CaseStatus(StrEnum):
    ACCEPTED = "accepted"                # 已受理，程序进行中
    WITHDRAWN = "withdrawn"              # 全部撤回
    PARTIAL_SETTLED = "partial_settled"  # 部分和解（剩余请求继续）
    CLOSED = "closed"                    # 终局办结


class NodeCode(StrEnum):
    """程序路径上的节点（受控词表）。"""

    INTAKE = "intake"                    # 首次登记受理
    MSA_ADMIN = "msa_admin"              # 海事机构行政处理/调查取证
    MEDIATION = "mediation"              # 行政/行业调解
    LABOR_ARBITRATION = "labor_arbitration"      # 劳动人事争议仲裁
    COMMERCIAL_ARBITRATION = "commercial_arbitration"  # 商事仲裁
    JUDICIAL_CONFIRMATION = "judicial_confirmation"    # 司法确认
    LITIGATION = "litigation"            # 诉讼
    PROCURATORIAL = "procuratorial"      # 检察环节（支持起诉/公益/监督）
    CROSS_BORDER_SERVICE = "cross_border_service"      # 跨境送达
    EXECUTION = "execution"              # 执行


class StageStatus(StrEnum):
    PENDING = "pending"        # 等待进入/等待接收单位确认
    ACTIVE = "active"          # 当前责任节点
    HANDED_OFF = "handed_off"  # 已正式移交（旧路径保留、只读）
    SUPERSEDED = "superseded"  # 被新路径取代但保留（如管辖异议改管）
    CLOSED = "closed"          # 该节点办结
    VOID = "void"              # 撤回等导致失效，留痕不可删


class HandshakeState(StrEnum):
    """移送交接的双向确认状态。"""

    PROPOSED = "proposed"      # 交出单位已发起
    RECEIVED = "received"      # 接收单位已签收
    REJECTED = "rejected"      # 接收单位拒收（材料/管辖不符）
    ROLLED_BACK = "rolled_back"  # 拒收后退回交出方


class EventKind(StrEnum):
    CASE_FILED = "case_filed"
    PATH_PLANNED = "path_planned"
    PATH_CHANGED = "path_changed"
    TRANSFER_PROPOSED = "transfer_proposed"
    TRANSFER_RECEIVED = "transfer_received"
    TRANSFER_REJECTED = "transfer_rejected"
    TRANSFER_ROLLED_BACK = "transfer_rolled_back"
    CLAIM_WITHDRAWN = "claim_withdrawn"
    PARTIAL_SETTLEMENT = "partial_settlement"
    JURISDICTION_OBJECTION = "jurisdiction_objection"
    JURISDICTION_RULING = "jurisdiction_ruling"
    CROSS_BORDER_SERVICE = "cross_border_service"
    DEADLINE_TOLLED = "deadline_tolled"
    STAGE_CLOSED = "stage_closed"
    CASE_CLOSED = "case_closed"


class Confidentiality(StrEnum):
    """保密级别随程序变化：只能升不能随意降。"""

    PUBLIC = "public"          # 进度类信息，当事人本人可见
    RESTRICTED = "restricted"  # 获授权协同单位可见
    CONFIDENTIAL = "confidential"  # 敏感材料（身份原始件、涉商业秘密）
    SECRET = "secret"          # 跨境/涉刑等特别保护
