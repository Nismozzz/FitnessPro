import { createContext, useContext, useState, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export type Citation = {
  id: string; exercise_id?: string; source_type?: 'article'; article_id?: string; chunk_id?: string; authors?: string; source_url?: string; review_note?: string; title: string; field: string;
  label: string; text: string; language: string;
};
type MarkdownNode = { type: string; value?: string; url?: string; children?: MarkdownNode[] };

// 只转换正文文本；代码、现有链接和图片中的文字保持原样。
function citationPlugin(citations: Citation[]) {
  const numbers = new Map(citations.map((item, index) => [item.id, index + 1]));
  return () => (tree: MarkdownNode) => {
    function visit(node: MarkdownNode) {
      if (!node.children || ['link', 'image', 'code', 'inlineCode'].includes(node.type)) return;
      node.children = node.children.flatMap(child => {
        if (child.type !== 'text') { visit(child); return [child]; }
        const text = child.value || '';
        const nodes: MarkdownNode[] = [];
        let offset = 0;
        for (const match of text.matchAll(/\[(E[A-Za-z0-9_]+)\]/g)) {
          nodes.push({ type: 'text', value: text.slice(offset, match.index) });
          const number = numbers.get(match[1]);
          if (number) nodes.push({ type: 'link', url: `#evidence-${match[1]}`, children: [{ type: 'text', value: `[${number}]` }] });
          else nodes.push({ type: 'text', value: match[0] });
          offset = match.index! + match[0].length;
        }
        nodes.push({ type: 'text', value: text.slice(offset) });
        return nodes;
      });
    }
    visit(tree);
  };
}

const CitationContext = createContext<{
  citations: Citation[]; selectedId: string | null; select: (id: string | null) => void;
}>({ citations: [], selectedId: null, select() {} });

function SourceLink({ href, children }: { href?: string; children?: ReactNode }) {
  const { citations, selectedId, select } = useContext(CitationContext);
  const evidence = href?.startsWith('#evidence-') ? citations.find(item => href === `#evidence-${item.id}`) : undefined;
  return evidence
    ? <button className="citation-button" title={`查看来源：${evidence.title} · ${evidence.label}`} aria-label={`查看来源：${evidence.title} · ${evidence.label}`} aria-expanded={selectedId === evidence.id} onClick={() => select(selectedId === evidence.id ? null : evidence.id)}>{children}</button>
    : <a href={href}>{children}</a>;
}
const markdownComponents = { a: SourceLink };

export function CitedMarkdown({ content, citations = [] }: { content: string; citations?: Citation[] }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = citations.find(item => item.id === selectedId);
  return <CitationContext.Provider value={{ citations, selectedId, select: setSelectedId }}>
    <Markdown remarkPlugins={[remarkGfm, citationPlugin(citations)]} components={markdownComponents}>{content}</Markdown>
    {selected && <aside className="citation-card" aria-label="知识库来源">
      <div className="citation-heading"><strong>知识库来源</strong><button onClick={() => setSelectedId(null)} aria-label="关闭来源">关闭</button></div>
      <p>{selected.title}</p>
      <dl><dt>位置</dt><dd>{selected.label} <code>{selected.field}</code></dd>
        <dt>{selected.source_type === 'article' ? '文献 ID' : '动作 ID'}</dt><dd>{selected.article_id || selected.exercise_id}</dd>
        {selected.source_type === 'article' && <><dt>作者</dt><dd>{selected.authors}</dd><dt>来源</dt><dd>{selected.source_url && /^https:\/\//.test(selected.source_url) ? <a href={selected.source_url} target="_blank" rel="noopener noreferrer">查看文献</a> : '未提供链接'}</dd></>}</dl>
      <div className="citation-original">{selected.source_type === 'article' ? '文献摘录片段（非全文核验）' : selected.label.startsWith('动作步骤') ? selected.language === 'zh' ? '知识库中文步骤' : '知识库英文原文' : '知识库字段展示值'}</div>
      <blockquote>{selected.text}</blockquote>
      <small>{selected.review_note && <>{selected.review_note}<br/></>}回答时的资料快照</small>
    </aside>}
  </CitationContext.Provider>;
}
