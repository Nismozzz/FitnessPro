import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const source = ts.transpileModule(readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
const { request, streamChat, ApiError } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
globalThis.localStorage = { getItem: () => 'test-token' };
function responseFor(text) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({ start(controller) {
    // One byte per chunk exercises split Chinese characters and frame boundaries.
    for (let i = 0; i < bytes.length; i++) controller.enqueue(bytes.slice(i, i + 1));
    controller.close();
  } }));
}
test('SSE handles split UTF-8, CRLF, tools, errors, and terminal status', async () => {
  let sent;
  globalThis.fetch = async (url, options) => {
    sent = { url, options };
    return responseFor('event: session\r\ndata: {"session_id":"s1"}\r\n\r\nevent: thinking\r\ndata: {"delta":"分析训练目标"}\r\n\r\nevent: content\r\ndata: {"delta":"开始训练"}\r\n\r\nevent: tool_call\r\ndata: {"name":"search_exercises"}\r\n\r\nevent: error\r\ndata: {"message":"超时"}\r\n\r\nevent: done\r\ndata: {"status":"timeout"}');
  };
  const events = [];
  await streamChat('训练建议', null, new AbortController().signal, (type, data) => events.push({ type, data }));
  assert.equal(events[1].data.delta, '分析训练目标');
  assert.deepEqual(events.map(e => e.type), ['session', 'thinking', 'content', 'tool_call', 'error', 'done']);
  assert.equal(sent.options.headers.Authorization, 'Bearer test-token');
  assert.deepEqual(JSON.parse(sent.options.body), { message: '训练建议', session_id: null });
});
test('SSE reports prematurely closed connections', async () => {
  globalThis.fetch = async () => responseFor('event: content\ndata: {"delta":"部分回答"}\n\n');
  await assert.rejects(streamChat('test', null, new AbortController().signal, () => {}), /连接已中断/);
});
test('HTTP errors retain status and server message', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: '登录已失效' }), { status: 401 });
  await assert.rejects(request('/users/me'), e => e instanceof ApiError && e.status === 401 && e.message === '登录已失效');
  await assert.rejects(streamChat('test', null, new AbortController().signal, () => {}), e => e.status === 401);
});
test('DELETE requests support empty 204 responses', async () => {
  globalThis.fetch = async (_, options) => { assert.equal(options.method, 'DELETE'); return new Response(null, { status: 204 }); };
  assert.equal(await request('/memories/test', { method: 'DELETE' }), undefined);
});
