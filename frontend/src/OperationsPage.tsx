import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, ExternalLink, Loader2, RefreshCcw, Search, X } from 'lucide-react';
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { request } from './api';
import type { LLMStats, RequestRow, RequestTrace, RetrievalStats } from './dashboardTypes';

const MODULES: Record<string, string> = { agent: 'Agent 对话', compress: '摘要压缩', memory: '记忆提取', translate: '知识库翻译', evaluation: 'RAGAS 评估' };
const number = (v: number | null | undefined) => v == null ? '—' : v.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
const ms = (v: number | null | undefined) => v == null ? '—' : `${number(v)} ms`;
const percent = (v: number | null | undefined) => v == null ? '—' : `${(v * 100).toFixed(1)}%`;
const date = (v: string) => new Date(v.endsWith('Z') || /[+-]\d\d:\d\d$/.test(v) ? v : v + 'Z').toLocaleString('zh-CN');
const tooltip = { background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 8 };
function Stat({ title, value, note }: { title: string; value: string; note: string }) {
  return <article className="eval-stat"><div><p>{title}</p><strong>{value}</strong><small>{note}</small></div></article>;
}
function TraceDetail({ trace }: { trace: RequestTrace }) {
  const rows = [
    ...trace.llm_calls.map(r => ({ ...r, kind: 'LLM', title: `${MODULES[r.module ?? ''] ?? r.module} · ${r.model}` })),
    ...trace.tool_calls.map(r => ({ ...r, kind: '工具', title: r.tool_name })),
    ...trace.retrieval_logs.map(r => ({ ...r, kind: '检索', title: r.query || '仅条件筛选' })),
  ].sort((a, b) => a.created_at.localeCompare(b.created_at));
  return <div className="trace-timeline">{rows.map(r => <article key={r.id} className="trace-event">
    <div><span className="tiny-tag">{r.kind}</span><b>{r.title}</b><span className={r.status === 'error' || r.status === 'cancelled' || r.success === false ? 'trace-failed' : 'trace-ok'}>{r.status ?? (r.success ? '成功' : '失败')}</span></div>
    <small>{date(r.created_at)} · 耗时 {ms(r.latency_ms)}{r.kind === 'LLM' && ` · 首 token ${ms(r.first_token_ms)} · 输入 ${number(r.prompt_tokens)} / 输出 ${number(r.completion_tokens)} token`}</small>
    {r.kind === '检索' && <><p>{r.mode} · 候选 {r.candidates} · {r.degraded ? '已降级' : '未降级'}</p><p className="mono">命中：{r.hit_ids?.join(', ') || '无'}</p><details><summary>筛选条件</summary><pre>{JSON.stringify(r.filters, null, 2)}</pre></details></>}
    {r.error && <p className="trace-failed">{r.error}</p>}
  </article>)}</div>;
}

