export type ToolStep = {
  id: string; name: string; arguments: unknown; startedAt: number;
  status: 'running' | 'success' | 'error' | 'interrupted';
  result?: unknown; error?: string; ms?: number;
};
export type Execution = {
  startedAt: number; endedAt?: number; status: string; steps: ToolStep[]; error?: string;
};
export function updateExecution(execution: Execution, event: string, data: Record<string, unknown>, now = Date.now()): Execution {
  if (event === 'tool_call') return { ...execution, steps: [...execution.steps, {
    id: String(data.id), name: String(data.name), arguments: data.arguments, startedAt: now, status: 'running',
  }] };
  if (event === 'tool_result') return { ...execution, steps: execution.steps.map(step => step.id === data.id ? {
    ...step, status: data.success ? 'success' : 'error', result: data.data,
    error: data.error ? String(data.error) : undefined, ms: Number(data.ms),
  } : step) };
  if (event === 'error') return { ...execution, error: String(data.message || '执行失败') };
  if (event === 'done') return { ...execution, endedAt: now, status: String(data.status), steps: execution.steps.map(step => step.status === 'running' ? {
    ...step, status: 'interrupted', ms: now - step.startedAt,
  } : step) };
  return execution;
}
