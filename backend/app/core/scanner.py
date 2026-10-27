from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.browser.cdp_playwright_scraper import CdpPlaywrightFacebookGroupScraper
from app.browser.playwright_scraper import PlaywrightFacebookGroupScraper
from app.browser.seleniumbase_scraper import SeleniumBaseFacebookGroupScraper
from app.config import Settings
from app.core.csv_writer import append_posts_to_csv
from app.core.keyword_filter import match_keywords, normalize_keywords
from app.core.lead_scoring_settings import get_lead_scoring_settings
from app.db.models import ErrorLog, GroupSource, Keyword, ScanRun, ScrapedPost
from app.integrations.google_sheets import write_posts_to_google_sheets
from app.integrations.telegram import send_telegram_posts
from app.schemas import ScanRequest, ScanResponse

logger = logging.getLogger(__name__)


class FacebookGroupScanner:
    def __init__(self, settings: Settings, db: Session) -> None:
        self.settings = settings
        self.db = db

    def _scraper(self, engine: str):
        if engine == 'seleniumbase':
            return SeleniumBaseFacebookGroupScraper(self.settings)
        if engine == 'cdp_playwright':
            return CdpPlaywrightFacebookGroupScraper(self.settings)
        return PlaywrightFacebookGroupScraper(self.settings)

    def ensure_login(self, engine: str) -> None:
        self._scraper(engine).ensure_login()

    def login_status(self, engine: str) -> dict[str, object]:
        scraper = self._scraper(engine)
        if hasattr(scraper, 'login_status'):
            return scraper.login_status()
        return {
            'logged_in': False,
            'profile_dir': str(self.settings.seleniumbase_profile_dir),
            'storage_state_file': None,
        }

    def _group_urls(self, request: ScanRequest) -> list[tuple[str, str]]:
        if request.group_urls:
            return [(url, '') for url in request.group_urls]
        rows = self.db.execute(
            select(GroupSource)
            .where(GroupSource.is_active == True)
            .order_by(GroupSource.created_at.desc())
        ).scalars().all()
        return [(row.url, row.name) for row in rows]

    def _keywords(self, request: ScanRequest) -> list[str]:
        if request.keywords is not None:
            return normalize_keywords(request.keywords)
        rows = self.db.execute(
            select(Keyword)
            .where(Keyword.is_active == True)
            .order_by(Keyword.created_at.desc())
        ).scalars().all()
        return normalize_keywords([row.keyword for row in rows])

    def _exists(self, post_id: str | None, content_hash: str, dedupe_key: str) -> bool:
        conditions = [
            ScrapedPost.content_hash == content_hash,
            ScrapedPost.dedupe_key == dedupe_key,
        ]
        if post_id:
            conditions.append(ScrapedPost.post_id == post_id)
        return self.db.execute(
            select(ScrapedPost.id).where(or_(*conditions)).limit(1)
        ).first() is not None

    def _fail_run_before_scan(
        self,
        engine: str,
        groups_total: int,
        message: str,
    ) -> ScanResponse:
        run = ScanRun(
            engine=engine,
            status='failed',
            groups_total=groups_total,
            groups_failed=groups_total,
            finished_at=datetime.now(timezone.utc),
            message=message,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        self.db.add(ErrorLog(run_id=run.id, engine=engine, error_message=message))
        self.db.commit()
        return ScanResponse(
            run_id=run.id,
            status=run.status,
            engine=engine,  # type: ignore[arg-type]
            groups_total=run.groups_total,
            groups_success=0,
            groups_failed=run.groups_failed,
            posts_seen=0,
            posts_inserted=0,
            posts_matched=0,
            errors=[message],
        )

    def scan(self, request: ScanRequest) -> ScanResponse:
        engine = request.engine or self.settings.default_engine
        max_scrolls = request.max_scrolls or self.settings.max_scrolls_per_group
        scoring_settings = get_lead_scoring_settings(self.settings, self.db)
        max_posts = request.max_posts_per_group or int(scoring_settings['max_posts_per_group'])
        groups = self._group_urls(request)
        keywords = self._keywords(request)

        if engine in ('playwright', 'cdp_playwright') and groups:
            try:
                logged_in = bool(self.login_status(engine).get('logged_in'))
            except Exception as exc:
                logger.warning('Không thể kiểm tra session trước khi quét engine=%s: %s', engine, exc)
                return self._fail_run_before_scan(
                    engine,
                    len(groups),
                    f'Không thể kiểm tra session Facebook: {exc}',
                )
            if not logged_in:
                return self._fail_run_before_scan(
                    engine,
                    len(groups),
                    f'Facebook profile {engine} chưa đăng nhập hoặc session đã hết hạn. '
                    'Đăng nhập lại bằng Chạy quét > Mở đăng nhập.',
                )

        run = ScanRun(engine=engine, status='running', groups_total=len(groups))
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        scraper = self._scraper(engine)
        errors: list[str] = []
        inserted_posts: list[ScrapedPost] = []

        for group_url, group_name in groups:
            group_ok = False
            last_error = ''
            for attempt in range(1, self.settings.retry_times + 2):
                try:
                    logger.info(
                        'Scanning group=%s attempt=%s engine=%s',
                        group_url,
                        attempt,
                        engine,
                    )
                    raw_posts = scraper.scrape_group(
                        group_url,
                        max_scrolls,
                        max_posts,
                        self.settings.screenshot_dir,
                    )
                    run.posts_seen += len(raw_posts)

                    for raw in raw_posts:
                        matched = match_keywords(raw.content, keywords)
                        if keywords and not matched:
                            continue
                        run.posts_matched += 1
                        if self._exists(raw.post_id, raw.content_hash, raw.dedupe_key):
                            continue
                        post = ScrapedPost(
                            run_id=run.id,
                            group_url=raw.group_url,
                            group_name=group_name,
                            post_id=raw.post_id,
                            content_hash=raw.content_hash,
                            dedupe_key=raw.dedupe_key,
                            post_url=raw.post_url,
                            author=raw.author,
                            content=raw.content,
                            matched_keywords=', '.join(matched),
                            engine=raw.engine,
                            scraped_at=raw.scraped_at,
                        )
                        self.db.add(post)
                        try:
                            self.db.commit()
                            self.db.refresh(post)
                            inserted_posts.append(post)
                            run.posts_inserted += 1
                        except IntegrityError:
                            self.db.rollback()
                            continue
                    group_ok = True
                    run.groups_success += 1
                    break
                except Exception as exc:
                    self.db.rollback()
                    last_error = str(exc)
                    logger.exception(
                        'Group scan failed group=%s attempt=%s error=%s',
                        group_url,
                        attempt,
                        exc,
                    )
                    if attempt <= self.settings.retry_times:
                        time.sleep(self.settings.retry_sleep_seconds)

            if not group_ok:
                run.groups_failed += 1
                errors.append(f'{group_url}: {last_error}')
                self.db.add(
                    ErrorLog(
                        run_id=run.id,
                        group_url=group_url,
                        engine=engine,
                        error_message=last_error or 'Unknown group scan error',
                        screenshot_path=str(self.settings.screenshot_dir),
                    )
                )
                self.db.commit()

            self.db.add(run)
            self.db.commit()

        run.status = 'success' if not errors else (
            'partial_failed' if run.groups_success else 'failed'
        )
        run.finished_at = datetime.now(timezone.utc)
        run.message = '; '.join(errors[:5]) if errors else 'Scan completed'
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        append_posts_to_csv(self.settings.csv_output_file, inserted_posts)
        if request.send_telegram:
            send_telegram_posts(self.settings, inserted_posts, self.db)
        if request.write_google_sheets:
            write_posts_to_google_sheets(self.settings, inserted_posts)

        return ScanResponse(
            run_id=run.id,
            status=run.status,
            engine=engine,  # type: ignore[arg-type]
            groups_total=run.groups_total,
            groups_success=run.groups_success,
            groups_failed=run.groups_failed,
            posts_seen=run.posts_seen,
            posts_inserted=run.posts_inserted,
            posts_matched=run.posts_matched,
            errors=errors,
        )
