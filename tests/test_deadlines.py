"""期限中止/恢复计算的确定性测试。"""

import unittest

from src.domain.deadlines import TollSegment, compute_deadline

ACCEPTED = "2026-09-01T09:00:00+08:00"


class DeadlineTest(unittest.TestCase):
    def test_no_toll(self) -> None:
        out = compute_deadline(ACCEPTED, 10, [], as_of="2026-09-06T09:00:00+08:00")
        self.assertEqual(out["deadline_at"], "2026-09-11T09:00:00+08:00")
        self.assertEqual(out["remaining_days"], 5.0)
        self.assertIsNone(out["open_toll"])

    def test_closed_toll_extends_deadline(self) -> None:
        # 9月3日起中止3天（移送等待签收），截止日应顺延3天
        tolls = [TollSegment("2026-09-03T09:00:00+08:00",
                             "2026-09-06T09:00:00+08:00", "移送等待签收")]
        out = compute_deadline(ACCEPTED, 10, tolls, as_of="2026-09-10T09:00:00+08:00")
        self.assertEqual(out["deadline_at"], "2026-09-14T09:00:00+08:00")
        self.assertEqual(out["tolled_days"], 3.0)
        self.assertEqual(out["used_days"], 6.0)

    def test_open_toll_deadline_undetermined(self) -> None:
        # 跨境送达等待回证、中止持续中：截止日待定，但剩余额度不被消耗
        tolls = [TollSegment("2026-09-03T09:00:00+08:00", None, "跨境送达等待回证")]
        out = compute_deadline(ACCEPTED, 10, tolls, as_of="2026-10-01T09:00:00+08:00")
        self.assertIsNone(out["deadline_at"])
        self.assertIsNotNone(out["open_toll"])
        # 中止前已用 2 天，剩余 8 天，尽管自然时间已过一个月
        self.assertEqual(out["remaining_days"], 8.0)
        self.assertGreaterEqual(out["tolled_days"], 27.9)

    def test_overlapping_tolls_merged(self) -> None:
        tolls = [
            TollSegment("2026-09-03T09:00:00+08:00", "2026-09-06T09:00:00+08:00", "移送"),
            TollSegment("2026-09-05T09:00:00+08:00", "2026-09-08T09:00:00+08:00", "异议"),
        ]
        out = compute_deadline(ACCEPTED, 10, tolls, as_of="2026-09-09T09:00:00+08:00")
        # 合并后中止 5 天（3日→8日），截止日 16 日
        self.assertEqual(out["deadline_at"], "2026-09-16T09:00:00+08:00")
        self.assertEqual(out["tolled_days"], 5.0)


if __name__ == "__main__":
    unittest.main()
