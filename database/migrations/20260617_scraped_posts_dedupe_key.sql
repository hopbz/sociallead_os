CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE scraped_posts
    ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(128);

UPDATE scraped_posts
SET dedupe_key = CASE
    WHEN post_url ~ 'set=pcb\.([0-9]+)'
        THEN 'post:' || substring(post_url from 'set=pcb\.([0-9]+)')
    WHEN post_url ~ 'set=gm\.([0-9]+)'
        THEN 'post:' || substring(post_url from 'set=gm\.([0-9]+)')
    WHEN post_id IS NOT NULL AND post_id <> ''
        THEN 'post:' || post_id
    ELSE 'content:' || left(encode(digest(coalesce(group_url, '') || '|' || coalesce(content_hash, ''), 'sha256'), 'hex'), 40)
END
WHERE dedupe_key IS NULL OR dedupe_key = '';

WITH ranked AS (
    SELECT
        id,
        first_value(id) OVER (
            PARTITION BY dedupe_key
            ORDER BY created_at ASC, id ASC
        ) AS keep_id,
        row_number() OVER (
            PARTITION BY dedupe_key
            ORDER BY created_at ASC, id ASC
        ) AS row_number
    FROM scraped_posts
    WHERE dedupe_key IS NOT NULL
),
duplicates AS (
    SELECT id, keep_id
    FROM ranked
    WHERE row_number > 1
)
DELETE FROM lead_candidates lc
USING duplicates d
WHERE lc.post_id = d.id
  AND EXISTS (
      SELECT 1
      FROM lead_candidates kept
      WHERE kept.post_id = d.keep_id
  );

WITH ranked AS (
    SELECT
        id,
        first_value(id) OVER (
            PARTITION BY dedupe_key
            ORDER BY created_at ASC, id ASC
        ) AS keep_id,
        row_number() OVER (
            PARTITION BY dedupe_key
            ORDER BY created_at ASC, id ASC
        ) AS row_number
    FROM scraped_posts
    WHERE dedupe_key IS NOT NULL
),
duplicates AS (
    SELECT id, keep_id
    FROM ranked
    WHERE row_number > 1
)
UPDATE lead_candidates lc
SET post_id = d.keep_id
FROM duplicates d
WHERE lc.post_id = d.id
  AND NOT EXISTS (
      SELECT 1
      FROM lead_candidates kept
      WHERE kept.post_id = d.keep_id
  );

WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY dedupe_key
            ORDER BY created_at ASC, id ASC
        ) AS row_number
    FROM scraped_posts
    WHERE dedupe_key IS NOT NULL
),
duplicates AS (
    SELECT id
    FROM ranked
    WHERE row_number > 1
)
DELETE FROM scraped_posts
WHERE id IN (SELECT id FROM duplicates);

ALTER TABLE scraped_posts
    ALTER COLUMN dedupe_key SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_scraped_posts_dedupe_key
    ON scraped_posts(dedupe_key);
