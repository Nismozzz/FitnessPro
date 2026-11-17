import { useEffect, useState } from 'react';
import { request } from './api';

type Tool = { name: string; description: string; side_effect: boolean; available: boolean; unavailable_reason: string | null };
export type ToolSelection = string[] | null;
const STORAGE = 'fitnesspro.session-tools';
function loadSelections(): Record<string, ToolSelection> {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE) || '{}');
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([, v]) => v === null || (Array.isArray(v) && v.every(n => typeof n === 'string')))) as Record<string, ToolSelection>;
  } catch { return {}; }
}
export function useSessionTools(user: string, session: string | null) {
  const [selections, setSelections] = useState(loadSelections);
  const key = `${user}:${session || 'draft'}`;
  const selected = selections[key] ?? null;
  function save(target: string, value: ToolSelection) {
    setSelections(old => { const next = { ...old, [target]: value }; try { localStorage.setItem(STORAGE, JSON.stringify(next)); } catch { /* 本次会话仍可使用选择。 */ } return next; });
  }
  return {
    selectedTools: selected,
    setSelectedTools: (value: ToolSelection) => save(key, value),
    bindSession: (id: string) => { if (!session) save(`${user}:${id}`, selected); },
    resetDraft: () => save(`${user}:draft`, null),
  };
}
const groups = [
  { title: '动作知识库', color: 'knowledge', names: ['search_exercises', 'get_exercise_detail'] },
  { title: '训练计划与记录', color: 'training', names: ['draft_weekly_plan', 'save_plan', 'get_active_plan', 'update_plan_exercise', 'log_workout', 'get_training_stats'] },
  { title: '档案与记忆', color: 'memory', names: ['get_user_profile', 'update_user_profile', 'save_memory', 'confirm_memory', 'forget_memory'] },
  { title: '实时信息', color: 'web', names: ['get_current_time', 'web_search'] },
];
export function ToolSelector({ token, value, onChange, disabled, labels }: { token: string | null; value: ToolSelection; onChange: (value: ToolSelection) => void; disabled: boolean; labels: Record<string, string> }) {
  const [tools, setTools] = useState<Tool[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    setTools([]); setError('');
    if (!token) return;
    let active = true; setLoading(true);
    request<Tool[]>('/agent/tools').then(data => { if (active) setTools(data); }).catch(e => { if (active) setError(e instanceof Error ? e.message : '工具加载失败'); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [token, retry]);
  const selected = value === null ? tools.map(t => t.name) : value;
  const locked = disabled || !token || loading || !!error;
  return <details className="tool-selector"><summary>工具 · {value === null ? '默认' : `${value.length} 项`}</summary><div className="tool-selector-panel">
    <p>当前会话 · {value === null ? '使用默认工具' : value.length ? '自定义工具' : '已禁用全部工具'}</p>
    {!token ? <p>登录后可选择工具</p> : loading ? <p role="status">正在加载工具…</p> : error ? <p role="alert">{error} <button type="button" disabled={disabled} onClick={() => setRetry(n => n + 1)}>重试</button></p> : null}
    <div className="tool-selector-actions"><button type="button" disabled={locked} onClick={() => onChange(null)}>恢复默认</button><button type="button" disabled={locked} onClick={() => onChange(tools.filter(t => t.available).map(t => t.name))}>全选可用</button><button type="button" disabled={locked} onClick={() => onChange([])}>全部取消</button></div>
    <details open><summary>Tools · {tools.filter(t => selected.includes(t.name)).length}/{tools.length}</summary>{groups.map(group => {
      const members = tools.filter(t => group.names.includes(t.name));
      if (!members.length) return null;
      return <section className={`tool-service ${group.color}`} key={group.title}><h4>{group.title} <small>{members.filter(t => selected.includes(t.name)).length}/{members.length}</small></h4>{members.map(tool => <label key={tool.name} title={tool.description}><input type="checkbox" checked={selected.includes(tool.name)} disabled={locked || (!tool.available && !selected.includes(tool.name))} onChange={e => onChange(e.target.checked ? [...selected, tool.name] : selected.filter(n => n !== tool.name))}/><span>{labels[tool.name] || tool.name}{tool.side_effect && <small> · 可写入</small>}{!tool.available && <small> · {tool.unavailable_reason}</small>}</span></label>)}</section>;
    })}</details>
    <details><summary>MCP · 0</summary><p>项目尚未接入 MCP 服务</p></details><details><summary>Agents · 0</summary><p>项目尚未接入子 Agent</p></details>
    <p className="tool-selection-note">仅限制本轮模型调用的工具；已有上下文和后台记忆提取保持原有设置。切换选择从下一条消息生效。</p>
  </div></details>;
}
