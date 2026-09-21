"""SQLite 数据访问：所有写操作在同一连接事务内完成，事件账本只追加。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(database_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(database_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> int:
    """建表并写入种子数据，返回当前 schema 版本。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    from ..domain.rules import UNITS

    for code, info in UNITS.items():
        conn.execute(
            "INSERT OR IGNORE INTO service_meta(key, value) VALUES(?, ?)",
            (f"unit:{code}", info["name"]),
        )
    conn.execute(
        "INSERT OR IGNORE INTO service_meta(key, value) VALUES('schema_version', '2')"
    )
    conn.commit()
    return 2


def digest_payload(payload: dict) -> str:
    import hashlib

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- 案件登记 -----------------------------------------------------

    def insert_case(self, case: dict) -> None:
        c = self.conn
        c.execute(
            """INSERT INTO cases(case_ref, dispute_type, status, relation_ref,
                  fp_hard, fp_soft, accepted_at, limitation_days,
                  limitation_basis, active_route_id, created_at)
               VALUES(:case_ref,:dispute_type,:status,:relation_ref,:fp_hard,:fp_soft,
                  :accepted_at,:limitation_days,:limitation_basis,NULL,:created_at)""",
            case,
        )
        for p in case["parties"]:
            c.execute(
                "INSERT INTO parties(case_ref, party_ref, role, contact_ref) VALUES(?,?,?,?)",
                (case["case_ref"], p["party_ref"], p["role"], p.get("contact_ref")),
            )
        for i, cl in enumerate(case["claims"], start=1):
            c.execute(
                """INSERT INTO claims(case_ref, claim_seq, claim_kind, subject_ref,
                       period_start, period_end, amount_ref, status, updated_at)
                   VALUES(?,?,?,?,?,?,?,'active',?)""",
                (case["case_ref"], i, cl["claim_kind"], cl.get("subject_ref"),
                 cl.get("period_start"), cl.get("period_end"), cl.get("amount_ref"),
                 case["accepted_at"]),
            )
        for i, pt in enumerate(case.get("connections", []), start=1):
            c.execute(
                "INSERT INTO connections(case_ref, seq, point_type, point_ref) VALUES(?,?,?,?)",
                (case["case_ref"], i, pt["point_type"], pt["point_ref"]),
            )
        for i, ev in enumerate(case.get("evidence", []), start=1):
            c.execute(
                """INSERT INTO evidence(case_ref, seq, evidence_ref, digest,
                       confidentiality, holder_unit, sealed_at, note)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (case["case_ref"], i, ev["evidence_ref"], ev["digest"],
                 ev["confidentiality"], ev["holder_unit"], ev["sealed_at"],
                 ev.get("note")),
            )

    def find_duplicates(self, fp_hard: str, fp_soft: str) -> dict:
        c = self.conn
        hard = [r["case_ref"] for r in c.execute(
            "SELECT case_ref FROM cases WHERE fp_hard = ? AND status != 'withdrawn'", (fp_hard,))]
        soft = [r["case_ref"] for r in c.execute(
            "SELECT case_ref FROM cases WHERE fp_soft = ? AND fp_hard != ? AND status != 'withdrawn'",
            (fp_soft, fp_hard))]
        return {"hard": hard, "soft": soft}

    def add_link(self, a: str, b: str, kind: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO case_links(case_ref_a, case_ref_b, kind) VALUES(?,?,?)",
            (a, b, kind),
        )

    def get_case(self, case_ref: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM cases WHERE case_ref = ?", (case_ref,)).fetchone()

    def list_parties(self, case_ref: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM parties WHERE case_ref = ? ORDER BY rowid", (case_ref,)).fetchall()

    def list_claims(self, case_ref: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM claims WHERE case_ref = ? ORDER BY claim_seq", (case_ref,)).fetchall()

    def list_connections(self, case_ref: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM connections WHERE case_ref = ? ORDER BY seq", (case_ref,)).fetchall()

    def list_evidence(self, case_ref: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM evidence WHERE case_ref = ? ORDER BY seq", (case_ref,)).fetchall()

    def update_claim_status(self, case_ref: str, seqs: list[int], status: str, at: str) -> None:
        if not seqs:
            return
        self.conn.executemany(
            "UPDATE claims SET status = ?, updated_at = ? WHERE case_ref = ? AND claim_seq = ?",
            [(status, at, case_ref, s) for s in seqs],
        )

    # ---- 路径与节点 ---------------------------------------------------

    def insert_route(self, case_ref: str, route: dict) -> int:
        c = self.conn
        cur = c.execute(
            """INSERT INTO routes(case_ref, version, route_key, route_name,
                  choice_explanation, status, change_reason, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (case_ref, route["version"], route["route_key"], route["route_name"],
             route["choice_explanation"], "active", route.get("change_reason"),
             route["created_at"]),
        )
        route_id = cur.lastrowid
        for i, w in enumerate(route.get("warnings", ()), start=1):
            c.execute("INSERT INTO route_warnings(route_id, seq, warning) VALUES(?,?,?)",
                      (route_id, i, w))
        for i, seg in enumerate(route["segments"], start=1):
            c.execute(
                """INSERT INTO stages(case_ref, route_id, seq, node, unit, label,
                       legal_basis, reason, time_limit_days, optional, status,
                       entered_at, closed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (case_ref, route_id, i, seg["node"], seg["unit"], seg["label"],
                 seg["legal_basis"], seg["reason"], seg["time_limit_days"],
                 1 if seg.get("optional") else 0,
                 "active" if i == 1 else "pending",
                 route["created_at"] if i == 1 else None, None),
            )
        c.execute("UPDATE cases SET active_route_id = ? WHERE case_ref = ?",
                  (route_id, case_ref))
        return route_id

    def supersede_route(self, case_ref: str, at: str) -> None:
        """旧路径整体置为 superseded 但保留；其上未交接的活动/等待节点一并标记。"""
        c = self.conn
        row = c.execute(
            "SELECT id FROM routes WHERE case_ref = ? AND status = 'active'", (case_ref,)).fetchone()
        if not row:
            return
        c.execute("UPDATE routes SET status = 'superseded' WHERE id = ?", (row["id"],))
        c.execute(
            "UPDATE stages SET status = 'superseded' WHERE route_id = ? AND status IN ('active','pending')",
            (row["id"],))

    def get_active_route(self, case_ref: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM routes WHERE case_ref = ? AND status = 'active'", (case_ref,)).fetchone()

    def list_routes(self, case_ref: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM routes WHERE case_ref = ? ORDER BY version", (case_ref,)).fetchall()

    def list_stages(self, route_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM stages WHERE route_id = ? ORDER BY seq", (route_id,)).fetchall()

    def get_stage(self, stage_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM stages WHERE id = ?", (stage_id,)).fetchone()

    def active_stage(self, case_ref: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT s.* FROM stages s JOIN cases c ON c.active_route_id = s.route_id
               WHERE c.case_ref = ? AND s.status IN ('active','pending')
               ORDER BY s.seq LIMIT 1""", (case_ref,)).fetchone()

    def mark_stage(self, stage_id: int, status: str, at: str) -> None:
        if status == "active":
            self.conn.execute(
                "UPDATE stages SET status = 'active', entered_at = COALESCE(entered_at, ?) WHERE id = ?",
                (at, stage_id))
        elif status in ("handed_off", "closed", "void", "superseded"):
            self.conn.execute(
                "UPDATE stages SET status = ?, closed_at = COALESCE(closed_at, ?) WHERE id = ?",
                (status, at, stage_id))
        else:
            self.conn.execute("UPDATE stages SET status = ? WHERE id = ?", (status, stage_id))

    # ---- 移送双向确认 -------------------------------------------------

    def insert_transfer(self, t: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO transfers(transfer_ref, case_ref, from_stage_id, to_stage_id,
                  from_unit, to_unit, state, material_digest, toll_id,
                  proposed_at, proposed_by, responded_at, responded_by, response_note)
               VALUES(:transfer_ref,:case_ref,:from_stage_id,:to_stage_id,:from_unit,:to_unit,
                  'proposed',:material_digest,:toll_id,:proposed_at,:proposed_by,NULL,NULL,NULL)""",
            t,
        )
        return cur.lastrowid

    def get_transfer(self, transfer_ref: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM transfers WHERE transfer_ref = ?", (transfer_ref,)).fetchone()

    def respond_transfer(self, transfer_ref: str, state: str, at: str,
                         actor: str, note: str | None) -> None:
        self.conn.execute(
            """UPDATE transfers SET state = ?, responded_at = ?, responded_by = ?,
                   response_note = ? WHERE transfer_ref = ?""",
            (state, at, actor, note, transfer_ref))

    # ---- 期限中止 -----------------------------------------------------

    def open_toll(self, case_ref: str, reason: str, start_at: str, actor: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO tolls(case_ref, reason, start_at, end_at, started_by, ended_by) VALUES(?,?,?,NULL,?,NULL)",
            (case_ref, reason, start_at, actor))
        return cur.lastrowid

    def close_toll(self, toll_id: int, end_at: str, actor: str) -> None:
        self.conn.execute(
            "UPDATE tolls SET end_at = ?, ended_by = ? WHERE id = ? AND end_at IS NULL",
            (end_at, actor, toll_id))

    def list_tolls(self, case_ref: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tolls WHERE case_ref = ? ORDER BY start_at", (case_ref,)).fetchall()

    def bind_transfer_toll(self, transfer_id: int, toll_id: int) -> None:
        self.conn.execute("UPDATE transfers SET toll_id = ? WHERE id = ?", (toll_id, transfer_id))

    # ---- 事件账本 -----------------------------------------------------

    def append_event(self, case_ref: str, kind: str, occurred_at: str,
                     unit: str | None, actor_ref: str | None, payload: dict) -> int:
        c = self.conn
        seq_row = c.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE case_ref = ?",
            (case_ref,)).fetchone()
        seq = seq_row["next_seq"]
        event_id = f"EVENT-{case_ref}-{seq:04d}"
        c.execute(
            """INSERT INTO events(case_ref, seq, event_id, kind, occurred_at, unit,
                  actor_ref, payload_json, payload_digest)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (case_ref, seq, event_id, kind, occurred_at, unit, actor_ref,
             json.dumps(payload, ensure_ascii=False), digest_payload(payload)),
        )
        return seq

    def list_events(self, case_ref: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE case_ref = ? ORDER BY seq", (case_ref,)).fetchall()

    # ---- 授权 ---------------------------------------------------------

    def grant(self, case_ref: str, unit: str, scope: str, ceiling: str,
              actor: str, at: str) -> None:
        self.conn.execute(
            """INSERT INTO grants(case_ref, unit, scope, confidentiality_ceiling,
                  granted_by, granted_at, revoked_at)
               VALUES(?,?,?,?,?,?,NULL)
               ON CONFLICT(case_ref, unit, scope) DO UPDATE SET
                  confidentiality_ceiling=excluded.confidentiality_ceiling,
                  granted_by=excluded.granted_by, granted_at=excluded.granted_at,
                  revoked_at=NULL""",
            (case_ref, unit, scope, ceiling, actor, at))

    def list_grants(self, case_ref: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM grants WHERE case_ref = ? AND revoked_at IS NULL",
            (case_ref,)).fetchall()

    def unit_grant(self, case_ref: str, unit: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM grants WHERE case_ref = ? AND unit = ? AND revoked_at IS NULL
               ORDER BY CASE scope WHEN 'materials' THEN 0 ELSE 1 END LIMIT 1""",
            (case_ref, unit)).fetchone()

    # ---- 状态与监督 ---------------------------------------------------

    def set_case_status(self, case_ref: str, status: str) -> None:
        self.conn.execute("UPDATE cases SET status = ? WHERE case_ref = ?", (status, case_ref))

    def supervision_rows(self) -> list[sqlite3.Row]:
        """每个案件当前活动节点及其实际停留时间（秒）。"""
        return self.conn.execute(
            """SELECT c.case_ref, c.dispute_type, c.status AS case_status,
                      s.id AS stage_id, s.route_id AS route_id,
                      s.seq AS stage_seq, s.node, s.unit,
                      s.label, s.entered_at, s.time_limit_days,
                      r.route_name, r.version
                 FROM cases c
                 JOIN routes r ON r.id = c.active_route_id
                 JOIN stages s ON s.route_id = r.id
                WHERE s.status = 'active'
                ORDER BY c.case_ref""").fetchall()

    def pending_transfers(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM transfers WHERE state = 'proposed' ORDER BY proposed_at").fetchall()

    # ---- 演示令牌 -----------------------------------------------------

    def upsert_party_token(self, party_ref: str, token: str, at: str) -> None:
        self.conn.execute(
            "INSERT INTO party_tokens(party_ref, token, issued_at) VALUES(?,?,?) "
            "ON CONFLICT(party_ref) DO UPDATE SET token=excluded.token, issued_at=excluded.issued_at",
            (party_ref, token, at))

    def party_by_token(self, token: str) -> str | None:
        row = self.conn.execute(
            "SELECT party_ref FROM party_tokens WHERE token = ?", (token,)).fetchone()
        return row["party_ref"] if row else None

    def upsert_unit_token(self, token: str, unit: str, role: str) -> None:
        self.conn.execute(
            "INSERT INTO unit_tokens(token, unit, role) VALUES(?,?,?) "
            "ON CONFLICT(token) DO UPDATE SET unit=excluded.unit, role=excluded.role",
            (token, unit, role))

    def unit_by_token(self, token: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT unit, role FROM unit_tokens WHERE token = ?", (token,)).fetchone()

    def list_case_refs_for_party(self, party_ref: str) -> list[str]:
        return [r["case_ref"] for r in self.conn.execute(
            "SELECT case_ref FROM parties WHERE party_ref = ? ORDER BY case_ref",
            (party_ref,)).fetchall()]
