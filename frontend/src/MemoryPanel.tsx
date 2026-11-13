import { useState, type FormEvent } from 'react';
import { Brain, Check, Pencil, Plus, RefreshCw, Trash2, X } from 'lucide-react';
export type Memory = { id: string; content: string; status: string; kind: string };
type Props = {
  memories: Memory[]; tab: 'active' | 'candidate'; setTab: (tab: 'active' | 'candidate') => void;
  working: boolean; error: string; refreshing: boolean;
  refresh: () => void;
  add: (content: string) => Promise<boolean>;
  action: (id: string, action: string, content?: string) => Promise<boolean>;
};
export function MemoryPanel({ memories, tab, setTab, working, error, refreshing, refresh, add, action }: Props) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [content, setContent] = useState('');
  const visible = memories.filter(m => m.status === tab);
  async function submit(e: FormEvent) { e.preventDefault(); if (await add(content.trim())) setContent(''); }
  return <>
    <p className="modal-description">自动提取的记忆需要你确认。对话结束后会延迟刷新，也可以手动查看最新结果。</p>
    <form className="memory-add" onSubmit={submit}>
      <input aria-label="添加训练偏好" minLength={2} maxLength={200} required value={content} onChange={e => setContent(e.target.value)} placeholder="例如：我更喜欢在早晨训练"/>
      <button className="primary-button" disabled={working || content.trim().length < 2}><Plus size={17}/>记住</button>
    </form>
    <div className="memory-toolbar"><div className="memory-tabs" role="tablist" aria-label="记忆状态">
      {(['active', 'candidate'] as const).map(status => <button key={status} role="tab" id={`memory-tab-${status}`} aria-selected={tab === status} aria-controls="memory-results" disabled={working} onClick={() => { setTab(status); setEditing(null); }}>{status === 'active' ? '已确认' : '待确认'} <span>{memories.filter(m => m.status === status).length}</span></button>)}
    </div><button className="icon-button" aria-label="刷新记忆" disabled={working || refreshing} onClick={refresh}><RefreshCw size={16}/></button></div>
    {error && <div className="error" role="alert">{error}</div>}
    <div className="memory-list" id="memory-results" role="tabpanel" aria-labelledby={`memory-tab-${tab}`} aria-busy={refreshing}>
      {visible.map(m => <div className="memory-item" key={m.id}><Brain size={18}/><div>
        <span className={`memory-status ${m.status}`}>{m.status === 'candidate' ? '等待你确认' : '已确认'}</span>
        {editing === m.id ? <form className="memory-edit" onSubmit={async e => { e.preventDefault(); if (await action(m.id, 'edit', draft.trim())) setEditing(null); }}>
          <textarea aria-label="编辑记忆内容" required minLength={2} maxLength={200} value={draft} onChange={e => setDraft(e.target.value)}/>
          <div><button className="primary-button" disabled={working || draft.trim().length < 2}>保存</button><button type="button" className="text-button" disabled={working} onClick={() => setEditing(null)}>取消</button></div>
          {m.status === 'candidate' && <small>保存后仍为待确认，不会自动生效。</small>}
        </form> : <><p>{m.content}</p><div className="memory-actions">
          {m.status === 'candidate' && <><button disabled={working} onClick={() => action(m.id, 'confirm')}><Check size={14}/>确认</button><button disabled={working} onClick={() => action(m.id, 'reject')}><X size={14}/>忽略</button></>}
          <button disabled={working} onClick={() => { setEditing(m.id); setDraft(m.content); }}><Pencil size={14}/>编辑</button>
          <button disabled={working} onClick={() => action(m.id, 'delete')}><Trash2 size={14}/>删除</button>
        </div></>}
      </div></div>)}
      {!visible.length && <div className="knowledge-empty"><Brain size={32}/><h3>{tab === 'candidate' ? '没有待确认的记忆' : '还没有已确认的记忆'}</h3><p>{tab === 'candidate' ? '和教练聊聊你的情况，提取完成后会出现在这里。' : '确认候选记忆，或在上方主动添加训练偏好。'}</p></div>}
    </div>
  </>;
}
