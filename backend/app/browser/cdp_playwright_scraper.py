from __future__ import annotations

import logging
import os
import random
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.sync_api import Browser, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
import playwright_stealth

from app.browser.post_dom_filter import (
    TOP_LEVEL_POSTS_JS,
    looks_like_comment,
    normalize_facebook_text,
)
from app.browser.extractors import RawFacebookPost, make_post, pick_post_url
from app.config import Settings

logger = logging.getLogger(__name__)
_CDP_SESSION_LOCK = threading.RLock()


def apply_stealth(page) -> None:
    stealth_sync = getattr(playwright_stealth, 'stealth_sync', None)
    if callable(stealth_sync):
        try:
            stealth_sync(page)
        except (AttributeError, TypeError) as exc:
            logger.warning('playwright-stealth legacy API is incompatible; using built-in init script: %s', exc)
        return

    stealth_class = getattr(playwright_stealth, 'Stealth', None)
    if stealth_class is not None:
        try:
            stealth_class().apply_stealth_sync(page)
        except (AttributeError, TypeError) as exc:
            logger.warning('playwright-stealth API is incompatible; using built-in init script: %s', exc)
        return

    logger.warning('playwright-stealth has no supported API; using built-in init script')

ARTICLE_SELECTORS = [
    '[role="article"]',
    'div[aria-posinset]',
    'div[data-pagelet*="FeedUnit"]',
]

LOGIN_SIGNALS = [
    'login', 'log in', 'dang nhap', 'đăng nhập', 'email or phone',
    'mat khau', 'mật khẩu',
]

LOGGED_IN_SELECTORS = [
    '[aria-label="Facebook"]',
    '[aria-label="Home"]',
    '[aria-label="Trang chủ"]',
    '[role="navigation"]',
    '[role="feed"]',
]