export function OperationsPage({ token, login, openSession }: { token: string | null; login: () => void; openSession: (id: string) => void }) {
  const [range, setRange] = useState('7d');
  const [scope, setScope] = useState('self');
  const [stats, setStats] = useState<LLMStats | null>(null);
  const [retrieval, setRetrieval] = useState<RetrievalStats | null>(null);
  const [requests, setRequests] = useState<RequestRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [requestId, setRequestId] = useState('');
  const [trace, setTrace] = useState<RequestTrace | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceError, setTraceError] = useState('');
  const generation = useRef(0), traceGeneration = useRef(0);
  const load = useCallback(async () => {
    const version = ++generation.current;
    if (!token) return;
    setLoading(true); setError('');
    try {
      const params = `?range=${range}&scope=${scope}`;
      const [s, r, q] = await Promise.all([
        request<LLMStats>(`/stats/llm${params}`), request<RetrievalStats>(`/stats/retrieval${params}`), request<RequestRow[]>(`/stats/requests${params}`),
      ]);
      if (generation.current === version) { setStats(s); setRetrieval(r); setRequests(q); }
    } catch (e) { if (generation.current === version) setError(e instanceof Error ? e.message : '加载监控数据失败'); }
    finally { if (generation.current === version) setLoading(false); }
  }, [token, range, scope]);
  useEffect(() => { setStats(null); setRetrieval(null); setRequests([]); setTrace(null); setTraceError(''); setRequestId(''); ++traceGeneration.current; setTraceLoading(false); void load(); return () => { ++generation.current; ++traceGeneration.current; }; }, [load]);
  async function inspect(id: string) {
    if (!id.trim()) return;
    const version = ++traceGeneration.current;
    setTraceLoading(true); setTraceError(''); setTrace(null); setRequestId(id.trim());
    try {
      const result = await request<RequestTrace>(`/stats/requests/${encodeURIComponent(id.trim())}?scope=${scope}`);
      if (version === traceGeneration.current) setTrace(result);
    } catch (e) { if (version === traceGeneration.current) setTraceError(e instanceof Error ? e.message : '读取请求失败'); }
    finally { if (version === traceGeneration.current) setTraceLoading(false); }
  }
  if (!token) return <section className="eval-page"><div className="eval-empty tall"><Activity size={34}/><p>登录后查看运行监控</p><button className="primary-button" onClick={login}>登录</button></div></section>;
  const s = stats?.summary;
  return <section className="eval-page"><div className="eval-inner">
    <header className="eval-header"><div><span className="section-eyebrow">OBSERVABILITY</span><h1><Activity size={22}/>运行监控</h1><p>查看模型用量、调用性能与检索状态，按请求追踪完整调用链。</p></div>
      <div className="eval-header-actions"><select aria-label="监控时间范围" value={range} onChange={e => setRange(e.target.value)}>{['1d', '7d', '30d', '90d'].map(v => <option key={v} value={v}>最近 {v.slice(0, -1)} 天</option>)}</select>
      {stats?.is_admin && <select aria-label="数据范围" value={scope} onChange={e => setScope(e.target.value)}><option value="self">我的数据</option><option value="all">全局数据</option></select>}
      <button className="icon-button" aria-label="刷新运行数据" disabled={loading} onClick={() => void load()}><RefreshCcw size={16} className={loading ? 'spin' : ''}/></button></div>
    </header>
    <p className="dashboard-scope">{scope === 'all' ? '管理员 · 全局数据' : '仅展示当前账号的调用数据'} · 趋势按 UTC 日聚合 · 费用为配置单价估算</p>
    {error && <div className="error" role="alert">{error}<button className="text-button" onClick={() => void load()}>重试</button></div>}
    {loading && !stats ? <div className="eval-empty tall"><Loader2 className="spin"/>正在加载运行数据</div> : <>
      {s?.calls === 0 && <p className="notice">暂无 LLM 调用记录。完成一次新对话后刷新；历史对话不会补录，后台写入可能稍有延迟。</p>}
      <div className="eval-stats">
        <Stat title="LLM 调用" value={number(s?.calls)} note={`失败 / 中断 ${number(s?.failures)} 次`}/>
        <Stat title="Token 总量" value={s ? number(s.prompt_tokens + s.completion_tokens) : '—'} note={`输入 ${number(s?.prompt_tokens)} · 输出 ${number(s?.completion_tokens)}`}/>
        <Stat title="估算费用" value={s?.estimated_cost == null ? '未定价' : `${s.estimated_cost.toFixed(6)} ${stats?.currency}`} note={`未定价 ${number(s?.unpriced_calls)} 次 · 缺用量 ${number(s?.missing_usage_calls)} 次`}/>
        <Stat title="工具失败率" value={percent(stats?.tools.failure_rate)} note={`${number(stats?.tools.failures)} / ${number(stats?.tools.calls)} 次调用`}/>
      </div>
      <div className="eval-stats">
        <Stat title="LLM 延迟 P50 / P95" value={`${number(s?.latency_ms.p50)} / ${number(s?.latency_ms.p95)}`} note="单位 ms · 单次模型调用总耗时"/>
        <Stat title="首 token P50 / P95" value={`${number(s?.first_token_ms.p50)} / ${number(s?.first_token_ms.p95)}`} note="单位 ms · 含思考与工具参数首片段"/>
        <Stat title="检索降级" value={number(retrieval?.degraded_count)} note={`共 ${number(retrieval?.calls)} 次检索 · 失败 ${number(retrieval?.failures)}`}/>
        <Stat title="检索平均延迟" value={ms(retrieval?.latency_ms.mean)} note={`P95 ${ms(retrieval?.latency_ms.p95)}`}/>
      </div>
      <div className="eval-columns">
        <article className="eval-card"><div className="eval-card-head"><h2>Token 用量趋势</h2></div><div className="dashboard-chart"><ResponsiveContainer width="100%" height="100%"><AreaChart data={stats?.daily ?? []} margin={{ right: 15, left: 5 }}><CartesianGrid strokeDasharray="3 3" stroke="var(--border)"/><XAxis dataKey="date" tick={{ fontSize: 10 }}/><YAxis width={55} tick={{ fontSize: 10 }}/><Tooltip contentStyle={tooltip}/><Legend/><Area type="monotone" stackId="tokens" dataKey="prompt_tokens" name="输入 token" stroke="#e98542" fill="#e98542" fillOpacity={.2} isAnimationActive={false}/><Area type="monotone" stackId="tokens" dataKey="completion_tokens" name="输出 token" stroke="#558bd7" fill="#558bd7" fillOpacity={.2} isAnimationActive={false}/></AreaChart></ResponsiveContainer></div></article>
        <article className="eval-card"><div className="eval-card-head"><h2>模块调用占比</h2></div>{stats?.by_module.length ? <div className="dashboard-chart"><ResponsiveContainer width="100%" height="100%"><BarChart data={stats.by_module.map(m => ({ ...m, label: MODULES[m.name] ?? m.name }))} layout="vertical"><XAxis type="number" allowDecimals={false}/><YAxis type="category" dataKey="label" width={90} tick={{ fontSize: 11 }}/><Tooltip contentStyle={tooltip}/><Bar dataKey="calls" name="调用次数" fill="#e98542" radius={[0, 4, 4, 0]} isAnimationActive={false}/></BarChart></ResponsiveContainer></div> : <div className="eval-empty">暂无模块数据</div>}
        {stats?.by_module.map(m => <div className="dashboard-distribution" key={m.name}><span>{MODULES[m.name] ?? m.name}</span><b>{percent(m.calls / (s?.calls || 1))}</b></div>)}</article>
      </div>
      <div className="eval-columns dashboard-section">
        <article className="eval-card"><div className="eval-card-head"><h2>模型用量</h2></div><div className="dashboard-table-wrap"><table className="dashboard-table"><thead><tr><th>模型</th><th>调用</th><th>输入 / 输出 token</th><th>P95</th><th>估算费用</th></tr></thead><tbody>{stats?.by_model.map(m => <tr key={m.name}><td>{m.name}</td><td>{m.calls}</td><td>{number(m.prompt_tokens)} / {number(m.completion_tokens)}</td><td>{ms(m.latency_ms.p95)}</td><td>{m.estimated_cost == null ? '未定价' : `${m.estimated_cost.toFixed(6)} ${stats.currency}`}</td></tr>)}</tbody></table></div></article>
        <article className="eval-card"><div className="eval-card-head"><h2>检索模式分布</h2></div>{retrieval?.modes.length ? retrieval.modes.map(m => <div className="dashboard-distribution" key={m.name}><span className="mono">{m.name}</span><b>{m.count} 次</b></div>) : <div className="eval-empty">暂无检索记录</div>}<p className="dashboard-help">降级统计包含混合检索缺失召回通道、向量服务不可用或重排失败。</p></article>
      </div>
      <article className="eval-card dashboard-section"><div className="eval-card-head"><div><h2>请求追踪</h2><small>点击最近请求，或输入 request_id 查询调用链</small></div></div>
        <form className="trace-search" onSubmit={e => { e.preventDefault(); void inspect(requestId); }}><input aria-label="request_id" placeholder="输入 request_id" value={requestId} onChange={e => setRequestId(e.target.value)}/><button className="primary-button" disabled={traceLoading || !requestId.trim()}><Search size={14}/>查询</button></form>
        <div className="dashboard-table-wrap"><table className="dashboard-table"><thead><tr><th>请求 / 时间</th><th>LLM</th><th>工具</th><th>检索</th><th>异常</th><th>会话</th></tr></thead><tbody>{requests.map(r => <tr key={r.request_id}><td><button className="text-button mono" onClick={() => void inspect(r.request_id)}>{r.request_id}</button><small className="trace-date">{date(r.created_at)}</small></td><td>{r.llm_calls}</td><td>{r.tool_calls}</td><td>{r.retrieval_logs}</td><td className={r.failures ? 'trace-failed' : ''}>{r.failures}</td><td>{r.session_id ? <button className="text-button" onClick={() => openSession(r.session_id!)}><ExternalLink size={14}/>查看</button> : '—'}</td></tr>)}</tbody></table>{!requests.length && <div className="eval-empty">当前范围暂无请求记录</div>}</div>
        {traceLoading && <p className="dashboard-help"><Loader2 className="spin" size={16}/>正在读取调用链</p>}
        {traceError && <p className="error" role="alert">{traceError}</p>}
        {trace && <div className="trace-detail"><div className="eval-card-head"><h3 className="mono">{trace.request_id}</h3><button className="icon-button" aria-label="关闭请求详情" onClick={() => setTrace(null)}><X size={16}/></button></div><TraceDetail trace={trace}/></div>}
      </article>
      <article className="eval-card dashboard-section"><div className="eval-card-head"><div><h2>离线检索评测</h2><small>阶段 3 评测报告 · 不受当前账号和时间范围筛选</small></div></div>
        {retrieval?.offline.status === 'available' ? <><div className="dashboard-table-wrap"><table className="dashboard-table"><thead><tr><th>配置</th><th>Recall@{retrieval.offline.k ?? 5}</th><th>MRR@{retrieval.offline.k ?? 5}</th><th>nDCG@{retrieval.offline.k ?? 5}</th><th>评分覆盖</th><th>P95</th></tr></thead><tbody>{Object.entries(retrieval.offline.summary).map(([mode, values]) => <tr key={mode}><td>{mode}</td>{['recall', 'mrr', 'ndcg'].map(metric => `${metric}@${retrieval.offline.k ?? 5}`).map(k => <td key={k}>{percent(typeof values[k] === 'number' ? values[k] : null)}</td>)}<td>{String(values.scored_cases ?? '—')} / {String(values.cases ?? '—')}</td><td>{ms(typeof values.latency_ms_p95 === 'number' ? values.latency_ms_p95 : null)}</td></tr>)}</tbody></table></div><p className="dashboard-help">{retrieval.offline.created_at && `报告生成于 ${date(retrieval.offline.created_at)}。`}指标按报告原始标注计算；正式结论需使用已审核的相关性标注。</p></> : <p className="dashboard-help">{retrieval?.offline.status === 'invalid' ? '评测报告无法读取，请重新生成。' : '尚无离线评测报告。运行阶段 3 检索评测后将在此展示对比结果。'}</p>}
      </article>
    </>}
  </div></section>;
}
