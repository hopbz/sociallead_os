from app.browser.extractors import build_dedupe_key, extract_post_id
from app.browser.post_dom_filter import (
    has_comment_url,
    looks_like_comment,
    normalize_facebook_text,
)


def test_normalize_facebook_text_collapses_whitespace() -> None:
    assert normalize_facebook_text('  Hello\n\n Facebook  ') == 'Hello Facebook'


def test_comment_urls_are_detected() -> None:
    assert has_comment_url('https://facebook.com/groups/a/posts/1?comment_id=2')
    assert has_comment_url('https://facebook.com/groups/a/posts/1/comments/2')
    assert not has_comment_url('https://facebook.com/groups/a/posts/1')


def test_comment_action_is_filtered_but_post_action_is_kept() -> None:
    assert looks_like_comment('Kim Love this 10 giờ Thích Trả lời Xem bản dịch')
    assert not looks_like_comment(
        'Nội dung bài viết đủ dài để được giữ lại Thích Bình luận Chia sẻ'
    )


def test_photo_urls_use_parent_post_id_for_dedupe() -> None:
    first_photo = 'https://www.facebook.com/photo/?fbid=111&set=pcb.999'
    second_photo = 'https://www.facebook.com/photo/?fbid=222&set=pcb.999'

    assert extract_post_id(first_photo, 'https://www.facebook.com/groups/example', 'same post') == '999'
    assert extract_post_id(second_photo, 'https://www.facebook.com/groups/example', 'same post') == '999'
    assert build_dedupe_key('https://www.facebook.com/groups/example', '999', 'same post') == 'post:999'
