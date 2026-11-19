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
const { EquipmentImagePicker, splitImageMessage, EquipmentPhoto } = await import('../src/EquipmentImages.tsx');
const { streamChat } = await import('../src/api.ts');
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = dom.window.localStorage;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
localStorage.setItem('fitnesspro.token', 'token');
const root = createRoot(document.getElementById('root'));
const id = 'a'.repeat(32);
const errors = [];
let currentIds = [], states = [];
function Harness({ disabled = false, token = 'token' }) {
  const [ids, setIds] = useState([]);
  currentIds = ids;
  return React.createElement(EquipmentImagePicker, { token, ids, onChange: setIds, disabled, uploading: value => states.push(value), fail: e => errors.push(e.message) });
}
async function render(element) { await act(async () => { root.render(element); }); }
async function select(file) {
  const input = document.querySelector('input[type=file]');
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await act(async () => { input.dispatchEvent(new window.Event('change', { bubbles: true })); });
}
function config(available = true) { return Response.json({ available, max_upload_bytes: 1024, provider: 'vision.test', notice: '图片会发送至视觉服务' }); }

test('image marker splits history without accepting arbitrary URL/path', () => {
  assert.deepEqual(splitImageMessage(`这是器械\n[图片附件：${id}]`), { text: '这是器械', ids: [id] });
  assert.deepEqual(splitImageMessage('[图片附件：../../secret]'), { text: '[图片附件：../../secret]', ids: [] });
});

test('picker handles configuration, validation, authenticated upload and removal', async () => {
  let uploads = 0, deletes = 0;
  globalThis.fetch = async (url, options = {}) => {
    assert.equal(options.headers.Authorization, 'Bearer token');
    if (url.endsWith('/config')) return config();
    if (options.method === 'POST') {
      uploads++; assert.equal(options.headers['Content-Type'], 'image/png');
      assert.ok(options.body instanceof Blob);
      return Response.json({ id }, { status: 201 });
    }
    if (options.method === 'DELETE') { deletes++; return new Response(null, { status: 204 }); }
    return new Response('jpeg', { headers: { 'Content-Type': 'image/jpeg' } });
  };
  URL.createObjectURL = () => 'blob:private-photo'; URL.revokeObjectURL = () => {};
  await render(React.createElement(Harness));
  assert.equal(document.querySelector('[aria-label="上传器械照片"]').title, '上传器械照片');
  assert.ok(!document.body.textContent.includes('图片会发送'));
  await select(new File(['bad'], 'x.svg', { type: 'image/svg+xml' }));
  await select(new File(['x'.repeat(1025)], 'x.png', { type: 'image/png' }));
  assert.equal(uploads, 0); assert.equal(errors.length, 2);
  await select(new File(['image'], 'x.png', { type: 'image/png' }));
  assert.equal(uploads, 1); assert.deepEqual(currentIds, [id]); assert.deepEqual(states, [true, false]);
  assert.equal(document.querySelector('img').src, 'blob:private-photo');
  assert.ok(document.querySelector('.equipment-thumbnail'));
  assert.equal(document.querySelector('[aria-label="删除器械照片"]').textContent, '');
  assert.ok([...document.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === '上传器械照片').disabled);
  window.confirm = () => true;
  await act(async () => document.querySelector('[aria-label="删除器械照片"]').click());
  assert.equal(deletes, 1); assert.deepEqual(currentIds, []);
  await render(React.createElement(Harness, { key: 'guest', token: null }));
  assert.equal(document.querySelector('[aria-label="上传器械照片"]').title, '登录后可上传');
  assert.ok(document.querySelector('button').disabled);
  globalThis.fetch = async () => config(false);
  await render(React.createElement(Harness, { key: 'disabled' }));
  assert.equal(document.querySelector('[aria-label="上传器械照片"]').title, '请先配置并启用视觉模型');
});

test('private photo handles deleted images and chat sends only image IDs', async () => {
  globalThis.fetch = async () => new Response('', { status: 404 });
  await render(React.createElement(EquipmentPhoto, { id, disabled: false, fail: () => {} }));
  assert.match(document.body.textContent, /图片已删除或无法读取/);
  globalThis.fetch = async (_, options) => {
    const body = JSON.parse(options.body);
    assert.deepEqual(body.image_ids, [id]); assert.ok(!options.body.includes('base64'));
    return new Response('event: done\ndata: {"status":"completed"}\n\n');
  };
  await streamChat('识别器械', null, new AbortController().signal, () => {}, 'normal', null, [id]);
});
