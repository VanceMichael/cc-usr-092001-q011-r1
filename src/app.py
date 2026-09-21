"""运河纠纷程序衔接引擎的 HTTP 服务。

路由与请求处理通过 `handle_request` 与 socket 解耦，测试无需启动网络服务。
鉴权使用演示级请求头：`X-Actor-Ref`（行为主体引用）、`X-Actor-Role`
（registrar / unit / public / supervisor）、`X-Actor-Unit`（单位类别）。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import domain
from .domain import DomainError
from .store import Store

ROLES = {"registrar", "unit", "public", "supervisor"}


def health_payload() -> dict[str, str]:
    """返回可供运行环境探测的服务状态。"""
    return {"status": "ok"}


class Actor:
    """请求行为主体：角色决定可见范围。"""

    def __init__(self, ref: str, role: str, unit_kind: str | None):
        self.ref = ref
        self.role = role
        self.unit_kind = unit_kind


def _build_actor(headers: dict) -> Actor:
    ref = headers.get("x-actor-ref")
    if not ref:
        raise DomainError(401, "unauthenticated", "缺少 X-Actor-Ref 请求头")
    role = headers.get("x-actor-role", "public")
    if role not in ROLES:
        raise DomainError(400, "role_unknown", f"未知角色：{role!r}")
    unit_kind = headers.get("x-actor-unit")
    if role == "unit" and unit_kind not in domain.UNIT_KINDS:
        raise DomainError(400, "unit_unknown", "单位角色须提供有效的 X-Actor-Unit")
    return Actor(ref=ref, role=role, unit_kind=unit_kind)


def _require(actor: Actor, *roles: str) -> None:
    if actor.role not in roles:
        raise DomainError(403, "forbidden", f"该操作仅限 {'/'.join(roles)} 角色")


def _get_case(store: Store, case_ref: str) -> dict:
    case = store.get_case(case_ref)
    if case is None:
        raise DomainError(404, "case_not_found", f"案件不存在：{case_ref}")
    return case


def _require_unit_grant(store: Store, actor: Actor, case_ref: str) -> dict:
    """协同单位必须持有该案件的有效授权。"""
    if actor.role != "unit":
        raise DomainError(403, "forbidden", "该操作仅限协同单位")
    grant = store.grant_for_unit(case_ref, actor.unit_kind)
    if grant is None:
        raise DomainError(403, "not_authorized", "本单位未获该案件授权")
    return grant


def _active_node(store: Store, case_ref: str) -> dict | None:
    version = store.current_version(case_ref)
    if version is None:
        return None
    for node in store.nodes_of_version(version["version_ref"]):
        if node["status"] == domain.NODE_ACTIVE:
            return node
    return None


def _branch_version(store: Store, case_ref: str, reason: str, now: str) -> str:
    """以当前版本为底另起路径版本，旧版本保留并可查询。"""
    current = store.current_version(case_ref)
    if current is None:
        raise DomainError(409, "no_path", "案件尚未生成程序路径")
    store.supersede_version(current["version_ref"], now)
    version_ref = domain.new_ref("VER")
    store.insert_version({
        "version_ref": version_ref,
        "case_ref": case_ref,
        "version_no": current["version_no"] + 1,
        "reason": reason,
        "created_at": now,
    })
    for node in store.nodes_of_version(current["version_ref"]):
        store.insert_node({**node, "node_ref": domain.new_ref("NODE"), "version_ref": version_ref})
    return version_ref


def _close_case(store: Store, case_ref: str, status: str, now: str) -> None:
    """终结案件：关闭当前节点与运行中的期限，路径版本保留可查。"""
    version = store.current_version(case_ref)
    if version:
        for node in store.nodes_of_version(version["version_ref"]):
            if node["status"] in (domain.NODE_ACTIVE, domain.NODE_PENDING, domain.NODE_PAUSED):
                store.update_node(node["node_ref"], status=domain.NODE_CLOSED, left_at=now)
        store.supersede_version(version["version_ref"], now)
    for row in store.deadlines_of_case(case_ref):
        if row["status"] == domain.DEADLINE_RUNNING:
            store.update_deadline(row["deadline_ref"], status=domain.DEADLINE_CLOSED)
    store.update_case(case_ref, status=status, closed_at=now)


# ---- 各端点处理 ----

def h_health(store, actor, now, match, payload):
    return 200, health_payload()


def h_create_party(store, actor, now, match, payload):
    _require(actor, "registrar")
    party_type = payload.get("party_type")
    if party_type not in ("自然人", "法人", "非法人组织"):
        raise DomainError(400, "party_type_invalid", "party_type 须为 自然人/法人/非法人组织")
    id_hash = payload.get("id_hash")
    if not id_hash:
        raise DomainError(400, "id_hash_required", "最小身份资料须包含身份散列 id_hash")
    party_ref = payload.get("party_ref") or domain.new_ref("PARTY")
    if store.get_party(party_ref):
        raise DomainError(409, "party_exists", f"当事方已登记：{party_ref}")
    store.insert_party({
        "party_ref": party_ref,
        "party_type": party_type,
        "id_hash": id_hash,
        "contact_ref": payload.get("contact_ref"),
        "registered_at": now,
    })
    return 201, {"party_ref": party_ref}


def h_create_case(store, actor, now, match, payload):
    _require(actor, "registrar")
    dispute_type = payload.get("dispute_type")
    election = payload.get("election")
    if dispute_type not in domain.DISPUTE_TYPES:
        raise DomainError(400, "dispute_type_unknown", f"未知争议类型：{dispute_type!r}")
    if election not in domain.ELECTIONS:
        raise DomainError(400, "election_unknown", f"未知程序选择：{election!r}")
    parties = payload.get("parties") or []
    claims = payload.get("claims") or []
    if not parties:
        raise DomainError(400, "parties_required", "至少登记一方当事人")
    if not claims:
        raise DomainError(400, "claims_required", "至少登记一项请求事项")
    confidentiality = payload.get("confidentiality", "L2")
    if confidentiality not in domain.CONFIDENTIALITY_LEVELS:
        raise DomainError(400, "confidentiality_invalid", "保密级别须为 L1/L2/L3")

    case_ref = payload.get("case_ref") or domain.new_ref("CASE")
    if store.get_case(case_ref):
        raise DomainError(409, "case_exists", f"案件已登记：{case_ref}")
    store.insert_case({
        "case_ref": case_ref,
        "dispute_type": dispute_type,
        "connection_points": json.dumps(payload.get("connection_points") or [], ensure_ascii=False),
        "election": election,
        "arbitration_agreement": 1 if payload.get("arbitration_agreement") else 0,
        "cross_border": 1 if payload.get("cross_border") else 0,
        "status": domain.CASE_OPEN,
        "confidentiality": confidentiality,
        "opened_by_unit": payload.get("opened_by_unit") or "registrar",
        "opened_at": now,
    })

    party_hashes: set[str] = set()
    for entry in parties:
        party = store.get_party(entry.get("party_ref", ""))
        if party is None:
            raise DomainError(400, "party_unknown", f"当事方未登记：{entry.get('party_ref')}")
        store.insert_case_party(case_ref, party["party_ref"], entry.get("role", "申请人"))
        party_hashes.add(party["id_hash"])

    claim_refs = []
    for item in claims:
        claim_ref = domain.new_ref("CLAIM")
        store.insert_claim({
            "claim_ref": claim_ref,
            "case_ref": case_ref,
            "claim_type": item.get("claim_type"),
            "amount": item.get("amount"),
            "currency": item.get("currency"),
            "summary_ref": item.get("summary_ref"),
            "status": domain.CLAIM_ACTIVE,
        })
        claim_refs.append(claim_ref)

    for item in payload.get("evidence") or []:
        digest = item.get("digest", "")
        if not digest.startswith("sha256:"):
            raise DomainError(400, "digest_invalid", "证据封存摘要须为 sha256: 前缀")
        store.insert_evidence({
            "evidence_ref": item.get("evidence_ref") or domain.new_ref("EVID"),
            "case_ref": case_ref,
            "digest": digest,
            "sealed_by_unit": payload.get("opened_by_unit") or "registrar",
            "sealed_at": now,
            "note_ref": item.get("note_ref"),
        })

    for item in payload.get("deadlines") or []:
        domain.parse_iso(item.get("due_at", ""))  # 校验期限时间格式
        store.insert_deadline({
            "deadline_ref": domain.new_ref("DDL"),
            "case_ref": case_ref,
            "kind": item.get("kind"),
            "basis": item.get("basis", ""),
            "start_at": item.get("start_at") or now,
            "due_at": item["due_at"],
            "warn_days": int(item.get("warn_days", 30)),
            "status": domain.DEADLINE_RUNNING,
        })

    # 重复立案识别：只生成标记，由人工决定，绝不自动合并。
    new_claims = [{"claim_ref": ref, "claim_type": c.get("claim_type")}
                  for ref, c in zip(claim_refs, claims)]
    existing = store.claims_for_duplicate_scan(case_ref)
    flags = []
    for flag in domain.detect_duplicates(new_claims, party_hashes, dispute_type, existing):
        flag_ref = domain.new_ref("FLAG")
        store.insert_flag({
            "flag_ref": flag_ref,
            "case_ref": case_ref,
            "claim_ref": flag["claim_ref"],
            "matched_claim_ref": flag["matched_claim_ref"],
            "explanation": flag["explanation"],
            "status": domain.FLAG_PENDING,
            "created_at": now,
        })
        flags.append({"flag_ref": flag_ref, **flag})

    # 登记单位获得该案件的协同授权。
    evidence_refs = [row["evidence_ref"] for row in store.evidence_of_case(case_ref)]
    store.insert_grant({
        "grant_ref": domain.new_ref("GRANT"),
        "case_ref": case_ref,
        "grantee_kind": "unit",
        "grantee_ref": payload.get("opened_by_unit") or "registrar",
        "scopes": json.dumps(["progress", "materials"], ensure_ascii=False),
        "evidence_refs": json.dumps(evidence_refs, ensure_ascii=False),
        "valid_from": now,
    })
    return 201, {
        "case_ref": case_ref,
        "claim_refs": claim_refs,
        "evidence_refs": evidence_refs,
        "duplicate_flags": flags,
    }


def h_accept_case(store, actor, now, match, payload):
    _require(actor, "registrar", "unit")
    case_ref = match.group("case_ref")
    case = _get_case(store, case_ref)
    if actor.role == "unit":
        _require_unit_grant(store, actor, case_ref)
    if case["status"] != domain.CASE_OPEN:
        raise DomainError(409, "case_state", f"案件状态为 {case['status']}，不能受理")
    nodes, explanation = domain.generate_path(
        case["dispute_type"],
        case["election"],
        bool(case["arbitration_agreement"]),
        bool(case["cross_border"]),
    )
    version_ref = domain.new_ref("VER")
    store.insert_version({
        "version_ref": version_ref,
        "case_ref": case_ref,
        "version_no": 1,
        "reason": f"受理：{explanation}",
        "created_at": now,
    })
    created = []
    for seq, node in enumerate(nodes, start=1):
        node_ref = domain.new_ref("NODE")
        store.insert_node({
            "node_ref": node_ref,
            "version_ref": version_ref,
            "case_ref": case_ref,
            "seq": seq,
            "unit_kind": node["unit_kind"],
            "action": node["action"],
            "reason": node["reason"],
            "planned_days": node["planned_days"],
            "status": domain.NODE_ACTIVE if seq == 1 else domain.NODE_PENDING,
            "entered_at": now if seq == 1 else None,
        })
        created.append({"node_ref": node_ref, **node})
    store.update_case(case_ref, status=domain.CASE_ACCEPTED)
    return 200, {"case_ref": case_ref, "version_ref": version_ref,
                 "explanation": explanation, "nodes": created}


def h_case_path(store, actor, now, match, payload):
    _require(actor, "registrar", "unit", "supervisor")
    case_ref = match.group("case_ref")
    _get_case(store, case_ref)
    if actor.role == "unit":
        _require_unit_grant(store, actor, case_ref)
    versions = []
    for version in store.versions_of_case(case_ref):
        versions.append({**version, "nodes": store.nodes_of_version(version["version_ref"])})
    return 200, {"case_ref": case_ref, "versions": versions}


def h_case_progress(store, actor, now, match, payload):
    case_ref = match.group("case_ref")
    case = _get_case(store, case_ref)
    if actor.role == "public":
        if actor.ref not in store.case_party_refs(case_ref):
            raise DomainError(403, "not_party", "当事人仅可查看本人案件进度")
    else:
        _require(actor, "registrar", "supervisor")
    version = store.current_version(case_ref)
    nodes = store.nodes_of_version(version["version_ref"]) if version else []
    moment = domain.parse_iso(now)
    view = domain.progress_view(
        case, store.claims_of_case(case_ref), nodes, store.deadlines_of_case(case_ref), moment
    )
    return 200, view


def h_case_materials(store, actor, now, match, payload):
    _require(actor, "unit")
    case_ref = match.group("case_ref")
    _get_case(store, case_ref)
    grant = _require_unit_grant(store, actor, case_ref)
    view = domain.materials_view(
        store.get_case(case_ref), grant, store.evidence_of_case(case_ref)
    )
    return 200, view


def h_case_supervision(store, actor, now, match, payload):
    _require(actor, "supervisor")
    case_ref = match.group("case_ref")
    case = _get_case(store, case_ref)
    versions = store.versions_of_case(case_ref)
    nodes_by_version = {
        v["version_ref"]: store.nodes_of_version(v["version_ref"]) for v in versions
    }
    view = domain.supervision_view(
        case,
        versions,
        nodes_by_version,
        store.transfers_of_case(case_ref),
        store.deadlines_of_case(case_ref),
        domain.parse_iso(now),
    )
    return 200, view


def h_case_deadlines(store, actor, now, match, payload):
    _require(actor, "registrar", "unit", "supervisor")
    case_ref = match.group("case_ref")
    _get_case(store, case_ref)
    if actor.role == "unit":
        _require_unit_grant(store, actor, case_ref)
    moment = domain.parse_iso(now)
    views = [domain.deadline_view(row, moment) for row in store.deadlines_of_case(case_ref)]
    return 200, {"case_ref": case_ref, "deadlines": views}


def h_create_transfer(store, actor, now, match, payload):
    _require(actor, "unit")
    case_ref = match.group("case_ref")
    case = _get_case(store, case_ref)
    _require_unit_grant(store, actor, case_ref)
    active = _active_node(store, case_ref)
    if active is None or active["unit_kind"] != actor.unit_kind:
        raise DomainError(403, "not_responsible", "仅当前责任单位可以发起移送")
    if store.open_transfer_of_case(case_ref):
        raise DomainError(409, "transfer_open", "存在未完成的移送，不能重复发起")
    kind = payload.get("kind")
    if kind not in domain.TRANSFER_KINDS:
        raise DomainError(400, "kind_unknown", f"未知移送类型：{kind!r}")
    to_unit_kind = payload.get("to_unit_kind")
    if to_unit_kind not in domain.UNIT_KINDS or to_unit_kind == actor.unit_kind:
        raise DomainError(400, "unit_invalid", "接收单位类别无效")
    confidentiality = payload.get("confidentiality", case["confidentiality"])
    if confidentiality not in domain.CONFIDENTIALITY_LEVELS:
        raise DomainError(400, "confidentiality_invalid", "保密级别须为 L1/L2/L3")
    known = {row["evidence_ref"] for row in store.evidence_of_case(case_ref)}
    materials = payload.get("materials") or []
    unknown = [ref for ref in materials if ref not in known]
    if unknown:
        raise DomainError(400, "materials_unknown", f"材料引用不属于本案：{unknown}")

    moment = domain.parse_iso(now)
    transfer_ref = domain.new_ref("TRANS")
    store.insert_transfer({
        "transfer_ref": transfer_ref,
        "case_ref": case_ref,
        "kind": kind,
        "from_node_ref": active["node_ref"],
        "from_unit_kind": actor.unit_kind,
        "to_unit_kind": to_unit_kind,
        "reason": payload.get("reason", ""),
        "materials": json.dumps(materials, ensure_ascii=False),
        "confidentiality": confidentiality,
        "deadline_snapshot": json.dumps(
            domain.deadline_snapshot(store.deadlines_of_case(case_ref), moment),
            ensure_ascii=False,
        ),
        "status": domain.TRANSFER_PENDING_HANDOVER,
        "created_at": now,
    })
    return 201, {"transfer_ref": transfer_ref, "status": domain.TRANSFER_PENDING_HANDOVER}


def h_get_transfer(store, actor, now, match, payload):
    transfer = store.get_transfer(match.group("transfer_ref"))
    if transfer is None:
        raise DomainError(404, "transfer_not_found", "移送记录不存在")
    if actor.role == "unit" and actor.unit_kind not in (
        transfer["from_unit_kind"], transfer["to_unit_kind"]
    ):
        raise DomainError(403, "not_involved", "仅移送双方单位可查看")
    _require(actor, "registrar", "unit", "supervisor")
    return 200, transfer


def h_confirm_transfer(store, actor, now, match, payload):
    _require(actor, "unit")
    transfer = store.get_transfer(match.group("transfer_ref"))
    if transfer is None:
        raise DomainError(404, "transfer_not_found", "移送记录不存在")
    side = payload.get("side")
    moment = domain.parse_iso(now)
    if payload.get("decision") == "reject":
        updates = domain.reject_transfer(transfer, actor.unit_kind, moment)
        store.update_transfer(transfer["transfer_ref"], **updates)
        return 200, {"transfer_ref": transfer["transfer_ref"], **updates}

    updates = domain.confirm_transfer(transfer, side, actor.unit_kind, moment)
    store.update_transfer(transfer["transfer_ref"], **updates)

    if updates["status"] == domain.TRANSFER_COMPLETED:
        case_ref = transfer["case_ref"]
        # 交出节点关闭，接收节点激活；无对应节点时另起版本追加。
        if transfer["from_node_ref"]:
            store.update_node(transfer["from_node_ref"],
                              status=domain.NODE_TRANSFERRED, left_at=now)
        version = store.current_version(case_ref)
        target = None
        if version:
            target = next(
                (n for n in store.nodes_of_version(version["version_ref"])
                 if n["unit_kind"] == transfer["to_unit_kind"]
                 and n["status"] == domain.NODE_PENDING),
                None,
            )
        if target:
            store.update_node(target["node_ref"], status=domain.NODE_ACTIVE, entered_at=now)
        else:
            version_ref = _branch_version(store, case_ref,
                                          f"移送至{transfer['to_unit_kind']}：{transfer['kind']}", now)
            nodes = store.nodes_of_version(version_ref)
            store.insert_node({
                "node_ref": domain.new_ref("NODE"),
                "version_ref": version_ref,
                "case_ref": case_ref,
                "seq": (nodes[-1]["seq"] + 1) if nodes else 1,
                "unit_kind": transfer["to_unit_kind"],
                "action": transfer["kind"],
                "reason": transfer["reason"] or "移送接收确认后立案",
                "planned_days": None,
                "status": domain.NODE_ACTIVE,
                "entered_at": now,
            })
        # 权限与保密级别随程序变化：接收单位获得授权，案件保密级别就高。
        store.insert_grant({
            "grant_ref": domain.new_ref("GRANT"),
            "case_ref": case_ref,
            "grantee_kind": "unit",
            "grantee_ref": transfer["to_unit_kind"],
            "scopes": json.dumps(["progress", "materials"], ensure_ascii=False),
            "evidence_refs": transfer["materials"],
            "valid_from": now,
        })
        case = store.get_case(case_ref)
        levels = domain.CONFIDENTIALITY_LEVELS
        if levels.index(transfer["confidentiality"]) > levels.index(case["confidentiality"]):
            store.update_case(case_ref, confidentiality=transfer["confidentiality"])
    return 200, {"transfer_ref": transfer["transfer_ref"], **updates}


def h_case_action(store, actor, now, match, payload):
    """撤回、部分和解、管辖异议、跨境送达：均保留旧路径。"""
    _require(actor, "registrar", "unit")
    case_ref = match.group("case_ref")
    case = _get_case(store, case_ref)
    if actor.role == "unit":
        _require_unit_grant(store, actor, case_ref)
    action = payload.get("action")

    if action == "withdraw":
        for claim_ref in payload.get("claim_refs") or []:
            claim = store.get_claim(claim_ref)
            if claim is None or claim["case_ref"] != case_ref:
                raise DomainError(404, "claim_not_found", f"请求不存在：{claim_ref}")
            store.update_claim(claim_ref, status=domain.CLAIM_WITHDRAWN)
        remaining = [c for c in store.claims_of_case(case_ref)
                     if c["status"] == domain.CLAIM_ACTIVE]
        if not payload.get("claim_refs") or not remaining:
            _close_case(store, case_ref, domain.CASE_WITHDRAWN, now)
        return 200, {"case_ref": case_ref, "status": store.get_case(case_ref)["status"]}

    if action == "partial_settlement":
        claim_refs = payload.get("claim_refs") or []
        if not claim_refs:
            raise DomainError(400, "claims_required", "部分和解须指明请求")
        for claim_ref in claim_refs:
            claim = store.get_claim(claim_ref)
            if claim is None or claim["case_ref"] != case_ref:
                raise DomainError(404, "claim_not_found", f"请求不存在：{claim_ref}")
            store.update_claim(claim_ref, status=domain.CLAIM_SETTLED)
        remaining = [c for c in store.claims_of_case(case_ref)
                     if c["status"] == domain.CLAIM_ACTIVE]
        if remaining:
            # 剩余请求继续推进，另起版本说明缘由，旧路径保留。
            _branch_version(store, case_ref,
                            f"部分和解：{len(claim_refs)} 项请求和解，剩余 {len(remaining)} 项继续", now)
        else:
            _close_case(store, case_ref, domain.CASE_SETTLED, now)
        return 200, {"case_ref": case_ref, "status": store.get_case(case_ref)["status"],
                     "remaining_claims": len(remaining)}

    if action == "jurisdiction_objection":
        version_ref = _branch_version(store, case_ref,
                                      f"管辖异议：{payload.get('reason', '当事人提出')}", now)
        nodes = store.nodes_of_version(version_ref)
        for node in nodes:
            if node["status"] == domain.NODE_ACTIVE:
                store.update_node(node["node_ref"], status=domain.NODE_PAUSED)
        store.insert_node({
            "node_ref": domain.new_ref("NODE"),
            "version_ref": version_ref,
            "case_ref": case_ref,
            "seq": (nodes[-1]["seq"] + 1) if nodes else 1,
            "unit_kind": "court",
            "action": "管辖异议审查",
            "reason": "管辖异议成立前原程序暂停，旧路径保留备查",
            "planned_days": 15,
            "status": domain.NODE_ACTIVE,
            "entered_at": now,
        })
        store.update_case(case_ref, status=domain.CASE_OBJECTION)
        return 200, {"case_ref": case_ref, "status": domain.CASE_OBJECTION,
                     "version_ref": version_ref}

    if action == "cross_border_service":
        version_ref = _branch_version(store, case_ref, "跨境送达：涉外送达程序启动", now)
        nodes = store.nodes_of_version(version_ref)
        store.insert_node({
            "node_ref": domain.new_ref("NODE"),
            "version_ref": version_ref,
            "case_ref": case_ref,
            "seq": (nodes[-1]["seq"] + 1) if nodes else 1,
            "unit_kind": "justice_admin",
            "action": "跨境送达协助",
            "reason": "涉外送达依海牙送达公约或司法协助途径办理，期间不计入审理期限",
            "planned_days": 180,
            "status": domain.NODE_ACTIVE,
            "entered_at": now,
        })
        return 200, {"case_ref": case_ref, "version_ref": version_ref}

    raise DomainError(400, "action_unknown", f"未知操作：{action!r}")


def h_duplicate_decision(store, actor, now, match, payload):
    _require(actor, "registrar")
    flag = store.get_flag(match.group("flag_ref"))
    if flag is None:
        raise DomainError(404, "flag_not_found", "重复立案标记不存在")
    if flag["status"] != domain.FLAG_PENDING:
        raise DomainError(409, "flag_decided", "该标记已处理")
    decision = payload.get("decision")
    reason = payload.get("reason", "")
    if decision == "confirm":
        store.update_claim(flag["claim_ref"], status=domain.CLAIM_DUPLICATE,
                           duplicate_of=flag["matched_claim_ref"])
        store.update_flag(flag["flag_ref"], status=domain.FLAG_CONFIRMED,
                          decided_by=actor.ref, decided_at=now, decision_reason=reason)
    elif decision == "dismiss":
        if not reason:
            raise DomainError(400, "reason_required", "排除重复须说明理由")
        store.update_flag(flag["flag_ref"], status=domain.FLAG_DISMISSED,
                          decided_by=actor.ref, decided_at=now, decision_reason=reason)
    else:
        raise DomainError(400, "decision_unknown", "decision 须为 confirm/dismiss")
    return 200, store.get_flag(match.group("flag_ref"))


def h_ingest_event(store, actor, now, match, payload):
    """接收外部交换事件：保留来源发生时间，event_id 幂等。"""
    _require(actor, "registrar", "unit")
    required = ["event_id", "subject_ref", "occurred_at", "source_sequence", "payload_digest"]
    missing = [key for key in required if key not in payload]
    if missing:
        raise DomainError(400, "fields_missing", f"缺少字段：{missing}")
    domain.parse_iso(payload["occurred_at"])  # 来源发生时间必须带偏移量
    existing = store.get_event(payload["event_id"])
    if existing:
        return 200, {**existing, "duplicate": True}
    store.insert_event({
        "event_id": payload["event_id"],
        "subject_ref": payload["subject_ref"],
        "occurred_at": payload["occurred_at"],
        "source_unit": actor.unit_kind or actor.ref,
        "source_sequence": int(payload["source_sequence"]),
        "payload_digest": payload["payload_digest"],
        "recorded_at": now,
    })
    return 201, {"event_id": payload["event_id"], "recorded_at": now, "duplicate": False}


ROUTES = [
    ("GET", re.compile(r"^/health$"), h_health),
    ("POST", re.compile(r"^/parties$"), h_create_party),
    ("POST", re.compile(r"^/cases$"), h_create_case),
    ("POST", re.compile(r"^/cases/(?P<case_ref>[^/]+)/accept$"), h_accept_case),
    ("GET", re.compile(r"^/cases/(?P<case_ref>[^/]+)/path$"), h_case_path),
    ("GET", re.compile(r"^/cases/(?P<case_ref>[^/]+)/progress$"), h_case_progress),
    ("GET", re.compile(r"^/cases/(?P<case_ref>[^/]+)/materials$"), h_case_materials),
    ("GET", re.compile(r"^/cases/(?P<case_ref>[^/]+)/supervision$"), h_case_supervision),
    ("GET", re.compile(r"^/cases/(?P<case_ref>[^/]+)/deadlines$"), h_case_deadlines),
    ("POST", re.compile(r"^/cases/(?P<case_ref>[^/]+)/transfers$"), h_create_transfer),
    ("POST", re.compile(r"^/cases/(?P<case_ref>[^/]+)/actions$"), h_case_action),
    ("GET", re.compile(r"^/transfers/(?P<transfer_ref>[^/]+)$"), h_get_transfer),
    ("POST", re.compile(r"^/transfers/(?P<transfer_ref>[^/]+)/confirm$"), h_confirm_transfer),
    ("POST", re.compile(r"^/duplicates/(?P<flag_ref>[^/]+)/decision$"), h_duplicate_decision),
    ("POST", re.compile(r"^/events$"), h_ingest_event),
]


def handle_request(
    method: str,
    path: str,
    headers: dict,
    body: bytes | None,
    store: Store,
    now: str | None = None,
) -> tuple[int, dict]:
    """按路由处理一次请求，返回 (状态码, 响应体)。测试可直接调用。"""
    now = now or domain.fmt_iso(domain.now_utc())
    try:
        normalized = {str(k).lower(): v for k, v in dict(headers).items()}
        payload = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise DomainError(400, "body_invalid", "请求体须为 JSON")
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                actor = _build_actor(normalized) if handler is not h_health else None
                return handler(store, actor, now, match, payload)
        return 404, {"error": "not_found", "message": f"路径不存在：{method} {path}"}
    except DomainError as error:
        return error.status, {"error": error.code, "message": error.message, **error.extra}


class Handler(BaseHTTPRequestHandler):
    """处理基础 HTTP 请求。"""

    store: Store  # 由 main 注入

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        status, payload = handle_request(
            method, self.path.split("?", 1)[0], self.headers, body, self.server.store
        )
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    database_path = os.environ.get("DATABASE_PATH", "data/app.sqlite3")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.store = Store(database_path)
    server.serve_forever()


if __name__ == "__main__":
    main()
