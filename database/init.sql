CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS group_sources (
    id VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    name VARCHAR(255) NOT NULL,
    url TEXT NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS keywords (
    id VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    keyword VARCHAR(255) NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    engine VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    groups_total INTEGER NOT NULL DEFAULT 0,
    groups_success INTEGER NOT NULL DEFAULT 0,
    groups_failed INTEGER NOT NULL DEFAULT 0,
    posts_seen INTEGER NOT NULL DEFAULT 0,
    posts_inserted INTEGER NOT NULL DEFAULT 0,
    posts_matched INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

CREATE TABLE IF NOT EXISTS scraped_posts (
    id VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    run_id VARCHAR(36) REFERENCES scan_runs(id) ON DELETE SET NULL,
    group_url TEXT NOT NULL,
    group_name VARCHAR(255),
    post_id VARCHAR(255),
    content_hash VARCHAR(64) NOT NULL,
    dedupe_key VARCHAR(128) NOT NULL,
    post_url TEXT,
    author VARCHAR(255),
    content TEXT NOT NULL,
    matched_keywords TEXT,
    engine VARCHAR(32) NOT NULL,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    UNIQUE (post_id),
    UNIQUE (content_hash),
    UNIQUE (dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_scraped_posts_created_at ON scraped_posts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scraped_posts_group_url ON scraped_posts(group_url);
CREATE INDEX IF NOT EXISTS idx_scraped_posts_content_hash ON scraped_posts(content_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scraped_posts_dedupe_key ON scraped_posts(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_scraped_posts_processed_at ON scraped_posts(processed_at);

CREATE TABLE IF NOT EXISTS lead_candidates (
    id VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    post_id VARCHAR(36) NOT NULL UNIQUE,
    group_name VARCHAR(255),
    group_url TEXT,
    post_url TEXT,
    author VARCHAR(255),
    content TEXT NOT NULL,
    score INTEGER NOT NULL,
    need_stage VARCHAR(32),
    persona VARCHAR(255),
    pain_points TEXT,
    reason TEXT,
    suggested_comment TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'new',
    source VARCHAR(64) NOT NULL DEFAULT 'facebook_group',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lead_candidates_post_id ON lead_candidates(post_id);
CREATE INDEX IF NOT EXISTS idx_lead_candidates_status ON lead_candidates(status);

CREATE TABLE IF NOT EXISTS error_logs (
    id VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    run_id VARCHAR(36) REFERENCES scan_runs(id) ON DELETE SET NULL,
    group_url TEXT,
    engine VARCHAR(32),
    error_message TEXT NOT NULL,
    screenshot_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app_settings (
    key VARCHAR(120) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
