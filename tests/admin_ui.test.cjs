/* Run with: node --test tests/admin_ui.test.cjs (no npm dependencies). */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

function harness(handler) {
  const elements = new Map();
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, {
      value: '', textContent: '', innerHTML: '', style: {}, isConnected: true,
      addEventListener() {}, classList: { add() {}, remove() {}, toggle() {} },
      querySelectorAll() { return []; },
    });
    return elements.get(selector);
  };
  const timers = [];
  const calls = [];
  const context = vm.createContext({
    document: { querySelector: element, querySelectorAll: () => [], addEventListener() {} },
    window: { addEventListener() {} },
    crypto: require('node:crypto').webcrypto,
    Blob, ArrayBuffer, AbortController, console,
    setTimeout(fn) { timers.push(fn); return timers.length; },
    clearTimeout(id) { if (id) timers[id - 1] = null; },
    fetch: async (url, options) => {
      calls.push({ url, ...options });
      const result = await handler(url, options);
      return {
        status: result.status || 200, ok: !result.status || result.status < 400,
        text: async () => JSON.stringify(result.body || {}),
      };
    },
  });
  Object.defineProperty(element('#view'), 'innerHTML', {
    get() { return this.html || ''; },
    set(html) {
      this.html = html;
      const body = html.match(/<textarea[^>]*id="mdEditor"[^>]*>([\s\S]*?)<\/textarea>/);
      if (body) element('#mdEditor').value = body[1];
    },
  });
  const path = require('node:path').join(__dirname, '../src/ctrlstore/admin_ui/assets/admin.js');
  const entryPath = require('node:path').join(__dirname, '../src/ctrlstore/admin_ui/assets/entries.js');
  vm.runInContext(readFileSync(entryPath, 'utf8'), context);
  const source = readFileSync(path, 'utf8').replace('  boot();', `
    globalThis.ui = { state, uploadOne, saveEditor, updateEditorPreview, uploadQueueHTML, showConfirmError };
  `);
  vm.runInContext(source, context);
  const ui = context.ui;
  ui.state.csrf = 'test-csrf';
  // Execute retry delays immediately; preview timers remain explicitly controllable.
  context.setTimeout = (fn, ms) => {
    if (ms === 500 || ms === 1000) { fn(); return 0; }
    timers.push(fn); return timers.length;
  };
  return { ui, calls, element, timers, context };
}

const failure = status => ({ status, body: { error: { code: 'failure', message: '模拟失败' } } });
const upload = () => ({
  file: Object.assign(new Blob([new Uint8Array(4 * 1024 * 1024 + 5)]), { name: 'file.png' }),
  progress: 0,
});

test('a transient failure retries only that part in the same upload session', async () => {
  let failures = 0;
  const h = harness(async url => {
    if (url.endsWith('/uploads')) return { body: { id: 'session-1' } };
    if (url.endsWith('/parts/2') && failures++ === 0) return failure(503);
    return { body: { asset_id: 'asset-1' } };
  });
  const item = upload();
  await h.ui.uploadOne(item);
  assert.equal(h.calls.filter(c => c.url.endsWith('/uploads')).length, 1);
  assert.equal(h.calls.filter(c => c.url.endsWith('/parts/1')).length, 1);
  assert.equal(h.calls.filter(c => c.url.endsWith('/parts/2')).length, 2);
  assert.equal(h.calls.filter(c => c.method === 'DELETE').length, 0);
  assert.equal(item.status, 'done');
  assert.equal(item.progress, 100);
  assert.ok(h.calls.every(c => c.headers['X-CSRF-Token'] === 'test-csrf'));
});

test('manual retry resumes the failed part after automatic retries are exhausted', async () => {
  let unavailable = true;
  const h = harness(async url => {
    if (url.endsWith('/uploads')) return { body: { id: 'session-2' } };
    if (url.endsWith('/parts/2') && unavailable) return failure(503);
    return { body: { asset_id: 'asset-2' } };
  });
  const item = upload();
  await assert.rejects(h.ui.uploadOne(item));
  assert.equal(item.nextPart, 1);
  assert.equal(item.uploadSession.id, 'session-2');
  assert.equal(item.retryable, true);
  h.ui.state.uploadQueue = [item];
  assert.match(h.ui.uploadQueueHTML(), /重试失败分片/);
  unavailable = false;
  await h.ui.uploadOne(item);
  assert.equal(h.calls.filter(c => c.url.endsWith('/uploads')).length, 1);
  assert.equal(h.calls.filter(c => c.url.endsWith('/parts/1')).length, 1);
  assert.equal(item.status, 'done');
});

