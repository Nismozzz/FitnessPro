export type ToolStep = {
  id: string; name: string; arguments: unknown; startedAt: number;
  status: 'running' | 'success' | 'error' | 'interrupted';
  result?: unknown; error?: string; ms?: number;
};
export type Phase = { phase: string; iteration?: number; startedAt: number; endedAt?: number };
export type Execution = {
  startedAt: number; endedAt?: number; status: string; steps: ToolStep[]; error?: string; phases?: Phase[]; lastSignalAt?: number;
};
export function updateExecution(execution: Execution, event: string, data: Record<string, unknown>, now = Date.now()): Execution {
  if (event === 'progress' || event === 'tool_call') {
    const phase = event === 'tool_call' ? 'tool' : String(data.phase);
    const phases = [...(execution.phases ?? [])];
    const previous = phases.at(-1);
    if (!previous || previous.phase !== phase || (event === 'progress' && previous.iteration !== data.iteration)) {
      if (previous) phases[phases.length - 1] = { ...previous, endedAt: now };
      phases.push({ phase, iteration: event === 'progress' ? Number(data.iteration) : undefined, startedAt: now });
    }
    execution = { ...execution, phases, lastSignalAt: now };
    if (event === 'progress') return execution;
  }
  if (event === 'tool_result') execution = { ...execution, lastSignalAt: now };
  if (event === 'tool_call') return { ...execution, steps: [...execution.steps, {
    id: String(data.id), name: String(data.name), arguments: data.arguments, startedAt: now, status: 'running',
  }] };
  if (event === 'tool_result') return { ...execution, steps: execution.steps.map(step => step.id === data.id ? {
    ...step, status: data.success ? 'success' : 'error', result: data.data,
    error: data.error ? String(data.error) : undefined, ms: Number(data.ms),
  } : step) };
  if (event === 'error') return { ...execution, error: String(data.message || '执行失败') };
  if (event === 'done') return { ...execution, endedAt: now, status: String(data.status), phases: execution.phases?.map(p => p.endedAt ? p : { ...p, endedAt: now }), steps: execution.steps.map(step => step.status === 'running' ? {
    ...step, status: 'interrupted', ms: now - step.startedAt,
  } : step) };
  return execution;
}
