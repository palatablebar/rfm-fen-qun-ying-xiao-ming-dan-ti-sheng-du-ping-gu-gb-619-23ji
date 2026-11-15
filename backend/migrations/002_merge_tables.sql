-- 跨渠道会员归并相关表结构参考（PostgreSQL 语法，运行时不执行；建表由 SQLAlchemy create_all 完成）
-- 既有 members 表新增两列；对已存在的库，src/config/schema_migration.py 会幂等补列：
--   ALTER TABLE members ADD COLUMN merged_into_id INTEGER NULL REFERENCES members(id) ON DELETE SET NULL;
--   ALTER TABLE members ADD COLUMN merged_at      TIMESTAMP NULL;

-- 渠道档案：门店 / 小程序 / 外卖各一份的原始档案，归并的输入来源
CREATE TABLE channel_profiles (
    id                 SERIAL PRIMARY KEY,
    member_id          INTEGER     NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    channel            VARCHAR(16) NOT NULL,
    channel_member_no  VARCHAR(40) NOT NULL,
    name               VARCHAR(40) NOT NULL,
    phone              VARCHAR(20),
    phone_norm         VARCHAR(20),              -- 规范化手机号（仅数字），跨渠道同号匹配
    external_type      VARCHAR(24),
    external_id        VARCHAR(64),
    city               VARCHAR(24) NOT NULL DEFAULT '上海',
    tier               VARCHAR(12) NOT NULL DEFAULT 'normal',
    status             VARCHAR(16) NOT NULL DEFAULT 'active',  -- active / merged
    merged_into_id     INTEGER     REFERENCES members(id) ON DELETE SET NULL,
    source_updated_at  TIMESTAMP   NOT NULL,
    created_at         TIMESTAMP   NOT NULL,
    updated_at         TIMESTAMP   NOT NULL
);
CREATE INDEX ix_channel_profiles_member        ON channel_profiles (member_id);
CREATE INDEX ix_channel_profiles_phone_norm    ON channel_profiles (phone_norm);
CREATE INDEX ix_channel_profiles_status        ON channel_profiles (status);
CREATE INDEX ix_channel_profiles_external      ON channel_profiles (external_type, external_id);

-- 历史券响应事实：归并时随主档合并（只改 member_id，不删事实）
CREATE TABLE coupon_responses (
    id            SERIAL PRIMARY KEY,
    member_id     INTEGER          NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    campaign_code VARCHAR(40)      NOT NULL,
    coupon_name   VARCHAR(80)      NOT NULL,
    channel       VARCHAR(16)      NOT NULL DEFAULT 'miniapp',
    status        VARCHAR(16)      NOT NULL DEFAULT 'sent',  -- sent / redeemed
    face_value    DOUBLE PRECISION NOT NULL DEFAULT 0,
    sent_at       TIMESTAMP        NOT NULL,
    redeemed_at   TIMESTAMP,
    created_at    TIMESTAMP        NOT NULL
);
CREATE INDEX ix_coupon_responses_member ON coupon_responses (member_id);
CREATE INDEX ix_coupon_responses_status ON coupon_responses (status);

-- 归并任务：待处理 → 处理中 → 已完成 / 失败（可从 last_cursor 断点续跑）
CREATE TABLE merge_jobs (
    id                 SERIAL PRIMARY KEY,
    job_no             VARCHAR(32) NOT NULL UNIQUE,
    status             VARCHAR(16) NOT NULL DEFAULT 'pending',
    trigger_source     VARCHAR(16) NOT NULL DEFAULT 'manual',
    operator_email     VARCHAR(160) NOT NULL,
    last_cursor        INTEGER     NOT NULL DEFAULT 0,   -- 已处理到的 channel_profiles.id
    profiles_before    INTEGER     NOT NULL DEFAULT 0,
    profiles_after     INTEGER     NOT NULL DEFAULT 0,
    members_before     INTEGER     NOT NULL DEFAULT 0,
    members_after      INTEGER     NOT NULL DEFAULT 0,
    groups_merged      INTEGER     NOT NULL DEFAULT 0,
    records_merged     INTEGER     NOT NULL DEFAULT 0,
    conflict_count     INTEGER     NOT NULL DEFAULT 0,
    processed_profiles INTEGER     NOT NULL DEFAULT 0,
    error_message      TEXT,
    stats              JSON,
    started_at         TIMESTAMP,
    finished_at        TIMESTAMP,
    created_at         TIMESTAMP   NOT NULL,
    updated_at         TIMESTAMP   NOT NULL
);

-- 归并记录：每个被合并档案一行，含主档/被合并档案/识别依据/冲突来源与时间
CREATE TABLE merge_records (
    id               SERIAL PRIMARY KEY,
    merge_job_id     INTEGER      NOT NULL REFERENCES merge_jobs(id) ON DELETE CASCADE,
    master_member_id INTEGER      NOT NULL,
    merged_member_id INTEGER      NOT NULL,
    master_profile_id   INTEGER  NOT NULL,
    merged_profile_id   INTEGER  NOT NULL,
    match_kind       VARCHAR(16)  NOT NULL,               -- phone / external
    match_value      VARCHAR(120),
    external_type    VARCHAR(24),
    master_reason    VARCHAR(240) NOT NULL DEFAULT '',
    conflicts        JSON,                                -- 冲突字段的全部来源与时间依据
    orders_moved     INTEGER      NOT NULL DEFAULT 0,
    coupons_moved    INTEGER      NOT NULL DEFAULT 0,
    created_at       TIMESTAMP    NOT NULL
);
CREATE INDEX ix_merge_records_job     ON merge_records (merge_job_id);
CREATE INDEX ix_merge_records_master  ON merge_records (master_member_id);
CREATE INDEX ix_merge_records_merged  ON merge_records (merged_member_id);

-- 名单会员快照：名单生成那一刻冻结的会员资料（已确认名单按快照导出）
CREATE TABLE audience_snapshots (
    id              SERIAL PRIMARY KEY,
    audience_id     INTEGER          NOT NULL REFERENCES audiences(id) ON DELETE CASCADE,
    member_id       INTEGER          NOT NULL,
    member_no       VARCHAR(24)      NOT NULL,
    name            VARCHAR(40)      NOT NULL,
    phone           VARCHAR(20)      NOT NULL,
    city            VARCHAR(24)      NOT NULL,
    tier            VARCHAR(12)      NOT NULL,
    segment_code    VARCHAR(20),
    composite_score DOUBLE PRECISION,
    purchase_rate   DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at      TIMESTAMP        NOT NULL
);
CREATE UNIQUE INDEX ix_audience_snapshots_audience ON audience_snapshots (audience_id, member_id);
