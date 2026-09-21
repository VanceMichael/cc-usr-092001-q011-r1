"""数据库结构定义。

所有表结构集中在本模块，`scripts/migrate.py` 与 `src.store` 共用同一份 DDL，
避免迁移脚本与运行时代码出现结构漂移。列表列（如连接点、授权范围）以 JSON
文本保存，读取方负责解析。
"""

SCHEMA_VERSION = "2"

DDL = [
    """
    CREATE TABLE IF NOT EXISTS service_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # 当事方：只保存最小身份资料，真实身份信息以散列形式登记用于重复识别。
    """
    CREATE TABLE IF NOT EXISTS parties (
        party_ref TEXT PRIMARY KEY,
        party_type TEXT NOT NULL,
        id_hash TEXT NOT NULL,
        contact_ref TEXT,
        registered_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cases (
        case_ref TEXT PRIMARY KEY,
        dispute_type TEXT NOT NULL,
        connection_points TEXT NOT NULL,
        election TEXT NOT NULL,
        arbitration_agreement INTEGER NOT NULL DEFAULT 0,
        cross_border INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        confidentiality TEXT NOT NULL,
        opened_by_unit TEXT NOT NULL,
        opened_at TEXT NOT NULL,
        closed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS case_parties (
        case_ref TEXT NOT NULL,
        party_ref TEXT NOT NULL,
        role TEXT NOT NULL,
        PRIMARY KEY (case_ref, party_ref, role)
    )
    """,
    # 请求事项：重复立案识别以请求为粒度，绝不自动合并不同请求。
    """
    CREATE TABLE IF NOT EXISTS claims (
        claim_ref TEXT PRIMARY KEY,
        case_ref TEXT NOT NULL,
        claim_type TEXT NOT NULL,
        amount REAL,
        currency TEXT,
        summary_ref TEXT,
        status TEXT NOT NULL,
        duplicate_of TEXT
    )
    """,
    # 证据封存：只保存受控引用与 sha256 摘要，不保存材料内容。
    """
    CREATE TABLE IF NOT EXISTS evidence_seals (
        evidence_ref TEXT PRIMARY KEY,
        case_ref TEXT NOT NULL,
        digest TEXT NOT NULL,
        sealed_by_unit TEXT NOT NULL,
        sealed_at TEXT NOT NULL,
        note_ref TEXT
    )
    """,
    # 法定期限：移送不中止法定时效，引擎只负责计算剩余并预警。
    """
    CREATE TABLE IF NOT EXISTS deadlines (
        deadline_ref TEXT PRIMARY KEY,
        case_ref TEXT NOT NULL,
        kind TEXT NOT NULL,
        basis TEXT NOT NULL,
        start_at TEXT NOT NULL,
        due_at TEXT NOT NULL,
        warn_days INTEGER NOT NULL DEFAULT 30,
        status TEXT NOT NULL
    )
    """,
    # 程序路径按版本保存：撤回、部分和解、管辖异议、跨境送达均另起版本，
    # 旧版本只标记 superseded_at，永不删除。
    """
    CREATE TABLE IF NOT EXISTS path_versions (
        version_ref TEXT PRIMARY KEY,
        case_ref TEXT NOT NULL,
        version_no INTEGER NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        superseded_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS path_nodes (
        node_ref TEXT PRIMARY KEY,
        version_ref TEXT NOT NULL,
        case_ref TEXT NOT NULL,
        seq INTEGER NOT NULL,
        unit_kind TEXT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT NOT NULL,
        planned_days INTEGER,
        status TEXT NOT NULL,
        entered_at TEXT,
        left_at TEXT
    )
    """,
    # 移送：交出与接收双向确认，材料清单、保密级别与期限快照随移送保存。
    """
    CREATE TABLE IF NOT EXISTS transfers (
        transfer_ref TEXT PRIMARY KEY,
        case_ref TEXT NOT NULL,
        kind TEXT NOT NULL,
        from_node_ref TEXT,
        from_unit_kind TEXT NOT NULL,
        to_unit_kind TEXT NOT NULL,
        reason TEXT NOT NULL,
        materials TEXT NOT NULL,
        confidentiality TEXT NOT NULL,
        deadline_snapshot TEXT NOT NULL,
        status TEXT NOT NULL,
        handover_by TEXT,
        handover_at TEXT,
        receipt_by TEXT,
        receipt_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    # 重复立案标记：只提示、由人工决定，决定必须留痕。
    """
    CREATE TABLE IF NOT EXISTS duplicate_flags (
        flag_ref TEXT PRIMARY KEY,
        case_ref TEXT NOT NULL,
        claim_ref TEXT NOT NULL,
        matched_claim_ref TEXT NOT NULL,
        explanation TEXT NOT NULL,
        status TEXT NOT NULL,
        decided_by TEXT,
        decided_at TEXT,
        decision_reason TEXT,
        created_at TEXT NOT NULL
    )
    """,
    # 协同单位授权：按案件与单位授予可见范围与具体证据引用。
    """
    CREATE TABLE IF NOT EXISTS grants (
        grant_ref TEXT PRIMARY KEY,
        case_ref TEXT NOT NULL,
        grantee_kind TEXT NOT NULL,
        grantee_ref TEXT NOT NULL,
        scopes TEXT NOT NULL,
        evidence_refs TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        valid_to TEXT
    )
    """,
    # 外部交换事件：保留来源发生时间，接收时间另记，event_id 幂等。
    """
    CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        subject_ref TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        source_unit TEXT NOT NULL,
        source_sequence INTEGER NOT NULL,
        payload_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """,
]
