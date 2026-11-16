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
const { DashboardPage } = await import('../src/DashboardPage.tsx');
const { OperationsPage } = await import('../src/OperationsPage.tsx');
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = dom.window.localStorage;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
const root = createRoot(document.getElementById('root'));
const h = React.createElement;
const calls = [];
const latency = { count: 0, mean: null, p50: null, p95: null };
const summary = { calls: 0, prompt_tokens: 0, completion_tokens: 0, failures: 0, latency_ms: latency, first_token_ms: latency, estimated_cost: null, unpriced_calls: 0, missing_usage_calls: 0 };
let admin = false, fail = false, traceOk = false;
globalThis.fetch = async url => {
  calls.push(String(url));
  if (fail) return new Response(JSON.stringify({ detail: '监控暂不可用' }), { status: 503 });
  const path = String(url).replace('/api/v1', '').split('?')[0];
  const data = {
    '/stats/llm': { is_admin: admin, scope: 'self', currency: 'CNY', summary, by_module: [], by_model: [], daily: [], tools: { calls: 0, failures: 0, failure_rate: null, latency_ms: latency } },
    '/stats/retrieval': { calls: 0, degraded_count: 0, failures: 0, latency_ms: latency, modes: [], offline: { status: 'missing', summary: {} } },
    '/stats/requests': [{ request_id: 'unknown-request', session_id: null, created_at: '2026-11-16T00:00:00', llm_calls: 1, tool_calls: 0, retrieval_logs: 0, failures: 0 }],
    '/stats/requests/unknown-request': traceOk ? { request_id: 'unknown-request', llm_calls: [{ id: 'llm-1', created_at: '2026-11-16T00:00:00', latency_ms: 100, first_token_ms: 10, module: 'agent', model: 'test-model', prompt_tokens: 10, completion_tokens: 2, status: 'success' }], tool_calls: [], retrieval_logs: [] } : null,
    '/evaluations/statistics': { total_evaluations: 0, completed_count: 0, pending_count: 0, failed_count: 0, metrics: {}, avg_evaluation_duration_ms: null },
    '/evaluations/trends': { trends: [], data_points: 0 },
    '/evaluations/alerts': { alerts: [], count: 0 },
    '/evaluations/health': { enabled: false, sample_rate: 1, alert_thresholds: { faithfulness: .3, answer_relevancy: .5 } },
  }[path];
  return new Response(JSON.stringify(data ?? { detail: '请求记录不存在' }), { status: data ? 200 : 404 });
};
async function render(element) { await act(async () => { root.render(element); await new Promise(resolve => setTimeout(resolve, 5)); }); }
async function click(element) { assert.ok(element); await act(async () => { element.click(); await new Promise(resolve => setTimeout(resolve, 5)); }); }
function button(text) { return [...document.querySelectorAll('button')].find(e => e.textContent.trim() === text); }

test('Dashborad switches between operations and the existing RAGAS page, with guest login', async () => {
  let logins = 0;
  await render(h(DashboardPage, { token: null, login: () => logins++, openSession() {} }));
  assert.match(document.body.textContent, /登录后查看运行监控/);
  await click(button('登录'));
  assert.equal(logins, 1);
  await click(button('RAGAS 评估'));
  assert.equal(document.querySelector('[role="tab"][aria-selected="true"]').textContent, 'RAGAS 评估');
  assert.match(document.body.textContent, /登录后查看 RAG 评估监控/);
  localStorage.setItem('fitnesspro.token', 'test-token');
  await render(h(DashboardPage, { key: 'signed-in', token: 'test-token', login() {}, openSession() {} }));
  assert.match(document.body.textContent, /暂无 LLM 调用记录/);
  assert.equal(document.querySelector('[aria-label="数据范围"]'), null);
  assert.match(document.body.textContent, /未定价/);
  await click(button('RAGAS 评估'));
  assert.match(document.body.textContent, /RAG 评估监控/);
  assert.ok(calls.some(url => url.includes('/evaluations/statistics')));
  await click(button('运行监控'));
  assert.ok(calls.some(url => url.includes('/stats/retrieval?range=7d&scope=self')));
});

test('operations supports admin scope, time filter, trace errors and load retry', async () => {
  admin = true;
  await render(h(OperationsPage, { key: 'admin', token: 'test-token', login() {}, openSession() {} }));
  const scope = document.querySelector('[aria-label="数据范围"]');
  assert.ok(scope);
  await act(async () => { scope.value = 'all'; scope.dispatchEvent(new window.Event('change', { bubbles: true })); await new Promise(resolve => setTimeout(resolve, 5)); });
  assert.ok(calls.some(url => url.includes('scope=all')));
  const range = document.querySelector('[aria-label="监控时间范围"]');
  await act(async () => { range.value = '30d'; range.dispatchEvent(new window.Event('change', { bubbles: true })); await new Promise(resolve => setTimeout(resolve, 5)); });
  assert.ok(calls.some(url => url.includes('range=30d&scope=all')));
  await click(button('unknown-request'));
  assert.ok(calls.some(url => url.includes('/requests/unknown-request?scope=all')));
  assert.match(document.body.textContent, /请求记录不存在/);
  traceOk = true;
  await click(button('unknown-request'));
  assert.match(document.body.textContent, /test-model/);
  assert.match(document.body.textContent, /输入 10 \/ 输出 2 token/);
  await click(document.querySelector('[aria-label="关闭请求详情"]'));
  assert.equal(document.querySelector('.trace-detail'), null);
  fail = true;
  await click(document.querySelector('[aria-label="刷新运行数据"]'));
  assert.match(document.querySelector('[role="alert"]').textContent, /监控暂不可用/);
  fail = false;
  await click(button('重试'));
  assert.equal(document.querySelector('[role="alert"]'), null);
  await render(null);
});