BLOCKED_OR_NO_ACCESS_SIGNALS = [
    "this content isn't available", 'this content is not available',
    'content unavailable', "you can't access this group",
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

CHECKPOINT_SIGNALS = [
    'checkpoint', 'confirm your identity', 'verify your account',
    'we detected unusual activity', 'bảo vệ tài khoản', 'xác minh danh tính',
    'nhập mã bảo mật', 'enter security code', 'captcha', 'security check',
]

CAPTCHA_IFRAME_SELECTORS = [
    'iframe[title*="captcha"]',
    'iframe[src*="captcha"]',
    'iframe[src*="recaptcha"]',
    'div[id*="captcha"]',
]


class CdpPlaywrightFacebookGroupScraper:
    engine = 'cdp_playwright'

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ---------- Chrome CDP launcher với anti-detection tối đa ----------
    def _chrome_executable(self) -> Path:
        configured = self.settings.cdp_chrome_executable.strip()
        if configured and Path(configured).is_file():
            return Path(configured)

        playwright_cache = Path.home() / '.cache/ms-playwright'
        candidates = [
            shutil.which('chrome'), shutil.which('chrome.exe'),
            Path.home() / 'AppData/Local/Google/Chrome/Application/chrome.exe',
            Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
            Path('C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'),
            shutil.which('msedge'),
            Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),
            *playwright_cache.glob('chromium-*/chrome-linux/chrome'),
            *playwright_cache.glob('chromium-*/chrome-linux64/chrome'),
            *playwright_cache.glob('chromium-*/chrome-win/chrome.exe'),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return Path(candidate)
        raise RuntimeError('Không tìm thấy Chrome/Edge. Cài trình duyệt Chromium-based.')

    def _remove_stale_profile_locks(self) -> None:
        profile_dir = self.settings.cdp_playwright_profile_dir.resolve()
        for name in ('SingletonCookie', 'SingletonLock', 'SingletonSocket', 'DevToolsActivePort'):
            path = profile_dir / name
            try:
                if os.path.lexists(path):
                    path.unlink()
            except OSError as exc:
                logger.warning('Không thể xóa khóa Chrome cũ %s: %s', path, exc)

    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def _wait_for_endpoint(self, endpoint_url: str) -> None:
        deadline = time.monotonic() + max(10, self.settings.cdp_reconnect_time)
        while time.monotonic() < deadline:
            try:
                from urllib.request import urlopen
                with urlopen(f'{endpoint_url}/json/version', timeout=1):
                    return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError('Chrome CDP endpoint không sẵn sàng')

    @contextmanager
    def _browser_session(self, *, headed: bool) -> Iterator[str]:
        with _CDP_SESSION_LOCK:
            with self._browser_session_unlocked(headed=headed) as endpoint_url:
                yield endpoint_url

    @contextmanager
    def _browser_session_unlocked(self, *, headed: bool) -> Iterator[str]:
        if headed and os.name != 'nt' and not os.environ.get('DISPLAY'):
            raise RuntimeError(
                'Backend đang chạy trong Docker không có màn hình nên không thể mở cửa sổ đăng nhập. '
                'Hãy chạy backend trực tiếp trên Windows để đăng nhập profile, sau đó khởi động lại Docker.'
            )

        self.settings.cdp_playwright_profile_dir.mkdir(parents=True, exist_ok=True)
        self._remove_stale_profile_locks()
        port = self._free_port()
        endpoint_url = f'http://127.0.0.1:{port}'
        args = [
            str(self._chrome_executable()),
            f'--remote-debugging-port={port}',
            f'--user-data-dir={self.settings.cdp_playwright_profile_dir.resolve()}',
            '--remote-allow-origins=*',
            '--no-first-run', '--no-default-browser-check',
            '--disable-dev-shm-usage', '--no-sandbox',
            '--lang=vi-VN', '--window-size=1365,850',
            # Anti-detection flags
            '--disable-blink-features=AutomationControlled',
            '--disable-features=ChromeWhatsNewUI,ChromeTips,ChromeTipsUI,TranslateUI',
            '--disable-default-apps',
            '--disable-component-extensions-with-background-pages',
            '--disable-sync', '--disable-notifications', '--disable-popup-blocking',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--disable-ipc-flooding-protection', '--disable-hang-monitor',
            '--disable-prompt-on-repost', '--disable-client-side-phishing-detection',
            '--disable-crash-reporter', '--disable-logging', '--log-level=3',
            '--silent-debugger-extension-api',
            'about:blank',
        ]
        args = [arg for arg in args if arg != '--enable-automation']
        if not headed:
            args.append('--headless=new')
        if self.settings.uc_mode:
            args.append('--disable-blink-features=AutomationControlled')

        creation_flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creation_flags)
        try:
            self._wait_for_endpoint(endpoint_url)
            yield endpoint_url
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            self._remove_stale_profile_locks()

    def _connect(self, playwright, endpoint_url: str) -> Browser:
        deadline = time.monotonic() + max(1, self.settings.cdp_reconnect_time)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return playwright.chromium.connect_over_cdp(endpoint_url)
            except Exception as exc:
                last_error = exc
                time.sleep(0.25)
        detail = str(last_error).splitlines()[0] if last_error else 'không rõ nguyên nhân'
        raise RuntimeError(
            'Không thể kết nối Playwright vào Chrome CDP. '
            f'Profile Chrome có thể bị hỏng hoặc đang bị khóa ({detail}).'
        ) from last_error

    # ---------- Phát hiện checkpoint (chỉ cảnh báo, không tự giải) ----------
    def _has_checkpoint_or_captcha(self, page) -> bool:
        try:
            body = page.locator('body').inner_text(timeout=5000).lower()
            url = page.url.lower()
        except Exception:
            return False
        for signal in CHECKPOINT_SIGNALS:
            if signal in body or signal in url:
                logger.warning(f"⚠️ Phát hiện checkpoint/captcha: {signal}")
                return True
        for selector in CAPTCHA_IFRAME_SELECTORS:
            if page.locator(selector).count() > 0:
                logger.warning(f"⚠️ Phát hiện iframe captcha: {selector}")
                return True
        if page.locator('input[name="approvals_code"]').count() > 0:
            logger.warning("⚠️ Phát hiện form mã bảo mật Facebook")
            return True
        return False

    # ---------- Hành vi giống người ----------
    def _human_like_behavior(self, page):
        if not self.settings.human_like_mouse_movement:
            return
        try:
            viewport = page.viewport_size
            if not viewport:
                return
            width, height = viewport['width'], viewport['height']
            x = random.randint(50, width - 50)
            y = random.randint(50, height - 50)
            page.mouse.move(x, y, steps=random.randint(8, 20))
            time.sleep(random.uniform(0.2, 0.5))
            if random.random() < 0.3:
                page.evaluate(f"window.scrollBy(0, {random.randint(-80, 80)});")
                time.sleep(random.uniform(0.1, 0.3))
        except Exception:
            pass

    # ---------- Login & status ----------
    def _is_logged_in(self, page) -> bool:
        if self._has_checkpoint_or_captcha(page):
            return False
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
        with self._browser_session(headed=False) as endpoint_url:
            with sync_playwright() as playwright:
                browser = self._connect(playwright, endpoint_url)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
                apply_stealth(page)
                page.goto('https://www.facebook.com/', wait_until='domcontentloaded', timeout=self.settings.page_load_timeout_ms)
                page.wait_for_timeout(1500)
                return {
                    'logged_in': self._is_logged_in(page),
                    'profile_dir': str(self.settings.cdp_playwright_profile_dir),
                    'storage_state_file': None,
                }

    def ensure_login(self) -> None:
        logger.info('Mở CDP Playwright để đăng nhập thủ công')
        deadline = time.monotonic() + self.settings.login_wait_timeout_seconds
        with self._browser_session(headed=True) as endpoint_url:
            with sync_playwright() as playwright:
                browser = self._connect(playwright, endpoint_url)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
                apply_stealth(page)
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['vi-VN', 'vi', 'en-US', 'en']});
                    window.chrome = { runtime: {} };
                    delete window.__playwright;
                    delete window.__pwInitScripts;
                """)
                page.goto('https://www.facebook.com/', wait_until='domcontentloaded', timeout=self.settings.page_load_timeout_ms)
                logger.info(
                    'Cửa sổ đăng nhập Facebook đã mở. '
                    'Hãy đăng nhập và xử lý checkpoint thủ công nếu Facebook yêu cầu.'
                )
                while time.monotonic() < deadline:
                    page.wait_for_timeout(2500)
                    if self._is_logged_in(page):
                        logger.info('✅ Đăng nhập thành công. Profile đã được lưu.')
                        return
                    if self._has_checkpoint_or_captcha(page):
                        logger.warning("❗ Checkpoint/CAPTCHA xuất hiện. Vui lòng xử lý thủ công trong trình duyệt.")
                raise TimeoutError(f'Không phát hiện đăng nhập sau {self.settings.login_wait_timeout_seconds} giây.')

    # ---------- Core scrape (chỉ cảnh báo, không tự giải) ----------
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
        """Extract top-level Facebook group posts without nested comments."""
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

            post = make_post(
                group_url=group_url,
                content=text,
                post_url=post_url,
                engine=self.engine,
            )
            if post:
                posts[post.post_id] = post

        return posts

    def scrape_group(
        self,
        group_url: str,
        max_scrolls: int,
        max_posts: int,
        screenshot_dir: Path,
    ) -> list[RawFacebookPost]:
        posts = {}
        with self._browser_session(headed=not self.settings.headless) as endpoint_url:
            with sync_playwright() as playwright:
                browser = self._connect(playwright, endpoint_url)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(self.settings.page_load_timeout_ms)
                apply_stealth(page)
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['vi-VN', 'vi', 'en-US', 'en']});
                    window.chrome = { runtime: {} };
                    delete window.__playwright;
                    delete window.__pwInitScripts;
                """)
                try:
                    target_url = self._latest_group_url(group_url)
                    page.goto(target_url, wait_until='domcontentloaded', timeout=self.settings.page_load_timeout_ms)
                    page.wait_for_timeout(5000)

                    # Nếu có checkpoint ngay từ đầu → báo và chờ thủ công (nếu headed)
                    if self._has_checkpoint_or_captcha(page):
                        raise RuntimeError(
                            'Facebook yêu cầu checkpoint/CAPTCHA. '
                            'Hãy mở đăng nhập, xử lý thủ công rồi chạy quét lại.'
                        )

                    if not self._is_logged_in(page):
                        raise RuntimeError('Profile chưa đăng nhập hoặc session hết hạn.')
                    if self._page_has_no_access(page):
                        raise RuntimeError('Tài khoản không có quyền xem group này.')

                    idle_rounds = 0
                    for _ in range(max_scrolls):
                        try:
                            page.wait_for_selector(','.join(ARTICLE_SELECTORS), timeout=15000)
                        except PlaywrightTimeoutError:
                            logger.info('Chưa thấy bài viết nào cho %s', group_url)
                            if not posts and self._page_requires_join_without_posts(page):
                                raise RuntimeError(
                                    'Group này là private hoặc yêu cầu tham gia. '
                                    'Tài khoản Facebook hiện tại chưa join hoặc chưa được duyệt.'
                                )

                        before = len(posts)
                        posts.update(self._extract_visible_posts(page, group_url))
                        if len(posts) >= max_posts:
                            break

                        idle_rounds = idle_rounds + 1 if len(posts) == before else 0
                        if idle_rounds >= 2:
                            break

                        # Cuộn mượt + delay ngẫu nhiên
                        scroll_dist = random.randint(500, 1200)
                        if self.settings.scroll_smooth:
                            page.evaluate(f"window.scrollBy({{top: {scroll_dist}, behavior: 'smooth'}});")
                        else:
                            page.mouse.wheel(0, scroll_dist)

                        delay_ms = random.randint(self.settings.min_scroll_delay_ms, self.settings.max_scroll_delay_ms)
                        page.wait_for_timeout(delay_ms)
                        self._human_like_behavior(page)

                        # Kiểm tra checkpoint trong quá trình scroll
                        if self._has_checkpoint_or_captcha(page):
                            raise RuntimeError(
                                'Facebook yêu cầu checkpoint/CAPTCHA trong lúc cuộn. '
                                'Hãy mở đăng nhập, xử lý thủ công rồi chạy quét lại.'
                            )

                    return list(posts.values())[:max_posts]

                except Exception:
                    screenshot_dir.mkdir(parents=True, exist_ok=True)
                    screenshot = screenshot_dir / f'cdp_error_{abs(hash(group_url))}.png'
                    try:
                        page.screenshot(path=str(screenshot), full_page=True)
                    except Exception:
                        pass
                    raise
