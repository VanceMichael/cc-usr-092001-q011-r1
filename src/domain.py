"""领域逻辑：程序路径生成、重复立案识别、期限计算、移送状态机与视图裁剪。

本模块不接触数据库与 HTTP，只处理字典与值，便于独立测试。所有时间均为带
偏移量的 ISO 8601 字符串；解析时缺少偏移量一律拒绝。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

# ---- 枚举与标签 ----

UNIT_KINDS = [
    "maritime",       # 海事管理机构
    "procuratorate",  # 检察机关
    "justice_admin",  # 司法行政机关
    "labor",          # 人社仲裁机构
    "mediation",      # 联合调解组织
    "arbitration",    # 商事仲裁机构
    "court",          # 法院
]
# 八家共建单位按上述七类归口登记，第八家可在同类下另立单位编号。

UNIT_LABELS = {
    "maritime": "海事管理机构",
    "procuratorate": "检察机关",
    "justice_admin": "司法行政机关",
    "labor": "人社仲裁机构",
    "mediation": "联合调解组织",
    "arbitration": "商事仲裁机构",
    "court": "法院",
}

DISPUTE_TYPES = ["water_accident", "crew_labor", "ship_pollution", "cargo_transport"]
DISPUTE_LABELS = {
    "water_accident": "水上交通事故",
    "crew_labor": "船员劳资",
    "ship_pollution": "船舶污染",
    "cargo_transport": "货物运输",
}

ELECTIONS = ["mediation", "labor_arbitration", "commercial_arbitration", "litigation"]
ELECTION_LABELS = {
    "mediation": "调解",
    "labor_arbitration": "劳动仲裁",
    "commercial_arbitration": "商事仲裁",
    "litigation": "诉讼",
}

CONFIDENTIALITY_LEVELS = ["L1", "L2", "L3"]  # 依次升高

TRANSFER_KINDS = [
    "调解转司法确认",
    "转劳动仲裁",
    "转商事仲裁",
    "转诉讼",
    "管辖移送",
    "其他移送",
]

NODE_ACTIVE = "进行中"
NODE_PENDING = "待进入"
NODE_PAUSED = "暂停"
NODE_DONE = "已完成"
NODE_TRANSFERRED = "已移送"
NODE_CLOSED = "已终结"

CASE_OPEN = "已登记"
CASE_ACCEPTED = "已受理"
CASE_WITHDRAWN = "已撤回"
CASE_SETTLED = "已结案"
CASE_OBJECTION = "异议审查中"

CLAIM_ACTIVE = "有效"
CLAIM_SETTLED = "已和解"
CLAIM_WITHDRAWN = "已撤回"
CLAIM_DUPLICATE = "重复立案"

DEADLINE_RUNNING = "运行中"
DEADLINE_CLOSED = "已终结"

TRANSFER_PENDING_HANDOVER = "待交出确认"
TRANSFER_PENDING_RECEIPT = "待接收确认"
TRANSFER_COMPLETED = "已完成"
TRANSFER_REJECTED = "已拒绝"

FLAG_PENDING = "待处理"
FLAG_CONFIRMED = "已确认重复"
FLAG_DISMISSED = "已排除"


class DomainError(Exception):
    """领域规则拒绝：携带 HTTP 状态码与机器可读错误码。"""

    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


# ---- 时间与标识 ----

def parse_iso(value: str) -> datetime:
    """解析带偏移量的 ISO 8601 时间；缺少偏移量视为格式错误。"""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise DomainError(400, "invalid_time", f"时间格式无效：{value!r}，须为带偏移量的 ISO 8601")
    if parsed.tzinfo is None:
        raise DomainError(400, "invalid_time", f"时间缺少时区偏移量：{value!r}")
    return parsed


def fmt_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


# ---- 程序路径生成（可解释） ----

def _node(unit_kind: str, action: str, planned_days: int, reason: str) -> dict:
    return {
        "unit_kind": unit_kind,
        "action": action,
        "planned_days": planned_days,
        "reason": reason,
    }


def generate_path(
    dispute_type: str,
    election: str,
    arbitration_agreement: bool,
    cross_border: bool,
) -> tuple[list[dict], str]:
    """按争议类型与当事人选择生成程序路径。

    返回 (节点列表, 总体说明)；每个节点自带理由，保证路径可解释。
    选择不可行时抛出 DomainError(422)，说明理由并给出可选替代。
    """
    if dispute_type not in DISPUTE_TYPES:
        raise DomainError(400, "dispute_type_unknown", f"未知争议类型：{dispute_type!r}")
    if election not in ELECTIONS:
        raise DomainError(400, "election_unknown", f"未知程序选择：{election!r}")

    if election == "commercial_arbitration" and dispute_type == "crew_labor":
        raise DomainError(
            422,
            "election_not_available",
            "船员劳资争议属劳动争议，不适用商事仲裁（仲裁法排除劳动争议）。",
            {"suggested": ["labor_arbitration", "litigation", "mediation"]},
        )

    if election == "mediation":
        nodes = [
            _node("mediation", "联合调解", 30,
                  "共建机制先行调解：受理后30日内由联合调解组织组织调解"),
            _node("court", "司法确认", 30,
                  "调解协议生效后30日内双方可共同申请司法确认，确认裁定具有强制执行力"),
        ]
        explanation = "当事人选择调解：先行联合调解，达成协议可转司法确认；调解不成可再转仲裁或诉讼。"
    elif election == "commercial_arbitration":
        if not arbitration_agreement:
            raise DomainError(
                422,
                "election_not_available",
                "商事仲裁须以有效仲裁协议为前提（仲裁法第四条），本案未登记仲裁协议。",
                {"suggested": ["litigation", "mediation"]},
            )
        nodes = [
            _node("arbitration", "商事仲裁受理", 180,
                  "存在有效仲裁协议，仲裁排除法院管辖（仲裁法第五条），一裁终局"),
        ]
        explanation = "当事人选择商事仲裁：依仲裁协议由仲裁机构管辖。"
    elif election == "labor_arbitration" or (election == "litigation" and dispute_type == "crew_labor"):
        nodes = [
            _node("labor", "劳动人事争议仲裁", 45,
                  "船员劳资争议属劳动争议，仲裁前置（劳动争议调解仲裁法第五条），仲裁时效一年"),
            _node("court", "诉讼", 180,
                  "对仲裁裁决不服的，可自收到裁决书之日起十五日内向法院起诉"),
        ]
        explanation = "船员劳资争议实行仲裁前置：先经劳动仲裁，不服再诉，仲裁程序不可跳过。"
    else:  # litigation，非劳资争议
        if dispute_type in ("water_accident", "ship_pollution"):
            nodes = [
                _node("court", "海事法院立案", 180,
                      "海事侵权纠纷由海事法院专属管辖（海事诉讼特别程序法），连接点：事故发生地、被告住所地"),
            ]
        else:
            nodes = [
                _node("court", "法院立案", 180,
                      "依管辖连接点（被告住所地、合同履行地、运输始发地或目的地）确定管辖法院"),
            ]
        explanation = "当事人选择诉讼：按管辖连接点确定受案法院。"

    if cross_border:
        nodes.append(
            _node("justice_admin", "跨境送达协助", 180,
                  "涉外当事人送达依海牙送达公约或司法协助途径办理，送达期间不计入审理期限")
        )
        explanation += "本案含涉外因素，已附加跨境送达协助节点。"
    return nodes, explanation


# ---- 重复立案识别（只标记，不自动合并） ----

def detect_duplicates(
    new_claims: list[dict],
    new_party_hashes: set[str],
    dispute_type: str,
    existing: list[dict],
) -> list[dict]:
    """将新请求与既有有效请求比对，返回疑似重复标记。

    规则：同一争议类型、同一请求类型、且当事人身份散列重合达到双方较小
    规模时方标记；请求类型不同属于不同请求，绝不标记，避免误合并。
    """
    flags: list[dict] = []
    for claim in new_claims:
        for other in existing:
            if other["dispute_type"] != dispute_type:
                continue
            if other["claim_type"] != claim["claim_type"]:
                continue  # 不同请求类型不得误并
            shared = new_party_hashes & set(other["party_hashes"])
            required = min(2, len(new_party_hashes), len(other["party_hashes"]))
            if required > 0 and len(shared) >= required:
                flags.append({
                    "claim_ref": claim["claim_ref"],
                    "matched_claim_ref": other["claim_ref"],
                    "explanation": (
                        f"与案件 {other['case_ref']} 的请求 {other['claim_ref']} "
                        "当事人一致、争议类型与请求类型相同，疑似重复立案，需人工确认"
                    ),
                })
                break
    return flags


# ---- 法定期限 ----

def deadline_view(row: dict, now: datetime) -> dict:
    """计算期限的剩余天数与预警状态；届满为计算结果，不改写存储状态。"""
    due = parse_iso(row["due_at"])
    remaining = (due - now).days
    status = row["status"]
    if status == DEADLINE_RUNNING and due <= now:
        status = "已届满"
    warning = status == DEADLINE_RUNNING and remaining <= row["warn_days"]
    return {
        "deadline_ref": row["deadline_ref"],
        "kind": row["kind"],
        "basis": row["basis"],
        "due_at": row["due_at"],
        "status": status,
        "remaining_days": remaining,
        "warning": warning,
    }


def deadline_snapshot(rows: list[dict], now: datetime) -> list[dict]:
    """移送时保存的期限快照：移送不中止法定时效，快照用于交接核对。"""
    return [deadline_view(row, now) for row in rows]


# ---- 移送双向确认 ----

def confirm_transfer(transfer: dict, side: str, by_unit_kind: str, now: datetime) -> dict:
    """推进移送状态机，返回应写入的字段。

    顺序固定：先交出方确认，后接收方确认；任一侧顺序或单位不符即拒绝。
    """
    if transfer["status"] == TRANSFER_PENDING_HANDOVER:
        if side != "handover":
            raise DomainError(409, "transfer_order", "须先由交出单位确认交出")
        if by_unit_kind != transfer["from_unit_kind"]:
            raise DomainError(403, "transfer_side", "交出确认须由交出单位办理")
        return {
            "status": TRANSFER_PENDING_RECEIPT,
            "handover_by": by_unit_kind,
            "handover_at": fmt_iso(now),
        }
    if transfer["status"] == TRANSFER_PENDING_RECEIPT:
        if side != "receipt":
            raise DomainError(409, "transfer_order", "交出已确认，须由接收单位确认接收")
        if by_unit_kind != transfer["to_unit_kind"]:
            raise DomainError(403, "transfer_side", "接收确认须由接收单位办理")
        return {
            "status": TRANSFER_COMPLETED,
            "receipt_by": by_unit_kind,
            "receipt_at": fmt_iso(now),
        }
    raise DomainError(409, "transfer_closed", f"移送已处于 {transfer['status']} 状态，不能再确认")


def reject_transfer(transfer: dict, by_unit_kind: str, now: datetime) -> dict:
    """接收方拒绝接收：移送终止，原路径保留，案件仍归交出单位。"""
    if transfer["status"] != TRANSFER_PENDING_RECEIPT:
        raise DomainError(409, "transfer_order", "仅待接收确认的移送可以拒绝")
    if by_unit_kind != transfer["to_unit_kind"]:
        raise DomainError(403, "transfer_side", "拒绝须由接收单位办理")
    return {"status": TRANSFER_REJECTED, "receipt_by": by_unit_kind, "receipt_at": fmt_iso(now)}


# ---- 视图裁剪 ----

def _node_public(node: dict) -> dict:
    return {
        "unit_kind": node["unit_kind"],
        "unit_label": UNIT_LABELS.get(node["unit_kind"], node["unit_kind"]),
        "action": node["action"],
        "reason": node["reason"],
        "status": node["status"],
        "entered_at": node["entered_at"],
    }


def progress_view(
    case: dict,
    claims: list[dict],
    current_nodes: list[dict],
    deadlines: list[dict],
    now: datetime,
) -> dict:
    """公众端视图：仅本人案件进度，不含证据摘要、内部备注与他人信息。"""
    active = next((n for n in current_nodes if n["status"] == NODE_ACTIVE), None)
    upcoming = next((n for n in current_nodes if n["status"] == NODE_PENDING), None)
    return {
        "case_ref": case["case_ref"],
        "dispute_type": case["dispute_type"],
        "dispute_label": DISPUTE_LABELS.get(case["dispute_type"], case["dispute_type"]),
        "status": case["status"],
        "current_step": _node_public(active) if active else None,
        "next_step": (
            {
                "unit_label": UNIT_LABELS.get(upcoming["unit_kind"], upcoming["unit_kind"]),
                "action": upcoming["action"],
            }
            if upcoming
            else None
        ),
        "claims": [
            {"claim_ref": c["claim_ref"], "claim_type": c["claim_type"], "status": c["status"]}
            for c in claims
        ],
        "deadlines": [
            {
                "kind": view["kind"],
                "due_at": view["due_at"],
                "status": view["status"],
                "remaining_days": view["remaining_days"],
            }
            for view in (deadline_view(row, now) for row in deadlines)
        ],
    }


def materials_view(case: dict, grant: dict, evidence: list[dict]) -> dict:
    """协同单位视图：仅授权范围内、且列明引用的证据封存摘要。"""
    allowed = set(json.loads(grant["evidence_refs"]))
    return {
        "case_ref": case["case_ref"],
        "scopes": json.loads(grant["scopes"]),
        "evidence": [
            {
                "evidence_ref": row["evidence_ref"],
                "digest": row["digest"],
                "sealed_by_unit": row["sealed_by_unit"],
                "sealed_at": row["sealed_at"],
            }
            for row in evidence
            if row["evidence_ref"] in allowed
        ],
    }


def _node_supervision(node: dict, version: dict, now: datetime) -> dict:
    entered = parse_iso(node["entered_at"]) if node["entered_at"] else None
    left = parse_iso(node["left_at"]) if node["left_at"] else None
    dwell_days = None
    if entered is not None:
        end = left or now
        dwell_days = round((end - entered).total_seconds() / 86400, 2)
    if version["superseded_at"]:
        validity = "已随版本更替失效"
    elif node["status"] in (NODE_ACTIVE, NODE_PENDING, NODE_PAUSED):
        validity = "有效"
    else:
        validity = "已办结"
    return {
        "node_ref": node["node_ref"],
        "seq": node["seq"],
        "unit_kind": node["unit_kind"],
        "unit_label": UNIT_LABELS.get(node["unit_kind"], node["unit_kind"]),
        "action": node["action"],
        "reason": node["reason"],
        "status": node["status"],
        "validity": validity,
        "entered_at": node["entered_at"],
        "left_at": node["left_at"],
        "dwell_days": dwell_days,
    }


def supervision_view(
    case: dict,
    versions: list[dict],
    nodes_by_version: dict[str, list[dict]],
    transfers: list[dict],
    deadlines: list[dict],
    now: datetime,
) -> dict:
    """监督视图：任一节点可核对实际停留时间、效力状态与下一责任方。"""
    open_transfer = next(
        (t for t in transfers if t["status"] in (TRANSFER_PENDING_HANDOVER, TRANSFER_PENDING_RECEIPT)),
        None,
    )
    if open_transfer and open_transfer["status"] == TRANSFER_PENDING_RECEIPT:
        next_responsible = (
            f"{UNIT_LABELS.get(open_transfer['to_unit_kind'], open_transfer['to_unit_kind'])}"
            "（待接收确认）"
        )
    elif open_transfer:
        next_responsible = (
            f"{UNIT_LABELS.get(open_transfer['from_unit_kind'], open_transfer['from_unit_kind'])}"
            "（待交出确认）"
        )
    else:
        current = next((v for v in versions if not v["superseded_at"]), None)
        active = None
        if current:
            active = next(
                (n for n in nodes_by_version.get(current["version_ref"], []) if n["status"] == NODE_ACTIVE),
                None,
            )
        next_responsible = (
            UNIT_LABELS.get(active["unit_kind"], active["unit_kind"]) if active else "无（案件已终结）"
        )

    return {
        "case_ref": case["case_ref"],
        "status": case["status"],
        "confidentiality": case["confidentiality"],
        "next_responsible": next_responsible,
        "versions": [
            {
                "version_ref": version["version_ref"],
                "version_no": version["version_no"],
                "reason": version["reason"],
                "created_at": version["created_at"],
                "superseded_at": version["superseded_at"],
                "nodes": [
                    _node_supervision(node, version, now)
                    for node in nodes_by_version.get(version["version_ref"], [])
                ],
            }
            for version in versions
        ],
        "transfers": [
            {
                "transfer_ref": t["transfer_ref"],
                "kind": t["kind"],
                "from_unit_kind": t["from_unit_kind"],
                "to_unit_kind": t["to_unit_kind"],
                "status": t["status"],
                "confidentiality": t["confidentiality"],
                "handover_by": t["handover_by"],
                "handover_at": t["handover_at"],
                "receipt_by": t["receipt_by"],
                "receipt_at": t["receipt_at"],
                "deadline_snapshot": json.loads(t["deadline_snapshot"]),
            }
            for t in transfers
        ],
        "deadlines": [deadline_view(row, now) for row in deadlines],
    }
