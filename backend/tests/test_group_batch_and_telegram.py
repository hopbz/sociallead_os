from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.routes import create_groups_batch, list_posts
from app.config import Settings
from app.db.models import AppSetting, GroupSource, ScrapedPost
from app.db.session import Base
from app.integrations.telegram import get_telegram_settings, save_telegram_settings
from app.schemas import GroupBatchCreate, GroupSourceCreate


def make_session() -> tuple[Session, object]:
    engine = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine)
    return Session(engine), engine


def test_create_groups_batch_normalizes_and_skips_existing_urls() -> None:
    db, engine = make_session()
    try:
        first = create_groups_batch(
            GroupBatchCreate(groups=[
                GroupSourceCreate(name='Group one', url='facebook.com/groups/group-one/'),
                GroupSourceCreate(name='Group two', url='https://www.facebook.com/groups/group-two?ref=share'),
            ]),
            db,
        )
        second = create_groups_batch(
            GroupBatchCreate(groups=[
                GroupSourceCreate(name='Duplicate', url='https://www.facebook.com/groups/group-one'),
            ]),
            db,
        )

        stored_urls = set(db.execute(select(GroupSource.url)).scalars())
        assert len(first.created) == 2
        assert second.created == []
        assert second.skipped_urls == ['https://www.facebook.com/groups/group-one']
        assert stored_urls == {
            'https://www.facebook.com/groups/group-one',
            'https://www.facebook.com/groups/group-two',
        }
    finally:
        db.close()
        engine.dispose()


def test_telegram_settings_are_persisted_in_database() -> None:
    db, engine = make_session()
    settings = Settings(
        _env_file=None,
        telegram_enabled=False,
        telegram_chat_id='env-chat-id',
        telegram_bot_token='secret-token',
    )
    try:
        save_telegram_settings(db, enabled=True, chat_id='-1001234567890')
        result = get_telegram_settings(settings, db)

        assert result == {
            'enabled': True,
            'chat_id': '-1001234567890',
            'bot_token_configured': True,
        }
        assert db.get(AppSetting, 'telegram_chat_id').value == '-1001234567890'
    finally:
        db.close()
        engine.dispose()


def test_post_search_matches_content_author_and_group() -> None:
    db, engine = make_session()
    try:
        db.add_all([
            ScrapedPost(
                group_url='https://www.facebook.com/groups/stylists',
                group_name='Atlanta Stylists',
                post_id='post-1',
                content_hash='hash-1',
                dedupe_key='post:post-1',
                post_url='https://www.facebook.com/groups/stylists/posts/1',
                author='Alice',
                content='New appointment slots',
                matched_keywords='hair',
                engine='cdp_playwright',
                scraped_at=datetime.now(timezone.utc),
            ),
            ScrapedPost(
                group_url='https://www.facebook.com/groups/vendors',
                group_name='Hair Vendors',
                post_id='post-2',
                content_hash='hash-2',
                dedupe_key='post:post-2',
                post_url='https://www.facebook.com/groups/vendors/posts/2',
                author='Bob',
                content='Wholesale extension list',
                matched_keywords='vendor',
                engine='cdp_playwright',
                scraped_at=datetime.now(timezone.utc),
            ),
        ])
        db.commit()

        assert [post.post_id for post in list_posts(limit=50, q='appointment', db=db)] == ['post-1']
        assert [post.post_id for post in list_posts(limit=50, q='Alice', db=db)] == ['post-1']
        assert [post.post_id for post in list_posts(limit=50, q='Vendors', db=db)] == ['post-2']
    finally:
        db.close()
        engine.dispose()