test('a lost completion response retries with the original idempotency key', async () => {
  let completions = 0;
  const h = harness(async url => {
    if (url.endsWith('/uploads')) return { body: { id: 'session-3' } };
    if (url.endsWith('/complete') && completions++ === 0) throw Error('connection lost');
    return { body: { asset_id: 'asset-3' } };
  });
  const item = upload();
  await assert.rejects(h.ui.uploadOne(item));
  await h.ui.uploadOne(item);
  const attempts = h.calls.filter(c => c.url.endsWith('/complete'));
  assert.equal(attempts[0].headers['Idempotency-Key'], attempts[1].headers['Idempotency-Key']);
  assert.equal(h.calls.filter(c => c.method === 'PUT').length, 2);
  assert.equal(item.status, 'done');
});

test('permission errors do not retry and release the rejected session', async () => {
  const h = harness(async url => {
    if (url.endsWith('/uploads')) return { body: { id: 'session-4' } };
    return failure(403);
  });
  const item = upload();
  await assert.rejects(h.ui.uploadOne(item));
  assert.equal(h.calls.filter(c => c.method === 'PUT').length, 1);
  assert.equal(h.calls.filter(c => c.method === 'DELETE').length, 1);
  assert.equal(item.retryable, false);
});

test('409 reloads the latest body and revision instead of resubmitting stale content', async () => {
  const h = harness(async (url, options) => {
    if (options.method === 'PATCH') return failure(409);
    if (url.endsWith('/body')) return { body: { body: '# 最新正文', revision: 9 } };
    return { body: { id: 'article', kind: 'markdown', revision: 9 } };
  });
  h.ui.state.editor = { asset: { id: 'article' }, revision: 1 };
  h.element('#mdEditor').value = '# 过时正文';
  await h.ui.saveEditor();
  assert.equal(h.ui.state.editor.revision, 9);
  assert.equal(h.ui.state.editor.body, '# 最新正文');
  assert.match(h.element('#view').innerHTML, /最新正文/);
  assert.match(h.element('#toastText').textContent, /已重新加载/);
  assert.equal(h.calls.filter(c => c.method === 'PATCH').length, 1);
});

test('out-of-order preview responses cannot overwrite newer unsaved input', async () => {
  const responses = [];
  const h = harness(() => new Promise(resolve => responses.push(resolve)));
  h.ui.state.editor = { asset: { id: 'article' }, revision: 1 };
  const editor = h.element('#mdEditor');
  editor.value = '# 第一版';
  h.ui.updateEditorPreview();
  const first = h.timers.at(-1)();
  editor.value = '# 第二版';
  h.ui.updateEditorPreview();
  const second = h.timers.at(-1)();
  responses[1]({ body: { html: '<h1>第二版</h1>' } });
  await second;
  responses[0]({ body: { html: '<h1>第一版</h1>' } });
  await first;
  assert.equal(h.element('#mdPreview').innerHTML, '<h1>第二版</h1>');
  assert.deepEqual(h.calls.map(c => JSON.parse(c.body).body), ['# 第一版', '# 第二版']);
  assert.ok(h.calls.every(c => c.url.endsWith('/assets/article/render')));
});

test('purge conflicts list published covers and collections with escaped titles', () => {
  const h = harness(async () => ({}));
  let html = '';
  h.element('#modalBody').insertAdjacentHTML = (_, value) => { html = value; };
  h.ui.showConfirmError({ details: { references: {
    covers: [{ id: 'article', title: '<img onerror="bad()">', current: false, published: true }],
    collections: [{ id: 'collection', title: '首页', slug: 'home.hero' }],
  } } });
  assert.match(html, /已发布封面/);
  assert.match(html, /集合引用/);
  assert.match(html, /home.hero/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img/);
});
