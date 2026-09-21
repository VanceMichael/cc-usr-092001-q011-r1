"""程序衔接引擎的用例编排：登记受理、路径生成、移送交接、改道、期限与视图。"""

from __future__ import annotations

import re
import secrets

from ..domain import time_utils
from ..domain.deadlines import TollSegment, compute_deadline
from ..domain.enums import (
    CaseStatus,
    Confidentiality,
    DisputeType,
    EventKind,
    HandshakeState,
    NodeCode,
    StageStatus,
)
from ..domain.fingerprint import build_fingerprints
from ..domain.rules import (
    ALLOWED_ROUTES,
    UNITS,
    plan_path,
)
from ..storage.repo import Repository

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
CONF_ORDER = [c.value for c in (
    Confidentiality.PUBLIC, Confidentiality.RESTRICTED,
    Confidentiality.CONFIDENTIAL, Confidentiality.SECRET)]


class ServiceError(Exception):
    def __init__(self, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}


def _require(condition: bool, code: str, message: str,
             extra: dict | None = None) -> None:
    if not condition:
        raise ServiceError(code, message, extra)


class Engine:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ============ 1. 登记受理 ============

    def file_case(self, data: dict, *, force: bool = False) -> dict:
        """一口受理：登记最小身份资料、管辖连接点、请求事项、证据封存摘要与期限。"""
        now = time_utils.now()
        now_s = time_utils.format_value(now)

        dtype = self._parse_dispute(data.get("dispute_type"))
        parties = self._parse_parties(data.get("parties"))
        claims = self._parse_claims(data.get("claims"))
        connections = self._parse_connections(data.get("connections"))
        evidence = self._parse_evidence(data.get("evidence"))
        relation_ref = str(data.get("relation_ref", "")).strip()
        _require(bool(relation_ref), "relation_ref_required",
                 "必须填写法律关系稳定引用（航次号/合同号/事故编号），用于重复立案识别与管辖核对。")

        fps = build_fingerprints(dtype, parties, claims, relation_ref)
        dup = self.repo.find_duplicates(fps.hard, fps.soft)
        if dup["hard"] and not force:
            raise ServiceError(
                "duplicate_case",
                "识别到同一当事方、同一法律关系、相同请求事项的在办案件，已拦截重复立案；"
                "如确属新请求请核对请求事项后使用 force 重新提交。",
                {"duplicate_of": dup["hard"], "related": dup["soft"]},
            )

        case_ref = self._next_case_ref()
        plan_preview = plan_path(
            dtype, sorted(ALLOWED_ROUTES[dtype])[0],  # 仅取期限口径，路径稍后由当事人选择
        )
        case = {
            "case_ref": case_ref,
            "dispute_type": dtype.value,
            "status": CaseStatus.ACCEPTED.value,
            "relation_ref": relation_ref,
            "fp_hard": fps.hard,
            "fp_soft": fps.soft,
            "accepted_at": now_s,
            "limitation_days": plan_preview.limitation_days,
            "limitation_basis": plan_preview.limitation_basis,
            "created_at": now_s,
            "parties": parties,
            "claims": claims,
            "connections": connections,
            "evidence": evidence,
        }
        self.repo.insert_case(case)
        # 弱指纹案件只建立关联，不合并请求
        for other in dup["soft"]:
            self.repo.add_link(case_ref, other, "same_parties_distinct_claims")
        self.repo.append_event(case_ref, EventKind.CASE_FILED, now_s, "JUSTICE",
                               data.get("actor_ref"), {
                                   "dispute_type": dtype.value,
                                   "relation_ref": relation_ref,
                                   "duplicate_check": dup,
                                   "parties": parties,
                                   "claims": claims,
                                   "connections": connections,
                                   "evidence_digests": [
                                       {"evidence_ref": e["evidence_ref"], "digest": e["digest"],
                                        "confidentiality": e["confidentiality"]}
                                       for e in evidence],
                               })
        # 首端登记单位获得材料授权
        self.repo.grant(case_ref, "JUSTICE", "materials", Confidentiality.CONFIDENTIAL.value,
                        "SYSTEM", now_s)
        for p in parties:
            token = secrets.token_urlsafe(18)
            self.repo.upsert_party_token(p["party_ref"], token, now_s)
        self.repo.conn.commit()

        result = {
            "case_ref": case_ref,
            "status": CaseStatus.ACCEPTED.value,
            "accepted_at": now_s,
            "limitation": {
                "days": plan_preview.limitation_days,
                "basis": plan_preview.limitation_basis,
            },
            "duplicate_check": dup,
            "party_tokens": {p["party_ref"]: self._party_token(p["party_ref"])
                             for p in parties},
            "route_options": self.route_options(dtype),
        }
        return result

    def select_route(self, case_ref: str, route_key: str,
                     flags: dict | None = None, actor_ref: str | None = None) -> dict:
        """受理后依据争议类型与当事人选择生成可解释程序路径（首版）。"""
        case = self._require_case(case_ref)
        _require(case["status"] in (CaseStatus.ACCEPTED.value, CaseStatus.PARTIAL_SETTLED.value),
                 "case_not_open", f"案件当前状态 {case['status']}，不能选择路径。")
        existing = self.repo.get_active_route(case_ref)
        _require(existing is None, "route_exists",
                 "案件已有进行中的路径；改道请通过管辖异议或变更程序申请，旧路径将保留。")
        flags = flags or {}
        plan = plan_path(
            DisputeType(case["dispute_type"]), route_key,
            cross_border=bool(flags.get("cross_border")),
            procuratorial_support=bool(flags.get("procuratorial_support")),
            has_arbitration_clause=bool(flags.get("has_arbitration_clause")),
        )
        return self._persist_route(case_ref, plan, change_reason=None,
                                   actor_ref=actor_ref, unit="JUSTICE",
                                   event_kind=EventKind.PATH_PLANNED)

    def route_options(self, dtype: DisputeType) -> list[dict]:
        from ..domain.rules import ROUTES

        out = []
        for route in ROUTES[dtype]:
            out.append({
                "route_key": route.key,
                "name": route.name,
                "choice_explanation": route.choice_explanation,
                "segments": [s.node for s in route.segments],
            })
        return out

    # ============ 2. 移送：双向确认，期限随程序中止 ============

    def propose_transfer(self, case_ref: str, to_unit: str,
                         material_digest: str, actor_ref: str | None,
                         acting_unit: str,
                         material_items: list[str] | None = None) -> dict:
        case = self._require_case(case_ref)
        _require(to_unit in UNITS, "unknown_unit", f"未知的接收单位：{to_unit}")
        _require(bool(SHA256_RE.match(material_digest or "")), "bad_digest",
                 "移送材料包必须给出 sha256 摘要，供接收方核对一致。")
        route = self.repo.get_active_route(case_ref)
        _require(route is not None, "no_route", "尚未生成程序路径，不能移送。")
        stages = self.repo.list_stages(route["id"])
        current = next((s for s in stages if s["status"] == StageStatus.ACTIVE.value), None)
        _require(current is not None, "no_active_stage", "当前没有活动节点可供移交。")
        _require(current["unit"] == acting_unit, "forbidden",
                 f"当前节点责任单位是 {current['unit']}，只有该单位可以发起移送。")
        _require(current["unit"] != to_unit, "same_unit", "不能向本单位移送。")
        nxt = next((s for s in stages if s["seq"] == current["seq"] + 1
                    and s["status"] == StageStatus.PENDING.value), None)
        # 允许跳到下一节点：接收单位必须与路径设计一致；同单位串行节点不需要移送
        _require(nxt is not None and nxt["unit"] == to_unit,
                 "off_route_transfer",
                 "接收单位与当前路径的下一责任单位不一致；如管辖有变请先走管辖异议改道。")

        now_s = time_utils.format_value(time_utils.now())
        transfer_ref = f"TRF-{case_ref}-{secrets.token_hex(4).upper()}"
        toll_id = self.repo.open_toll(
            case_ref, f"移送 {current['unit']}→{to_unit} 等待双向签收", now_s, actor_ref)
        transfer_id = self.repo.insert_transfer({
            "transfer_ref": transfer_ref,
            "case_ref": case_ref,
            "from_stage_id": current["id"],
            "to_stage_id": nxt["id"],
            "from_unit": current["unit"],
            "to_unit": to_unit,
            "material_digest": material_digest,
            "toll_id": toll_id,
            "proposed_at": now_s,
            "proposed_by": actor_ref,
        })
        self.repo.append_event(case_ref, EventKind.TRANSFER_PROPOSED, now_s,
                               current["unit"], actor_ref, {
                                   "transfer_ref": transfer_ref,
                                   "from_unit": current["unit"],
                                   "to_unit": to_unit,
                                   "from_stage_seq": current["seq"],
                                   "to_stage_seq": nxt["seq"],
                                   "material_digest": material_digest,
                                   "material_items": material_items or [],
                                   "deadline_toll": "移送等待签收期间法定期间中止",
                               })
        self.repo.conn.commit()
        return {"transfer_ref": transfer_ref, "state": HandshakeState.PROPOSED.value,
                "from_unit": current["unit"], "to_unit": to_unit,
                "toll_started_at": now_s}

    def respond_transfer(self, transfer_ref: str, accept: bool,
                         actor_ref: str | None, acting_unit: str,
                         note: str | None = None,
                         material_digest: str | None = None) -> dict:
        t = self.repo.get_transfer(transfer_ref)
        _require(t is not None, "not_found", f"移送单不存在：{transfer_ref}")
        _require(t["to_unit"] == acting_unit, "forbidden",
                 f"该移送单的接收单位是 {t['to_unit']}，须由其签收或拒收。")
        _require(t["state"] == HandshakeState.PROPOSED.value, "transfer_closed",
                 f"移送单状态为 {t['state']}，不能重复响应。")
        now_s = time_utils.format_value(time_utils.now())

        if accept:
            if material_digest is not None:
                _require(material_digest == t["material_digest"], "digest_mismatch",
                         "接收方核对材料摘要与交出方不一致，已拒绝签收并退回补正。",
                         {"expected": t["material_digest"], "actual": material_digest})
            self.repo.respond_transfer(transfer_ref, HandshakeState.RECEIVED.value,
                                       now_s, actor_ref, note)
            self.repo.close_toll(t["toll_id"], now_s, actor_ref)
            self.repo.mark_stage(t["from_stage_id"], StageStatus.HANDED_OFF.value, now_s)
            self.repo.mark_stage(t["to_stage_id"], StageStatus.ACTIVE.value, now_s)
            # 接收单位获得与程序相称的材料授权（保密级别随程序变化）
            to_stage = self.repo.get_stage(t["to_stage_id"])
            ceiling = self._ceiling_for_node(to_stage["node"])
            self.repo.grant(t["case_ref"], t["to_unit"], "materials", ceiling,
                            actor_ref or t["to_unit"], now_s)
            self.repo.append_event(t["case_ref"], EventKind.TRANSFER_RECEIVED, now_s,
                                   t["to_unit"], actor_ref, {
                                       "transfer_ref": transfer_ref,
                                       "note": note,
                                       "toll_resumed_at": now_s,
                                       "confidentiality_ceiling": ceiling,
                                   })
            state = HandshakeState.RECEIVED.value
        else:
            self.repo.respond_transfer(transfer_ref, HandshakeState.REJECTED.value,
                                       now_s, actor_ref, note)
            self.repo.close_toll(t["toll_id"], now_s, actor_ref)
            self.repo.append_event(t["case_ref"], EventKind.TRANSFER_REJECTED, now_s,
                                   t["to_unit"], actor_ref,
                                   {"transfer_ref": transfer_ref, "note": note})
            state = HandshakeState.REJECTED.value
        self.repo.conn.commit()
        return {"transfer_ref": transfer_ref, "state": state, "responded_at": now_s}

    # ============ 3. 撤回 / 部分和解 ============

    def withdraw_claims(self, case_ref: str, claim_seqs: list[int],
                        actor_ref: str | None, reason: str | None = None) -> dict:
        case = self._require_case(case_ref)
        valid_seqs = {c["claim_seq"] for c in self.repo.list_claims(case_ref)}
        seqs = sorted(set(claim_seqs))
        _require(seqs and all(s in valid_seqs for s in seqs), "bad_claims",
                 "存在不属于本案的请求序号。")
        now_s = time_utils.format_value(time_utils.now())
        self.repo.update_claim_status(case_ref, seqs, "withdrawn", now_s)
        remaining = [c for c in self.repo.list_claims(case_ref) if c["status"] == "active"]
        new_status = CaseStatus.WITHDRAWN.value if not remaining else case["status"]
        if not remaining:
            self.repo.set_case_status(case_ref, CaseStatus.WITHDRAWN.value)
            self._void_active_path(case_ref, now_s, actor_ref)
        self.repo.append_event(case_ref, EventKind.CLAIM_WITHDRAWN, now_s, None,
                               actor_ref, {"claim_seqs": seqs, "reason": reason,
                                           "remaining_claims": len(remaining)})
        self.repo.conn.commit()
        return {"case_ref": case_ref, "withdrawn": seqs,
                "status": new_status, "remaining_claims": len(remaining)}

    def partial_settlement(self, case_ref: str, claim_seqs: list[int],
                           actor_ref: str | None, agreement_ref: str | None = None) -> dict:
        """部分和解：和解的请求可转司法确认，其余请求沿旧路径继续，禁止全案合并结案。"""
        case = self._require_case(case_ref)
        valid_seqs = {c["claim_seq"] for c in self.repo.list_claims(case_ref)
                      if c["status"] == "active"}
        seqs = sorted(set(claim_seqs))
        _require(seqs and all(s in valid_seqs for s in seqs), "bad_claims",
                 "只能对在审的活动请求登记和解。")
        now_s = time_utils.format_value(time_utils.now())
        self.repo.update_claim_status(case_ref, seqs, "settled", now_s)
        remaining = [c for c in self.repo.list_claims(case_ref) if c["status"] == "active"]
        if remaining:
            self.repo.set_case_status(case_ref, CaseStatus.PARTIAL_SETTLED.value)
        else:
            self.repo.set_case_status(case_ref, CaseStatus.CLOSED.value)
        self.repo.append_event(case_ref, EventKind.PARTIAL_SETTLEMENT, now_s, "MED",
                               actor_ref,
                               {"settled_claims": seqs, "agreement_ref": agreement_ref,
                                "remaining_claims": [c["claim_seq"] for c in remaining],
                                "note": "已和解请求可申请司法确认；其余请求沿原路径继续，旧路径保留。"})
        self.repo.conn.commit()
        return {"case_ref": case_ref, "settled_claims": seqs,
                "remaining_claims": [c["claim_seq"] for c in remaining],
                "status": self.repo.get_case(case_ref)["status"]}

    # ============ 4. 管辖异议：旧路径保留，审查期间中止，成立则改道 ============

    def file_jurisdiction_objection(self, case_ref: str, actor_ref: str | None,
                                    reason: str | None = None) -> dict:
        """提出管辖异议：审查期间法定期间中止，原路径继续挂起并保留。"""
        self._require_case(case_ref)
        now_s = time_utils.format_value(time_utils.now())
        open_toll = self.repo.open_toll(case_ref, "管辖异议审查期间期限中止", now_s, actor_ref)
        self.repo.append_event(case_ref, EventKind.JURISDICTION_OBJECTION, now_s,
                               None, actor_ref,
                               {"reason": reason, "toll_id": open_toll,
                                "toll": "审查期间期限中止，原路径保留挂起"})
        self.repo.conn.commit()
        return {"case_ref": case_ref, "objection": "filed",
                "toll_started_at": now_s, "toll_id": open_toll}

    def rule_jurisdiction_objection(self, case_ref: str, upheld: bool,
                                    actor_ref: str | None,
                                    new_route_key: str | None = None,
                                    reason: str | None = None,
                                    flags: dict | None = None) -> dict:
        """裁定管辖异议：驳回则恢复原路径；成立则旧路径置 superseded 并生成新路径。"""
        case = self._require_case(case_ref)
        open_tolls = [t for t in self.repo.list_tolls(case_ref)
                      if t["end_at"] is None and "管辖异议" in t["reason"]]
        _require(open_tolls, "no_open_objection", "没有审查中的管辖异议，不能作出裁定。")
        now_s = time_utils.format_value(time_utils.now())
        for t in open_tolls:
            self.repo.close_toll(t["id"], now_s, actor_ref)

        new_version = None
        if upheld:
            _require(new_route_key is not None, "new_route_required",
                     "管辖异议成立时必须给出新的程序路径。")
            plan = plan_path(
                DisputeType(case["dispute_type"]), new_route_key,
                cross_border=bool((flags or {}).get("cross_border")),
                procuratorial_support=bool((flags or {}).get("procuratorial_support")),
                has_arbitration_clause=bool((flags or {}).get("has_arbitration_clause")),
            )
            self.repo.supersede_route(case_ref, now_s)
            persisted = self._persist_route(
                case_ref, plan,
                change_reason=f"管辖异议成立，改道：{reason or '移送有管辖权机关受理'}",
                actor_ref=actor_ref, unit="COURT",
                event_kind=EventKind.PATH_CHANGED,
                extra_payload={"supersedes": "previous"})
            new_version = persisted["version"]

        self.repo.append_event(case_ref, EventKind.JURISDICTION_RULING, now_s, "COURT",
                               actor_ref,
                               {"upheld": upheld, "reason": reason,
                                "new_route_version": new_version,
                                "toll_resumed_at": None if upheld else now_s})
        self.repo.conn.commit()
        return {"case_ref": case_ref, "upheld": upheld,
                "new_route_version": new_version, "resumed_at": now_s}

    # ============ 5. 跨境送达：等待回证期间持续中止，旧路径保留 ============

    def start_cross_border_service(self, case_ref: str, actor_ref: str | None,
                                   channel: str, target_ref: str) -> dict:
        case = self._require_case(case_ref)
        now_s = time_utils.format_value(time_utils.now())
        self.repo.open_toll(case_ref, f"跨境送达（{channel}）等待域外回证", now_s, actor_ref)
        self.repo.append_event(case_ref, EventKind.CROSS_BORDER_SERVICE, now_s, "COURT",
                               actor_ref, {"channel": channel, "target_ref": target_ref,
                                           "toll": "等待域外回证期间期限中止，旧路径保留"})
        self.repo.conn.commit()
        return {"case_ref": case_ref, "service": "started", "started_at": now_s}

    def complete_cross_border_service(self, case_ref: str, proof_digest: str,
                                      actor_ref: str | None) -> dict:
        _require(bool(SHA256_RE.match(proof_digest or "")), "bad_digest",
                 "回证必须附 sha256 摘要。")
        now_s = time_utils.format_value(time_utils.now())
        open_tolls = [t for t in self.repo.list_tolls(case_ref)
                      if t["end_at"] is None and "跨境送达" in t["reason"]]
        _require(open_tolls, "no_open_service", "没有进行中的跨境送达。")
        for t in open_tolls:
            self.repo.close_toll(t["id"], now_s, actor_ref)
        self.repo.append_event(case_ref, EventKind.CROSS_BORDER_SERVICE, now_s, "COURT",
                               actor_ref, {"proof_digest": proof_digest,
                                           "toll_resumed_at": now_s})
        self.repo.conn.commit()
        return {"case_ref": case_ref, "service": "completed", "resumed_at": now_s}

    # ============ 6. 节点办结 / 案件办结 ============

    def close_active_stage(self, case_ref: str, actor_ref: str | None,
                           acting_unit: str,
                           outcome_ref: str | None = None) -> dict:
        case = self._require_case(case_ref)
        route = self.repo.get_active_route(case_ref)
        _require(route is not None, "no_route", "尚未生成程序路径。")
        stages = self.repo.list_stages(route["id"])
        current = next((s for s in stages if s["status"] == StageStatus.ACTIVE.value), None)
        _require(current is not None, "no_active_stage", "没有活动节点。")
        _require(current["unit"] == acting_unit, "forbidden",
                 f"当前节点责任单位是 {current['unit']}，只有该单位可以办结节点。")
        nxt = next((s for s in stages if s["seq"] == current["seq"] + 1), None)
        if nxt is not None and nxt["unit"] != current["unit"]:
            # 责任尚未交接，不能直接办结：先发起移送，接收方签收时本节点才置 handed_off
            raise ServiceError(
                "handoff_required",
                f"下一节点 {nxt['label']} 的责任单位是 {nxt['unit']}，"
                "须先发起移送并经双向签收；签收前本节点继续承担责任。",
                {"next_unit": nxt["unit"], "to_stage_seq": nxt["seq"]})
        now_s = time_utils.format_value(time_utils.now())
        self.repo.mark_stage(current["id"], StageStatus.CLOSED.value, now_s)
        next_info = None
        if nxt is not None:
            self.repo.mark_stage(nxt["id"], StageStatus.ACTIVE.value, now_s)
            next_info = {"seq": nxt["seq"], "node": nxt["node"], "unit": nxt["unit"]}
        self.repo.append_event(case_ref, EventKind.STAGE_CLOSED, now_s,
                               current["unit"], actor_ref,
                               {"stage_seq": current["seq"], "node": current["node"],
                                "outcome_ref": outcome_ref, "next": next_info})
        self.repo.conn.commit()
        return {"case_ref": case_ref, "closed_stage": current["seq"],
                "next_stage": nxt["seq"] if nxt else None}

    def close_case(self, case_ref: str, actor_ref: str | None, reason: str) -> dict:
        self._require_case(case_ref)
        now_s = time_utils.format_value(time_utils.now())
        self.repo.set_case_status(case_ref, CaseStatus.CLOSED.value)
        route = self.repo.get_active_route(case_ref)
        if route:
            for s in self.repo.list_stages(route["id"]):
                if s["status"] in (StageStatus.ACTIVE.value, StageStatus.PENDING.value):
                    self.repo.mark_stage(s["id"], StageStatus.CLOSED.value, now_s)
        self.repo.append_event(case_ref, EventKind.CASE_CLOSED, now_s, None,
                               actor_ref, {"reason": reason})
        self.repo.conn.commit()
        return {"case_ref": case_ref, "status": CaseStatus.CLOSED.value}

    # ============ 7. 视图 ============

    def deadline_view(self, case_ref: str, as_of: str | None = None) -> dict:
        case = self._require_case(case_ref)
        tolls = [TollSegment(t["start_at"], t["end_at"], t["reason"])
                 for t in self.repo.list_tolls(case_ref)]
        calc = compute_deadline(case["accepted_at"], case["limitation_days"], tolls,
                                as_of=as_of)
        return {"case_ref": case_ref, "limitation_days": case["limitation_days"],
                "limitation_basis": case["limitation_basis"], **calc,
                "toll_segments": [
                    {"reason": t.reason, "start": t.start,
                     "end": t.end, "open": t.end is None} for t in tolls]}

    def party_view(self, case_ref: str, party_ref: str, as_of: str | None = None) -> dict:
        """公众端：只能看到自身案件进度，不暴露其他协同单位的内部材料。"""
        case = self._require_case(case_ref)
        parties = [dict(p) for p in self.repo.list_parties(case_ref)]
        _require(any(p["party_ref"] == party_ref for p in parties), "forbidden",
                 "无权查看该案件。")
        route = self.repo.get_active_route(case_ref)
        stages_out = []
        if route:
            stages = self.repo.list_stages(route["id"])
            for s in stages:
                stages_out.append({
                    "seq": s["seq"], "node": s["node"], "label": s["label"],
                    "unit": s["unit"], "status": s["status"],
                    "entered_at": s["entered_at"], "closed_at": s["closed_at"],
                    "reason": s["reason"],
                })
        return {
            "case_ref": case_ref,
            "status": case["status"],
            "dispute_type": case["dispute_type"],
            "relation_ref": case["relation_ref"],
            "accepted_at": case["accepted_at"],
            "route": ({"version": route["version"], "route_name": route["route_name"],
                       "choice_explanation": route["choice_explanation"],
                       "stages": stages_out} if route else None),
            "claims": [{"seq": c["claim_seq"], "kind": c["claim_kind"],
                        "status": c["status"]} for c in self.repo.list_claims(case_ref)],
            "deadline": self.deadline_view(case_ref, as_of=as_of),
            "timeline": [{"seq": e["seq"], "kind": e["kind"],
                          "occurred_at": e["occurred_at"]}
                         for e in self.repo.list_events(case_ref)],
        }

    def unit_view(self, case_ref: str, unit: str, as_of: str | None = None) -> dict:
        """协同单位：仅查看获授权材料，保密级别超过授权上限的字段做脱敏。"""
        case = self._require_case(case_ref)
        grant = self.repo.unit_grant(case_ref, unit)
        _require(grant is not None, "forbidden",
                 f"单位 {unit} 未获得本案授权，不可查看。")
        ceiling = grant["confidentiality_ceiling"]
        route = self.repo.get_active_route(case_ref)
        evidence = []
        for e in self.repo.list_evidence(case_ref):
            visible = CONF_ORDER.index(e["confidentiality"]) <= CONF_ORDER.index(ceiling)
            evidence.append({
                "seq": e["seq"], "evidence_ref": e["evidence_ref"],
                "digest": e["digest"], "holder_unit": e["holder_unit"],
                "sealed_at": e["sealed_at"],
                "confidentiality": e["confidentiality"],
                "note": e["note"] if visible else "超出本单位授权保密级别，已屏蔽",
            })
        return {
            "case_ref": case_ref, "status": case["status"],
            "dispute_type": case["dispute_type"],
            "grant": {"unit": unit, "scope": grant["scope"],
                      "confidentiality_ceiling": ceiling},
            "connections": [{"point_type": r["point_type"], "point_ref": r["point_ref"]}
                            for r in self.repo.list_connections(case_ref)],
            "evidence": evidence,
            "deadline": self.deadline_view(case_ref, as_of=as_of),
            "route_history": [
                {"version": r["version"], "route_key": r["route_key"],
                 "route_name": r["route_name"], "status": r["status"],
                 "change_reason": r["change_reason"], "created_at": r["created_at"],
                 "stages": [{"seq": s["seq"], "node": s["node"], "unit": s["unit"],
                             "label": s["label"], "status": s["status"],
                             "entered_at": s["entered_at"], "closed_at": s["closed_at"],
                             "time_limit_days": s["time_limit_days"]}
                            for s in self.repo.list_stages(r["id"])]}
                for r in self.repo.list_routes(case_ref)
            ],
            "active_route_version": route["version"] if route else None,
            "transfers": self._transfers_of(case_ref),
            "events": [{"seq": e["seq"], "event_id": e["event_id"], "kind": e["kind"],
                        "occurred_at": e["occurred_at"], "unit": e["unit"]}
                       for e in self.repo.list_events(case_ref)],
        }

    def supervision_view(self, as_of: str | None = None) -> dict:
        """监督人员：按任一节点核对实际停留时间、效力状态和下一责任方。"""
        now_dt = time_utils.parse(as_of) if as_of else time_utils.now()
        items = []
        for r in self.repo.supervision_rows():
            entered = time_utils.parse(r["entered_at"]) if r["entered_at"] else None
            dwell = (now_dt - entered).total_seconds() / 86400 if entered else None
            overdue = None
            if dwell is not None and r["time_limit_days"] is not None:
                overdue = dwell > r["time_limit_days"]
            items.append({
                "case_ref": r["case_ref"], "dispute_type": r["dispute_type"],
                "case_status": r["case_status"], "route_version": r["version"],
                "stage_seq": r["stage_seq"], "node": r["node"], "label": r["label"],
                "current_unit": r["unit"], "entered_at": r["entered_at"],
                "dwell_days": round(dwell, 2) if dwell is not None else None,
                "time_limit_days": r["time_limit_days"],
                "overdue": overdue,
                "next_responsible_unit": self._next_unit(r),
                "effect": self._effect_label(r["case_status"]),
            })
        pending = []
        for t in self.repo.pending_transfers():
            age = (now_dt - time_utils.parse(t["proposed_at"])).total_seconds() / 86400
            pending.append({"transfer_ref": t["transfer_ref"], "case_ref": t["case_ref"],
                            "from_unit": t["from_unit"], "to_unit": t["to_unit"],
                            "proposed_at": t["proposed_at"],
                            "waiting_days": round(age, 2)})
        return {"as_of": time_utils.format_value(now_dt),
                "active_nodes": items, "pending_transfers": pending}

    # ============ 8. 外部事件接入（保留原始发生时间） ============

    def ingest_event(self, payload: dict) -> dict:
        """接收共建单位交换事件：幂等去重，source_sequence 同源递增校验，不覆盖 occurred_at。"""
        for key in ("event_id", "subject_ref", "source_unit", "source_sequence",
                    "occurred_at", "payload_digest"):
            _require(payload.get(key) is not None, "invalid_event", f"事件缺少字段：{key}")
        _require(payload["source_unit"] in UNITS, "unknown_unit",
                 f"未知来源单位：{payload['source_unit']}")
        occurred = time_utils.parse(str(payload["occurred_at"]))
        now_s = time_utils.format_value(time_utils.now())

        existing = self.repo.conn.execute(
            "SELECT event_id FROM inbound_events WHERE event_id = ?",
            (payload["event_id"],)).fetchone()
        if existing:
            return {"event_id": payload["event_id"], "deduplicated": True}
        prev = self.repo.conn.execute(
            """SELECT MAX(source_sequence) AS m FROM inbound_events
               WHERE source_unit = ?""", (payload["source_unit"],)).fetchone()
        if prev["m"] is not None and int(payload["source_sequence"]) <= prev["m"]:
            raise ServiceError("sequence_regression",
                               "来源序号必须在同一来源内递增；乱序事件请先补登缺口。")
        self.repo.conn.execute(
            """INSERT INTO inbound_events(event_id, subject_ref, source_unit,
                  source_sequence, occurred_at, received_at, payload_digest)
               VALUES(?,?,?,?,?,?,?)""",
            (payload["event_id"], payload["subject_ref"], payload["source_unit"],
             int(payload["source_sequence"]), time_utils.format_value(occurred),
             now_s, payload["payload_digest"]))
        # 若事件指向在办案件，追加到案内账本，保留原始发生时间
        case = self.repo.get_case(payload["subject_ref"])
        if case:
            self.repo.append_event(case["case_ref"], payload.get("kind", "external_notice"),
                                   time_utils.format_value(occurred),
                                   payload["source_unit"], payload.get("actor_ref"),
                                   {"external_event_id": payload["event_id"],
                                    "payload_digest": payload["payload_digest"],
                                    "source_sequence": int(payload["source_sequence"])})
        self.repo.conn.commit()
        return {"event_id": payload["event_id"], "deduplicated": False,
                "occurred_at": time_utils.format_value(occurred), "received_at": now_s}

    # ============ 内部辅助 ============

    def _persist_route(self, case_ref, plan, *, change_reason, actor_ref, unit,
                       event_kind, extra_payload=None) -> dict:
        now_s = time_utils.format_value(time_utils.now())
        version_row = self.repo.conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM routes WHERE case_ref = ?",
            (case_ref,)).fetchone()
        route_id = self.repo.insert_route(case_ref, {
            "version": version_row["v"],
            "route_key": plan.route_key,
            "route_name": plan.route_name,
            "choice_explanation": plan.choice_explanation,
            "warnings": plan.warnings,
            "created_at": now_s,
            "change_reason": change_reason,
            "segments": [
                {"node": s.node.value, "unit": s.unit, "label": s.label,
                 "legal_basis": s.legal_basis, "reason": s.reason,
                 "time_limit_days": s.time_limit_days, "optional": s.optional}
                for s in plan.segments],
        })
        # 路径涉及的单位获得进度授权（正式移送签收后才升级为材料授权）
        for seg in plan.segments:
            self.repo.grant(case_ref, seg.unit, "progress", Confidentiality.PUBLIC.value,
                            actor_ref or "SYSTEM", now_s)
        payload = {
            "version": version_row["v"], "route_key": plan.route_key,
            "route_name": plan.route_name,
            "choice_explanation": plan.choice_explanation,
            "warnings": list(plan.warnings),
            "segments": [{"seq": i + 1, "node": s.node.value, "unit": s.unit,
                          "label": s.label, "legal_basis": s.legal_basis,
                          "reason": s.reason, "time_limit_days": s.time_limit_days,
                          "optional": s.optional}
                         for i, s in enumerate(plan.segments)],
            "limitation_days": plan.limitation_days,
            "limitation_basis": plan.limitation_basis,
        }
        if extra_payload:
            payload.update(extra_payload)
        self.repo.append_event(case_ref, event_kind, now_s, unit, actor_ref, payload)
        self.repo.conn.commit()
        return {"route_id": route_id, "version": version_row["v"],
                "route_key": plan.route_key, "route_name": plan.route_name,
                "choice_explanation": plan.choice_explanation,
                "warnings": list(plan.warnings),
                "segments": payload["segments"],
                "limitation_days": plan.limitation_days,
                "limitation_basis": plan.limitation_basis}

    def _void_active_path(self, case_ref: str, at: str, actor_ref: str | None) -> None:
        route = self.repo.get_active_route(case_ref)
        if not route:
            return
        self.repo.conn.execute(
            "UPDATE routes SET status = 'void' WHERE id = ?", (route["id"],))
        self.repo.conn.execute(
            "UPDATE stages SET status = 'void', closed_at = COALESCE(closed_at, ?) "
            "WHERE route_id = ? AND status IN ('active','pending')", (at, route["id"]))

    def _ceiling_for_node(self, node: str) -> str:
        if node == NodeCode.LITIGATION.value:
            return Confidentiality.CONFIDENTIAL.value
        if node == NodeCode.CROSS_BORDER_SERVICE.value:
            return Confidentiality.SECRET.value
        if node in (NodeCode.PROCURATORIAL.value, NodeCode.JUDICIAL_CONFIRMATION.value):
            return Confidentiality.CONFIDENTIAL.value
        return Confidentiality.RESTRICTED.value

    def _next_unit(self, row) -> str | None:
        nxt = self.repo.conn.execute(
            "SELECT unit FROM stages WHERE route_id = ? AND seq > ? "
            "AND status NOT IN ('void','superseded') ORDER BY seq LIMIT 1",
            (row["route_id"], row["stage_seq"])).fetchone()
        return nxt["unit"] if nxt else None

    def _effect_label(self, case_status: str) -> str:
        return {
            CaseStatus.ACCEPTED.value: "程序进行中，行为有效",
            CaseStatus.PARTIAL_SETTLED.value: "部分和解生效，剩余请求继续审理",
            CaseStatus.WITHDRAWN.value: "撤回终止，原程序行为留痕不再执行",
            CaseStatus.CLOSED.value: "已终局办结",
        }.get(case_status, case_status)

    def _transfers_of(self, case_ref: str) -> list[dict]:
        rows = self.repo.conn.execute(
            "SELECT * FROM transfers WHERE case_ref = ? ORDER BY id", (case_ref,)).fetchall()
        return [dict(t) for t in rows]

    def _party_token(self, party_ref: str) -> str | None:
        row = self.repo.conn.execute(
            "SELECT token FROM party_tokens WHERE party_ref = ?", (party_ref,)).fetchone()
        return row["token"] if row else None

    def _require_case(self, case_ref: str):
        case = self.repo.get_case(case_ref)
        _require(case is not None, "not_found", f"案件不存在：{case_ref}")
        return case

    def _next_case_ref(self) -> str:
        row = self.repo.conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()
        year = time_utils.now().year
        return f"CASE-{year}-{row['n'] + 1:05d}"

    # ---- 入参校验 ----

    def _parse_dispute(self, value) -> DisputeType:
        try:
            return DisputeType(value)
        except ValueError:
            raise ServiceError("bad_dispute_type",
                               f"不支持的争议类型：{value}；"
                               f"可选：{[t.value for t in DisputeType]}")

    def _parse_parties(self, value) -> list[dict]:
        _require(isinstance(value, list) and len(value) >= 2, "parties_required",
                 "至少登记申请人与被申请人两方的最小身份资料。")
        roles = set()
        out = []
        for p in value:
            ref = str(p.get("party_ref", "")).strip()
            role = str(p.get("role", "")).strip()
            _require(bool(REF_RE.match(ref)), "bad_party_ref",
                     f"当事方引用不合规：{ref!r}（3-64 位字母数字 ._-）")
            _require(role in {"claimant", "respondent", "third_party"}, "bad_role",
                     f"当事方角色必须为 claimant/respondent/third_party：{role}")
            roles.add(role)
            out.append({"party_ref": ref, "role": role,
                        "contact_ref": p.get("contact_ref")})
        _require("claimant" in roles and "respondent" in roles, "parties_required",
                 "必须同时包含 claimant 与 respondent。")
        _require(len({p["party_ref"] for p in out}) == len(out), "duplicate_party",
                 "当事方引用重复。")
        return out

    def _parse_claims(self, value) -> list[dict]:
        _require(isinstance(value, list) and value, "claims_required",
                 "至少填写一项请求事项。")
        out = []
        for c in value:
            kind = str(c.get("claim_kind", "")).strip()
            _require(bool(REF_RE.match(kind)), "bad_claim",
                     "请求事项需给出 claim_kind 稳定引用。")
            out.append({
                "claim_kind": kind,
                "subject_ref": c.get("subject_ref"),
                "period_start": c.get("period_start"),
                "period_end": c.get("period_end"),
                "amount_ref": c.get("amount_ref"),
            })
        return out

    def _parse_connections(self, value) -> list[dict]:
        _require(isinstance(value, list) and value, "connections_required",
                 "至少登记一个管辖连接点（事故发生地/船籍港/用人单位所在地/合同签订地等）。")
        allowed = {"accident_locale", "vessel_registry", "employer_locale",
                   "contract_place", "performance_locale", "domicile", "damage_locale"}
        out = []
        for pt in value:
            t = str(pt.get("point_type", "")).strip()
            ref = str(pt.get("point_ref", "")).strip()
            _require(t in allowed or t.startswith("x-"), "bad_connection",
                     f"管辖连接点类型不合规：{t}")
            _require(bool(ref), "bad_connection", "管辖连接点引用不能为空。")
            out.append({"point_type": t, "point_ref": ref})
        return out

    def _parse_evidence(self, value) -> list[dict]:
        value = value or []
        _require(isinstance(value, list), "bad_evidence", "证据封存摘要必须为列表。")
        now_s = time_utils.format_value(time_utils.now())
        out = []
        for e in value:
            ref = str(e.get("evidence_ref", "")).strip()
            digest = str(e.get("digest", "")).strip().lower()
            holder = str(e.get("holder_unit", "")).strip()
            conf = str(e.get("confidentiality", Confidentiality.RESTRICTED.value)).strip()
            _require(bool(REF_RE.match(ref)), "bad_evidence", "证据引用不合规。")
            _require(bool(SHA256_RE.match(digest)), "bad_digest",
                     f"证据 {ref} 必须提供 sha256: 开头的 64 位摘要。")
            _require(holder in UNITS, "bad_evidence", f"证据持有人必须是共建单位：{holder}")
            _require(conf in CONF_ORDER, "bad_confidentiality", f"保密级别不合规：{conf}")
            out.append({"evidence_ref": ref, "digest": digest, "holder_unit": holder,
                        "confidentiality": conf,
                        "sealed_at": e.get("sealed_at") or now_s,
                        "note": e.get("note")})
        return out
