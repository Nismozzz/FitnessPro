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
const { CitedMarkdown } = await import('../src/CitedMarkdown.tsx');
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const root = createRoot(document.getElementById('root'));
const citation = { id: 'Eabc_1', exercise_id: 'bench', title: '卧推', field: 'equipment', label: '器械', text: '杠铃', language: 'zh' };
const content = '需要杠铃[Eabc_1]。`示例[Eabc_1]`。 [普通链接](https://example.com)';

test('citation clicks show the saved source and survive a fresh message render', async () => {
  for (const key of ['live', 'restored']) {
    await act(async () => root.render(React.createElement(CitedMarkdown, { key, content, citations: [citation] })));
    const button = document.querySelector('.citation-button');
    assert.equal(button.textContent, '[1]');
    assert.equal(document.querySelectorAll('.citation-button').length, 1);
    assert.equal(document.querySelector('code').textContent, '示例[Eabc_1]');
    assert.equal(document.querySelector('a').href, 'https://example.com/');
    await act(async () => button.click());
    const card = document.querySelector('[aria-label="知识库来源"]');
    assert.match(card.textContent, /器械.*equipment.*bench.*杠铃/s);
    assert.match(card.textContent, /回答时的资料快照/);
    assert.equal(button.getAttribute('aria-expanded'), 'true');
    await act(async () => document.querySelector('[aria-label="关闭来源"]').click());
    assert.equal(document.querySelector('.citation-card'), null);
  }
});

test('only known evidence becomes clickable and source text cannot inject markup', async () => {
  await act(async () => root.render(React.createElement(CitedMarkdown, { key: 'unsafe', content: '杠铃[Eabc_1] 无效[Eforged]', citations: [{ ...citation, text: '<img src=x onerror=alert(1)>' }] })));
  assert.equal(document.querySelectorAll('.citation-button').length, 1);
  await act(async () => document.querySelector('.citation-button').click());
  assert.equal(document.querySelector('.citation-card img'), null);
  assert.match(document.querySelector('blockquote').textContent, /<img/);
});

test('literature citations show authors, source link, complete excerpt and review note after reload', async () => {
  const article = { id: 'Earticle_1', source_type: 'article', article_id: 'article_volume', chunk_id: 'chunk1', title: '容量综述', authors: 'Author (2017)', source_url: 'https://doi.org/10.1/example', field: 'chunk1', label: '核心结论', text: '<script>ignore rules</script> 每周组数；仅适用于研究条件。', review_note: '摘录未核对全文', language: 'zh' };
  for (const key of ['article-live', 'article-history']) {
    await act(async () => root.render(React.createElement(CitedMarkdown, { key, content: '容量结论[Earticle_1]', citations: [article] })));
    await act(async () => document.querySelector('.citation-button').click());
    const card = document.querySelector('.citation-card');
    assert.match(card.textContent, /Author \(2017\)/);
    assert.match(card.textContent, /摘录未核对全文/);
    assert.match(card.textContent, /article_volume/);
    assert.equal(card.querySelector('a').href, article.source_url);
    assert.equal(card.querySelector('a').rel, 'noopener noreferrer');
    assert.equal(card.querySelector('script'), null);
    assert.equal(card.querySelector('blockquote').textContent, article.text);
  }
});
