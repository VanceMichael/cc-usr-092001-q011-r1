"""端到端行为检查：登记-受理-移送-确认-视图与旧路径保留。"""

import json
import unittest

from src.app import handle_request
from src.store import Store

NOW = "2026-09-21T12:00:00+00:00"
LATER = "2026-09-22T12:00:00+00:00"


def call(store, method, path, role=None, actor="ACTOR-1", unit=None, payload=None, now=NOW):
    headers = {"X-Actor-Ref": actor}
    if role:
        headers["X-Actor-Role"] = role
    if unit:
        headers["X-Actor-Unit"] = unit
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    return handle_request(method, path, headers, body, store, now=now)


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self._seq = 0
        for ref, id_hash in (("PARTY-A", "hash-a"), ("PARTY-B", "hash-b")):
            status, _ = call(self.store, "POST", "/parties", role="registrar", payload={
                "party_ref": ref, "party_type": "自然人", "id_hash": id_hash,
            })
            self.assertEqual(status, 201)

    def tearDown(self) -> None:
        self.store.close()

    def _create_case(self, claim_type="工资报酬", election="litigation",
                     dispute_type="crew_labor", opened_by="labor", extra=None):
        self._seq += 1
        payload = {
            "dispute_type": dispute_type,
            "election": election,
            "opened_by_unit": opened_by,
            "connection_points": ["事故发生地：平陆运河钦州段"],
            "parties": [
                {"party_ref": "PARTY-A", "role": "申请人"},
                {"party_ref": "PARTY-B", "role": "被申请人"},
            ],
            "claims": [{"claim_type": claim_type, "amount": 80000, "currency": "CNY"}],
            "evidence": [{"evidence_ref": f"EVID-{self._seq}", "digest": "sha256:demo"}],
            "deadlines": [{
                "kind": "仲裁时效",
                "basis": "劳动争议调解仲裁法第二十七条",
                "due_at": "2027-03-01T00:00:00+08:00",
                "warn_days": 30,
            }],
        }
        if extra:
            payload.update(extra)
        return call(self.store, "POST", "/cases", role="registrar", payload=payload)

    def _accepted_case(self, **kwargs):
        status, body = self._create_case(**kwargs)
        self.assertEqual(status, 201)
        case_ref = body["case_ref"]
        status, path = call(self.store, "POST", f"/cases/{case_ref}/accept",
                            role="registrar")
        self.assertEqual(status, 200)
        return case_ref, path

    # ---- 主流程：登记→受理→移送→双向确认→视图 ----

    def test_full_journey(self) -> None:
        status, created = self._create_case()
        self.assertEqual(status, 201)
        case_ref = created["case_ref"]
        evidence_ref = created["evidence_refs"][0]
        status, path = call(self.store, "POST", f"/cases/{case_ref}/accept",
                            role="registrar")
        self.assertEqual(status, 200)
        # 船员劳资选择诉讼：路径从劳动仲裁开始，且每个节点都有理由。
        self.assertEqual(path["nodes"][0]["unit_kind"], "labor")
        self.assertTrue(all(node["reason"] for node in path["nodes"]))
        status, full_path = call(self.store, "GET", f"/cases/{case_ref}/path",
                                 role="supervisor")
        self.assertEqual(full_path["versions"][0]["nodes"][0]["status"], "进行中")

        # 公众端：当事人可见本人进度，非当事人拒绝。
        status, progress = call(self.store, "GET", f"/cases/{case_ref}/progress",
                                role="public", actor="PARTY-A")
        self.assertEqual(status, 200)
        self.assertEqual(progress["current_step"]["unit_kind"], "labor")
        self.assertNotIn("evidence", progress)
        status, _ = call(self.store, "GET", f"/cases/{case_ref}/progress",
                         role="public", actor="PARTY-STRANGER")
        self.assertEqual(status, 403)

        # 协同单位：登记单位可见材料，未授权单位拒绝。
        status, materials = call(self.store, "GET", f"/cases/{case_ref}/materials",
                                 role="unit", unit="labor")
        self.assertEqual(status, 200)
        self.assertEqual(materials["evidence"][0]["digest"], "sha256:demo")
        status, _ = call(self.store, "GET", f"/cases/{case_ref}/materials",
                         role="unit", unit="court")
        self.assertEqual(status, 403)

        # 移送：当前责任单位发起，先交出后接收，顺序不可逆。
        status, transfer = call(self.store, "POST", f"/cases/{case_ref}/transfers",
                                role="unit", unit="labor", payload={
                                    "kind": "转诉讼", "to_unit_kind": "court",
                                    "reason": "仲裁裁决作出，当事人起诉",
                                    "materials": [evidence_ref], "confidentiality": "L3",
                                })
        self.assertEqual(status, 201)
        transfer_ref = transfer["transfer_ref"]
        status, _ = call(self.store, "POST", f"/transfers/{transfer_ref}/confirm",
                         role="unit", unit="court", payload={"side": "receipt"})
        self.assertEqual(status, 409)  # 交出未确认，接收不能先行
        status, body = call(self.store, "POST", f"/transfers/{transfer_ref}/confirm",
                            role="unit", unit="labor", payload={"side": "handover"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "待接收确认")

        # 移送途中：监督视图指向接收方，期限快照已随移送保存。
        status, sup = call(self.store, "GET", f"/cases/{case_ref}/supervision",
                           role="supervisor")
        self.assertEqual(status, 200)
        self.assertIn("待接收确认", sup["next_responsible"])
        self.assertEqual(sup["transfers"][0]["deadline_snapshot"][0]["kind"], "仲裁时效")

        status, body = call(self.store, "POST", f"/transfers/{transfer_ref}/confirm",
                            role="unit", unit="court", payload={"side": "receipt"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "已完成")

        # 交接完成：权限随程序变化，法院获得材料授权，保密级别就高为 L3。
        status, materials = call(self.store, "GET", f"/cases/{case_ref}/materials",
                                 role="unit", unit="court")
        self.assertEqual(status, 200)
        self.assertEqual(len(materials["evidence"]), 1)
        status, sup = call(self.store, "GET", f"/cases/{case_ref}/supervision",
                           role="supervisor")
        self.assertEqual(sup["confidentiality"], "L3")
        self.assertEqual(sup["next_responsible"], "法院")
        nodes = sup["versions"][0]["nodes"]
        labor_node = next(n for n in nodes if n["unit_kind"] == "labor")
        court_node = next(n for n in nodes if n["unit_kind"] == "court")
        self.assertEqual(labor_node["status"], "已移送")
        self.assertEqual(court_node["status"], "进行中")
        self.assertIsNotNone(labor_node["dwell_days"])
        self.assertEqual(labor_node["validity"], "已办结")

    # ---- 重复立案：识别但不误并 ----

    def test_duplicate_flag_and_decision(self) -> None:
        self._accepted_case()
        status, body = self._create_case()
        self.assertEqual(status, 201)
        self.assertEqual(len(body["duplicate_flags"]), 1)
        flag_ref = body["duplicate_flags"][0]["flag_ref"]
        status, flag = call(self.store, "POST", f"/duplicates/{flag_ref}/decision",
                            role="registrar", payload={"decision": "confirm"})
        self.assertEqual(status, 200)
        self.assertEqual(flag["status"], "已确认重复")
        status, progress = call(self.store, "GET", f"/cases/{body['case_ref']}/progress",
                                role="public", actor="PARTY-A")
        self.assertEqual(progress["claims"][0]["status"], "重复立案")

    def test_different_claim_type_not_flagged(self) -> None:
        self._accepted_case()
        status, body = self._create_case(claim_type="工伤赔偿")
        self.assertEqual(status, 201)
        self.assertEqual(body["duplicate_flags"], [])

    def test_dismiss_requires_reason(self) -> None:
        self._accepted_case()
        _, body = self._create_case()
        flag_ref = body["duplicate_flags"][0]["flag_ref"]
        status, _ = call(self.store, "POST", f"/duplicates/{flag_ref}/decision",
                         role="registrar", payload={"decision": "dismiss"})
        self.assertEqual(status, 400)
        status, flag = call(self.store, "POST", f"/duplicates/{flag_ref}/decision",
                            role="registrar",
                            payload={"decision": "dismiss", "reason": "诉求期间不同"})
        self.assertEqual(status, 200)
        self.assertEqual(flag["status"], "已排除")

    # ---- 撤回、部分和解、管辖异议、跨境送达：旧路径保留 ----

    def test_withdraw_keeps_path(self) -> None:
        case_ref, _ = self._accepted_case()
        status, body = call(self.store, "POST", f"/cases/{case_ref}/actions",
                            role="registrar", payload={"action": "withdraw"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "已撤回")
        status, path = call(self.store, "GET", f"/cases/{case_ref}/path", role="supervisor")
        self.assertEqual(status, 200)
        self.assertIsNotNone(path["versions"][0]["superseded_at"])
        self.assertTrue(all(n["status"] == "已终结" for n in path["versions"][0]["nodes"]))

    def test_partial_settlement_branches_path(self) -> None:
        status, body = self._create_case(extra={
            "claims": [
                {"claim_type": "工资报酬", "amount": 50000},
                {"claim_type": "经济补偿", "amount": 30000},
            ],
        })
        case_ref = body["case_ref"]
        claim_refs = body["claim_refs"]
        call(self.store, "POST", f"/cases/{case_ref}/accept", role="registrar")
        status, result = call(self.store, "POST", f"/cases/{case_ref}/actions",
                              role="registrar", payload={
                                  "action": "partial_settlement",
                                  "claim_refs": [claim_refs[0]],
                              })
        self.assertEqual(status, 200)
        self.assertEqual(result["remaining_claims"], 1)
        status, path = call(self.store, "GET", f"/cases/{case_ref}/path", role="supervisor")
        self.assertEqual(len(path["versions"]), 2)
        self.assertIsNotNone(path["versions"][0]["superseded_at"])
        self.assertIn("部分和解", path["versions"][1]["reason"])
        # 剩余请求全部和解后案件结案。
        call(self.store, "POST", f"/cases/{case_ref}/actions", role="registrar",
             payload={"action": "partial_settlement", "claim_refs": [claim_refs[1]]})
        status, progress = call(self.store, "GET", f"/cases/{case_ref}/progress",
                                role="public", actor="PARTY-A")
        self.assertEqual(progress["status"], "已结案")

    def test_jurisdiction_objection_preserves_old_path(self) -> None:
        case_ref, _ = self._accepted_case()
        status, _ = call(self.store, "POST", f"/cases/{case_ref}/actions",
                         role="registrar", payload={
                             "action": "jurisdiction_objection",
                             "reason": "被申请人主张应由船籍港法院管辖",
                         })
        self.assertEqual(status, 200)
        status, path = call(self.store, "GET", f"/cases/{case_ref}/path", role="supervisor")
        self.assertEqual(len(path["versions"]), 2)
        old_nodes = path["versions"][0]["nodes"]
        self.assertEqual(old_nodes[0]["unit_kind"], "labor")  # 旧路径原样保留
        new_nodes = path["versions"][1]["nodes"]
        objection = next(n for n in new_nodes if n["action"] == "管辖异议审查")
        self.assertEqual(objection["status"], "进行中")
        paused = next(n for n in new_nodes if n["unit_kind"] == "labor")
        self.assertEqual(paused["status"], "暂停")

    def test_cross_border_service_appends_node(self) -> None:
        case_ref, _ = self._accepted_case()
        status, _ = call(self.store, "POST", f"/cases/{case_ref}/actions",
                         role="registrar", payload={"action": "cross_border_service"})
        self.assertEqual(status, 200)
        status, path = call(self.store, "GET", f"/cases/{case_ref}/path", role="supervisor")
        new_nodes = path["versions"][1]["nodes"]
        self.assertEqual(new_nodes[-1]["action"], "跨境送达协助")
        self.assertEqual(len(path["versions"]), 2)

    # ---- 事件交换：保留来源时间、幂等 ----

    def test_event_ingest_is_idempotent_and_keeps_occurred_at(self) -> None:
        event = {
            "event_id": "EVENT-1",
            "subject_ref": "SUBJECT-1",
            "occurred_at": "2026-09-19T09:30:00+08:00",
            "source_sequence": 1,
            "payload_digest": "sha256:demo",
        }
        status, body = call(self.store, "POST", "/events", role="unit",
                            unit="maritime", payload=event)
        self.assertEqual(status, 201)
        status, again = call(self.store, "POST", "/events", role="unit",
                             unit="maritime", payload=event, now=LATER)
        self.assertEqual(status, 200)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["occurred_at"], "2026-09-19T09:30:00+08:00")
        self.assertEqual(again["recorded_at"], NOW)  # 不被到达时间覆盖

    def test_event_requires_offset(self) -> None:
        status, _ = call(self.store, "POST", "/events", role="registrar", payload={
            "event_id": "EVENT-2", "subject_ref": "S", "occurred_at": "2026-09-19 09:30",
            "source_sequence": 1, "payload_digest": "sha256:demo",
        })
        self.assertEqual(status, 400)

    # ---- 权限边界 ----

    def test_role_boundaries(self) -> None:
        case_ref, _ = self._accepted_case()
        status, _ = call(self.store, "GET", f"/cases/{case_ref}/supervision",
                         role="public", actor="PARTY-A")
        self.assertEqual(status, 403)
        status, _ = call(self.store, "POST", f"/cases/{case_ref}/transfers",
                         role="unit", unit="court", payload={
                             "kind": "转诉讼", "to_unit_kind": "labor",
                         })
        self.assertEqual(status, 403)  # 非当前责任单位
        status, _ = call(self.store, "POST", "/cases", role="public", actor="PARTY-A",
                         payload={})
        self.assertEqual(status, 403)
        status, _ = handle_request("GET", f"/cases/{case_ref}/path", {}, None,
                                   self.store, now=NOW)
        self.assertEqual(status, 401)

    def test_deadline_view_for_unit(self) -> None:
        case_ref, _ = self._accepted_case()
        status, body = call(self.store, "GET", f"/cases/{case_ref}/deadlines",
                            role="unit", unit="labor")
        self.assertEqual(status, 200)
        deadline = body["deadlines"][0]
        self.assertEqual(deadline["status"], "运行中")
        self.assertFalse(deadline["warning"])
        self.assertGreater(deadline["remaining_days"], 150)


if __name__ == "__main__":
    unittest.main()
