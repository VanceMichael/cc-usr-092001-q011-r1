"""SQLite 数据访问层。

所有 SQL 集中在该模块，领域层与接口层只面对字典。存储持有单一连接并用锁
串行化写入，配合 `check_same_thread=False` 支持线程安全的 HTTP 服务。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .schema import DDL, SCHEMA_VERSION


class Store:
    """线程安全的 SQLite 存储。"""

    def __init__(self, database_path: str):
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(database_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._conn:
            for statement in DDL:
                self._conn.execute(statement)
            self._conn.execute(
                "INSERT OR IGNORE INTO service_meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 基础读写 ----

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock, self._conn:
            self._conn.execute(sql, params)

    # ---- 当事方 ----

    def insert_party(self, row: dict) -> None:
        self._write(
            "INSERT INTO parties(party_ref, party_type, id_hash, contact_ref, registered_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (
                row["party_ref"],
                row["party_type"],
                row["id_hash"],
                row.get("contact_ref"),
                row["registered_at"],
            ),
        )

    def get_party(self, party_ref: str) -> dict | None:
        return self._one("SELECT * FROM parties WHERE party_ref = ?", (party_ref,))

    # ---- 案件 ----

    def insert_case(self, row: dict) -> None:
        self._write(
            "INSERT INTO cases(case_ref, dispute_type, connection_points, election,"
            " arbitration_agreement, cross_border, status, confidentiality,"
            " opened_by_unit, opened_at, closed_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["case_ref"],
                row["dispute_type"],
                row["connection_points"],
                row["election"],
                row["arbitration_agreement"],
                row["cross_border"],
                row["status"],
                row["confidentiality"],
                row["opened_by_unit"],
                row["opened_at"],
                row.get("closed_at"),
            ),
        )

    def get_case(self, case_ref: str) -> dict | None:
        return self._one("SELECT * FROM cases WHERE case_ref = ?", (case_ref,))

    def update_case(self, case_ref: str, **fields: object) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self._write(
            f"UPDATE cases SET {assignments} WHERE case_ref = ?",
            (*fields.values(), case_ref),
        )

    def insert_case_party(self, case_ref: str, party_ref: str, role: str) -> None:
        self._write(
            "INSERT OR IGNORE INTO case_parties(case_ref, party_ref, role) VALUES(?, ?, ?)",
            (case_ref, party_ref, role),
        )

    def case_party_refs(self, case_ref: str) -> list[str]:
        rows = self._rows(
            "SELECT DISTINCT party_ref FROM case_parties WHERE case_ref = ?", (case_ref,)
        )
        return [row["party_ref"] for row in rows]

    # ---- 请求事项 ----

    def insert_claim(self, row: dict) -> None:
        self._write(
            "INSERT INTO claims(claim_ref, case_ref, claim_type, amount, currency,"
            " summary_ref, status, duplicate_of) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["claim_ref"],
                row["case_ref"],
                row["claim_type"],
                row.get("amount"),
                row.get("currency"),
                row.get("summary_ref"),
                row["status"],
                row.get("duplicate_of"),
            ),
        )

    def get_claim(self, claim_ref: str) -> dict | None:
        return self._one("SELECT * FROM claims WHERE claim_ref = ?", (claim_ref,))

    def claims_of_case(self, case_ref: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM claims WHERE case_ref = ? ORDER BY claim_ref", (case_ref,)
        )

    def update_claim(self, claim_ref: str, **fields: object) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self._write(
            f"UPDATE claims SET {assignments} WHERE claim_ref = ?",
            (*fields.values(), claim_ref),
        )

    def claims_for_duplicate_scan(self, exclude_case_ref: str) -> list[dict]:
        """返回其他案件的有效请求及其当事人身份散列，供重复立案识别。"""
        rows = self._rows(
            "SELECT c.claim_ref, c.case_ref, c.claim_type, k.dispute_type"
            " FROM claims c JOIN cases k ON k.case_ref = c.case_ref"
            " WHERE k.case_ref != ? AND c.status = '有效'",
            (exclude_case_ref,),
        )
        for row in rows:
            hashes = self._rows(
                "SELECT p.id_hash FROM case_parties cp"
                " JOIN parties p ON p.party_ref = cp.party_ref"
                " WHERE cp.case_ref = ?",
                (row["case_ref"],),
            )
            row["party_hashes"] = [item["id_hash"] for item in hashes]
        return rows

    # ---- 证据封存 ----

    def insert_evidence(self, row: dict) -> None:
        self._write(
            "INSERT INTO evidence_seals(evidence_ref, case_ref, digest, sealed_by_unit,"
            " sealed_at, note_ref) VALUES(?, ?, ?, ?, ?, ?)",
            (
                row["evidence_ref"],
                row["case_ref"],
                row["digest"],
                row["sealed_by_unit"],
                row["sealed_at"],
                row.get("note_ref"),
            ),
        )

    def evidence_of_case(self, case_ref: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM evidence_seals WHERE case_ref = ? ORDER BY evidence_ref",
            (case_ref,),
        )

    # ---- 法定期限 ----

    def insert_deadline(self, row: dict) -> None:
        self._write(
            "INSERT INTO deadlines(deadline_ref, case_ref, kind, basis, start_at, due_at,"
            " warn_days, status) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["deadline_ref"],
                row["case_ref"],
                row["kind"],
                row["basis"],
                row["start_at"],
                row["due_at"],
                row["warn_days"],
                row["status"],
            ),
        )

    def deadlines_of_case(self, case_ref: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM deadlines WHERE case_ref = ? ORDER BY due_at", (case_ref,)
        )

    def update_deadline(self, deadline_ref: str, **fields: object) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self._write(
            f"UPDATE deadlines SET {assignments} WHERE deadline_ref = ?",
            (*fields.values(), deadline_ref),
        )

    # ---- 程序路径 ----

    def insert_version(self, row: dict) -> None:
        self._write(
            "INSERT INTO path_versions(version_ref, case_ref, version_no, reason,"
            " created_at, superseded_at) VALUES(?, ?, ?, ?, ?, ?)",
            (
                row["version_ref"],
                row["case_ref"],
                row["version_no"],
                row["reason"],
                row["created_at"],
                row.get("superseded_at"),
            ),
        )

    def versions_of_case(self, case_ref: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM path_versions WHERE case_ref = ? ORDER BY version_no",
            (case_ref,),
        )

    def current_version(self, case_ref: str) -> dict | None:
        return self._one(
            "SELECT * FROM path_versions WHERE case_ref = ? AND superseded_at IS NULL"
            " ORDER BY version_no DESC LIMIT 1",
            (case_ref,),
        )

    def supersede_version(self, version_ref: str, at: str) -> None:
        self._write(
            "UPDATE path_versions SET superseded_at = ? WHERE version_ref = ?",
            (at, version_ref),
        )

    def insert_node(self, row: dict) -> None:
        self._write(
            "INSERT INTO path_nodes(node_ref, version_ref, case_ref, seq, unit_kind,"
            " action, reason, planned_days, status, entered_at, left_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["node_ref"],
                row["version_ref"],
                row["case_ref"],
                row["seq"],
                row["unit_kind"],
                row["action"],
                row["reason"],
                row.get("planned_days"),
                row["status"],
                row.get("entered_at"),
                row.get("left_at"),
            ),
        )

    def nodes_of_version(self, version_ref: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM path_nodes WHERE version_ref = ? ORDER BY seq", (version_ref,)
        )

    def get_node(self, node_ref: str) -> dict | None:
        return self._one("SELECT * FROM path_nodes WHERE node_ref = ?", (node_ref,))

    def update_node(self, node_ref: str, **fields: object) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self._write(
            f"UPDATE path_nodes SET {assignments} WHERE node_ref = ?",
            (*fields.values(), node_ref),
        )

    # ---- 移送 ----

    def insert_transfer(self, row: dict) -> None:
        self._write(
            "INSERT INTO transfers(transfer_ref, case_ref, kind, from_node_ref,"
            " from_unit_kind, to_unit_kind, reason, materials, confidentiality,"
            " deadline_snapshot, status, handover_by, handover_at, receipt_by,"
            " receipt_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["transfer_ref"],
                row["case_ref"],
                row["kind"],
                row.get("from_node_ref"),
                row["from_unit_kind"],
                row["to_unit_kind"],
                row["reason"],
                row["materials"],
                row["confidentiality"],
                row["deadline_snapshot"],
                row["status"],
                row.get("handover_by"),
                row.get("handover_at"),
                row.get("receipt_by"),
                row.get("receipt_at"),
                row["created_at"],
            ),
        )

    def get_transfer(self, transfer_ref: str) -> dict | None:
        return self._one(
            "SELECT * FROM transfers WHERE transfer_ref = ?", (transfer_ref,)
        )

    def transfers_of_case(self, case_ref: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM transfers WHERE case_ref = ? ORDER BY created_at", (case_ref,)
        )

    def open_transfer_of_case(self, case_ref: str) -> dict | None:
        return self._one(
            "SELECT * FROM transfers WHERE case_ref = ? AND status IN"
            " ('待交出确认', '待接收确认') ORDER BY created_at DESC LIMIT 1",
            (case_ref,),
        )

    def update_transfer(self, transfer_ref: str, **fields: object) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self._write(
            f"UPDATE transfers SET {assignments} WHERE transfer_ref = ?",
            (*fields.values(), transfer_ref),
        )

    # ---- 重复立案标记 ----

    def insert_flag(self, row: dict) -> None:
        self._write(
            "INSERT INTO duplicate_flags(flag_ref, case_ref, claim_ref, matched_claim_ref,"
            " explanation, status, decided_by, decided_at, decision_reason, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["flag_ref"],
                row["case_ref"],
                row["claim_ref"],
                row["matched_claim_ref"],
                row["explanation"],
                row["status"],
                row.get("decided_by"),
                row.get("decided_at"),
                row.get("decision_reason"),
                row["created_at"],
            ),
        )

    def get_flag(self, flag_ref: str) -> dict | None:
        return self._one(
            "SELECT * FROM duplicate_flags WHERE flag_ref = ?", (flag_ref,)
        )

    def flags_of_case(self, case_ref: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM duplicate_flags WHERE case_ref = ? ORDER BY created_at",
            (case_ref,),
        )

    def update_flag(self, flag_ref: str, **fields: object) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self._write(
            f"UPDATE duplicate_flags SET {assignments} WHERE flag_ref = ?",
            (*fields.values(), flag_ref),
        )

    # ---- 授权 ----

    def insert_grant(self, row: dict) -> None:
        self._write(
            "INSERT INTO grants(grant_ref, case_ref, grantee_kind, grantee_ref, scopes,"
            " evidence_refs, valid_from, valid_to) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["grant_ref"],
                row["case_ref"],
                row["grantee_kind"],
                row["grantee_ref"],
                row["scopes"],
                row["evidence_refs"],
                row["valid_from"],
                row.get("valid_to"),
            ),
        )

    def grants_of_case(self, case_ref: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM grants WHERE case_ref = ? ORDER BY valid_from", (case_ref,)
        )

    def grant_for_unit(self, case_ref: str, unit_kind: str) -> dict | None:
        return self._one(
            "SELECT * FROM grants WHERE case_ref = ? AND grantee_kind = 'unit'"
            " AND grantee_ref = ? AND valid_to IS NULL"
            " ORDER BY valid_from DESC LIMIT 1",
            (case_ref, unit_kind),
        )

    # ---- 事件 ----

    def insert_event(self, row: dict) -> None:
        self._write(
            "INSERT INTO events(event_id, subject_ref, occurred_at, source_unit,"
            " source_sequence, payload_digest, recorded_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                row["event_id"],
                row["subject_ref"],
                row["occurred_at"],
                row["source_unit"],
                row["source_sequence"],
                row["payload_digest"],
                row["recorded_at"],
            ),
        )

    def get_event(self, event_id: str) -> dict | None:
        return self._one("SELECT * FROM events WHERE event_id = ?", (event_id,))
