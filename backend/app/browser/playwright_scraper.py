from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.browser.extractors import RawFacebookPost, make_post, pick_post_url
from app.browser.post_dom_filter import (
    TOP_LEVEL_POSTS_JS,
    looks_like_comment,
    normalize_facebook_text,
)
from app.config import Settings

logger = logging.getLogger(__name__)

ARTICLE_SELECTORS = [
    '[role="article"]',
    'div[aria-posinset]',
    'div[data-pagelet*="FeedUnit"]',
]

LOGIN_SIGNALS = [
    'login',
    'log in',
    'dang nhap',
    'đăng nhập',
    'email or phone',
    'mat khau',
    'mật khẩu',
]

LOGGED_IN_SELECTORS = [
    '[aria-label="Facebook"]',
    '[aria-label="Home"]',
    '[aria-label="Trang chủ"]',
    '[role="navigation"]',
    '[role="feed"]',
]

BLOCKED_OR_NO_ACCESS_SIGNALS = [
    'this content isn\'t available',
    'this content is not available',
    'content unavailable',
    'you can\'t access this group',
    'nội dung này hiện không hiển thị',
]

JOIN_REQUIRED_WITHOUT_POSTS_SIGNALS = [
    'join this group to view',
    'join this group to see',
    'join group to see',
    'you must join this group',
    'private group',
    'tham gia nhóm này để xem',
    'tham gia nhóm để xem',
    'nhóm riêng tư',
    'nhóm kín',
]


