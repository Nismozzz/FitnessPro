import { useEffect, useRef, useState } from 'react';
import { Activity, Check, LoaderCircle, X } from 'lucide-react';
import { WebSearchSources } from './WebSearchSources';
import type { Execution } from './execution';

const statusLabels: Record<string, string> = { running: '执行中', success: '成功', error: '失败', interrupted: '已中断' };
const phaseLabels: Record<string, string> = { waiting: '等待模型响应', reasoning: '模型推理中', tool_arguments: '生成工具调用参数', answering: '生成回答', model_done: '模型响应完成', tool: '执行工具' };
const format = (value: unknown) => JSON.stringify(value ?? null, null, 2);
export function ThinkingBlock({ execution, labels }: { execution?: Execution; labels: Record<string, string> }) {
  const ref = useRef<HTMLDetailsElement>(null);
  const [now, setNow] = useState(Date.now());
  const [showModelProgress, setShowModelProgress] = useState(true);
  const running = execution?.status === 'running';
  useEffect(() => {
    if (!execution) return;
    if (ref.current) ref.current.open = running;
    if (!running) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [running, execution?.startedAt]);
  if (!execution) return null;
  const elapsed = Math.max(0, (execution.endedAt ?? now) - execution.startedAt);
  return <details ref={ref} className="thinking-block execution-panel">
    <summary>{running ? <LoaderCircle size={14} className="execution-spinner"/> : <Activity size={14}/>}
      <span>执行过程</span><small role="status">{running ? '处理中' : execution.status === 'completed' ? '已完成' : execution.status === 'cancelled' ? '已终止' : execution.status === 'stopped' ? '连接已断开' : '未完成'} · {(elapsed / 1000).toFixed(1)} 秒 · {execution.steps.length} 项工具</small>
    </summary>
    <div className="execution-steps">
      <label className="execution-progress-toggle">
        <input type="checkbox" role="switch" checked={showModelProgress} onChange={e => setShowModelProgress(e.target.checked)}/>
        模型阶段与耗时
      </label>
      {showModelProgress && execution.phases?.map((phase, i) => <div className="execution-step-heading" key={i}>
        <strong>{phase.iteration ? `第 ${phase.iteration} 轮 · ` : ''}{phaseLabels[phase.phase] || phase.phase}</strong>
        <small>{(Math.max(0, (phase.endedAt ?? execution.endedAt ?? now) - phase.startedAt) / 1000).toFixed(1)} 秒</small>
      </div>)}
      {showModelProgress && running && execution.lastSignalAt && <p role="status">距最近进度信号 {Math.max(0, (now - execution.lastSignalAt) / 1000).toFixed(1)} 秒</p>}
      {!execution.steps.length && <p>{running ? '正在准备回答…' : '本轮未调用工具'}</p>}
      {execution.steps.map(step => <section className="execution-step" key={step.id}>
        <div className="execution-step-heading">{step.status === 'running' ? <LoaderCircle size={13} className="execution-spinner"/> : step.status === 'success' ? <Check size={13}/> : <X size={13}/>}
          <strong>{labels[step.name] || step.name}</strong><span>{statusLabels[step.status]}</span><small>{((step.ms ?? (execution.endedAt ?? now) - step.startedAt) / 1000).toFixed(1)} 秒</small>
        </div>
        <code>{step.name}</code>
        <details><summary>调用参数</summary><pre>{format(step.arguments)}</pre></details>
        {step.name === 'web_search' && step.status === 'success' && <WebSearchSources result={step.result}/>}
        {step.result !== undefined && <details><summary>执行结果</summary><pre>{format(step.result)}</pre></details>}
        {step.error && <p className="execution-error">{step.error}</p>}
      </section>)}
      {execution.error && <p className="execution-error" role="alert">{execution.error}</p>}
    </div>
  </details>;
}
