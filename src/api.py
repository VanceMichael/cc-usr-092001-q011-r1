"""HTTP 接口与三类角色鉴权。

角色：
- party      公众端当事人：凭 party token 只能看到自身案件进度；
- staff      协同单位经办人：凭 unit token 在本单位职责内操作、查看获授权材料；
- supervisor 监督人员：可按任一节点核对停留时间、效力状态与下一责任方。

令牌仅用于演示与联调，生产环境应由统一身份设施签发并绑定到具体自然人/岗位。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .services.engine import Engine, ServiceError
from .storage.repo import Repository, connect, init_schema

# 演示令牌（由 migrate 落库，测试环境共用）
DEMO_UNIT_TOKENS = {
    "tok-unit-msa": ("MSA", "staff"),
    "tok-unit-proc": ("PROC", "staff"),
    "tok-unit-justice": ("JUSTICE", "staff"),
    "tok-unit-hrss": ("HRSS", "staff"),
    "tok-unit-med": ("MED", "staff"),
    "tok-unit-lac": ("LAC", "staff"),
    "tok-unit-cai": ("CAI", "staff"),
    "tok-unit-court": ("COURT", "staff"),
    "tok-supervisor": ("SUPV", "supervisor"),
}


class Principal:
    def __init__(self, kind: str, *, party_ref: str | None = None,
                 unit: str | None = None, role: str | None = None):
        self.kind = kind  # party / staff / supervisor / anonymous
        self.party_ref = party_ref
        self.unit = unit
        self.role = role

    @property
    def actor(self) -> str:
        return self.party_ref or self.unit or "anonymous"


def build_engine(database_path: str) -> Engine:
    conn = connect(database_path)
    init_schema(conn)
    return Engine(Repository(conn))


def make_handler(engine_factory):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CanalLink/1"

        # ---- 基础收发 ----

        def _write_json(self, status: int, payload: dict | list) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ServiceError("bad_json", f"请求体不是合法 JSON：{exc}")
            if not isinstance(data, dict):
                raise ServiceError("bad_json", "请求体必须是 JSON 对象。")
            return data

        def _principal(self) -> Principal:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return Principal("anonymous")
            token = auth[7:].strip()
            repo = self.server.repo  # type: ignore[attr-defined]
            party = repo.party_by_token(token)
            if party:
                return Principal("party", party_ref=party)
            unit_row = repo.unit_by_token(token)
            if unit_row:
                if unit_row["role"] == "supervisor":
                    return Principal("supervisor", unit=unit_row["unit"], role="supervisor")
                return Principal("staff", unit=unit_row["unit"], role=unit_row["role"])
            return Principal("anonymous")

        def _require(self, principal: Principal, *kinds: str) -> None:
            if principal.kind == "anonymous":
                raise ServiceError("unauthorized", "缺少有效访问令牌。")
            if principal.kind not in kinds:
                raise ServiceError("forbidden",
                                   f"当前身份 {principal.kind} 无权执行该操作。")

        def log_message(self, fmt: str, *args: object) -> None:
            return

        # ---- 路由 ----

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                path = urlsplit(self.path).path
                if method == "GET" and path == "/health":
                    self._write_json(200, {"status": "ok"})
                    return
                if not path.startswith("/api/"):
                    raise ServiceError("not_found", "路径不存在。")
                principal = self._principal()
                engine: Engine = self.server.engine  # type: ignore[attr-defined]
                parts = [p for p in path.split("/") if p]

                if method == "POST" and parts == ["api", "cases"]:
                    self._require(principal, "staff")
                    if principal.unit != "JUSTICE":
                        raise ServiceError("forbidden", "一口受理登记由司法行政受理窗口办理。")
                    self._write_json(201, engine.file_case(self._read_json()))

                elif method == "POST" and len(parts) == 4 and parts[1] == "cases" \
                        and parts[3] == "routes":
                    self._require(principal, "staff")
                    body = self._read_json()
                    self._write_json(201, engine.select_route(
                        parts[2], body["route_key"], body.get("flags"), principal.actor))

                elif method == "POST" and len(parts) == 4 and parts[1] == "cases" \
                        and parts[3] == "transfers":
                    self._require(principal, "staff")
                    body = self._read_json()
                    result = engine.propose_transfer(
                        parts[2], body["to_unit"], body["material_digest"],
                        principal.actor, principal.unit, body.get("material_items"))
                    self._write_json(201, result)

                elif method == "POST" and len(parts) == 4 and parts[1] == "transfers" \
                        and parts[3] == "respond":
                    self._require(principal, "staff")
                    body = self._read_json()
                    result = engine.respond_transfer(
                        parts[2], bool(body.get("accept")), principal.actor,
                        principal.unit, body.get("note"), body.get("material_digest"))
                    self._write_json(200, result)

                elif method == "POST" and len(parts) == 4 and parts[1] == "cases" \
                        and parts[3] == "withdraw":
                    self._require(principal, "party", "staff", "supervisor")
                    body = self._read_json()
                    self._write_json(200, engine.withdraw_claims(
                        parts[2], body["claim_seqs"], principal.actor, body.get("reason")))

                elif method == "POST" and len(parts) == 4 and parts[1] == "cases" \
                        and parts[3] == "partial-settlement":
                    self._require(principal, "staff")
                    if principal.unit not in ("MED", "COURT", "JUSTICE"):
                        raise ServiceError("forbidden", "和解登记由调解组织或法院办理。")
                    body = self._read_json()
                    self._write_json(200, engine.partial_settlement(
                        parts[2], body["claim_seqs"], principal.actor,
                        body.get("agreement_ref")))

                elif method == "POST" and len(parts) == 5 and parts[1] == "cases" \
                        and parts[3] == "jurisdiction-objection" and parts[4] == "file":
                    self._require(principal, "party", "staff")
                    body = self._read_json()
                    self._write_json(200, engine.file_jurisdiction_objection(
                        parts[2], principal.actor, body.get("reason")))

                elif method == "POST" and len(parts) == 5 and parts[1] == "cases" \
                        and parts[3] == "jurisdiction-objection" and parts[4] == "rule":
                    self._require(principal, "staff")
                    if principal.unit not in ("COURT", "LAC", "CAI"):
                        raise ServiceError("forbidden", "管辖异议由被请求的裁判机构裁定。")
                    body = self._read_json()
                    self._write_json(200, engine.rule_jurisdiction_objection(
                        parts[2], bool(body.get("upheld")), principal.actor,
                        body.get("new_route_key"), body.get("reason"), body.get("flags")))

                elif method == "POST" and len(parts) == 5 and parts[1] == "cases" \
                        and parts[3] == "cross-border" and parts[4] == "start":
                    self._require(principal, "staff")
                    if principal.unit not in ("COURT", "CAI"):
                        raise ServiceError("forbidden", "跨境送达由法院或商事仲裁机构发起。")
                    body = self._read_json()
                    self._write_json(200, engine.start_cross_border_service(
                        parts[2], principal.actor, body["channel"], body["target_ref"]))

                elif method == "POST" and len(parts) == 5 and parts[1] == "cases" \
                        and parts[3] == "cross-border" and parts[4] == "complete":
                    self._require(principal, "staff")
                    body = self._read_json()
                    self._write_json(200, engine.complete_cross_border_service(
                        parts[2], body["proof_digest"], principal.actor))

                elif method == "POST" and len(parts) == 5 and parts[1] == "cases" \
                        and parts[3] == "stages" and parts[4] == "close":
                    self._require(principal, "staff")
                    body = self._read_json()
                    self._write_json(200, engine.close_active_stage(
                        parts[2], principal.actor, principal.unit,
                        body.get("outcome_ref")))

                elif method == "POST" and len(parts) == 4 and parts[1] == "cases" \
                        and parts[3] == "close":
                    self._require(principal, "staff", "supervisor")
                    body = self._read_json()
                    self._write_json(200, engine.close_case(
                        parts[2], principal.actor, body.get("reason", "办结")))

                elif method == "GET" and len(parts) == 3 and parts[1] == "cases":
                    self._serve_case(engine, principal, parts[2])

                elif method == "GET" and parts == ["api", "cases"]:
                    self._require(principal, "party", "supervisor", "staff")
                    if principal.kind == "party":
                        refs = engine.repo.list_case_refs_for_party(principal.party_ref)
                        self._write_json(200, {"case_refs": refs})
                    elif principal.kind == "supervisor":
                        rows = engine.repo.conn.execute(
                            "SELECT case_ref, dispute_type, status FROM cases ORDER BY case_ref"
                        ).fetchall()
                        self._write_json(200, {"cases": [dict(r) for r in rows]})
                    else:
                        # 协同单位只能看到路径节点或授权涉本单位的案件
                        rows = engine.repo.conn.execute(
                            """SELECT DISTINCT c.case_ref, c.dispute_type, c.status
                                 FROM cases c
                                 LEFT JOIN routes r ON r.case_ref = c.case_ref
                                                       AND r.status = 'active'
                                 LEFT JOIN stages s ON s.route_id = r.id
                                 LEFT JOIN grants g ON g.case_ref = c.case_ref
                                                      AND g.unit = ?
                                                      AND g.revoked_at IS NULL
                                WHERE s.unit = ? OR g.id IS NOT NULL
                                ORDER BY c.case_ref""",
                            (principal.unit, principal.unit)).fetchall()
                        self._write_json(200, {"cases": [dict(r) for r in rows]})

                elif method == "GET" and parts == ["api", "supervision"]:
                    self._require(principal, "supervisor")
                    self._write_json(200, engine.supervision_view())

                elif method == "POST" and parts == ["api", "events", "ingest"]:
                    self._require(principal, "staff")
                    self._write_json(202, engine.ingest_event(self._read_json()))

                else:
                    raise ServiceError("not_found", "路径不存在。")

            except ServiceError as exc:
                status = {
                    "unauthorized": 401, "forbidden": 403,
                    "not_found": 404, "duplicate_case": 409,
                }.get(exc.code, 400)
                self._write_json(status, {"error": {"code": exc.code,
                                                    "message": str(exc),
                                                    "details": exc.extra}})

        def _serve_case(self, engine: Engine, principal: Principal, case_ref: str) -> None:
            if principal.kind == "party":
                self._write_json(200, engine.party_view(case_ref, principal.party_ref))
            elif principal.kind == "supervisor":
                # 监督人员可查看全量材料与留痕
                view = engine.unit_view(case_ref, "SUPV") if engine.repo.unit_grant(case_ref, "SUPV") \
                    else self._supervisor_case_view(engine, case_ref)
                self._write_json(200, view)
            else:
                self._write_json(200, engine.unit_view(case_ref, principal.unit))

        def _supervisor_case_view(self, engine: Engine, case_ref: str) -> dict:
            """监督人员无需逐案授权：组合单位视图并放开全部证据。"""
            case = engine.repo.get_case(case_ref)
            if case is None:
                raise ServiceError("not_found", f"案件不存在：{case_ref}")
            view = engine.deadline_view(case_ref)
            return {
                "case_ref": case_ref, "status": case["status"],
                "dispute_type": case["dispute_type"],
                "grant": {"unit": "SUPV", "scope": "supervision",
                          "confidentiality_ceiling": "secret"},
                "evidence": [dict(e) for e in engine.repo.list_evidence(case_ref)],
                "connections": [dict(c) for c in engine.repo.list_connections(case_ref)],
                "deadline": view,
                "route_history": [
                    {"version": r["version"], "route_name": r["route_name"],
                     "status": r["status"], "change_reason": r["change_reason"]}
                    for r in engine.repo.list_routes(case_ref)],
                "events": [{"seq": e["seq"], "event_id": e["event_id"],
                            "kind": e["kind"], "occurred_at": e["occurred_at"],
                            "unit": e["unit"], "payload_digest": e["payload_digest"]}
                           for e in engine.repo.list_events(case_ref)],
            }

    return Handler


def build_server(database_path: str, port: int) -> ThreadingHTTPServer:
    engine = build_engine(database_path)
    handler = make_handler(None)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.engine = engine          # type: ignore[attr-defined]
    server.repo = engine.repo       # type: ignore[attr-defined]
    return server
