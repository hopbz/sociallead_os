from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.lead_routes import (
    LeadCreate,
    LeadScoringSettings,
    LeadStatusUpdate,
    create_lead,
    get_unprocessed_posts,
    list_leads,
    mark_post_processed,
    read_lead_scoring_settings,
    update_lead_scoring_settings,
    update_lead_status,
)
from app.config import Settings
from app.db.models import LeadCandidate, ScrapedPost
from app.db.session import Base


def make_session() -> tuple[Session, object]:
    engine = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine)
    return Session(engine), engine


def add_post(db: Session) -> ScrapedPost:
    post = ScrapedPost(
        group_url='https://www.facebook.com/groups/example',
        group_name='Example Group',
        post_id='facebook-post-1',
        content_hash='hash-1',
        dedupe_key='post:facebook-post-1',
        post_url='https://www.facebook.com/groups/example/posts/1',
        author='Alice',
        content='I need help choosing a service provider.',
        matched_keywords='service',
        engine='cdp_playwright',
        scraped_at=datetime.now(timezone.utc),
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


def test_lead_workflow_marks_posts_and_deduplicates_leads() -> None:
    db, engine = make_session()
    try:
        post = add_post(db)
        assert [item.id for item in get_unprocessed_posts(limit=50, db=db)] == [post.id]

        payload = LeadCreate(
            post_id=post.id,
            group_name=post.group_name,
            group_url=post.group_url,
            post_url=post.post_url,
            author=post.author,
            content=post.content,
            score=8,
            need_stage='hot',
            persona='Buyer',
            pain_points='Needs a provider',
            reason='Clear purchase intent',
            suggested_comment='Bạn đang ưu tiên tiêu chí nào khi chọn nhà cung cấp?',
        )
        first = create_lead(payload, db)
        second = create_lead(payload, db)

        assert first['duplicated'] is False
        assert second['duplicated'] is True
        assert db.scalar(select(LeadCandidate.score)) == 8
        assert [lead.id for lead in list_leads(limit=50, min_score=7, status='new', q='Example', db=db)] == [
            first['lead_id']
        ]

        updated = update_lead_status(
            first['lead_id'],
            LeadStatusUpdate(status='reviewed'),
            db,
        )
        assert updated.status == 'reviewed'

        assert mark_post_processed(post.id, db) == {'ok': True, 'post_id': post.id}
        assert get_unprocessed_posts(limit=50, db=db) == []
    finally:
        db.close()
        engine.dispose()


def test_lead_scoring_settings_are_persisted_and_normalized(monkeypatch) -> None:
    db, engine = make_session()
    settings = Settings(_env_file=None)
    env_file = Path('data/logs/test_lead_scoring.env').resolve()
    monkeypatch.setenv('ENV_FILE_PATH', str(env_file))
    try:
        defaults = read_lead_scoring_settings(settings=settings, db=db)
        assert defaults['score_threshold'] == 7

        saved = update_lead_scoring_settings(
            LeadScoringSettings(
                score_threshold=8,
                niche_name='Salon tóc',
                positive_keywords=['cần salon', ' Cần salon ', 'báo giá'],
                negative_keywords=['spam'],
                comment_tone='Ấm áp và chuyên nghiệp.',
                max_posts_per_group=15,
                scan_interval_minutes=45,
            ),
            db,
        )
        assert saved['positive_keywords'] == ['cần salon', 'báo giá']
        assert read_lead_scoring_settings(settings=settings, db=db)['niche_name'] == 'Salon tóc'
        env_text = env_file.read_text(encoding='utf-8')
        assert 'LEAD_SCORE_THRESHOLD=8' in env_text
        assert 'LEAD_MAX_POSTS_PER_GROUP=15' in env_text
        assert 'LEAD_NICHE_NAME="Salon tóc"' in env_text
    finally:
        db.close()
        engine.dispose()
