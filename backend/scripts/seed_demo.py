from datetime import datetime, timezone

from sqlalchemy import select

from app.browser.extractors import build_dedupe_key
from app.core.hash_utils import content_hash
from app.db.models import GroupSource, Keyword, LeadCandidate, ScrapedPost
from app.db.session import SessionLocal, init_db

EXAMPLE_GROUPS = [
    ("Example Facebook Group", "https://www.facebook.com/groups/your_group_slug"),
]
EXAMPLE_KEYWORDS = ["java", "spring boot", "thực tập", "tuyển dụng"]

DEMO_POSTS = [
    {
        'post_id': 'demo-salon-001',
        'group_name': 'Cộng đồng chủ salon Demo',
        'group_url': 'https://www.facebook.com/groups/demo-salon-community',
        'post_url': 'https://www.facebook.com/groups/demo-salon-community/posts/demo-salon-001',
        'author': 'Khách hàng Demo A',
        'content': 'Mình đang tìm đơn vị làm landing page và chạy quảng cáo cho salon mới mở, cần tư vấn chi phí trong tháng này.',
        'matched_keywords': 'cần tìm, tư vấn, chi phí',
    },
    {
        'post_id': 'demo-real-estate-002',
        'group_name': 'Đầu tư bất động sản Demo',
        'group_url': 'https://www.facebook.com/groups/demo-real-estate',
        'post_url': 'https://www.facebook.com/groups/demo-real-estate/posts/demo-real-estate-002',
        'author': 'Khách hàng Demo B',
        'content': 'Có anh chị nào giới thiệu giúp môi giới khu vực Thủ Đức làm việc rõ ràng không? Mình cần xem nhà cuối tuần.',
        'matched_keywords': 'giới thiệu, cần xem nhà',
    },
    {
        'post_id': 'demo-study-abroad-003',
        'group_name': 'Du học cùng nhau Demo',
        'group_url': 'https://www.facebook.com/groups/demo-study-abroad',
        'post_url': 'https://www.facebook.com/groups/demo-study-abroad/posts/demo-study-abroad-003',
        'author': 'Khách hàng Demo C',
        'content': 'Gia đình đang tìm hiểu hồ sơ du học Úc cho kỳ tháng 2, muốn biết lộ trình và các khoản phí dự kiến.',
        'matched_keywords': 'tìm hiểu, lộ trình, chi phí',
    },
    {
        'post_id': 'demo-agency-004',
        'group_name': 'Marketing Agency Demo',
        'group_url': 'https://www.facebook.com/groups/demo-agency',
        'post_url': 'https://www.facebook.com/groups/demo-agency/posts/demo-agency-004',
        'author': 'Khách hàng Demo D',
        'content': 'Mình đang tham khảo các cách tăng nhận diện thương hiệu, chưa có kế hoạch triển khai cụ thể.',
        'matched_keywords': 'tham khảo, thương hiệu',
    },
]

DEMO_LEADS = {
    'demo-salon-001': {
        'score': 9,
        'need_stage': 'hot',
        'persona': 'Chủ salon mới khai trương',
        'pain_points': 'Cần website, quảng cáo và dự toán chi phí sớm',
        'reason': 'Nhu cầu dịch vụ rõ ràng, có thời hạn triển khai trong tháng.',
        'suggested_comment': 'Chúc mừng salon mới của bạn. Bạn đã có mức ngân sách và khu vực khách hàng mục tiêu chưa? Hai thông tin đó sẽ giúp chọn hướng landing page và quảng cáo phù hợp hơn.',
    },
    'demo-real-estate-002': {
        'score': 8,
        'need_stage': 'hot',
        'persona': 'Người mua nhà đang tìm môi giới',
        'pain_points': 'Cần người uy tín và muốn xem nhà sớm',
        'reason': 'Có nhu cầu cụ thể về khu vực và thời gian xem nhà.',
        'suggested_comment': 'Bạn đang ưu tiên loại hình nhà và khoảng ngân sách nào ở Thủ Đức? Nêu thêm hai tiêu chí này sẽ giúp mọi người giới thiệu môi giới sát nhu cầu hơn.',
    },
    'demo-study-abroad-003': {
        'score': 8,
        'need_stage': 'warm',
        'persona': 'Phụ huynh tìm hiểu du học Úc',
        'pain_points': 'Chưa rõ lộ trình hồ sơ và tổng chi phí',
        'reason': 'Đã có kỳ nhập học mục tiêu và đang chủ động tìm tư vấn.',
        'suggested_comment': 'Gia đình đã xác định bậc học và ngành dự kiến chưa? Hai thông tin đó ảnh hưởng khá nhiều đến timeline hồ sơ và phần dự toán chi phí.',
    },
}

if __name__ == "__main__":
    init_db()
    with SessionLocal() as db:
        for name, url in EXAMPLE_GROUPS:
            exists = db.query(GroupSource).filter(GroupSource.url == url).first()
            if not exists:
                db.add(GroupSource(name=name, url=url, is_active=True))
        for kw in EXAMPLE_KEYWORDS:
            exists = db.query(Keyword).filter(Keyword.keyword == kw).first()
            if not exists:
                db.add(Keyword(keyword=kw, is_active=True))

        for item in DEMO_POSTS:
            post = db.execute(
                select(ScrapedPost).where(ScrapedPost.post_id == item['post_id'])
            ).scalar_one_or_none()
            if not post:
                post = ScrapedPost(
                    **item,
                    content_hash=content_hash(item['group_url'], item['content']),
                    dedupe_key=build_dedupe_key(item['group_url'], item['post_id'], item['content']),
                    engine='demo',
                    scraped_at=datetime.now(timezone.utc),
                )
                db.add(post)
                db.flush()

            lead_data = DEMO_LEADS.get(item['post_id'])
            if lead_data:
                post.processed_at = datetime.now(timezone.utc)
                lead = db.execute(
                    select(LeadCandidate).where(LeadCandidate.post_id == post.id)
                ).scalar_one_or_none()
                if not lead:
                    db.add(LeadCandidate(
                        post_id=post.id,
                        group_name=post.group_name,
                        group_url=post.group_url,
                        post_url=post.post_url,
                        author=post.author,
                        content=post.content,
                        status='new',
                        **lead_data,
                    ))
        db.commit()
    print("Demo data ready: fake groups, posts, and lead candidates were added.")
