import { test } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { readFileSync, existsSync } from 'node:fs';
import ts from 'typescript';
import { JSDOM } from 'jsdom';
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'http://localhost' });
globalThis.window = dom.window; globalThis.document = dom.window.document;
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
const { TrainingWorkspace } = await import('../src/TrainingWorkspace.tsx');
const { TrainingLocationFields } = await import('../src/TrainingLocationFields.tsx');
const { monday } = await import('../src/trainingTypes.ts');
const h = React.createElement, root = createRoot(document.getElementById('root'));
const week = monday();
const exercise = { id: 'press', name: 'Dumbbell Press', name_zh: '哑铃卧推', equipment: 'dumbbell', primary_muscles: ['chest'], category: 'strength' };
const plan = { id: 'p1', name: '新手三日计划', week_start: week, status: 'draft', version: 1, goal: 'muscle_gain', validation: { warnings: [{ message: '请注意动作控制' }], estimated_minutes: { 0: 30 } }, days: [{ id: 'd1', weekday: 0, focus: '胸部训练', warmup_minutes: 5, cooldown_minutes: 5, notes: '', exercises: [{ id: 'pe1', exercise_id: 'press', position: 0, sets: 1, reps_min: 8, reps_max: 12, duration_seconds: null, rest_seconds: 60, rpe: null, target_weight_kg: 20, notes: '', exercise }] }] };
const stats = { week_start: week, plan_id: 'p1', planned_days: 1, completed_days: 1, completion_rate: 1, workouts: 1, completed_sets: 3, duration_min: 30, volume_kg_reps: 480, daily: Array.from({ length: 7 }, (_, i) => ({ date: `day${i}`, volume_kg_reps: i === 0 ? 480 : 0, duration_min: i === 0 ? 30 : 0, sets: i === 0 ? 3 : 0 })), muscles: [{ muscle: '胸部', planned_sets: 3, actual_sets: 3 }], prs: [{ exercise_id: 'press', name: '哑铃卧推', estimated_1rm_kg: 25.33, previous_best_kg: null, is_pr: true }], notes: ['只使用真实训练记录'] };
let calls = [], active = false;
const props = { token: 'test', completedTurns: 0, login: () => {}, ask: () => {}, detail: () => {} };
function mockFetch() {
  calls = []; active = false;
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith('/activate')) { active = true; return new Response('{}'); }
    if (url === '/api/v1/plans') return new Response(JSON.stringify([{ ...plan, status: active ? 'active' : 'draft' }]));
    if (url === '/api/v1/logs') return new Response(JSON.stringify([]));
    if (url.startsWith('/api/v1/stats/weekly')) return new Response(JSON.stringify(stats));
    if (url === '/api/v1/knowledge/search') return new Response(JSON.stringify({ hits: [exercise] }));
    throw new Error(`Unexpected request ${url}`);
  };
}
async function render(element) { await act(async () => root.render(element)); }
async function click(element) { assert.ok(element); await act(async () => element.click()); }
function button(text) { return [...document.querySelectorAll('button')].find(e => e.textContent.includes(text)); }
async function value(element, text) {
  assert.ok(element);
  await act(async () => {
    const proto = element.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, text);
    element.dispatchEvent(new dom.window.Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  });
}

test('gym preset avoids a long equipment form and retains exclusion categories', async () => {
  await render(h('form', null, h(TrainingLocationFields, { profile: {} })));
  await value(document.querySelector('[name=training_location]'), 'gym');
  assert.match(document.body.textContent, /不用逐项填写/);
  assert.equal(document.querySelectorAll('[name=equipment]').length, 0);
  assert.ok(document.querySelector('[name=excluded_equipment][value=cable]'));
  await value(document.querySelector('[name=training_location]'), 'home');
  assert.ok(document.querySelector('[name=equipment][value=dumbbell]'));
});

