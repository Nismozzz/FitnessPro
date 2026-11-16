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
const { ModelSelector, useModelTier } = await import('../src/ModelSelector.tsx');
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = dom.window.localStorage;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const root = createRoot(document.getElementById('root'));
function Harness({ busy = false }) {
  const [tier, setTier] = useModelTier();
  return React.createElement(ModelSelector, { value: tier, onChange: setTier, disabled: busy });
}
async function render(props) { await act(async () => root.render(React.createElement(Harness, props))); }

test('model selection defaults to normal, persists, restores, and disables during an answer', async () => {
  await render({ key: 'initial' });
  let select = document.querySelector('[aria-label="回答模型"]');
  assert.equal(select.value, 'normal');
  await act(async () => { select.value = 'fast'; select.dispatchEvent(new window.Event('change', { bubbles: true })); });
  assert.equal(localStorage.getItem('fitnesspro.model-tier'), 'fast');
  await render({ key: 'restored', busy: true });
  select = document.querySelector('select');
  assert.equal(select.value, 'fast');
  assert.equal(select.disabled, true);
  await render({ key: 'restored', busy: false });
  assert.equal(document.querySelector('select').disabled, false);
  localStorage.setItem('fitnesspro.model-tier', 'unknown');
  await render({ key: 'invalid' });
  assert.equal(document.querySelector('select').value, 'normal');
});
