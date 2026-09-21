"""引擎用例的端到端测试（SQLite 临时库）。"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from src.domain.enums import Confidentiality, DisputeType, HandshakeState, StageStatus
from src.services.engine import Engine, ServiceError
from src.storage.repo import Repository, connect, init_schema


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def case_payload(relation: str = "VOY-PL-2026-077",
                 claims=None, dtype: str = "maritime_accident",
                 evidence=None) -> dict:
    return {
        "dispute_type": dtype,
        "relation_ref": relation,
        "parties": [
            {"party_ref": "PARTY-CHEN-001", "role": "claimant",
             "contact_ref": "CONTACT-REF-001"},
            {"party_ref": "PARTY-SHIPCO-001", "role": "respondent"},
        ],
        "claims": claims if claims is not None else [
            {"claim_kind": "collision_damage", "subject_ref": "SUBJ-VESSEL-YUNHE02"}],
        "connections": [
            {"point_type": "accident_locale", "point_ref": "GEO-CANAL-KM83"}],
        "evidence": evidence if evidence is not None else [
            {"evidence_ref": "EVD-SEAL-001", "digest": sha("vdr-record"),
             "holder_unit": "MSA", "confidentiality": "restricted",
             "note": "航行数据记录仪封存"}],
    }


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test.sqlite3")
        conn = connect(self.db)
        init_schema(conn)
        self.engine = Engine(Repository(conn))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def file(self, **over):
        data = case_payload()
        data.update(over)
        return self.engine.file_case(data)

    # ---- 重复立案 ----

    def test_duplicate_hard_blocked_but_distinct_claims_linked(self) -> None:
        first = self.file()
        # 相同请求再次登记 → 拦截
        with self.assertRaises(ServiceError) as ctx:
            self.file()
        self.assertEqual(ctx.exception.code, "duplicate_case")
        self.assertEqual(ctx.exception.extra["duplicate_of"], [first["case_ref"]])

        # 同一航次、同一主体的不同请求 → 允许立案，只建立关联，不合并
        other = self.engine.file_case(case_payload(claims=[
            {"claim_kind": "cargo_loss", "subject_ref": "SUBJ-CONTAINER-77"}]))
        self.assertNotEqual(other["case_ref"], first["case_ref"])
        self.assertEqual(other["duplicate_check"]["soft"], [first["case_ref"]])
        links = self.engine.repo.conn.execute(
            "SELECT kind FROM case_links").fetchall()
        self.assertTrue(any(r["kind"] == "same_parties_distinct_claims" for r in links))

    def test_force_refile_after_withdrawal(self) -> None:
        self.file()
        with self.assertRaises(ServiceError):
            self.file()
        # 已撤回案件不阻挡重新立案
        case_ref = self.engine.repo.conn.execute(
            "SELECT case_ref FROM cases").fetchone()["case_ref"]
        self.engine.withdraw_claims(case_ref, [1], "PARTY-CHEN-001")
        again = self.file()
        self.assertTrue(again["case_ref"])

    # ---- 路径与移送双向确认 ----

    def test_route_and_transfer_handshake_updates_stage_and_grant(self) -> None:
        ref = self.file()["case_ref"]
        plan = self.engine.select_route(ref, "mediation_confirm")
        self.assertEqual(plan["version"], 1)
        self.assertIn("司法确认", plan["route_name"])

        # JUSTICE → MSA 发起移送，等待签收期间期限中止
        trf = self.engine.propose_transfer(ref, "MSA", sha("packet-v1"),
                                           "clerk-justice", "JUSTICE")
        self.assertEqual(trf["state"], HandshakeState.PROPOSED.value)
        dv = self.engine.deadline_view(ref, as_of="2027-01-01T00:00:00+08:00")
        self.assertIsNone(dv["deadline_at"])  # 中止持续中，截止日待定
        self.assertIsNotNone(dv["open_toll"])

        # 非接收单位不能签收
        with self.assertRaises(ServiceError) as ctx:
            self.engine.respond_transfer(trf["transfer_ref"], True,
                                         "x", "MED")
        self.assertEqual(ctx.exception.code, "forbidden")

        # 摘要不符 → 接收方可拒签
        with self.assertRaises(ServiceError) as ctx:
            self.engine.respond_transfer(trf["transfer_ref"], True,
                                         "officer-msa", "MSA",
                                         material_digest=sha("tampered"))
        self.assertEqual(ctx.exception.code, "digest_mismatch")

        # 正确签收：旧节点 handed_off，新节点 active，期限恢复，授权落地
        ack = self.engine.respond_transfer(trf["transfer_ref"], True,
                                           "officer-msa", "MSA",
                                           material_digest=sha("packet-v1"))
        self.assertEqual(ack["state"], HandshakeState.RECEIVED.value)
        stages = self.engine.repo.list_stages(
            self.engine.repo.get_active_route(ref)["id"])
        self.assertEqual(stages[0]["status"], StageStatus.HANDED_OFF.value)
        self.assertEqual(stages[1]["status"], StageStatus.ACTIVE.value)
        self.assertEqual(stages[1]["unit"], "MSA")
        dv2 = self.engine.deadline_view(ref, as_of="2027-01-01T00:00:00+08:00")
        self.assertIsNone(dv2["open_toll"])
        self.assertIsNotNone(dv2["deadline_at"])
        grant = self.engine.repo.unit_grant(ref, "MSA")
        self.assertIsNotNone(grant)

    def test_rejected_transfer_keeps_stage_with_sender(self) -> None:
        ref = self.file()["case_ref"]
        self.engine.select_route(ref, "mediation_confirm")
        trf = self.engine.propose_transfer(ref, "MSA", sha("p"), "u", "JUSTICE")
        self.engine.respond_transfer(trf["transfer_ref"], False, "msa",
                                     "MSA", note="材料缺封存清单")
        stages = self.engine.repo.list_stages(
            self.engine.repo.get_active_route(ref)["id"])
        self.assertEqual(stages[0]["status"], StageStatus.ACTIVE.value)
        self.assertEqual(stages[1]["status"], StageStatus.PENDING.value)
        # 中止区间已闭合
        toll = self.engine.repo.list_tolls(ref)[0]
        self.assertIsNotNone(toll["end_at"])

    def test_cross_unit_close_requires_handoff(self) -> None:
        ref = self.file()["case_ref"]
        self.engine.select_route(ref, "mediation_confirm")
        # JUSTICE 不能跳过移送直接"办结"并把责任甩给 MSA
        with self.assertRaises(ServiceError) as ctx:
            self.engine.close_active_stage(ref, "u", "JUSTICE")
        self.assertEqual(ctx.exception.code, "handoff_required")

    # ---- 撤回 / 部分和解 ----

    def test_withdraw_all_voids_path(self) -> None:
        ref = self.file()["case_ref"]
        self.engine.select_route(ref, "mediation_confirm")
        out = self.engine.withdraw_claims(ref, [1], "PARTY-CHEN-001", "双方自行协商")
        self.assertEqual(out["status"], "withdrawn")
        route = self.engine.repo.get_active_route(ref)
        self.assertIsNone(route)
        stages = self.engine.repo.list_stages(1)
        self.assertTrue(all(s["status"] == StageStatus.VOID.value for s in stages))

    def test_partial_settlement_keeps_remaining_on_route(self) -> None:
        data = case_payload(claims=[
            {"claim_kind": "wages", "subject_ref": "S1"},
            {"claim_kind": "medical_fee", "subject_ref": "S2"}])
        ref = self.engine.file_case(data)["case_ref"]
        self.engine.select_route(ref, "mediation_confirm",
                                 {"procuratorial_support": False})
        out = self.engine.partial_settlement(ref, [1], "med-01", "AGR-2026-009")
        self.assertEqual(out["remaining_claims"], [2])
        self.assertEqual(out["status"], "partial_settled")
        # 路径仍在，第 2 项请求继续
        self.assertIsNotNone(self.engine.repo.get_active_route(ref))

    # ---- 管辖异议：旧路径保留 ----

    def test_jurisdiction_objection_upheld_supersedes_old_route(self) -> None:
        ref = self.file()["case_ref"]
        self.engine.select_route(ref, "mediation_confirm")
        self.engine.file_jurisdiction_objection(ref, "PARTY-SHIPCO-001", "应由仲裁管辖")
        dv = self.engine.deadline_view(ref, as_of="2027-01-01T00:00:00+08:00")
        self.assertIsNotNone(dv["open_toll"])
        self.engine.rule_jurisdiction_objection(
            ref, True, "judge-1", new_route_key="litigation",
            reason="约定管辖有效", flags={"cross_border": False})
        routes = self.engine.repo.list_routes(ref)
        self.assertEqual([r["version"] for r in routes], [1, 2])
        self.assertEqual(routes[0]["status"], "superseded")  # 旧路径保留、只读
        self.assertEqual(routes[1]["status"], "active")
        # 新路径解释可追溯
        self.assertIn("管辖异议成立", routes[1]["change_reason"])
        dv2 = self.engine.deadline_view(ref, as_of="2027-01-01T00:00:00+08:00")
        self.assertIsNone(dv2["open_toll"])

    def test_jurisdiction_objection_overruled_resumes_same_path(self) -> None:
        ref = self.file()["case_ref"]
        self.engine.select_route(ref, "mediation_confirm")
        self.engine.file_jurisdiction_objection(ref, "p", "无管辖权")
        out = self.engine.rule_jurisdiction_objection(ref, False, "judge-1",
                                                      reason="本院有管辖权")
        self.assertIsNone(out["new_route_version"])
        self.assertEqual(len(self.engine.repo.list_routes(ref)), 1)

    def test_rule_without_filing_fails(self) -> None:
        ref = self.file()["case_ref"]
        self.engine.select_route(ref, "mediation_confirm")
        with self.assertRaises(ServiceError) as ctx:
            self.engine.rule_jurisdiction_objection(ref, False, "j")
        self.assertEqual(ctx.exception.code, "no_open_objection")

    # ---- 跨境送达 ----

    def test_cross_border_service_tolls_until_proof(self) -> None:
        ref = self.file()["case_ref"]
        self.engine.select_route(ref, "litigation", {"cross_border": True})
        self.engine.start_cross_border_service(ref, "clerk", "judicial_assistance",
                                               "PARTY-ABROAD-9")
        dv = self.engine.deadline_view(ref, as_of="2027-06-01T00:00:00+08:00")
        self.assertIsNone(dv["deadline_at"])
        self.engine.complete_cross_border_service(ref, sha("proof-of-service"), "clerk")
        dv2 = self.engine.deadline_view(ref, as_of="2027-06-01T00:00:00+08:00")
        self.assertIsNone(dv2["open_toll"])
        self.assertIsNotNone(dv2["deadline_at"])
        # 旧路径仍在
        self.assertEqual(self.engine.repo.get_active_route(ref)["version"], 1)

    # ---- 可见性与保密 ----

    def test_party_and_unit_visibility(self) -> None:
        ref = self.file()["case_ref"]
        self.engine.select_route(ref, "mediation_confirm")
        view = self.engine.party_view(ref, "PARTY-CHEN-001")
        # 公众端只有进度与期限，不暴露证据明细与其他当事方令牌
        self.assertNotIn("evidence", view)
        self.assertTrue(view["route"])

        with self.assertRaises(ServiceError) as ctx:
            self.engine.party_view(ref, "PARTY-STRANGER")
        self.assertEqual(ctx.exception.code, "forbidden")

        # 未获授权单位不可见
        with self.assertRaises(ServiceError) as ctx:
            self.engine.unit_view(ref, "CAI")
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_unit_confidentiality_ceiling_masks_note(self) -> None:
        data = case_payload(evidence=[
            {"evidence_ref": "EVD-1", "digest": sha("a"), "holder_unit": "MSA",
             "confidentiality": "confidential", "note": "涉商业秘密明细"}])
        ref = self.engine.file_case(data)["case_ref"]
        self.engine.select_route(ref, "mediation_confirm")
        accepted_at = self.engine.repo.get_case(ref)["accepted_at"]
        # 直接以受限授权查看：超级别字段屏蔽
        self.engine.repo.grant(ref, "MED", "materials",
                               Confidentiality.RESTRICTED.value, "u", accepted_at)
        uv = self.engine.unit_view(ref, "MED")
        self.assertIn("屏蔽", uv["evidence"][0]["note"])

    # ---- 监督 ----

    def test_supervision_shows_dwell_and_next_unit(self) -> None:
        ref = self.file()["case_ref"]
        self.engine.select_route(ref, "mediation_confirm")
        sv = self.engine.supervision_view(as_of="2026-09-21T12:00:00+08:00")
        self.assertEqual(len(sv["active_nodes"]), 1)
        node = sv["active_nodes"][0]
        self.assertEqual(node["current_unit"], "JUSTICE")
        self.assertEqual(node["next_responsible_unit"], "MSA")
        self.assertIsNotNone(node["dwell_days"])
        self.assertIn("有效", node["effect"])

    def test_supervision_tracks_pending_transfer(self) -> None:
        ref = self.file()["case_ref"]
        self.engine.select_route(ref, "mediation_confirm")
        self.engine.propose_transfer(ref, "MSA", sha("p"), "u", "JUSTICE")
        sv = self.engine.supervision_view(as_of="2026-09-26T00:00:00+08:00")
        self.assertEqual(len(sv["pending_transfers"]), 1)
        self.assertGreater(sv["pending_transfers"][0]["waiting_days"], 3)

    # ---- 外部事件接入 ----

    def test_ingest_preserves_occurred_at_and_is_idempotent(self) -> None:
        ref = self.file()["case_ref"]
        event = {
            "event_id": "EVENT-MSA-1001", "subject_ref": ref,
            "source_unit": "MSA", "source_sequence": 1,
            "occurred_at": "2026-09-19T09:30:00+08:00",
            "payload_digest": sha("notice-1"), "kind": "external_notice"}
        out = self.engine.ingest_event(event)
        self.assertFalse(out["deduplicated"])
        # 到达时间晚于发生时间，但案内账本保留原始 occurred_at
        stored = self.engine.repo.list_events(ref)[-1]
        self.assertEqual(stored["occurred_at"], "2026-09-19T09:30:00+08:00")
        again = self.engine.ingest_event(event)
        self.assertTrue(again["deduplicated"])
        with self.assertRaises(ServiceError) as ctx:
            self.engine.ingest_event({**event, "event_id": "EVENT-MSA-0999",
                                      "source_sequence": 1})
        self.assertEqual(ctx.exception.code, "sequence_regression")

    # ---- 入参校验 ----

    def test_evidence_digest_mandatory(self) -> None:
        bad = case_payload(evidence=[{"evidence_ref": "E", "digest": "nope",
                                      "holder_unit": "MSA"}])
        with self.assertRaises(ServiceError) as ctx:
            self.engine.file_case(bad)
        self.assertEqual(ctx.exception.code, "bad_digest")

    def test_crew_labor_route_limitation_one_year(self) -> None:
        out = self.engine.file_case(case_payload(
            dtype="crew_labor", relation="EMP-LAC-2026-5",
            claims=[{"claim_kind": "wages", "subject_ref": "PAY-09"}],
            evidence=[{"evidence_ref": "E1", "digest": sha("payroll"),
                       "holder_unit": "HRSS", "confidentiality": "restricted"}]))
        self.assertEqual(out["limitation"]["days"], 365)
        keys = {o["route_key"] for o in out["route_options"]}
        self.assertIn("labor_arbitration", keys)


if __name__ == "__main__":
    unittest.main()
