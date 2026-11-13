import { useEffect, useRef } from 'react';
import { Brain } from 'lucide-react';

/** Ephemeral stream text: never persisted or sent back by the browser. */
export function ThinkingBlock({ text, answering, streaming }: { text: string; answering: boolean; streaming: boolean }) {
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (answering && ref.current) ref.current.open = false;
  }, [answering]);
  if (!text) return null;
  return <details ref={ref} className="thinking-block">
    <summary><Brain size={14}/><span>思考过程</span><small>{streaming && !answering ? '思考中…' : '可展开查看'}</small></summary>
    <div className="thinking-text">{text}</div>
  </details>;
}
