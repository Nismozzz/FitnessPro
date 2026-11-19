import { ExternalLink } from 'lucide-react';

export function WebSearchSources({ result }: { result: unknown }) {
  const raw = (result as { untrusted_content?: { sources?: unknown } } | null)?.untrusted_content?.sources;
  if (!Array.isArray(raw)) return null;
  const sources = raw.flatMap(item => {
    if (!item || typeof item !== 'object' || typeof item.url !== 'string') return [];
    try {
      const url = new URL(item.url);
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return [];
      return [{ url: url.href, title: typeof item.title === 'string' ? item.title : url.hostname, snippet: typeof item.snippet === 'string' ? item.snippet : '' }];
    } catch { return []; }
  });
  return <div className="web-search-sources" aria-label="联网搜索来源">
    <p>外部来源 · 摘要仅供参考</p>
    {sources.length ? sources.map((source, i) => <article key={`${source.url}-${i}`}><a href={source.url} target="_blank" rel="noopener noreferrer nofollow"><ExternalLink size={13}/>{source.title}</a><small>{source.url}</small>{source.snippet && <p>{source.snippet}</p>}</article>) : <p>未找到可用来源</p>}
  </div>;
}