class PlaywrightFacebookGroupScraper:
    engine = 'playwright'

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _new_context(self, playwright, *, headless: bool):
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.playwright_profile_dir),
            headless=headless,
            slow_mo=self.settings.browser_slow_mo_ms,
            viewport={'width': 1365, 'height': 850},
            locale='vi-VN',
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
            ],
        )

    def _persist_state(self, context) -> None:
        try:
            self.settings.playwright_storage_state_file.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(self.settings.playwright_storage_state_file))
        except Exception as exc:
            logger.warning('Could not persist Playwright storage state: %s', exc)

    def _is_logged_in(self, page) -> bool:
        try:
            url = page.url.lower()
            body = page.locator('body').inner_text(timeout=4000).lower()
        except Exception:
            return False
        if any(signal in url or signal in body for signal in LOGIN_SIGNALS):
            return False
        for selector in LOGGED_IN_SELECTORS:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return 'facebook.com' in url and bool(body.strip())

    def login_status(self) -> dict[str, object]:
        with sync_playwright() as p:
            context = self._new_context(p, headless=True)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto('https://www.facebook.com/', wait_until='domcontentloaded', timeout=self.settings.page_load_timeout_ms)
                page.wait_for_timeout(1500)
                logged_in = self._is_logged_in(page)
                if logged_in:
                    self._persist_state(context)
                return {
                    'logged_in': logged_in,
                    'profile_dir': str(self.settings.playwright_profile_dir),
                    'storage_state_file': str(self.settings.playwright_storage_state_file),
                }
            finally:
                context.close()

    def ensure_login(self) -> None:
        logger.info('Opening Playwright browser for manual Facebook login')
        deadline = time.monotonic() + self.settings.login_wait_timeout_seconds
        with sync_playwright() as p:
            context = self._new_context(p, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto('https://www.facebook.com/', wait_until='domcontentloaded', timeout=self.settings.page_load_timeout_ms)
                logger.info('Login browser opened. Waiting for Facebook session to become authenticated.')
                while time.monotonic() < deadline:
                    page.wait_for_timeout(2500)
                    if self._is_logged_in(page):
                        self._persist_state(context)
                        logger.info('Facebook login detected and Playwright profile saved.')
                        return
                raise TimeoutError(
                    f'Chưa phát hiện đăng nhập Facebook sau {self.settings.login_wait_timeout_seconds} giây. '
                    'Hãy chạy lại login và hoàn tất checkpoint/CAPTCHA thủ công nếu Facebook yêu cầu.'
                )
            finally:
                context.close()

    def _login_required(self, page) -> bool:
        return not self._is_logged_in(page)

    def _latest_group_url(self, group_url: str) -> str:
        if not self.settings.facebook_latest_sorting:
            return group_url
        parsed = urlparse(group_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault('sorting_setting', 'CHRONOLOGICAL')
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _page_has_no_access(self, page) -> bool:
        try:
            body = page.locator('body').inner_text(timeout=4000).lower()
        except Exception:
            return False
        return any(signal in body for signal in BLOCKED_OR_NO_ACCESS_SIGNALS)

    def _page_requires_join_without_posts(self, page) -> bool:
        try:
            body = page.locator('body').inner_text(timeout=4000).lower()
            article_count = page.locator('[role="article"], div[data-pagelet*="FeedUnit"]').count()
        except Exception:
            return False
        if article_count > 0:
            return False
        return any(signal in body for signal in JOIN_REQUIRED_WITHOUT_POSTS_SIGNALS)

    def _article_cards(self, page):
        for selector in ARTICLE_SELECTORS:
            cards = page.locator(selector)
            try:
                if cards.count() > 0:
                    return cards
            except Exception:
                continue
        return page.locator('[role="article"]')

    def _extract_visible_posts(self, page, group_url: str) -> dict[str, RawFacebookPost]:
        posts: dict[str, RawFacebookPost] = {}
        try:
            items = page.evaluate(TOP_LEVEL_POSTS_JS)
        except Exception:
            logger.exception('Could not extract top-level posts from the DOM')
            return posts

        for item in items[:80]:
            text = normalize_facebook_text(item.get('text'))
            post_url = pick_post_url(item.get('hrefs') or [])
            if looks_like_comment(text, post_url):
                continue

            post = make_post(group_url, text, post_url, self.engine)
            if post:
                posts[post.post_id] = post

        return posts

    def scrape_group(self, group_url: str, max_scrolls: int, max_posts: int, screenshot_dir: Path) -> list[RawFacebookPost]:
        posts: dict[str, RawFacebookPost] = {}
        target_url = self._latest_group_url(group_url)

        with sync_playwright() as p:
            context = self._new_context(p, headless=self.settings.headless)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(self.settings.page_load_timeout_ms)
            try:
                page.goto(target_url, wait_until='domcontentloaded', timeout=self.settings.page_load_timeout_ms)
                page.wait_for_timeout(4500)
                if self._login_required(page):
                    raise RuntimeError(
                        'Facebook profile chưa đăng nhập hoặc session đã hết hạn. '
                        'Gọi POST /api/v1/login/playwright hoặc chạy scripts/login_playwright.py để đăng nhập lại.'
                    )
                if self._page_has_no_access(page):
                    raise RuntimeError('Tài khoản Facebook hiện tại chưa có quyền xem group này hoặc group yêu cầu join/approve.')

                idle_rounds = 0
                for _scroll_index in range(max_scrolls):
                    try:
                        page.wait_for_selector(','.join(ARTICLE_SELECTORS), timeout=15000)
                    except PlaywrightTimeoutError:
                        logger.info('No article cards visible yet for %s', group_url)
                        if not posts and self._page_requires_join_without_posts(page):
                            raise RuntimeError(
                                'Group này là private hoặc yêu cầu tham gia. '
                                'Tài khoản Facebook hiện tại chưa join hoặc chưa được duyệt.'
                            )

                    before_count = len(posts)
                    posts.update(self._extract_visible_posts(page, group_url))
                    if len(posts) >= max_posts:
                        break

                    idle_rounds = idle_rounds + 1 if len(posts) == before_count else 0
                    if idle_rounds >= 2:
                        break

                    page.mouse.wheel(0, 2600)
                    page.wait_for_timeout(int(self.settings.scroll_wait_seconds * 1000))

                self._persist_state(context)
                return list(posts.values())[:max_posts]
            except Exception:
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                shot = screenshot_dir / f'playwright_error_{abs(hash(group_url))}.png'
                try:
                    page.screenshot(path=str(shot), full_page=True)
                except Exception:
                    pass
                raise
            finally:
                context.close()
