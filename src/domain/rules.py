"""八家共建单位登记与程序路径规则。

路径"可解释"的含义：生成的每一条路径都附带
- 选择说明（为什么走这条、当事人放弃了哪条备选）；
- 节点说明（管辖连接点、法律依据、办理期限来源）；
- 交接出口（节点办结后可正式交接给哪一单位）。

期限数值为可配置的领域参数（见 DEADLINE_RULES），法律依据仅用于解释，
具体个案以有权机关认定为准。
"""

from dataclasses import dataclass, field

from .enums import DisputeType, NodeCode

# ---- 共建单位 ---------------------------------------------------------

UNITS: dict[str, dict[str, str]] = {
    "MSA": {"name": "海事管理机构", "kind": "maritime"},
    "PROC": {"name": "人民检察院", "kind": "procuratorate"},
    "JUSTICE": {"name": "司法行政机关", "kind": "justice"},
    "HRSS": {"name": "人力资源社会保障部门", "kind": "human_resources"},
    "MED": {"name": "涉运河纠纷调解组织", "kind": "mediation"},
    "LAC": {"name": "劳动人事争议仲裁委员会", "kind": "labor_arbitration"},
    "CAI": {"name": "商事（海事）仲裁机构", "kind": "commercial_arbitration"},
    "COURT": {"name": "人民法院（海事审判职能）", "kind": "court"},
}


@dataclass(frozen=True)
class Segment:
    """路径上的一个程序节点。"""

    node: NodeCode
    unit: str
    label: str
    legal_basis: str
    reason: str
    time_limit_days: int | None = None  # 办理期限（监督口径），跨境送达等无国内期限
    exits: tuple[str, ...] = ()         # 办结后允许交接的单位代码
    optional: bool = False              # 条件触发节点（跨境送达、检察支持）


@dataclass(frozen=True)
class Route:
    key: str
    name: str
    segments: tuple[Segment, ...]
    choice_explanation: str


@dataclass(frozen=True)
class PathPlan:
    dispute_type: DisputeType
    route_key: str
    route_name: str
    choice_explanation: str
    segments: tuple[Segment, ...]
    limitation_days: int
    limitation_basis: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ---- 办理期限（天）与时效口径 ------------------------------------------

DEADLINE_RULES: dict[NodeCode, dict[str, object]] = {
    NodeCode.INTAKE: {"time_limit_days": 7},
    NodeCode.MSA_ADMIN: {"time_limit_days": 30},
    NodeCode.MEDIATION: {"time_limit_days": 30},
    NodeCode.LABOR_ARBITRATION: {"time_limit_days": 45},
    NodeCode.COMMERCIAL_ARBITRATION: {"time_limit_days": None},  # 按仲裁规则
    NodeCode.JUDICIAL_CONFIRMATION: {"time_limit_days": 30},
    NodeCode.LITIGATION: {"time_limit_days": 180},
    NodeCode.PROCURATORIAL: {"time_limit_days": 30},
    NodeCode.CROSS_BORDER_SERVICE: {"time_limit_days": None},    # 等待域外回证
    NodeCode.EXECUTION: {"time_limit_days": 180},
}

# 仲裁/诉讼时效主张期间（天）与依据
LIMITATION_RULES: dict[DisputeType, tuple[int, str]] = {
    DisputeType.MARITIME_ACCIDENT: (1095, "《民法典》普通诉讼时效三年"),
    DisputeType.CREW_LABOR: (365, "《劳动争议调解仲裁法》第二十七条 仲裁时效一年"),
    DisputeType.VESSEL_POLLUTION: (1095, "《民法典》侵权损害赔偿诉讼时效三年"),
    DisputeType.CARGO_TRANSPORT: (1095, "《民法典》合同请求权诉讼时效三年"),
}

