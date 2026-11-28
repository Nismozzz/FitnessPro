import { test } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

registerHooks({
  load(url, context, next) {
    if (/\.tsx$/.test(url)) return { format: 'module', source: ts.transpileModule(readFileSync(new URL(url), 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX } }).outputText, shortCircuit: true };
    return next(url, context);
  },
});
const source = readFileSync(new URL('../src/ToolSelector.tsx', import.meta.url), 'utf8').replace("import { request } from './api';", 'const request = (...args: unknown[]) => (globalThis as any).toolRequest(...args);');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX } }).outputText;
// Resolve React imports from the frontend directory for the data module.
const { pathToFileURL } = await import('node:url');
const { createRequire } = await import('node:module');
const require = createRequire(import.meta.url);
const resolved = compiled.replace(/from ['"](react(?:\/jsx-runtime)?)['"]/g, (_, name) => `from "${pathToFileURL(require.resolve(name)).href}"`);
const { ToolSelector, useSessionTools } = await import(`data:text/javascript;base64,${Buffer.from(resolved).toString('base64')}`);
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = dom.window.localStorage;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.toolRequest = async () => [
  { name: 'get_current_time', description: '日期', side_effect: false, available: true },
  { name: 'web_search', description: '搜索', side_effect: false, available: false, unavailable_reason: '尚未配置' },
];
const root = createRoot(document.getElementById('root'));
let state;
function Harness({ user = 'alice', session = null, busy = false, token = 'token' }) {
  state = useSessionTools(user, session);
  return React.createElement(ToolSelector, { token, value: state.selectedTools, onChange: state.setSelectedTools, disabled: busy, labels: {} });
}
async function render(props) { await act(async () => root.render(React.createElement(Harness, props))); }
async function click(text) { const button = [...document.querySelectorAll('button')].find(b => b.textContent === text); await act(async () => button.click()); }

test('tool selection persists per session and account, binds new sessions, and distinguishes none from default', async () => {
  await render({});
  assert.equal(state.selectedTools, null);
  await click('全部取消');
  assert.deepEqual(state.selectedTools, []);
  assert.match(document.body.textContent, /已禁用全部工具/);
  await act(async () => state.bindSession('s1'));
  await render({ session: 's1' });
  assert.deepEqual(state.selectedTools, []);
  await click('全选可用');
  assert.deepEqual(state.selectedTools, ['get_current_time']);
  await render({ session: 's2' });
  assert.equal(state.selectedTools, null);
  await render({ session: 's1', busy: true });
  assert.ok([...document.querySelectorAll('button,input')].every(el => el.disabled));
  await render({ user: 'bob', session: 's1' });
  assert.equal(state.selectedTools, null);
  await render({ key: 'reload', session: 's1' });
  assert.deepEqual(state.selectedTools, ['get_current_time']);
  await click('恢复默认');
  assert.equal(state.selectedTools, null);
  await render({ token: null });
  assert.ok([...document.querySelectorAll('button')].every(el => el.disabled));
});

test('progression tools can be individually selected and are locked while generating', async () => {
  globalThis.toolRequest = async () => [
    { name: 'suggest_next_week', description: '预览', side_effect: false, available: true },
    { name: 'apply_next_week', description: '保存', side_effect: true, available: true },
  ];
  await render({ key: 'progression', user: 'progression', session: 'test' });
  const section = document.querySelector('.tool-service.training');
  assert.ok(section);
  assert.match(section.textContent, /训练计划与记录/);
  const boxes = [...section.querySelectorAll('input')];
  assert.equal(boxes.length, 2);
  assert.ok(boxes.every(el => el.checked));
  await act(async () => boxes[1].click());
  assert.deepEqual(state.selectedTools, ['suggest_next_week']);
  await act(async () => boxes[0].click());
  assert.deepEqual(state.selectedTools, []);
  await render({ key: 'progression', user: 'progression', session: 'test', busy: true });
  assert.ok([...document.querySelectorAll('input')].every(el => el.disabled));
});

test('the nutrition tool is visible and can be turned off for a conversation', async () => {
  globalThis.toolRequest = async () => [
    { name: 'search_meals', description: '检索健身菜谱', side_effect: false, available: true },
    { name: 'get_current_time', description: '日期', side_effect: false, available: true },
  ];
  await render({ key: 'nutrition', user: 'nutrition', session: 'test' });
  const section = [...document.querySelectorAll('.tool-service')]
    .find(group => group.querySelector('h4').textContent.includes('营养与菜谱'));
  assert.ok(section);
  const checkbox = section.querySelector('input');
  assert.equal(checkbox.checked, true);
  await act(async () => checkbox.click());
  assert.deepEqual(state.selectedTools, ['get_current_time']);
  await act(async () => checkbox.click());
  assert.deepEqual(state.selectedTools, ['get_current_time', 'search_meals']);
});
