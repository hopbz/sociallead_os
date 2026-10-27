ALTER TABLE scraped_posts
    ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_scraped_posts_processed_at
    ON scraped_posts(processed_at);

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

CREATE INDEX IF NOT EXISTS idx_lead_candidates_post_id
    ON lead_candidates(post_id);

CREATE INDEX IF NOT EXISTS idx_lead_candidates_status
    ON lead_candidates(status);
