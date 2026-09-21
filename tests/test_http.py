"""HTTP 接口集成测试：鉴权、角色视图与移送闭环。"""

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.api import build_server
from src.storage.repo import connect, init_schema


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def case_payload() -> dict:
    return {
        "dispute_type": "maritime_accident",
        "relation_ref": "VOY-PL-2026-088",
        "parties": [
            {"party_ref": "PARTY-LI-002", "role": "claimant"},
            {"party_ref": "PARTY-SHIPCO-002", "role": "respondent"}],
        "claims": [{"claim_kind": "collision_damage", "subject_ref": "SUBJ-HULL-2"}],
        "connections": [{"point_type": "accident_locale", "point_ref": "GEO-CANAL-KM90"}],
        "evidence": [{"evidence_ref": "EVD-X1", "digest": sha("vdr2"),
                      "holder_unit": "MSA", "confidentiality": "restricted"}],
    }


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "http.sqlite3")
        conn = connect(self.db)
        init_schema(conn)
        for token, unit, role in [
                ("tok-msa", "MSA", "staff"), ("tok-med", "MED", "staff"),
                ("tok-court", "COURT", "staff"), ("tok-justice", "JUSTICE", "staff"),
                ("tok-sup", "SUPV", "supervisor")]:
            conn.execute("INSERT INTO unit_tokens(token, unit, role) VALUES(?,?,?)",
                         (token, unit, role))
        conn.commit()
        conn.close()
        self.server = build_server(self.db, 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.tmp.cleanup()

    def call(self, method: str, path: str, token: str | None = None,
             body: dict | None = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_health_and_auth(self) -> None:
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        status, body = self.call("GET", "/api/cases")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_full_transfer_flow_and_views(self) -> None:
        # 受理（仅司法行政窗口）
        status, body = self.call("POST", "/api/cases", "tok-msa", case_payload())
        self.assertEqual(status, 403)
        status, filed = self.call("POST", "/api/cases", "tok-justice", case_payload())
        self.assertEqual(status, 201)
        ref = filed["case_ref"]
        party_token = filed["party_tokens"]["PARTY-LI-002"]

        # 重复立案 409
        status, body = self.call("POST", "/api/cases", "tok-justice", case_payload())
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "duplicate_case")

        # 选路径
        status, plan = self.call("POST", f"/api/cases/{ref}/routes", "tok-justice",
                                 {"route_key": "mediation_confirm"})
        self.assertEqual(status, 201)
        self.assertEqual(plan["version"], 1)

        # 移送 JUSTICE → MSA：错误单位不能发起
        status, body = self.call("POST", f"/api/cases/{ref}/transfers", "tok-med",
                                 {"to_unit": "MSA", "material_digest": sha("pk")})
        self.assertEqual(status, 403)
        status, trf = self.call("POST", f"/api/cases/{ref}/transfers", "tok-justice",
                                {"to_unit": "MSA", "material_digest": sha("pk")})
        self.assertEqual(status, 201)

        # MSA 签收
        status, ack = self.call("POST", f"/api/transfers/{trf['transfer_ref']}/respond",
                                "tok-msa", {"accept": True,
                                            "material_digest": sha("pk")})
        self.assertEqual(status, 200)
        self.assertEqual(ack["state"], "received")

        # 当事人公众端只见自身案件进度
        status, pview = self.call("GET", f"/api/cases/{ref}", party_token)
        self.assertEqual(status, 200)
        self.assertNotIn("evidence", pview)
        active = [s for s in pview["route"]["stages"] if s["status"] == "active"]
        self.assertEqual(active[0]["unit"], "MSA")
        self.assertIn("deadline", pview)

        # 协同单位获授权后可查看材料
        status, uview = self.call("GET", f"/api/cases/{ref}", "tok-msa")
        self.assertEqual(status, 200)
        self.assertEqual(uview["evidence"][0]["evidence_ref"], "EVD-X1")
        # 调解组织此时仅有进度授权（路径节点），证据按上限脱敏
        status, med_view = self.call("GET", f"/api/cases/{ref}", "tok-med")
        self.assertEqual(status, 200)
        self.assertTrue(all("屏蔽" in e["note"] for e in med_view["evidence"]
                            if e["confidentiality"] == "restricted") or
                        med_view["grant"]["confidentiality_ceiling"] in ("public",))

        # 监督视图：停留时间、下一责任方
        status, sup = self.call("GET", "/api/supervision", "tok-sup")
        self.assertEqual(status, 200)
        self.assertEqual(sup["active_nodes"][0]["current_unit"], "MSA")
        self.assertEqual(sup["active_nodes"][0]["next_responsible_unit"], "MED")

        # 普通经办不能访问监督端点
        status, _ = self.call("GET", "/api/supervision", "tok-msa")
        self.assertEqual(status, 403)

    def test_jurisdiction_objection_changes_route_keeps_history(self) -> None:
        _, filed = self.call("POST", "/api/cases", "tok-justice", case_payload())
        ref = filed["case_ref"]
        self.call("POST", f"/api/cases/{ref}/routes", "tok-justice",
                  {"route_key": "mediation_confirm"})
        party_token = filed["party_tokens"]["PARTY-LI-002"]
        status, _ = self.call("POST",
                              f"/api/cases/{ref}/jurisdiction-objection/file",
                              party_token, {"reason": "仲裁条款排除管辖"})
        self.assertEqual(status, 200)
        status, ruling = self.call("POST",
                                   f"/api/cases/{ref}/jurisdiction-objection/rule",
                                   "tok-court",
                                   {"upheld": True, "new_route_key": "litigation",
                                    "reason": "仲裁条款无效，本院管辖"})
        self.assertEqual(status, 200)
        self.assertEqual(ruling["new_route_version"], 2)
        status, sup_case = self.call("GET", f"/api/cases/{ref}", "tok-sup")
        versions = [r["version"] for r in sup_case["route_history"]]
        self.assertEqual(versions, [1, 2])


if __name__ == "__main__":
    unittest.main()
