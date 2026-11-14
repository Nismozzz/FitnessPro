import { useState } from 'react';
import { request } from './api';
import { weekdays, type Plan } from './trainingTypes';

type Choice = { weekday: number; position: number; exercise_id: string };
type Suggestion = {
  suggestion_id: string; source_plan_id: string; week_start: string;
  adjustments: { weekday: number; position: number; exercise_id: string; name: string; kind: string; reason: string; changes: Record<string, number>; candidates: { exercise_id: string; name: string }[] }[];
  validation: { valid: boolean; errors: { message: string }[]; warnings: { message: string }[] };
  warnings: string[]; notes: string[];
};
const fieldLabels: Record<string, string> = { target_weight_kg: '目标重量（kg）', reps_min: '每组最低次数', reps_max: '每组最高次数' };

export function NextWeekPanel({ planId, disabled, onSaved }: { planId: string; disabled: boolean; onSaved: (plan: Plan) => Promise<void> }) {
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function preview() {
    setBusy(true); setError(''); setSuggestion(null); setChoices({});
    try { setSuggestion(await request<Suggestion>(`/plans/${planId}/suggest-next-week`, { method: 'POST' })); }
    catch (e) { setError(e instanceof Error ? e.message : '建议加载失败'); }
    finally { setBusy(false); }
  }
  async function save() {
    if (!suggestion) return;
    setBusy(true); setError('');
    const replacements: Choice[] = suggestion.adjustments.flatMap(a => {
      const id = choices[`${a.weekday}:${a.position}`];
      return id ? [{ weekday: a.weekday, position: a.position, exercise_id: id }] : [];
    });
    try {
      const plan = await request<Plan>(`/plans/${planId}/next-week`, { method: 'POST', body: JSON.stringify({ suggestion_id: suggestion.suggestion_id, replacements }) });
      await onSaved(plan); setSuggestion(null);
    } catch (e) { setError(e instanceof Error ? e.message : '保存失败'); }
    finally { setBusy(false); }
  }
  return <div className="replacement-panel"><h3>根据记录安排下一周</h3><p className="field-note">查看加重、减重与动作替换建议，确认后保存下周草稿。</p>
    <button className="primary-button" disabled={disabled || busy} onClick={() => void preview()}>{busy ? '处理中…' : '查看下周调整建议'}</button>
    {error && <p className="error" role="alert">{error}</p>}
    {suggestion && <div aria-label="下周调整建议"><h3>{suggestion.week_start} 开始</h3>
      {!suggestion.adjustments.length && <p>现有记录尚未触发调整规则，下周沿用当前安排。</p>}
      {suggestion.adjustments.map(a => <div className="planned-exercise" key={`${a.weekday}:${a.position}`}><h4>{weekdays[a.weekday]} · {a.name}</h4><p>{a.reason}</p>
        {Object.entries(a.changes).map(([field, value]) => <p key={field}>{fieldLabels[field] || field} → {value}</p>)}
        {a.kind === 'replace' && <label className="field-label">选择替换动作<select disabled={busy} aria-label={`${weekdays[a.weekday]}${a.name}替换候选`} value={choices[`${a.weekday}:${a.position}`] || ''} onChange={e => setChoices({ ...choices, [`${a.weekday}:${a.position}`]: e.target.value })}><option value="">保留原动作</option>{a.candidates.map(c => <option key={c.exercise_id} value={c.exercise_id}>{c.name}（需重新确定重量）</option>)}</select></label>}
      </div>)}
      {[...suggestion.warnings, ...suggestion.validation.errors.map(e => e.message), ...suggestion.validation.warnings.map(w => w.message)].map((message, i) => <p className="notice" key={i}>{message}</p>)}
      <details><summary>调整规则与说明</summary>{suggestion.notes.map(note => <p className="field-note" key={note}>{note}</p>)}</details>
      <button className="primary-button" disabled={disabled || busy || !suggestion.validation.valid} onClick={() => void save()}>确认建议，保存下周草稿</button>
      <button className="text-button" disabled={busy} onClick={() => setSuggestion(null)}>取消</button>
    </div>}
  </div>;
}
