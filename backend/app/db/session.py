from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

settings = get_settings()
connect_args = {'check_same_thread': False} if settings.database_url.startswith('sqlite') else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.db import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _apply_compatible_schema_updates()


def _apply_compatible_schema_updates() -> None:
    """Apply small additive migrations needed by existing local databases."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if 'scraped_posts' not in tables:
        return

    columns = {column['name'] for column in inspector.get_columns('scraped_posts')}
    with engine.begin() as connection:
        if 'processed_at' not in columns:
            column_type = 'TIMESTAMP WITH TIME ZONE' if engine.dialect.name == 'postgresql' else 'DATETIME'
            connection.execute(text(f'ALTER TABLE scraped_posts ADD COLUMN processed_at {column_type} NULL'))
        if 'dedupe_key' not in columns:
            connection.execute(text('ALTER TABLE scraped_posts ADD COLUMN dedupe_key VARCHAR(128) NULL'))
        connection.execute(text(
            'CREATE INDEX IF NOT EXISTS idx_scraped_posts_processed_at '
            'ON scraped_posts(processed_at)'
        ))
        _backfill_scraped_post_dedupe_keys(connection, 'lead_candidates' in tables)
        if engine.dialect.name == 'postgresql':
            connection.execute(text('ALTER TABLE scraped_posts ALTER COLUMN dedupe_key SET NOT NULL'))
        connection.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_scraped_posts_dedupe_key '
            'ON scraped_posts(dedupe_key)'
        ))
        if 'lead_candidates' in tables:
            connection.execute(text(
                'CREATE INDEX IF NOT EXISTS idx_lead_candidates_post_id '
                'ON lead_candidates(post_id)'
            ))
            connection.execute(text(
                'CREATE INDEX IF NOT EXISTS idx_lead_candidates_status '
                'ON lead_candidates(status)'
            ))


def _backfill_scraped_post_dedupe_keys(connection, has_lead_candidates: bool) -> None:
    from app.browser.extractors import build_dedupe_key, canonical_post_id_from_url

    rows = connection.execute(text(
        'SELECT id, group_url, post_id, post_url, content '
        'FROM scraped_posts '
        "WHERE dedupe_key IS NULL OR dedupe_key = ''"
    )).mappings().all()
    for row in rows:
        post_id = canonical_post_id_from_url(row['post_url'] or '') or row['post_id']
        dedupe_key = build_dedupe_key(row['group_url'] or '', post_id, row['content'] or '')
        connection.execute(
            text('UPDATE scraped_posts SET dedupe_key = :dedupe_key WHERE id = :id'),
            {'dedupe_key': dedupe_key, 'id': row['id']},
        )

    all_rows = connection.execute(text(
        'SELECT id, dedupe_key '
        'FROM scraped_posts '
        'WHERE dedupe_key IS NOT NULL '
        'ORDER BY dedupe_key, created_at, id'
    )).mappings().all()
    lead_post_ids: set[str] = set()
    if has_lead_candidates:
        lead_post_ids = set(connection.execute(text('SELECT post_id FROM lead_candidates')).scalars().all())

    grouped: dict[str, list[str]] = {}
    for row in all_rows:
        grouped.setdefault(str(row['dedupe_key']), []).append(str(row['id']))

    for ids in grouped.values():
        if len(ids) < 2:
            continue
        keep_id = next((post_id for post_id in ids if post_id in lead_post_ids), ids[0])
        keep_has_lead = keep_id in lead_post_ids
        for duplicate_id in ids:
            if duplicate_id == keep_id:
                continue
            if has_lead_candidates and duplicate_id in lead_post_ids:
                if keep_has_lead:
                    connection.execute(
                        text('DELETE FROM lead_candidates WHERE post_id = :duplicate_id'),
                        {'duplicate_id': duplicate_id},
                    )
                else:
                    connection.execute(
                        text('UPDATE lead_candidates SET post_id = :keep_id WHERE post_id = :duplicate_id'),
                        {'keep_id': keep_id, 'duplicate_id': duplicate_id},
                    )
                    keep_has_lead = True
            connection.execute(
                text('DELETE FROM scraped_posts WHERE id = :duplicate_id'),
                {'duplicate_id': duplicate_id},
            )
