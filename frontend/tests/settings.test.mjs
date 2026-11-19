import { test } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { readFileSync, existsSync } from 'node:fs';
import ts from 'typescript';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = dom.window.localStorage;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
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
const { default: React, act } = await import('react');
const { createRoot } = await import('react-dom/client');
const { SettingsPage } = await import('../src/SettingsPage.tsx');
const h = React.createElement;
const root = createRoot(document.getElementById('root'));
const secret = 'sk-live-secret-should-not-echo';
const providers = {
  ready: true,
  normal: { model: 'deepseek-v4-pro', base_url: 'https://api.deepseek.com', api_key_set: true, api_key_hint: '••••abcd' },
  fast: { model: 'deepseek-flash', base_url: 'https://api.deepseek.com', api_key_set: true, api_key_hint: '••••abcd' },
  embedding: { provider: 'openai', model: 'BAAI/bge-m3', base_url: 'https://api.siliconflow.cn/v1', api_key_set: false, api_key_hint: '' },
  reranker: { provider: 'openai', enabled: true, model: 'BAAI/bge-reranker-v2-m3', base_url: 'https://api.siliconflow.cn/v1/rerank', api_key_set: false, api_key_hint: '' },
};
let calls = [];
function mockFetch() {
  calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (options.method === 'PUT') return new Response(JSON.stringify({ ...providers, normal: { ...providers.normal, api_key_hint: '••••echo' }, warnings: ['未找到向量索引，动作检索只使用关键词。'] }));
    return new Response(JSON.stringify(providers));
  };
}
async function render(element) { await act(async () => root.render(element)); }
async function click(element) { assert.ok(element); await act(async () => element.click()); }
function button(text) { return [...document.querySelectorAll('button')].find(e => e.textContent.includes(text)); }
async function type(element, text) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(element, text);
    element.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  });
}

test('settings stay behind login and never echo a saved key', async () => {
  mockFetch();
  await render(h(SettingsPage, { token: null, login: () => {} }));
  assert.match(document.body.textContent, /登录后填写模型密钥/);
  assert.equal(calls.length, 0);
  localStorage.setItem('fitnesspro.token', 'test');
  await render(h(SettingsPage, { token: 'test', login: () => {} }));
  assert.match(document.body.textContent, /已保存 ••••abcd/);
  assert.doesNotMatch(document.body.textContent, /sk-live/);
  await type(document.querySelector('[aria-label="教练模型 API Key"]'), secret);
  await click(button('保存连接'));
  const saved = calls.find(call => call.options.method === 'PUT');
  const body = JSON.parse(saved.options.body);
  assert.equal(body.normal.api_key, secret);
  assert.equal(body.fast.api_key, null);
  assert.equal(body.share_normal_key, true);
  assert.equal(document.querySelector('[aria-label="教练模型 API Key"]').value, '');
  assert.doesNotMatch(document.body.textContent, new RegExp(secret));
  assert.match(document.body.textContent, /连接已保存/);
});
