export const TOKEN_KEY = 'fitnesspro.token';
export class ApiError extends Error { constructor(message: string, public status: number) { super(message); } }
export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem(TOKEN_KEY);
  const response = await fetch(`/api/v1${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...options.headers } });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const details = data.validation?.errors?.map((item: { message: string }) => item.message).join('；');
    const message = typeof data.detail === 'string' ? data.detail : response.status === 422 ? '请检查填写内容是否符合要求' : '请求失败，请确认后端服务已启动';
    throw new ApiError(details ? `${message}：${details}` : message, response.status);
  }
  return response.status === 204 ? undefined as T : response.json();
}
export async function streamChat(message: string, session_id: string | null, signal: AbortSignal, onEvent: (event: string, data: Record<string, unknown>) => void) {
  const response = await fetch('/api/v1/agent/chat', { method: 'POST', signal, headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY)}` }, body: JSON.stringify({ message, session_id }) });
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw new ApiError(data.detail || '对话请求失败', response.status); }
  if (!response.body) throw new Error('浏览器不支持流式响应');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = '', doneReceived = false;
  function consume(frame: string) {
    const lines = frame.split('\n');
    const type = lines.find(line => line.startsWith('event:'))?.slice(6).trim();
    const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
    if (type && data) { onEvent(type, JSON.parse(data)); if (type === 'done') doneReceived = true; }
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      buffer = buffer.replace(/\r\n/g, '\n');
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) { consume(buffer.slice(0, boundary)); buffer = buffer.slice(boundary + 2); }
      if (done) { if (buffer.trim()) consume(buffer); break; }
    }
    if (!doneReceived) throw new Error('连接已中断，请重新打开会话查看结果');
  } finally { reader.releaseLock(); }
}