_INTAKE = Segment(
    NodeCode.INTAKE, "JUSTICE", "一口受理登记",
    "八家共建程序衔接工作规则（一口受理）",
    "统一登记最小身份资料、管辖连接点、请求事项、证据封存摘要与法定期限，"
    "材料一次提交、按授权共享，避免重复提交。",
    DEADLINE_RULES[NodeCode.INTAKE]["time_limit_days"], exits=("MED", "LAC", "CAI", "MSA", "COURT"),
)
_MEDIATION = Segment(
    NodeCode.MEDIATION, "MED", "涉运河纠纷调解",
    "《人民调解法》《海事行政执法与纠纷调解衔接意见》",
    "调解不收取费用、不阻断法定时效；达成协议可申请司法确认赋予强制执行力。",
    30, exits=("COURT", "LAC", "CAI"),
)
_JUDICIAL_CONFIRM = Segment(
    NodeCode.JUDICIAL_CONFIRMATION, "COURT", "调解协议司法确认",
    "《民事诉讼法》司法确认程序",
    "调解协议经司法确认后具有强制执行力，无须另行诉讼；确认不予的，转入诉讼/仲裁。",
    30, exits=("COURT",),
)
_LABOR_ARB = Segment(
    NodeCode.LABOR_ARBITRATION, "LAC", "劳动人事争议仲裁",
    "《劳动争议调解仲裁法》第五条（仲裁前置）",
    "船员劳动合同争议仲裁为诉讼前置程序；仲裁裁决作出后十五日内向法院起诉。",
    45, exits=("COURT",),
)
_COMM_ARB = Segment(
    NodeCode.COMMERCIAL_ARBITRATION, "CAI", "商事（海事）仲裁",
    "《仲裁法》及仲裁机构规则",
    "存在有效仲裁条款时排除法院管辖；一裁终局，裁决可申请法院执行。",
    None, exits=("COURT",),
)
_LITIGATION = Segment(
    NodeCode.LITIGATION, "COURT", "诉讼（海事审判职能）",
    "《海事诉讼特别程序法》《民事诉讼法》",
    "运河通航水域发生的水上事故、船载货物运输等纠纷由海事审判职能管辖。",
    180, exits=("PROC",),
)
_MSA_HANDLE = Segment(
    NodeCode.MSA_ADMIN, "MSA", "海事行政处理与调查取证",
    "《海上交通安全法》《海洋环境保护法》行政处理职责",
    "海事机构先行固定事故事实与污染证据，可主持行政调解；调解不成的不影响民事救济。",
    30, exits=("COURT", "CAI"),
)
_PROC_SUPPORT = Segment(
    NodeCode.PROCURATORIAL, "PROC", "检察支持起诉/法律监督",
    "《民事诉讼法》支持起诉、检察建议",
    "船员弱势维权可申请支持起诉；污染公共利益受损的可由公益诉讼检察环节介入。",
    30, exits=("COURT",), optional=True,
)
_CROSS_BORDER = Segment(
    NodeCode.CROSS_BORDER_SERVICE, "COURT", "跨境送达",
    "《海事诉讼特别程序法》及司法协助条约",
    "当事方或证据在域外时，按条约/外交途径送达，等待域外回证期间国内期限中止。",
    None, exits=("COURT", "CAI"), optional=True,
)


ROUTES: dict[DisputeType, tuple[Route, ...]] = {
    DisputeType.MARITIME_ACCIDENT: (
        Route(
            "mediation_confirm", "调解＋司法确认",
            (_INTAKE, _MSA_HANDLE, _MEDIATION, _JUDICIAL_CONFIRM),
            "事实清楚、当事方有调解意愿：海事机构固定证据后调解，协议经司法确认取得执行力。",
        ),
        Route(
            "litigation", "海事诉讼",
            (_INTAKE, _MSA_HANDLE, _LITIGATION),
            "争议较大或调解不成：由海事审判职能管辖；行政处理所固定的证据随案移送。",
        ),
    ),
    DisputeType.CREW_LABOR: (
        Route(
            "mediation_arbitration", "调解后劳动仲裁",
            (_INTAKE, _MEDIATION, _LABOR_ARB, _LITIGATION),
            "船员劳动合同争议先行调解，调解不成的对仲裁前置部分申请劳动仲裁，不服裁决可起诉。",
        ),
        Route(
            "labor_arbitration", "直接申请劳动仲裁",
            (_INTAKE, _LABOR_ARB, _LITIGATION),
            "当事人不愿调解的可直接申请劳动仲裁（仲裁前置），对裁决不服十五日内向法院起诉。",
        ),
        Route(
            "service_contract_litigation", "船员劳务合同诉讼/商事仲裁",
            (_INTAKE, _COMM_ARB, _LITIGATION),
            "不构成劳动关系的船员劳务合同纠纷，可依仲裁条款商事仲裁，或直接向海事审判职能起诉。",
        ),
    ),
    DisputeType.VESSEL_POLLUTION: (
        Route(
            "admin_mediation", "行政处理＋调解/司法确认",
            (_INTAKE, _MSA_HANDLE, _MEDIATION, _JUDICIAL_CONFIRM),
            "污染责任与损害范围可协商：海事机构应急处置与调查后调解，协议可申请司法确认。",
        ),
        Route(
            "civil_litigation", "污染损害赔偿诉讼",
            (_INTAKE, _MSA_HANDLE, _LITIGATION),
            "损害赔偿协商不成的提起侵权诉讼；涉及公共利益的由检察公益诉讼衔接。",
        ),
        Route(
            "commercial_arbitration", "商事仲裁",
            (_INTAKE, _COMM_ARB),
            "租船合同、运输合同载有有效仲裁条款的污染索赔提交商事仲裁。",
        ),
    ),
    DisputeType.CARGO_TRANSPORT: (
        Route(
            "commercial_arbitration", "商事仲裁",
            (_INTAKE, _COMM_ARB),
            "运输/租船合同存在有效仲裁条款，排除法院管辖，一裁终局。",
        ),
        Route(
            "mediation_confirm", "调解＋司法确认",
            (_INTAKE, _MEDIATION, _JUDICIAL_CONFIRM),
            "货损、滞期等争议希望快速履行的，调解协议经司法确认即可申请执行。",
        ),
        Route(
            "litigation", "海事货物运输诉讼",
            (_INTAKE, _LITIGATION),
            "无仲裁条款的水上货物运输合同纠纷由海事审判职能管辖。",
        ),
    ),
}

