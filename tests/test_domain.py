"""领域逻辑检查：路径生成、重复立案识别、期限计算与移送状态机。"""

import unittest
from datetime import datetime, timezone

from src import domain
from src.domain import DomainError

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


class GeneratePathTest(unittest.TestCase):
    def test_crew_labor_litigation_starts_with_arbitration(self) -> None:
        nodes, explanation = domain.generate_path("crew_labor", "litigation", False, False)
        self.assertEqual([n["unit_kind"] for n in nodes], ["labor", "court"])
        self.assertIn("仲裁前置", explanation)
        self.assertTrue(all(n["reason"] for n in nodes))

    def test_mediation_path_includes_judicial_confirmation(self) -> None:
        nodes, _ = domain.generate_path("water_accident", "mediation", False, False)
        self.assertEqual(nodes[0]["unit_kind"], "mediation")
        self.assertEqual(nodes[1]["action"], "司法确认")
        self.assertIn("强制执行力", nodes[1]["reason"])

    def test_commercial_arbitration_requires_agreement(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            domain.generate_path("cargo_transport", "commercial_arbitration", False, False)
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("litigation", ctx.exception.extra["suggested"])

    def test_crew_labor_cannot_use_commercial_arbitration(self) -> None:
        with self.assertRaises(DomainError):
            domain.generate_path("crew_labor", "commercial_arbitration", True, False)

    def test_cross_border_appends_service_node(self) -> None:
        nodes, explanation = domain.generate_path("cargo_transport", "litigation", False, True)
        self.assertEqual(nodes[-1]["action"], "跨境送达协助")
        self.assertIn("涉外", explanation)

    def test_water_accident_litigation_is_maritime_court(self) -> None:
        nodes, _ = domain.generate_path("water_accident", "litigation", False, False)
        self.assertIn("海事法院", nodes[0]["action"])


class DetectDuplicatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.existing = [{
            "claim_ref": "CLAIM-OLD",
            "case_ref": "CASE-OLD",
            "claim_type": "工资报酬",
            "dispute_type": "crew_labor",
            "party_hashes": ["hash-a", "hash-b"],
        }]

    def test_same_parties_type_and_claim_type_flagged(self) -> None:
        flags = domain.detect_duplicates(
            [{"claim_ref": "CLAIM-NEW", "claim_type": "工资报酬"}],
            {"hash-a", "hash-b"},
            "crew_labor",
            self.existing,
        )
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["matched_claim_ref"], "CLAIM-OLD")

    def test_different_claim_type_never_merged(self) -> None:
        flags = domain.detect_duplicates(
            [{"claim_ref": "CLAIM-NEW", "claim_type": "工伤赔偿"}],
            {"hash-a", "hash-b"},
            "crew_labor",
            self.existing,
        )
        self.assertEqual(flags, [])

    def test_different_dispute_type_not_flagged(self) -> None:
        flags = domain.detect_duplicates(
            [{"claim_ref": "CLAIM-NEW", "claim_type": "工资报酬"}],
            {"hash-a", "hash-b"},
            "cargo_transport",
            self.existing,
        )
        self.assertEqual(flags, [])

    def test_single_shared_party_not_enough(self) -> None:
        flags = domain.detect_duplicates(
            [{"claim_ref": "CLAIM-NEW", "claim_type": "工资报酬"}],
            {"hash-a", "hash-c"},
            "crew_labor",
            self.existing,
        )
        self.assertEqual(flags, [])


class DeadlineTest(unittest.TestCase):
    def _row(self, due_at: str, warn_days: int = 30) -> dict:
        return {
            "deadline_ref": "DDL-1",
            "kind": "仲裁时效",
            "basis": "劳动争议调解仲裁法第二十七条",
            "due_at": due_at,
            "warn_days": warn_days,
            "status": domain.DEADLINE_RUNNING,
        }

    def test_remaining_and_warning(self) -> None:
        view = domain.deadline_view(self._row("2026-10-01T00:00:00+08:00"), NOW)
        self.assertEqual(view["status"], "运行中")
        self.assertTrue(view["warning"])
        self.assertLessEqual(view["remaining_days"], 10)

    def test_expired_is_computed(self) -> None:
        view = domain.deadline_view(self._row("2026-09-01T00:00:00+08:00"), NOW)
        self.assertEqual(view["status"], "已届满")

    def test_naive_time_rejected(self) -> None:
        with self.assertRaises(DomainError):
            domain.parse_iso("2026-09-21 12:00:00")


class TransferStateTest(unittest.TestCase):
    def _transfer(self, status: str) -> dict:
        return {
            "transfer_ref": "TRANS-1",
            "status": status,
            "from_unit_kind": "mediation",
            "to_unit_kind": "court",
        }

    def test_bidirectional_confirm_order(self) -> None:
        transfer = self._transfer(domain.TRANSFER_PENDING_HANDOVER)
        with self.assertRaises(DomainError):
            domain.confirm_transfer(transfer, "receipt", "court", NOW)
        updates = domain.confirm_transfer(transfer, "handover", "mediation", NOW)
        self.assertEqual(updates["status"], domain.TRANSFER_PENDING_RECEIPT)
        updates = domain.confirm_transfer({**transfer, **updates}, "receipt", "court", NOW)
        self.assertEqual(updates["status"], domain.TRANSFER_COMPLETED)

    def test_wrong_unit_rejected(self) -> None:
        transfer = self._transfer(domain.TRANSFER_PENDING_HANDOVER)
        with self.assertRaises(DomainError):
            domain.confirm_transfer(transfer, "handover", "court", NOW)

    def test_reject_only_by_receiver(self) -> None:
        transfer = self._transfer(domain.TRANSFER_PENDING_RECEIPT)
        with self.assertRaises(DomainError):
            domain.reject_transfer(transfer, "mediation", NOW)
        updates = domain.reject_transfer(transfer, "court", NOW)
        self.assertEqual(updates["status"], domain.TRANSFER_REJECTED)


if __name__ == "__main__":
    unittest.main()
