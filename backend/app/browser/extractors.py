from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.hash_utils import clean_text, content_hash, sha256_text

POST_URL_PATTERNS = [
    r'/groups/[^/]+/posts/([^/?#]+)',
    r'/groups/[^/]+/permalink/([^/?#]+)',
    r'[?&]set=pcb\.(\d+)',
    r'[?&]set=gm\.(\d+)',
    r'/posts/(\d+)',
    r'/permalink/(\d+)',
    r'/posts/([^/?#]+)',
    r'/permalink/([^/?#]+)',
    r'story_fbid=(\d+)',
    r'multi_permalinks=(\d+)',
    r'fbid=(\d+)',
]
POST_URL_KEYWORDS = ['/posts/', '/permalink/', 'set=pcb.', 'set=gm.', 'story_fbid=', 'multi_permalinks=', 'fbid=']


@dataclass
class RawFacebookPost:
    group_url: str
    post_id: str
    content_hash: str
    dedupe_key: str
    post_url: str
    author: str
    content: str
    engine: str
    scraped_at: datetime
    extended: dict | None = None


def canonical_post_id_from_url(url: str) -> str | None:
    for pattern in POST_URL_PATTERNS:
        match = re.search(pattern, url or '')
        if match:
            return match.group(1)
    return None


def extract_post_id(url: str, group_url: str, content: str) -> str:
    post_id = canonical_post_id_from_url(url)
    if post_id:
        return post_id
    return sha256_text(f'{group_url}|{content[:800]}')[:32]


def dedupe_content_signature(content: str) -> str:
    text = clean_text(content).lower()
    text = re.sub(r'\b(xem thêm|see more)\b.*$', '', text)
    text = re.sub(r'\b\d+\s+(bình luận|comments?|trả lời|replies)\b.*$', '', text)
    text = re.sub(r'(?:\bfacebook\b\s*){3,}', ' ', text)
    text = re.sub(r'(?:\b[a-z0-9]\b\s*){12,}', ' ', text)
    return clean_text(text)[:900]


def build_dedupe_key(group_url: str, post_id: str | None, content: str) -> str:
    if post_id:
        return f'post:{post_id}'[:128]
    signature = dedupe_content_signature(content)
    return f'content:{sha256_text(f"{group_url}|{signature}")[:40]}'


def pick_post_url(urls: list[str]) -> str:
    for url in urls:
        if any(key in url for key in POST_URL_KEYWORDS):
            return url
    return ''


def guess_author_from_text(text: str) -> str:
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    if not lines:
        return ''
    first = clean_text(lines[0])
    blocked = ('like', 'comment', 'share', 'thích', 'bình luận', 'chia sẻ', 'write a comment')
    if len(first) <= 80 and not first.lower().startswith(blocked):
        return first
    return ''


def make_post(group_url: str, content: str, post_url: str, engine: str) -> RawFacebookPost | None:
    content = clean_text(content)
    blocked_fragments = (
        'suggested for you',
        'people you may know',
        'reels',
        'write a comment',
    )
    if any(fragment in content.lower() for fragment in blocked_fragments) and len(content) < 120:
        return None
    if len(content) < 30:
        return None
    post_url = post_url or ''
    post_id = extract_post_id(post_url, group_url, content)
    canonical_post_id = canonical_post_id_from_url(post_url)
    return RawFacebookPost(
        group_url=group_url,
        post_id=post_id,
        content_hash=content_hash(group_url, content),
        dedupe_key=build_dedupe_key(group_url, canonical_post_id, content),
        post_url=post_url,
        author=guess_author_from_text(content),
        content=content,
        engine=engine,
        scraped_at=datetime.now(timezone.utc),
    )