# 各争议类型允许的路线键（用于校验当事人选择）
ALLOWED_ROUTES: dict[DisputeType, frozenset[str]] = {
    dtype: frozenset(route.key for route in routes)
    for dtype, routes in ROUTES.items()
}


def plan_path(
    dispute_type: DisputeType,
    route_key: str,
    *,
    cross_border: bool = False,
    procuratorial_support: bool = False,
    has_arbitration_clause: bool = False,
) -> PathPlan:
    """根据争议类型与当事人选择生成可解释路径。

    条件节点（跨境送达、检察支持）按事实标志插入到裁判类节点之前；
    若当事人选择与事实标志冲突（如无仲裁条款却选仲裁），返回 warnings，
    由受理单位向当事人释明，而不是静默改道。
    """
    routes = ROUTES[dispute_type]
    chosen = next((r for r in routes if r.key == route_key), None)
    if chosen is None:
        raise ValueError(
            f"争议类型 {dispute_type} 不支持路线 {route_key}；"
            f"可选：{sorted(ALLOWED_ROUTES[dispute_type])}"
        )

    warnings: list[str] = []
    uses_arbitration = any(
        seg.node in (NodeCode.COMMERCIAL_ARBITRATION, NodeCode.LABOR_ARBITRATION)
        for seg in chosen.segments
    )
    if dispute_type is not DisputeType.CREW_LABOR and uses_arbitration and not has_arbitration_clause:
        if any(seg.node is NodeCode.COMMERCIAL_ARBITRATION for seg in chosen.segments):
            warnings.append("所选商事仲裁路线需以有效书面仲裁条款为前提，缺失时将释明改道诉讼。")
    if route_key in {"litigation", "civil_litigation"} and has_arbitration_clause:
        warnings.append("存在有效仲裁条款时法院可能不予受理，建议先走商事仲裁或确认条款效力。")

    segments: list[Segment] = []
    for seg in chosen.segments:
        if procuratorial_support and seg.node is NodeCode.LITIGATION and not any(
            s.node is NodeCode.PROCURATORIAL for s in segments
        ):
            segments.append(_PROC_SUPPORT)
        if cross_border and seg.node in (NodeCode.LITIGATION, NodeCode.COMMERCIAL_ARBITRATION) \
                and not any(s.node is NodeCode.CROSS_BORDER_SERVICE for s in segments):
            segments.append(_CROSS_BORDER)
        segments.append(seg)

    limitation_days, limitation_basis = LIMITATION_RULES[dispute_type]
    return PathPlan(
        dispute_type=dispute_type,
        route_key=chosen.key,
        route_name=chosen.name,
        choice_explanation=chosen.choice_explanation,
        segments=tuple(segments),
        limitation_days=limitation_days,
        limitation_basis=limitation_basis,
        warnings=tuple(warnings),
    )
