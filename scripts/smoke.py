"""端到端冒烟脚本：对运行中的服务走完整流程。用法：python scripts/smoke.py [base_url]"""

import hashlib
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
TOK = {
    "JUSTICE": "tok-unit-justice", "MSA": "tok-unit-msa", "MED": "tok-unit-med",
    "LAC": "tok-unit-lac", "CAI": "tok-unit-cai", "COURT": "tok-unit-court",
    "PROC": "tok-unit-proc", "HRSS": "tok-unit-hrss", "SUP": "tok-supervisor",
}


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def call(method: str, path: str, unit: str | None = None,
         body: dict | None = None, token: str | None = None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    bearer = token or (TOK[unit] if unit else None)
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def main() -> None:
    assert call("GET", "/health")[1] == {"status": "ok"}

    payload = {
        "dispute_type": "crew_labor",
        "relation_ref": "EMP-LAC-2026-0091",
        "parties": [
            {"party_ref": "PARTY-WANG-007", "role": "claimant"},
            {"party_ref": "PARTY-FLEET-007", "role": "respondent"}],
        "claims": [{"claim_kind": "wages", "subject_ref": "PAYROLL-0809",
                    "period_start": "2026-05-01", "period_end": "2026-08-31"}],
        "connections": [{"point_type": "employer_locale", "point_ref": "GEO-QINZHOU-HR"}],
        "evidence": [{"evidence_ref": "EVD-PAY-01", "digest": sha("payroll"),
                      "holder_unit": "HRSS", "confidentiality": "restricted",
                      "note": "工资表封存"}],
    }
    st, filed = call("POST", "/api/cases", "JUSTICE", payload)
    assert st == 201, filed
    ref = filed["case_ref"]
    party_token = filed["party_tokens"]["PARTY-WANG-007"]
    print(f"1. 受理：{ref}，仲裁时效口径 {filed['limitation']}")
    print("   重复立案预检：", filed["duplicate_check"])

    st, plan = call("POST", f"/api/cases/{ref}/routes", "JUSTICE",
                    {"route_key": "mediation_arbitration"})
    assert st == 201, plan
    print(f"2. 路径 v{plan['version']}：{plan['route_name']}；节点={plan['segments'] if 'segments' in plan else ''}")

    st, trf = call("POST", f"/api/cases/{ref}/transfers", "JUSTICE",
                   {"to_unit": "MED", "material_digest": sha("packet-v1")})
    assert st == 201, trf
    print(f"3. 移送发起：{trf['transfer_ref']}（{trf['from_unit']}→{trf['to_unit']}），期限自 {trf['toll_started_at']} 中止")

    st, ack = call("POST", f"/api/transfers/{trf['transfer_ref']}/respond", "MED",
                   {"accept": True, "material_digest": sha("packet-v1"), "note": "材料齐全"})
    assert st == 200 and ack["state"] == "received", ack
    print(f"4. 接收单位双向签收：{ack['state']}，期限恢复于 {ack['responded_at']}")

    st, pview = call("GET", f"/api/cases/{ref}", token=party_token)
    assert st == 200 and "evidence" not in pview
    stages = [(s["seq"], s["unit"], s["status"]) for s in pview["route"]["stages"]]
    print(f"5. 当事人视图：{stages}；剩余时效 {pview['deadline']['remaining_days']} 天")

    st, uv = call("GET", f"/api/cases/{ref}", "MED")
    assert st == 200
    print(f"6. 协同单位 {uv['grant']['unit']} 授权：{uv['grant']['scope']}/{uv['grant']['confidentiality_ceiling']}")

    st, sup = call("GET", "/api/supervision", "SUP")
    assert st == 200
    row = next(i for i in sup["active_nodes"] if i["case_ref"] == ref)
    print(f"7. 监督：当前责任 {row['current_unit']} → 下一责任 {row['next_responsible_unit']}，"
          f"已停留 {row['dwell_days']} 天，效力：{row['effect']}")

    # 管辖异议：提出（中止）→ 驳回（恢复，路径不换）
    st, obj = call("POST", f"/api/cases/{ref}/jurisdiction-objection/file",
                   token=party_token, body={"reason": "仲裁委无管辖权"})
    assert st == 200, obj
    st, ruling = call("POST", f"/api/cases/{ref}/jurisdiction-objection/rule", "LAC",
                      {"upheld": False, "reason": "本会有管辖权"})
    assert st == 200 and ruling["new_route_version"] is None, ruling
    print("8. 管辖异议驳回：原路径保留，期限恢复", ruling["resumed_at"])

    # 外部事件接入：发生时间不被覆盖
    st, ing = call("POST", "/api/events/ingest", "HRSS", {
        "event_id": f"EVENT-HRSS-{ref}", "subject_ref": ref, "source_unit": "HRSS",
        "source_sequence": 1, "occurred_at": "2026-09-19T09:30:00+08:00",
        "payload_digest": sha("notice"), "kind": "external_notice"})
    assert st == 202 and ing["occurred_at"] == "2026-09-19T09:30:00+08:00", ing
    print("9. 外部事件已接入，保留原始发生时间：", ing["occurred_at"])
    print("冒烟通过 ✅")


if __name__ == "__main__":
    main()