test('weekly plan requires explicit activation and exposes replacement flow', async () => {
  mockFetch(); let asked = '';
  await render(h(TrainingWorkspace, { ...props, page: 'plans', ask: text => { asked = text; } }));
  assert.equal(document.querySelectorAll('.training-day').length, 7);
  assert.match(document.body.textContent, /哑铃卧推/);
  assert.equal(calls.filter(c => c.url.endsWith('/activate')).length, 0);
  await click(button('使用这份计划'));
  assert.equal(calls.filter(c => c.url.endsWith('/activate')).length, 0);
  await click(button('确认开始使用'));
  assert.equal(calls.filter(c => c.url.endsWith('/activate')).length, 1);
  assert.match(document.body.textContent, /使用中/);
  await click(button('器械没有'));
  assert.match(document.body.textContent, /不改变长期器械设置/);
  await click(button('找替代动作'));
  const search = calls.find(c => c.url.endsWith('/knowledge/search'));
  assert.deepEqual(JSON.parse(search.options.body).exclude_ids, ['press']);
  await click(button('让教练制定计划'));
  assert.match(asked, /检索动作并校验/);
});

test('recorder uses actual values and never automatically marks planned sets completed', async () => {
  await render(null); mockFetch(); active = true;
  await render(h(TrainingWorkspace, { ...props, page: 'logs' }));
  await value(document.querySelector('[aria-label="关联计划日"]'), 'd1');
  const checkbox = document.querySelector('[aria-label="第1行完成"]');
  assert.ok(checkbox); assert.equal(checkbox.checked, false);
  assert.equal(document.querySelector('[aria-label="第1行重量"]').value, '');
  await value(document.querySelector('[aria-label="哑铃卧推第1组次数"]'), '8');
  await value(document.querySelector('[aria-label="第1行重量"]'), '20');
  await click(checkbox);
  await click(button('保存实际训练'));
  const saved = calls.find(c => c.url === '/api/v1/logs' && c.options.method === 'POST');
  assert.ok(saved);
  const body = JSON.parse(saved.options.body);
  assert.equal(body.plan_day_id, 'd1');
  assert.equal(body.sets[0].reps, 8); assert.equal(body.sets[0].weight_kg, 20); assert.equal(body.sets[0].completed, true);
  assert.ok(body.idempotency_key.length >= 8);
  assert.match(document.body.textContent, /训练已保存/);
});

test('statistics show completion, accessible capacity chart, sets and PR', async () => {
  await render(null); mockFetch();
  await render(h(TrainingWorkspace, { ...props, page: 'stats' }));
  assert.match(document.body.textContent, /100%/);
  assert.match(document.querySelector('[role=img]').getAttribute('aria-label'), /480/);
  assert.match(document.body.textContent, /25.33 kg/);
  assert.match(document.body.textContent, /胸部/);
});

test('private training pages do not make requests before login', async () => {
  await render(null); mockFetch(); let login = false;
  await render(h(TrainingWorkspace, { ...props, token: null, page: 'plans', login: () => { login = true; } }));
  assert.equal(calls.length, 0);
  await click(button('登录 / 注册')); assert.equal(login, true);
});

test('next week suggestions require preview and confirmation before saving a draft', async () => {
  await render(null);
  const { NextWeekPanel } = await import('../src/NextWeekPanel.tsx');
  calls = []; let saved;
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith('/suggest-next-week')) return new Response(JSON.stringify({
      suggestion_id: 'a'.repeat(64), source_plan_id: 'p1', week_start: '2026-12-07',
      adjustments: [{ weekday: 0, position: 0, exercise_id: 'press', name: '哑铃卧推', kind: 'replace', reason: '三个完整周无提升', changes: {}, candidates: [{ exercise_id: 'alt', name: '替代动作' }] }],
      validation: { valid: true, errors: [], warnings: [] }, warnings: [], notes: ['确认后保存草稿']
    }));
    if (url.endsWith('/next-week')) return new Response(JSON.stringify({ ...plan, id: 'p2', version: 2 }));
    throw new Error(`Unexpected request ${url}`);
  };
  await render(h(NextWeekPanel, { planId: 'p1', disabled: false, onSaved: async p => { saved = p; } }));
  assert.equal(calls.length, 0);
  await click(button('查看下周调整建议'));
  assert.equal(calls.length, 1);
  assert.match(document.body.textContent, /三个完整周无提升/);
  await value(document.querySelector('select'), 'alt');
  await click(button('确认建议，保存下周草稿'));
  assert.equal(saved.id, 'p2');
  assert.deepEqual(JSON.parse(calls[1].options.body), { suggestion_id: 'a'.repeat(64), replacements: [{ weekday: 0, position: 0, exercise_id: 'alt' }] });
  assert.equal(calls.filter(c => c.url.endsWith('/activate')).length, 0);
});
