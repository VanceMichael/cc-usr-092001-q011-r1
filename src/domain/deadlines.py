"""法定期限的计算与中止/顺延。

核心规则（回应"时效在转办途中耗尽"的担忧）：
- 案件在共建单位之间移送等待双向确认、管辖异议审查、跨境送达等待域外回证期间，
  法定时效/起诉期间中止（toll），不计入当事人的期限；
- 中止原因消除后恢复计算，已用天数与剩余额度保持不变；
- 任何中止/恢复都产生留痕事件，监督人员可逐段核对。

计算采用纯函数：截止时间 = 受理时间之后累计满 limitation_days 个"非中止日"的时刻；
若存在尚未结束的中止区间，截止时间待定（待恢复后确定）。
"""

from dataclasses import dataclass
from datetime import timedelta

from . import time_utils
from .time_utils import parse


@dataclass(frozen=True)
class TollSegment:
    """一段中止区间：start 时刻起中止，end 时刻恢复（end 不计入中止）。"""

    start: str
    end: str | None  # None 表示中止持续中
    reason: str


def _merge(intervals: list[tuple]) -> list[tuple]:
    """合并重叠/相邻的中止区间，防御重复登记导致的重复扣减。"""
    closed = sorted((parse(s), parse(e), r) for s, e, r in intervals if e is not None)
    merged: list[tuple] = []
    for s, e, r in closed:
        if merged and s <= merged[-1][1]:
            ps, pe, pr = merged[-1]
            merged[-1] = (ps, max(pe, e), f"{pr}；{r}")
        else:
            merged.append((s, e, r))
    return merged


def compute_deadline(
    accepted_at: str,
    limitation_days: int,
    tolls: list[TollSegment],
    *,
    as_of: str | None = None,
) -> dict:
    """计算法定截止时间与剩余额度。

    返回 deadline_at（中止持续中为 null）、used_days、tolled_days、
    remaining_days、open_toll。天数按日历日（24 小时一日）计算。
    """
    start = parse(accepted_at)
    now_dt = parse(as_of) if as_of else time_utils.now()

    opens = sorted(
        (parse(t.start), t.reason) for t in tolls if t.end is None
    )
    merged = _merge([(t.start, t.end, t.reason) for t in tolls])

    # 若存在未结束的中止，其开始点之后不再产生截止时间；只扫描该点之前的区间。
    open_start = opens[0][0] if opens else None
    open_reason = opens[0][1] if opens else None

    cursor = start
    remaining = float(limitation_days)
    deadline = None
    for s, e, _r in merged:
        if open_start is not None and s >= open_start:
            break
        s = max(s, cursor)
        if s > cursor:
            gap = (s - cursor).total_seconds() / 86400
            if gap >= remaining:
                deadline = cursor + timedelta(days=remaining)
                remaining = 0
                break
            remaining -= gap
            cursor = s
        cursor = max(cursor, e)
    if deadline is None and remaining > 0:
        if open_start is not None and cursor < open_start:
            gap = (open_start - cursor).total_seconds() / 86400
            if gap >= remaining:
                deadline = cursor + timedelta(days=remaining)
                remaining = 0
            else:
                remaining -= gap
        elif open_start is None:
            deadline = cursor + timedelta(days=remaining)

    # 统计口径（截至 now_dt）：中止区间按实际重叠 [start, now] 累计
    tolled_seconds = 0.0
    for s, e, _r in merged:
        seg_end = min(e, now_dt)
        if seg_end > s:
            tolled_seconds += (seg_end - s).total_seconds()
    if open_start is not None and now_dt > open_start:
        tolled_seconds += (now_dt - open_start).total_seconds()

    elapsed = max((now_dt - start).total_seconds(), 0)
    used_days = max(elapsed - tolled_seconds, 0) / 86400
    tolled_days = tolled_seconds / 86400

    if deadline is not None:
        remaining_now = max((deadline - now_dt).total_seconds() / 86400, 0)
        deadline_out = time_utils.format_value(deadline)
        open_toll = None
    else:
        remaining_now = max(remaining, 0)
        deadline_out = None
        open_toll = (
            {"start": time_utils.format_value(open_start), "reason": open_reason}
            if open_start is not None
            else None
        )

    return {
        "deadline_at": deadline_out,
        "used_days": round(used_days, 2),
        "tolled_days": round(max(tolled_days, 0), 2),
        "remaining_days": round(remaining_now, 2),
        "open_toll": open_toll,
    }
