from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta,timezone
from pathlib import Path
from typing import List, Dict, Optional, Any

import arrow
import dateparser
from seleniumbase import SB
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from app.browser.extractors import RawFacebookPost, make_post, pick_post_url
from app.browser.post_dom_filter import looks_like_comment, normalize_facebook_text
from app.config import Settings

logger = logging.getLogger(__name__)


class AdvancedFacebookScraper:
    """
    Facebook scraper mạnh mẽ dựa trên SeleniumBase + CDP.
    Có thể lấy: like, comment, share, reaction details, comments, media, author, timestamp.
    """

    engine = "advanced_fb_scraper"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sb = None

    def _start_sb(self, headless: bool = False) -> SB:
        """Khởi tạo SeleniumBase với UC mode và profile đã login."""
        sb = SB(
            uc=True,
            headless=headless,
            user_data_dir=str(self.settings.cdp_playwright_profile_dir),  # tái sử dụng profile
            locale="vi-VN",
            ad_block=True,
            block_images=False,
            disable_csp=True,
            window_size="1366,850",
            disable_automation_controlled=True,
            no_sandbox=True,
            disable_gpu=True,
            disable_dev_shm_usage=True,
            agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        sb.activate_cdp_mode()
        return sb

    # ------------------- Đăng nhập và kiểm tra -------------------
    def ensure_login(self) -> None:
        """Mở trình duyệt để đăng nhập thủ công (chỉ 1 lần)."""
        logger.info("Mở trình duyệt để đăng nhập Facebook...")
        sb = self._start_sb(headless=False)
        sb.open("https://www.facebook.com/")
        # Xoá dấu hiệu automation
        sb.execute_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            window.chrome = { runtime: {} };
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
        """)
        input("\n🔐 Đã đăng nhập xong? Nhấn Enter để lưu profile...")
        sb.quit()
        logger.info("Đã lưu profile đăng nhập.")

    def login_status(self) -> Dict[str, Any]:
        sb = self._start_sb(headless=True)
        try:
            sb.open("https://www.facebook.com/")
            time.sleep(3)
            logged_in = sb.is_element_visible('[aria-label="Facebook"]') or sb.is_element_visible('[role="feed"]')
            return {"logged_in": logged_in, "profile_dir": str(self.settings.cdp_playwright_profile_dir)}
        finally:
            sb.quit()

    # ------------------- Trích xuất từ một bài viết -------------------
    @staticmethod
    def _parse_facebook_time(time_str: str) -> datetime:
        """Chuyển '1 giờ trước', 'Yesterday at 15:30', 'December 5, 2024' -> datetime."""
        time_str = time_str.lower().strip()
        now = datetime.now()

        # Xử lý các dạng đặc biệt
        if "vừa xong" in time_str or "just now" in time_str:
            return now
        if "phút trước" in time_str or "minutes ago" in time_str:
            num = int(re.search(r'\d+', time_str).group())
            return now - timedelta(minutes=num)
        if "giờ trước" in time_str or "hours ago" in time_str:
            num = int(re.search(r'\d+', time_str).group())
            return now - timedelta(hours=num)
        if "ngày trước" in time_str or "days ago" in time_str:
            num = int(re.search(r'\d+', time_str).group())
            return now - timedelta(days=num)
        if "yesterday" in time_str or "hôm qua" in time_str:
            # Có thể kèm giờ
            hour_match = re.search(r'(\d{1,2}):(\d{2})', time_str)
            if hour_match:
                h, m = map(int, hour_match.groups())
                return now.replace(hour=h, minute=m, second=0) - timedelta(days=1)
            return now - timedelta(days=1)

        # Dùng dateparser cho các dạng khác
        parsed = dateparser.parse(time_str, languages=['vi', 'en'])
        if parsed:
            return parsed
        return now  # fallback

    def _extract_author(self, post_element) -> Dict[str, str]:
        """Trả về {'name': '...', 'url': '...'} của người đăng."""
        try:
            author_link = post_element.find_element(By.CSS_SELECTOR, 'h3 a, strong a, span a[href*="facebook.com/"]')
            name = author_link.text.strip()
            url = author_link.get_attribute('href')
            return {"name": name, "url": url}
        except:
            return {"name": "", "url": ""}

    def _extract_timestamp(self, post_element) -> Optional[datetime]:
        """Lấy timestamp từ thẻ time hoặc span chứa ngày."""
        try:
            time_elem = post_element.find_element(By.CSS_SELECTOR, 'abbr, time, span[data-store*="time"]')
            time_str = time_elem.get_attribute('title') or time_elem.text
            if time_str:
                return self._parse_facebook_time(time_str)
        except:
            pass
        return None

    def _extract_reaction_counts(self, post_element) -> Dict[str, int]:
        """Trả về tổng số reaction + chi tiết từng loại (nếu click mở popup)."""
        reactions = {"total": 0, "like": 0, "love": 0, "care": 0, "haha": 0, "wow": 0, "sad": 0, "angry": 0}
        try:
            # Tìm span chứa số reaction
            reaction_span = post_element.find_element(By.CSS_SELECTOR, 'div[aria-label*="reactions"] span, span[class*="reaction"]')
            total_text = reaction_span.text.replace(',', '').strip()
            if total_text.isdigit():
                reactions["total"] = int(total_text)
        except:
            pass

        # Click vào số reaction để mở popup chi tiết (cần WebDriverWait)
        try:
            reactor_btn = post_element.find_element(By.CSS_SELECTOR, 'a[href*="reactors"]')
            driver = post_element.parent
            driver.execute_script("arguments[0].scrollIntoView(true);", reactor_btn)
            reactor_btn.click()
            time.sleep(2)
            # Lấy các reaction tooltip
            popup = driver.find_element(By.CSS_SELECTOR, 'div[role="dialog"]')
            items = popup.find_elements(By.CSS_SELECTOR, 'div[role="tablist"] div[role="tab"]')
            for item in items:
                aria_label = item.get_attribute('aria-label') or ""
                count_text = item.find_element(By.CSS_SELECTOR, 'span').text.replace(',', '')
                if count_text.isdigit():
                    count = int(count_text)
                    if "like" in aria_label.lower():
                        reactions["like"] = count
                    elif "love" in aria_label.lower():
                        reactions["love"] = count
                    elif "care" in aria_label.lower():
                        reactions["care"] = count
                    elif "haha" in aria_label.lower():
                        reactions["haha"] = count
                    elif "wow" in aria_label.lower():
                        reactions["wow"] = count
                    elif "sad" in aria_label.lower():
                        reactions["sad"] = count
                    elif "angry" in aria_label.lower():
                        reactions["angry"] = count
            # Đóng popup
            driver.find_element(By.CSS_SELECTOR, 'div[aria-label="Close"]').click()
        except Exception as e:
            logger.debug(f"Không thể lấy reaction detail: {e}")
        return reactions

    def _extract_comments(self, post_element, max_comments: int = 50) -> List[Dict]:
        """Lấy comments (có phân trang) bằng cách click 'Xem thêm bình luận' và scroll."""
        comments = []
        try:
            # Tìm và click "Xem thêm bình luận" (nếu có)
            more_btn = post_element.find_element(By.XPATH, './/span[contains(text(), "Xem thêm bình luận")]/..')
            driver = post_element.parent
            driver.execute_script("arguments[0].scrollIntoView(true);", more_btn)
            more_btn.click()
            time.sleep(2)

            # Comment container
            comment_area = post_element.find_element(By.CSS_SELECTOR, 'div[data-ad-comet-preview="message"]')
            # Scroll trong comment area để load thêm
            last_height = 0
            for _ in range(5):  # tối đa 5 lần scroll comments
                driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight", comment_area)
                time.sleep(1.5)
                new_height = driver.execute_script("return arguments[0].scrollHeight", comment_area)
                if new_height == last_height:
                    break
                last_height = new_height

            # Lấy từng comment
            comment_divs = comment_area.find_elements(By.CSS_SELECTOR, 'div[data-ad-comet-preview="message"] div[dir="auto"]')
            for cmt in comment_divs[:max_comments]:
                try:
                    author = cmt.find_element(By.XPATH, './/h3/a').text.strip()
                    text = cmt.text.strip()
                    comments.append({"author": author, "text": text})
                except:
                    continue
        except Exception as e:
            logger.debug(f"Không lấy được comments: {e}")
        return comments

    def _extract_media(self, post_element) -> List[Dict]:
        """Trả về list các media: {'type': 'image'/'video', 'url': '...', 'thumbnail': '...'}."""
        media_list = []
        try:
            # Hình ảnh
            imgs = post_element.find_elements(By.CSS_SELECTOR, 'img[src*=".jpg"], img[src*=".png"]')
            for img in imgs[:5]:  # tối đa 5 ảnh
                src = img.get_attribute('src')
                if src and 'static' not in src:
                    media_list.append({"type": "image", "url": src})

            # Video
            videos = post_element.find_elements(By.CSS_SELECTOR, 'video[src*=".mp4"]')
            for vid in videos:
                src = vid.get_attribute('src')
                if src:
                    media_list.append({"type": "video", "url": src})
        except:
            pass
        return media_list

    def _extract_full_post_data(self, post_element, group_url: str) -> Optional[Dict]:
        """Trích xuất toàn bộ dữ liệu của một bài viết."""
        try:
            # Text content
            text_elem = post_element.find_element(By.CSS_SELECTOR, 'div[data-ad-comet-preview="message"] div[dir="auto"]')
            text = text_elem.text.strip()

            # Author
            author = self._extract_author(post_element)

            # Timestamp
            timestamp = self._extract_timestamp(post_element)

            # Like, comment, share counts
            reaction_counts = self._extract_reaction_counts(post_element)

            # Comments (chỉ lấy nếu cần, có thể bật/tắt)
            comments = []
            if self.settings.get("fetch_comments", False):
                comments = self._extract_comments(post_element, max_comments=20)

            # Media
            media = self._extract_media(post_element)

            # Số share (thường nằm cạnh like/comment)
            share_count = 0
            try:
                share_span = post_element.find_element(By.CSS_SELECTOR, 'span[aria-label*="share"] span, a[href*="share"] span')
                share_text = share_span.text.replace(',', '')
                if share_text.isdigit():
                    share_count = int(share_text)
            except:
                pass

            return {
                "post_id": None,  # sẽ generate sau
                "group_url": group_url,
                "content": text,
                "author_name": author["name"],
                "author_url": author["url"],
                "timestamp": timestamp.isoformat() if timestamp else None,
                "like_count": reaction_counts["total"],
                "reaction_detail": {k: v for k, v in reaction_counts.items() if k != "total"},
                "comment_count": len(comments),
                "share_count": share_count,
                "comments": comments,
                "media": media,
                "post_url": "",  # sẽ được set từ pick_post_url
            }
        except Exception as e:
            logger.error(f"Lỗi khi parse bài viết: {e}")
            return None

    # ------------------- Scrape chính -------------------
    def scrape_group(
        self,
        group_url: str,
        max_scrolls: int = 20,
        max_posts: int = 50,
        screenshot_dir: Path | None = None,
        fetch_comments: bool = False,
        min_timestamp: datetime | None = None,  # lọc bài đăng sau thời gian này
    ) -> List[RawFacebookPost]:
        """
        Scrape group với đầy đủ thông tin.
        - fetch_comments: True nếu muốn lấy nội dung comments (tốn thời gian).
        - min_timestamp: chỉ lấy bài có timestamp >= thời gian này.
        """
        if screenshot_dir is None:
            screenshot_dir = Path("data/screenshots")
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        self.settings.setdefault("fetch_comments", fetch_comments)  # tạm lưu
        self.sb = self._start_sb(headless=self.settings.headless)

        posts_result = []  # list RawFacebookPost
        seen_ids = set()

        try:
            # Mở group với sorting CHRONOLOGICAL
            from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
            parsed = urlparse(group_url)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            query['sorting_setting'] = 'CHRONOLOGICAL'
            target_url = urlunparse(parsed._replace(query=urlencode(query)))

            self.sb.open(target_url)
            self.sb.sleep(5)

            # Xoá dấu hiệu automation
            self.sb.execute_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = { runtime: {} };
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
            """)

            # Kiểm tra đăng nhập
            if not (self.sb.is_element_visible('[aria-label="Facebook"]') or self.sb.is_element_visible('[role="feed"]')):
                raise RuntimeError("Chưa đăng nhập hoặc session hết hạn. Hãy chạy ensure_login() trước.")

            scroll_attempt = 0
            last_post_count = 0

            while scroll_attempt < max_scrolls and len(posts_result) < max_posts:
                # Tìm tất cả bài viết (role="article")
                articles = self.sb.find_elements(By.CSS_SELECTOR, 'div[role="article"]')
                logger.info(f"Scroll {scroll_attempt+1}: tìm thấy {len(articles)} article elements")

                for article in articles:
                    if len(posts_result) >= max_posts:
                        break

                    parent_article = self.sb.execute_script(
                        "return arguments[0].parentElement"
                        "  ? arguments[0].parentElement.closest('[role=\"article\"]')"
                        "  : null;",
                        article,
                    )
                    if parent_article:
                        continue

                    # Lấy dữ liệu đầy đủ
                    data = self._extract_full_post_data(article, group_url)
                    if not data:
                        continue

                    # Lấy post_url từ các link trong bài
                    hrefs = [a.get_attribute('href') for a in article.find_elements(By.CSS_SELECTOR, 'a[href]') if a.get_attribute('href')]
                    post_url = pick_post_url(hrefs)
                    data['post_url'] = post_url
                    data['content'] = normalize_facebook_text(data['content'])
                    if looks_like_comment(data['content'], post_url):
                        continue

                    # Tạo post_id (dùng hàm extract_post_id từ extractors)
                    from app.browser.extractors import build_dedupe_key, canonical_post_id_from_url, extract_post_id
                    post_id = extract_post_id(post_url, group_url, data['content'])
                    dedupe_key = build_dedupe_key(
                        group_url,
                        canonical_post_id_from_url(post_url),
                        data['content'],
                    )
                    data['post_id'] = post_id

                    # Lọc theo timestamp nếu có
                    if min_timestamp and data['timestamp']:
                        post_dt = datetime.fromisoformat(data['timestamp'])
                        if post_dt < min_timestamp:
                            continue

                    if post_id not in seen_ids:
                        seen_ids.add(post_id)
                        # Chuyển thành RawFacebookPost
                        raw_post = RawFacebookPost(
                            group_url=group_url,
                            post_id=post_id,
                            content_hash=content_hash(group_url, data['content']),
                            dedupe_key=dedupe_key,
                            post_url=post_url,
                            author=data['author_name'],
                            content=data['content'],
                            engine=self.engine,
                            scraped_at=datetime.now(timezone.utc),
                        )
                        # Gắn thêm các trường mở rộng (có thể lưu riêng)
                        raw_post.extended = {
                            "author_url": data['author_url'],
                            "timestamp": data['timestamp'],
                            "like_count": data['like_count'],
                            "reaction_detail": data['reaction_detail'],
                            "comment_count": data['comment_count'],
                            "share_count": data['share_count'],
                            "comments": data['comments'],
                            "media": data['media'],
                        }
                        posts_result.append(raw_post)

                # Scroll mượt
                if len(posts_result) == last_post_count:
                    scroll_attempt += 1
                else:
                    scroll_attempt = 0
                last_post_count = len(posts_result)

                # Cuộn trang
                self.sb.execute_script("window.scrollBy({top: window.innerHeight * 0.9, behavior: 'smooth'});")
                wait_time = self.settings.scroll_wait_seconds
                time.sleep(wait_time)

            logger.info(f"Thu thập xong: {len(posts_result)} bài viết")
            return posts_result[:max_posts]

        except Exception as e:
            logger.exception("Lỗi trong quá trình scrape")
            try:
                self.sb.save_screenshot(str(screenshot_dir / "error_advanced.png"))
            except:
                pass
            raise
        finally:
            self.sb.quit()


# Hàm hash (copy từ extractors, tránh circular import)
def content_hash(group_url: str, content: str) -> str:
    import hashlib
    raw = f"{group_url}|{content[:800]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
