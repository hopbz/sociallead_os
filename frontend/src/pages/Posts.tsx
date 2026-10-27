import { useEffect, useMemo, useState } from 'react';
import { ExternalLink, Search } from 'lucide-react';

import { api } from '../lib/api';
import type { Post } from '../lib/api';
import { useAsync, formatDate } from '../lib/hooks';
import { Topbar } from '../components/Topbar';
import { Empty, ErrorBox, Loading } from '../components/State';

function looksLikeCommentOnClient(post: Post): boolean {
  const text = post.content || '';
  const url = post.post_url || '';

  const hasCommentUrl =
    /comment_id=|reply_comment_id=|comment_tracking=|\/comments?\//i.test(url);

  const hasCommentAction =
    /(Thích|Like)\s+(Trả lời|Reply)/i.test(text);

  const hasPostAction =
    /(Bình luận|Comment)\s+(Chia sẻ|Share)/i.test(text);

  const shortReplyLikeText =
    text.length < 450 &&
    /(Xem bản dịch|See translation)/i.test(text) &&
    /(Trả lời|Reply)/i.test(text);

  return hasCommentUrl || ((hasCommentAction || shortReplyLikeText) && !hasPostAction);
}

export function Posts() {
  const [query, setQuery] = useState('');
  const [searchQuery, setSearchQuery] = useState('');

  const { data, loading, error, reload } = useAsync(
    () => api.posts(searchQuery),
    [searchQuery],
  );

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSearchQuery(query.trim());
    }, 300);

    return () => window.clearTimeout(timer);
  }, [query]);

  const visiblePosts = useMemo(() => {
    return (data ?? []).filter((post) => !looksLikeCommentOnClient(post));
  }, [data]);

  return (
    <div>
      <Topbar
        title="Bài viết đã quét"
        subtitle="Chỉ hiển thị top-level post, không hiển thị comment/reply."
        onRefresh={reload}
      />

      <div className="mb-5 flex items-center gap-3 rounded-2xl border border-[#e2e5df] bg-white px-4 py-3">
        <Search size={18} className="text-[#7d8784]" />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Tìm theo nội dung, group, tác giả, keyword..."
          className="w-full bg-transparent text-sm outline-none"
        />
      </div>

      {loading ? (
        <Loading />
      ) : error ? (
        <ErrorBox message={error} />
      ) : visiblePosts.length ? (
        <div className="space-y-4">
          {visiblePosts.map((post, index) => (
            <article
              key={post.id}
              className="card-premium interactive-card row-reveal p-5"
              style={{ '--row-index': index } as React.CSSProperties}
            >
              <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-[11px] font-bold uppercase tracking-[0.14em] text-[#e7763d]">
                    Facebook source
                  </p>
                  <h2 className="mt-1 text-lg font-black text-[#102b2a]">
                    {post.group_name || 'Facebook Group'}
                  </h2>
                  <p className="mt-1 text-xs text-[#7d8784]">
                    {formatDate(post.created_at)} · {post.engine} ·{' '}
                    {post.author || 'Không rõ tác giả'}
                  </p>
                </div>

                {post.post_url && (
                  <a
                    href={post.post_url}
                    target="_blank"
                    rel="noreferrer"
                    className="btn-secondary text-xs"
                  >
                    <ExternalLink size={14} />
                    Mở bài viết
                  </a>
                )}
              </div>

              {post.matched_keywords && (
                <p className="mb-3 inline-flex rounded-full bg-[#f7eee8] px-3 py-1 text-[11px] font-bold text-[#c45726]">
                  #{post.matched_keywords}
                </p>
              )}

              <p className="whitespace-pre-wrap text-sm leading-7 text-[#2b3533]">
                {post.content}
              </p>
            </article>
          ))}
        </div>
      ) : (
        <Empty title="Chưa có bài viết" desc="Chưa có bài viết phù hợp hoặc dữ liệu hiện tại chỉ là comment/reply đã bị lọc." />
      )}
    </div>
  );
}