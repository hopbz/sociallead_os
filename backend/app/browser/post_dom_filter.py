from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


COMMENT_URL_KEYS = {
    "comment_id",
    "reply_comment_id",
    "comment_tracking",
}

COMMENT_ACTION_RE = re.compile(
    r"(?i)\b(like|thích)\s+(reply|trả lời)\b"
)

POST_ACTION_RE = re.compile(
    r"(?i)\b(like|thích)\s+(comment|bình luận)\s+(share|chia sẻ)\b"
)


TOP_LEVEL_POSTS_JS = r"""
() => {
  const feed = document.querySelector('[role="feed"]') || document.body;

  const candidates = Array.from(
    feed.querySelectorAll('div[aria-posinset], [role="article"], div[data-pagelet*="FeedUnit"]')
  );

  const results = [];
  const seen = new Set();

  for (const el of candidates) {
    // Nested articles are normally comments or replies, not feed posts.
    const parentArticle = el.parentElement
      ? el.parentElement.closest('[role="article"]')
      : null;

    if (parentArticle && !el.hasAttribute('aria-posinset')) continue;

    const text = (el.innerText || '').trim();
    if (!text || text.length < 20) continue;

    const hrefs = Array.from(el.querySelectorAll('a[href]'))
      .map(a => a.href)
      .filter(Boolean);

    const postLikeHref =
      hrefs.find(h => h.includes('/posts/')) ||
      hrefs.find(h => h.includes('/permalink/')) ||
      hrefs.find(h => h.includes('story_fbid=')) ||
      hrefs.find(h => h.includes('multi_permalinks=')) ||
      hrefs.find(h => h.includes('fbid='));

    const dedupeKey = postLikeHref || text.slice(0, 160);
    if (seen.has(dedupeKey)) continue;

    seen.add(dedupeKey);

    results.push({
      text,
      hrefs,
      postLikeHref,
    });
  }

  return results;
}
"""


def normalize_facebook_text(text: str | None) -> str:
    if not text:
        return ""
    clean = re.sub(r"\s+", " ", text).strip()
    clean = re.sub(r"^(?:Facebook\s+){3,}", "", clean, flags=re.IGNORECASE).strip()
    return clean


def has_comment_url(url: str | None) -> bool:
    if not url:
        return False

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    if any(key in query for key in COMMENT_URL_KEYS):
        return True

    path = parsed.path.lower()
    return "/comment/" in path or "/comments/" in path


def looks_like_comment(text: str | None, post_url: str | None = None) -> bool:
    clean_text = normalize_facebook_text(text)

    if not clean_text:
        return True

    if has_comment_url(post_url):
        return True

    # Comments commonly include "Like Reply"; posts include "Like Comment Share".
    if COMMENT_ACTION_RE.search(clean_text) and not POST_ACTION_RE.search(clean_text):
        return True

    # Short comments often include "See translation" together with "Reply".
    if len(clean_text) < 350 and re.search(
        r"(?i)(xem bản dịch|see translation).*(trả lời|reply)",
        clean_text,
    ):
        return True

    return False
