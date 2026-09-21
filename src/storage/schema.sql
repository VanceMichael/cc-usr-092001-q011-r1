-- 运河纠纷程序衔接引擎 · 存储结构
-- 约定：时间一律存带偏移量 ISO 8601；身份与材料只存稳定引用与 sha256 摘要。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS service_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 案件主表：最小身份资料外置到 parties，材料外置到 evidence
CREATE TABLE IF NOT EXISTS cases (
  case_ref         TEXT PRIMARY KEY,
  dispute_type     TEXT NOT NULL,
  status           TEXT NOT NULL,              -- accepted/withdrawn/partial_settled/closed
  relation_ref     TEXT NOT NULL,              -- 法律关系稳定引用（航次号/合同号/事故编号）
  fp_hard          TEXT NOT NULL,              -- 强指纹：同请求重复立案识别
  fp_soft          TEXT NOT NULL,              -- 弱指纹：不同请求关联但不合并
  accepted_at      TEXT NOT NULL,
  limitation_days  INTEGER NOT NULL,
  limitation_basis TEXT NOT NULL,
  active_route_id  INTEGER,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_fp_hard ON cases(fp_hard);
CREATE INDEX IF NOT EXISTS idx_cases_fp_soft ON cases(fp_soft);

CREATE TABLE IF NOT EXISTS parties (
  case_ref    TEXT NOT NULL REFERENCES cases(case_ref),
  party_ref   TEXT NOT NULL,                   -- 对外脱敏稳定引用
  role        TEXT NOT NULL,                   -- claimant/respondent/third_party
  contact_ref TEXT,                            -- 联系方式的受控引用，不存号码本身
  PRIMARY KEY (case_ref, party_ref)
);

CREATE TABLE IF NOT EXISTS claims (
  case_ref     TEXT NOT NULL REFERENCES cases(case_ref),
  claim_seq    INTEGER NOT NULL,
  claim_kind   TEXT NOT NULL,                  -- wages/damage/cargo_loss/pollution_cleanup...
  subject_ref  TEXT,                           -- 请求所指标的的稳定引用
  period_start TEXT,
  period_end   TEXT,
  amount_ref   TEXT,                           -- 金额明细的受控引用
  status       TEXT NOT NULL DEFAULT 'active', -- active/withdrawn/settled
  updated_at   TEXT,
  PRIMARY KEY (case_ref, claim_seq)
);

-- 管辖连接点（决定管辖是否成立、解释路径用）
CREATE TABLE IF NOT EXISTS connections (
  case_ref   TEXT NOT NULL REFERENCES cases(case_ref),
  seq        INTEGER NOT NULL,
  point_type TEXT NOT NULL,                   -- accident_locale/vessel_registry/employer_locale/contract_place...
  point_ref  TEXT NOT NULL,
  PRIMARY KEY (case_ref, seq)
);

-- 证据封存：只存引用、摘要、持有人与保密级别
CREATE TABLE IF NOT EXISTS evidence (
  case_ref        TEXT NOT NULL REFERENCES cases(case_ref),
  seq             INTEGER NOT NULL,
  evidence_ref    TEXT NOT NULL,
  digest          TEXT NOT NULL,               -- sha256:...
  confidentiality TEXT NOT NULL,
  holder_unit     TEXT NOT NULL,
  sealed_at       TEXT NOT NULL,
  note            TEXT,
  PRIMARY KEY (case_ref, seq)
);

-- 路径版本：改道（管辖异议成立、插入跨境送达等）一律新增版本，旧版本只读保留
CREATE TABLE IF NOT EXISTS routes (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  case_ref           TEXT NOT NULL REFERENCES cases(case_ref),
  version            INTEGER NOT NULL,
  route_key          TEXT NOT NULL,
  route_name         TEXT NOT NULL,
  choice_explanation TEXT NOT NULL,
  status             TEXT NOT NULL,            -- active/superseded/void
  change_reason      TEXT,
  created_at         TEXT NOT NULL,
  UNIQUE (case_ref, version)
);

CREATE TABLE IF NOT EXISTS route_warnings (
  route_id INTEGER NOT NULL REFERENCES routes(id),
  seq      INTEGER NOT NULL,
  warning  TEXT NOT NULL,
  PRIMARY KEY (route_id, seq)
);

CREATE TABLE IF NOT EXISTS stages (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  case_ref       TEXT NOT NULL REFERENCES cases(case_ref),
  route_id       INTEGER NOT NULL REFERENCES routes(id),
  seq            INTEGER NOT NULL,
  node           TEXT NOT NULL,
  unit           TEXT NOT NULL,
  label          TEXT NOT NULL,
  legal_basis    TEXT NOT NULL,
  reason         TEXT NOT NULL,
  time_limit_days INTEGER,
  optional       INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL,                -- pending/active/handed_off/superseded/closed/void
  entered_at     TEXT,
  closed_at      TEXT,
  UNIQUE (route_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_stages_case ON stages(case_ref);

-- 移送：交出/接收双向确认
CREATE TABLE IF NOT EXISTS transfers (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  transfer_ref    TEXT NOT NULL UNIQUE,
  case_ref        TEXT NOT NULL REFERENCES cases(case_ref),
  from_stage_id   INTEGER NOT NULL REFERENCES stages(id),
  to_stage_id     INTEGER NOT NULL REFERENCES stages(id),
  from_unit       TEXT NOT NULL,
  to_unit         TEXT NOT NULL,
  state           TEXT NOT NULL,               -- proposed/received/rejected/rolled_back
  material_digest TEXT,
  toll_id         INTEGER REFERENCES tolls(id),
  proposed_at     TEXT NOT NULL,
  proposed_by     TEXT,
  responded_at    TEXT,
  responded_by    TEXT,
  response_note   TEXT
);
CREATE INDEX IF NOT EXISTS idx_transfers_case ON transfers(case_ref);

-- 期限中止区间（移送待签收/管辖异议审查/跨境送达等待回证）
CREATE TABLE IF NOT EXISTS tolls (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  case_ref   TEXT NOT NULL REFERENCES cases(case_ref),
  reason     TEXT NOT NULL,
  start_at   TEXT NOT NULL,
  end_at     TEXT,
  started_by TEXT,
  ended_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tolls_case ON tolls(case_ref);

-- 留痕事件账本：seq 案内递增，occurred_at 保留原始发生时间
CREATE TABLE IF NOT EXISTS events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  case_ref       TEXT NOT NULL REFERENCES cases(case_ref),
  seq            INTEGER NOT NULL,
  event_id       TEXT NOT NULL UNIQUE,
  kind           TEXT NOT NULL,
  occurred_at    TEXT NOT NULL,
  unit           TEXT,
  actor_ref      TEXT,
  payload_json   TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  UNIQUE (case_ref, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_case ON events(case_ref, seq);

-- 协同授权：单位仅能查看获授权范围内、不超保密级别的材料
CREATE TABLE IF NOT EXISTS grants (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  case_ref               TEXT NOT NULL REFERENCES cases(case_ref),
  unit                   TEXT NOT NULL,
  scope                  TEXT NOT NULL,        -- progress/materials
  confidentiality_ceiling TEXT NOT NULL,       -- public/restricted/confidential/secret
  granted_by             TEXT,
  granted_at             TEXT NOT NULL,
  revoked_at             TEXT,
  UNIQUE (case_ref, unit, scope)
);

-- 弱指纹关联（不同请求并行，不自动合并）
CREATE TABLE IF NOT EXISTS case_links (
  case_ref_a TEXT NOT NULL REFERENCES cases(case_ref),
  case_ref_b TEXT NOT NULL REFERENCES cases(case_ref),
  kind       TEXT NOT NULL,
  PRIMARY KEY (case_ref_a, case_ref_b, kind)
);

-- 外部交换事件：保留原始发生时间，source_sequence 仅在同一来源内递增
CREATE TABLE IF NOT EXISTS inbound_events (
  event_id       TEXT PRIMARY KEY,
  subject_ref    TEXT NOT NULL,
  source_unit    TEXT NOT NULL,
  source_sequence INTEGER NOT NULL,
  occurred_at    TEXT NOT NULL,          -- 原始发生时间，不得被到达时间覆盖
  received_at    TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  UNIQUE (source_unit, source_sequence)
);
CREATE INDEX IF NOT EXISTS idx_inbound_subject ON inbound_events(subject_ref);

-- 演示用访问令牌映射（生产环境应由统一身份设施签发）
CREATE TABLE IF NOT EXISTS party_tokens (
  party_ref TEXT PRIMARY KEY,
  token     TEXT NOT NULL,
  issued_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unit_tokens (
  token TEXT PRIMARY KEY,
  unit  TEXT NOT NULL,
  role  TEXT NOT NULL DEFAULT 'staff'
);
