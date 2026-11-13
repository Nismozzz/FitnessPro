import { test } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { readFileSync, existsSync } from 'node:fs';
import ts from 'typescript';
import React, { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

registerHooks({
  resolve(specifier, context, next) {
    if (specifier.startsWith('.') && context.parentURL?.includes('/src/')) {
      const url = new URL(specifier, context.parentURL);
      for (const ext of ['.ts', '.tsx']) if (existsSync(new URL(url.href + ext))) return { url: url.href + ext, shortCircuit: true };
    }
    return next(specifier, context);
  },
  load(url, context, next) {
    if (/\.tsx?$/.test(url)) return { format: 'module', source: ts.transpileModule(readFileSync(new URL(url), 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX } }).outputText, shortCircuit: true };
    return next(url, context);
  },
});
const { ThinkingBlock } = await import('../src/ThinkingBlock.tsx');
const { MemoryPanel } = await import('../src/MemoryPanel.tsx');
const { useMemories } = await import('../src/useMemories.ts');
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = dom.window.localStorage;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const root = createRoot(document.getElementById('root'));
const h = React.createElement;
async function render(element) { await act(async () => root.render(element)); }
async function click(element) { assert.ok(element); await act(async () => element.click()); }
function button(text) { return [...document.querySelectorAll('button')].find(e => e.textContent.trim() === text); }

test('execution shows tool arguments, results and errors, then folds and stops animation', async () => {
  const { updateExecution } = await import('../src/execution.ts');
  let execution = { startedAt: Date.now(), status: 'running', steps: [] };
  const props = () => ({ execution, labels: { search_exercises: '检索动作' } });
  await render(h(ThinkingBlock, props()));
  const details = document.querySelector('.execution-panel');
  assert.equal(details.open, true);
  execution = updateExecution(execution, 'tool_call', { id: 'c1', name: 'search_exercises', arguments: { query: '胸部' } });
  await render(h(ThinkingBlock, props()));
  assert.match(details.textContent, /检索动作/);
  assert.match(details.textContent, /胸部/);
  assert.match(details.textContent, /执行中/);
  execution = updateExecution(execution, 'tool_result', { id: 'c1', success: false, data: { matched: 0 }, error: '检索超时', ms: 1200 });
  execution = updateExecution(execution, 'error', { message: '请稍后再试' });
  execution = updateExecution(execution, 'done', { status: 'error' });
  await render(h(ThinkingBlock, props()));
  assert.equal(details.open, false);
  assert.match(details.textContent, /matched/);
  assert.match(details.textContent, /检索超时/);
  assert.match(details.textContent, /请稍后再试/);
  assert.match(details.textContent, /1.2 秒/);
  assert.equal(document.querySelectorAll('.execution-spinner').length, 0);
  details.open = true;
  await render(h(ThinkingBlock, props()));
  assert.equal(details.open, true);
});

test('interrupted streams finish pending tool states', async () => {
  const { updateExecution } = await import('../src/execution.ts');
  let execution = { startedAt: 100, status: 'running', steps: [] };
  execution = updateExecution(execution, 'tool_call', { id: 'a', name: 'save_plan', arguments: {} }, 200);
  execution = updateExecution(execution, 'done', { status: 'stopped' }, 1500);
  assert.equal(execution.endedAt, 1500);
  assert.equal(execution.steps[0].status, 'interrupted');
  assert.equal(execution.steps[0].ms, 1300);
  await render(h(ThinkingBlock, { execution, labels: {} }));
  assert.equal(document.querySelectorAll('.execution-spinner').length, 0);
});

test('memory tabs isolate candidates; editing preserves confirmation and calls correct actions', async () => {
  const calls = [];
  const rows = [{ id: 'a', content: '喜欢哑铃', status: 'active', kind: 'preference' }, { id: 'c', content: '左膝有旧伤', status: 'candidate', kind: 'injury' }];
  function Harness() {
    const [tab, setTab] = useState('active');
    return h(MemoryPanel, { memories: rows, tab, setTab, working: false, error: '', refreshing: false, refresh: () => calls.push('refresh'), add: async () => true, action: async (...args) => { calls.push(args); return true; } });
  }
  await render(h(Harness));
  assert.match(document.querySelector('[role=tabpanel]').textContent, /喜欢哑铃/);
  assert.doesNotMatch(document.querySelector('[role=tabpanel]').textContent, /左膝/);
  await click([...document.querySelectorAll('[role=tab]')].find(e => e.textContent.includes('待确认')));
  assert.match(document.querySelector('[role=tabpanel]').textContent, /左膝/);
  await click(button('编辑'));
  assert.equal(document.querySelector('textarea').value, '左膝有旧伤');
  await click(button('保存'));
  assert.deepEqual(calls[0], ['c', 'edit', '左膝有旧伤']);
  assert.ok(button('确认')); // Editing never implicitly confirms a candidate.
  await click(button('确认'));
  await click(button('忽略'));
  assert.deepEqual(calls.slice(1).map(c => c.slice(0, 2)), [['c', 'confirm'], ['c', 'reject']]);
});

test('done refresh is delayed and cancelled on logout; stale responses cannot repopulate memory', async () => {
  const timers = new Map(); let id = 0, calls = 0, resolveRequest;
  const originalSet = window.setTimeout, originalClear = window.clearTimeout;
  window.setTimeout = (callback, ms) => { assert.equal(ms, 3000); timers.set(++id, callback); return id; };
  window.clearTimeout = timer => timers.delete(timer);
  globalThis.fetch = async () => { calls++; return new Response(JSON.stringify([{ id: 'c', status: 'candidate', content: '左膝有旧伤' }])); };
  let state;
  function Harness({ token, done }) { state = useMemories(token, done); return null; }
  try {
    await render(h(Harness, { token: 'alice', done: 0 }));
    assert.equal(calls, 1);
    await render(h(Harness, { token: 'alice', done: 1 }));
    assert.equal(calls, 1); assert.equal(timers.size, 1);
    const callback = [...timers.values()][0]; timers.clear();
    await act(async () => callback());
    assert.equal(calls, 2); assert.equal(state.memories[0].id, 'c');
    globalThis.fetch = () => new Promise(resolve => { resolveRequest = resolve; });
    let pending;
    await act(async () => { pending = state.refreshMemories(); });
    await render(h(Harness, { token: 'alice', done: 2 }));
    assert.equal(timers.size, 1);
    await render(h(Harness, { token: null, done: 2 }));
    assert.equal(timers.size, 0);
    await act(async () => { resolveRequest(new Response(JSON.stringify([{ id: 'stale' }]))); await pending; });
    assert.deepEqual(state.memories, []);
  } finally {
    window.setTimeout = originalSet; window.clearTimeout = originalClear;
    await render(null);
  }
});
