"""重复立案识别。

识别但不能误合并不同请求，因此指纹分两级：
- 强指纹（hard）：当事方 + 争议类型 + 同一法律关系标识 + 请求事项相同 → 判定重复立案；
- 弱指纹（soft）：当事方 + 争议类型相同但请求事项不同 → 仅提示关联，禁止自动合并，
  例如同一航次中船员既主张工资又主张人身损害赔偿，是两个可并行的案件。

法律关系标识使用登记方填写的稳定引用（航次号/合同号/事故编号），
不保存原始合同文本。
"""

import hashlib
import json
from dataclasses import dataclass

from .enums import DisputeType


def _normalize_party(party: dict) -> list[str]:
    """最小身份资料的归一化：只取受控引用与角色，姓名/证件号不参与指纹存储。"""
    refs = [str(party.get("party_ref", "")).strip().upper()]
    return refs


def _claim_text(claim: dict) -> str:
    return "|".join(
        str(claim.get(key, "")).strip().lower()
        for key in ("claim_kind", "subject_ref", "period_start", "period_end")
    )


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Fingerprints:
    hard: str
    soft: str
    relation_ref: str


def build_fingerprints(
    dispute_type: DisputeType,
    parties: list[dict],
    claims: list[dict],
    relation_ref: str,
) -> Fingerprints:
    """对一组当事方与请求事项计算指纹。

    parties: [{party_ref, role}]；claims: [{claim_kind, subject_ref, period_start, period_end}]。
    强指纹要求请求逐项一致（顺序无关），弱指纹只看主体与争议类型。
    """
    party_key = sorted(f"{p.get('role', '?')}:{r}" for p in parties for r in _normalize_party(p))
    claim_key = sorted(_claim_text(c) for c in claims)
    relation = relation_ref.strip().upper()

    hard = _digest({
        "v": 1,
        "type": dispute_type.value,
        "parties": party_key,
        "relation": relation,
        "claims": claim_key,
    })
    soft = _digest({
        "v": 1,
        "type": dispute_type.value,
        "parties": party_key,
    })
    return Fingerprints(hard=hard, soft=soft, relation_ref=relation)
