"""重复立案指纹：识别重复，但不同请求只关联不合并。"""

import unittest

from src.domain.enums import DisputeType
from src.domain.fingerprint import build_fingerprints

P = "VOY-PL-2026-077"
PARTIES = [
    {"party_ref": "PARTY-CREW-001", "role": "claimant"},
    {"party_ref": "PARTY-SHIPCO-001", "role": "respondent"},
]
WAGES = [{"claim_kind": "wages", "subject_ref": "SUBJ-PAYROLL-08",
          "period_start": "2026-06-01", "period_end": "2026-08-31"}]
INJURY = [{"claim_kind": "personal_injury", "subject_ref": "SUBJ-INJURY-0812"}]


class FingerprintTest(unittest.TestCase):
    def test_identical_requests_same_hard_fp(self) -> None:
        a = build_fingerprints(DisputeType.CREW_LABOR, PARTIES, WAGES, P)
        b = build_fingerprints(DisputeType.CREW_LABOR, list(reversed(PARTIES)), WAGES, P)
        self.assertEqual(a.hard, b.hard)  # 当事方顺序不影响

    def test_distinct_claims_soft_only(self) -> None:
        wages = build_fingerprints(DisputeType.CREW_LABOR, PARTIES, WAGES, P)
        injury = build_fingerprints(DisputeType.CREW_LABOR, PARTIES, INJURY, P)
        self.assertNotEqual(wages.hard, injury.hard)   # 不同请求 → 不能误合并
        self.assertEqual(wages.soft, injury.soft)      # 同主体同类型 → 仅关联

    def test_claims_order_insensitive(self) -> None:
        a = build_fingerprints(DisputeType.CARGO_TRANSPORT, PARTIES,
                               WAGES + INJURY, P)
        b = build_fingerprints(DisputeType.CARGO_TRANSPORT, PARTIES,
                               INJURY + WAGES, P)
        self.assertEqual(a.hard, b.hard)


if __name__ == "__main__":
    unittest.main()
