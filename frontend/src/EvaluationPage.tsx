import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { Activity, AlertTriangle, BarChart3, CheckCircle2, ChevronDown, ChevronUp, Clock, ExternalLink, Loader2, RefreshCcw, Target, TrendingUp, XCircle, Zap } from 'lucide-react';
import { Area, AreaChart, CartesianGrid, Legend, RadialBar, RadialBarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { request } from './api';
import type { AlertsResponse, EvaluationAlert, EvaluationHealth, EvaluationStatistics, MetricName, MetricStats, TrendsResponse } from './evaluationTypes';
import { METRIC_COLORS, METRIC_LABELS, percent, trendChartData } from './evaluationUtils';

const PERIODS = { day: [7, 14, 30], hour: [1, 3, 7] } as const;

function StatCard({ title, value, subtitle, icon, tone }: { title: string; value: string | number; subtitle: string; icon: ReactNode; tone: 'orange' | 'blue' | 'green' | 'red' | 'gray' }) {
  return <article className={`eval-stat ${tone}`}><div><p>{title}</p><strong>{value}</strong><small>{subtitle}</small></div><span className="eval-stat-icon">{icon}</span></article>;
}

function MetricGauge({ name, stats, threshold }: { name: MetricName; stats?: MetricStats; threshold: number }) {
  const value = stats?.mean ?? null, color = METRIC_COLORS[name], low = value !== null && value < threshold;
  return <article className="eval-gauge">
    <div className="eval-gauge-head"><span>{METRIC_LABELS[name]}</span>{low && <em>低于阈值</em>}</div>
    <div className="eval-gauge-body">
      <div className="eval-gauge-chart" role="img" aria-label={`${METRIC_LABELS[name]}平均 ${percent(value)}`}>
        <ResponsiveContainer width="100%" height="100%"><RadialBarChart cx="50%" cy="50%" innerRadius="72%" outerRadius="100%" startAngle={90} endAngle={-270} data={[{ value: Math.round((value ?? 0) * 100), fill: color }]}>
          <RadialBar background={{ fill: 'var(--hover)' }} dataKey="value" cornerRadius={10} isAnimationActive={false}/>
        </RadialBarChart></ResponsiveContainer>
        <b style={{ color }}>{percent(value)}</b>
      </div>
      <dl><div><dt>最低</dt><dd>{percent(stats?.min)}</dd></div><div><dt>最高</dt><dd>{percent(stats?.max)}</dd></div><div><dt>告警阈值</dt><dd>{percent(threshold)}</dd></div></dl>
    </div>
  </article>;
}

function TrendChart({ data, loading }: { data: TrendsResponse | null; loading: boolean }) {
  if (loading) return <div className="eval-empty"><Loader2 className="spin" size={24}/></div>;
  if (!data?.trends.length) return <div className="eval-empty"><BarChart3 size={30}/><p>暂无趋势数据</p></div>;
  return <div className="eval-trend-chart"><ResponsiveContainer width="100%" height="100%"><AreaChart data={trendChartData(data)} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
    <defs>{(Object.keys(METRIC_COLORS) as MetricName[]).map(k => <linearGradient key={k} id={`eval-${k}`} x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor={METRIC_COLORS[k]} stopOpacity={0.3}/><stop offset="95%" stopColor={METRIC_COLORS[k]} stopOpacity={0}/></linearGradient>)}</defs>
    <CartesianGrid strokeDasharray="3 3" stroke="var(--border)"/>
    <XAxis dataKey="period" tick={{ fontSize: 11, fill: 'var(--muted)' }} tickLine={false} axisLine={{ stroke: 'var(--border)' }}/>
    <YAxis domain={[0, 100]} tick={{ fontSize: 11, fill: 'var(--muted)' }} tickLine={false} axisLine={false} tickFormatter={v => `${v}%`}/>
    <Tooltip contentStyle={{ background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 8 }} formatter={(v, name) => [`${v}%`, METRIC_LABELS[name as MetricName] ?? name]}/>
    <Legend verticalAlign="bottom" height={32} formatter={v => METRIC_LABELS[v as MetricName] ?? v}/>
    {(Object.keys(METRIC_COLORS) as MetricName[]).map(k => <Area key={k} type="monotone" dataKey={k} name={k} stroke={METRIC_COLORS[k]} strokeWidth={2} fill={`url(#eval-${k})`} connectNulls isAnimationActive={false}/>)}
  </AreaChart></ResponsiveContainer></div>;
}

function AlertItem({ alert, open }: { alert: EvaluationAlert; open: (sessionId: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  return <div className="eval-alert">
    <button aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
      <AlertTriangle size={18} className="eval-alert-icon"/>
      <div><div className="eval-alert-tags">{alert.violated_thresholds.map(t => <span key={t}>{METRIC_LABELS[t]}</span>)}</div>
        <p>{alert.query}</p>
        <small><Target size={12}/>忠实度 {percent(alert.faithfulness)}　<Activity size={12}/>相关性 {percent(alert.answer_relevancy)}　{new Date(alert.created_at + 'Z').toLocaleDateString('zh-CN')}</small></div>
      {expanded ? <ChevronUp size={16}/> : <ChevronDown size={16}/>}
    </button>
    {expanded && <div className="eval-alert-detail">
      <h4>用户问题</h4><p>{alert.query}</p>
      <h4>教练回答</h4><p className="scroll">{alert.response}</p>
      <h4>检索上下文（{alert.contexts.length} 段）</h4><p className="scroll mono">{alert.contexts.join('\n\n')}</p>
      <button className="text-button" onClick={() => open(alert.session_id)}><ExternalLink size={13}/>查看对话</button>
    </div>}
  </div>;
}

export function EvaluationPage({ token, login, openSession }: { token: string | null; login: () => void; openSession: (sessionId: string) => void }) {
  const [stats, setStats] = useState<EvaluationStatistics | null>(null);
  const [trends, setTrends] = useState<TrendsResponse | null>(null);
  const [alerts, setAlerts] = useState<AlertsResponse | null>(null);
  const [health, setHealth] = useState<EvaluationHealth | null>(null);
  const [loading, setLoading] = useState(false);
  const [trendsLoading, setTrendsLoading] = useState(false);
  const [error, setError] = useState('');
  const [days, setDays] = useState(7);
  const [granularity, setGranularity] = useState<'day' | 'hour'>('day');

  const loadAll = useCallback(async () => {
    if (!token) return;
    setLoading(true); setError('');
    try {
      const [s, t, a, h] = await Promise.all([
        request<EvaluationStatistics>('/evaluations/statistics'),
        request<TrendsResponse>(`/evaluations/trends?days=${days}&granularity=${granularity}`),
        request<AlertsResponse>('/evaluations/alerts?limit=20'),
        request<EvaluationHealth>('/evaluations/health'),
      ]);
      setStats(s); setTrends(t); setAlerts(a); setHealth(h);
    } catch (e) { setError(e instanceof Error ? e.message : '加载评估数据失败'); }
    finally { setLoading(false); }
  // 切换周期只刷新趋势，见下方 effect
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);
  useEffect(() => { void loadAll(); }, [loadAll]);
  useEffect(() => {
    if (!token) return;
    let active = true;
    setTrendsLoading(true);
    request<TrendsResponse>(`/evaluations/trends?days=${days}&granularity=${granularity}`)
      .then(t => { if (active) setTrends(t); }).catch(() => undefined)
      .finally(() => { if (active) setTrendsLoading(false); });
    return () => { active = false; };
  }, [token, days, granularity]);

  if (!token) return <section className="eval-page"><div className="eval-empty tall"><BarChart3 size={34}/><p>登录后查看 RAG 评估监控</p><button className="primary-button" onClick={login}>登录</button></div></section>;
  const thresholds = health?.alert_thresholds ?? { faithfulness: 0.3, answer_relevancy: 0.5 };
  const noData = !loading && !error && stats?.total_evaluations === 0;
  return <section className="eval-page"><div className="eval-inner">
    <header className="eval-header">
      <div><span className="section-eyebrow">ANALYTICS</span><h1><BarChart3 size={22}/>RAG 评估监控</h1><p>基于 RAGAS 的检索增强回答质量：忠实度与答案相关性</p></div>
      <div className="eval-header-actions">
        {health && <span className={`eval-health ${health.enabled ? 'on' : 'off'}`}>{health.enabled ? <><CheckCircle2 size={13}/>评估已启用 · 采样 {percent(health.sample_rate)}</> : <><XCircle size={13}/>评估已禁用</>}</span>}
        <button className="icon-button" aria-label="刷新评估数据" disabled={loading} onClick={() => void loadAll()}><RefreshCcw size={16} className={loading ? 'spin' : ''}/></button>
      </div>
    </header>
    {error && <div className="error" role="alert">{error}<button className="text-button" onClick={() => void loadAll()}>重试</button></div>}
    {loading && !stats ? <div className="eval-empty tall"><Loader2 className="spin" size={30}/></div> : <>
      {health && !health.enabled && <p className="notice">评估功能未启用：在 config.yml 中设置 evaluation.enabled=true 并安装 ragas 后，新的检索类回答才会被评估。</p>}
      {noData && <p className="notice">还没有评估记录。向教练提出需要检索动作库的问题（如“推荐几个胸部动作”），回答完成后会在后台生成评分。</p>}
      <div className="eval-stats">
        <StatCard title="总评估数" value={stats?.total_evaluations ?? 0} subtitle={`待处理 ${stats?.pending_count ?? 0}`} icon={<Activity size={20}/>} tone="orange"/>
        <StatCard title="质量警报" value={alerts?.count ?? 0} subtitle="低于阈值的评估" icon={<AlertTriangle size={20}/>} tone={alerts?.count ? 'red' : 'green'}/>
        <StatCard title="平均耗时" value={stats?.avg_evaluation_duration_ms ? `${(stats.avg_evaluation_duration_ms / 1000).toFixed(1)}s` : '-'} subtitle="每次评估" icon={<Clock size={20}/>} tone="blue"/>
        <StatCard title="失败评估" value={stats?.failed_count ?? 0} subtitle="需要关注" icon={<Zap size={20}/>} tone={stats?.failed_count ? 'red' : 'gray'}/>
      </div>
      <div className="eval-gauges">{(Object.keys(METRIC_LABELS) as MetricName[]).map(name => <MetricGauge key={name} name={name} stats={stats?.metrics[name]} threshold={thresholds[name]}/>)}</div>
      <div className="eval-columns">
        <article className="eval-card eval-trends">
          <div className="eval-card-head"><div><span className="eval-card-icon orange"><TrendingUp size={17}/></span><div><h2>指标趋势</h2><small>{trends?.data_points ?? 0} 个数据点</small></div></div>
            <div className="eval-periods">
              <div role="group" aria-label="统计周期">{PERIODS[granularity].map(d => <button key={d} aria-pressed={days === d} className={days === d ? 'active' : ''} onClick={() => setDays(d)}>{granularity === 'hour' ? `${d * 24}小时` : `${d}天`}</button>)}</div>
              <div role="group" aria-label="聚合粒度">{(['day', 'hour'] as const).map(g => <button key={g} aria-pressed={granularity === g} className={granularity === g ? 'active' : ''} onClick={() => { setGranularity(g); if (!PERIODS[g].some(d => d === days)) setDays(PERIODS[g][0]); }}>{g === 'day' ? '按天' : '按小时'}</button>)}</div>
            </div></div>
          <TrendChart data={trends} loading={trendsLoading}/>
        </article>
        <article className="eval-card">
          <div className="eval-card-head"><div><span className="eval-card-icon red"><AlertTriangle size={17}/></span><div><h2>质量警报</h2><small>阈值：忠实度 {percent(thresholds.faithfulness)}，相关性 {percent(thresholds.answer_relevancy)}</small></div></div></div>
          {alerts?.alerts.length ? <div className="eval-alert-list">{alerts.alerts.map(a => <AlertItem key={a.id} alert={a} open={openSession}/>)}</div> : <div className="eval-empty"><CheckCircle2 size={30} className="ok"/><p>暂无质量警报</p><small>所有评估指标均在阈值之上</small></div>}
        </article>
      </div>
    </>}
  </div></section>;
}
