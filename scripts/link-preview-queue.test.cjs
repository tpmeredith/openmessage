const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

// Exercise the shipped UI functions without starting another browser or live
// transport. The fetch double leaves responses pending until a test releases
// them, reproducing a thread full of slow preview hosts deterministically.
const html = readFileSync(process.env.OPENMESSAGE_TEST_HTML || resolve(__dirname, '../internal/web/static/index.html'), 'utf8');
function section(start, end) {
  const first = html.indexOf(start);
  const last = html.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `UI section missing: ${start}`);
  return html.slice(first, last);
}

function harness() {
  const requests = [];
  const timers = new Map();
  let timerID = 0;
  const context = vm.createContext({
    AbortController,
    Error,
    console: { error() {} },
    setTimeout(callback) { timers.set(++timerID, callback); return timerID; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, { signal } = {}) {
      return new Promise((resolveResponse, reject) => {
        const request = {
          url: new URL(url, 'http://127.0.0.1').searchParams.get('url'),
          signal,
          respond(preview) { resolveResponse({ ok: true, json: async () => preview }); },
          reject,
        };
        requests.push(request);
        signal?.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
      });
    },
  });
  const declarations = html.match(/^  (?:const|let) (?:LINK_PREVIEW_\w+|linkPreview\w+|activeLinkPreviewRequests) = .*;$/gm).join('\n');
  vm.runInContext(`
    const API = '';
    ${declarations}
    ${section('  function cacheGetFresh(', '  function conversationByID(')}
    ${section('  async function fetchJSON(', '  async function responseError(')}
    ${section('  function hasRenderableLinkPreview(', '  function renderLinkPreviewHTML(')}
    this.load = fetchLinkPreview;
    this.cancel = typeof cancelLinkPreviews === 'function' ? cancelLinkPreviews : () => {};
  `, context);
  return { load: context.load, cancel: context.cancel, requests, timers };
}

const flush = () => new Promise(resolve => setImmediate(resolve));
const url = n => `https://example.com/story-${n}`;

test('slow previews leave connection slots available and drain two at a time', async () => {
  const h = harness();
  const pending = Array.from({ length: 53 }, (_, i) => h.load(url(i)));
  assert.equal(h.requests.length, 2, 'a link-heavy thread must not flood fetch');
  h.requests[0].respond({ title: 'First preview' });
  await flush();
  assert.equal(h.requests.length, 3, 'one completed request releases one slot');
  h.cancel();
  await Promise.all(pending);
  await flush();
  assert.equal(h.requests.length, 3, 'cancellation must discard queued URLs');
  assert.equal(h.timers.size, 0);
});

test('a thread switch aborts old previews and can immediately request the same URL', async () => {
  const h = harness();
  const old = [h.load(url(1)), h.load(url(2)), h.load(url(3))];
  h.cancel();
  const reopened = h.load(url(1));
  assert.deepEqual(await Promise.all(old), [null, null, null]);
  await flush();
  assert.ok(h.requests.slice(0, 2).every(request => request.signal.aborted));
  assert.equal(h.requests.length, 3);
  assert.equal(h.requests[2].url, url(1), 'cancelled previews must remain retryable');
  // Cleanup from the old same-URL request must not remove the new request.
  assert.equal(h.load(url(1)), reopened);
  h.requests[2].respond({ title: 'Reopened preview' });
  assert.equal((await reopened).title, 'Reopened preview');
  await flush();
  assert.equal(h.timers.size, 0);
});

test('duplicate URLs share one request while queued, active, and cached', async () => {
  const h = harness();
  h.load(url(1));
  h.load(url(2));
  const queued = h.load(url(3));
  assert.equal(h.load(url(3)), queued);
  h.requests[0].respond({ title: 'First' });
  await flush();
  assert.equal(h.requests.length, 3);
  assert.equal(h.load(url(3)), queued);
  h.requests[2].respond({ title: 'Third' });
  assert.equal((await queued).title, 'Third');
  assert.equal((await h.load(url(3))).title, 'Third');
  assert.equal(h.requests.length, 3);
  h.cancel();
  await flush();
});

test('a timed out preview releases its slot and is retryable', async () => {
  const h = harness();
  const timedOut = h.load(url(1));
  h.load(url(2));
  const queued = h.load(url(3));
  h.timers.values().next().value();
  assert.equal(await timedOut, null);
  await flush();
  assert.equal(h.requests.length, 3);
  assert.ok(h.requests[0].signal.aborted);
  h.requests[2].respond({ title: 'After timeout' });
  assert.equal((await queued).title, 'After timeout');
  await flush();
  const retry = h.load(url(1));
  assert.equal(h.requests.length, 4);
  h.requests[3].respond({ title: 'Retry succeeded' });
  assert.equal((await retry).title, 'Retry succeeded');
  h.cancel();
  await flush();
});

test('a failed preview does not stop later previews from loading', async () => {
  const h = harness();
  const failed = h.load(url(1));
  h.load(url(2));
  const queued = h.load(url(3));
  h.requests[0].reject(new Error('preview host unavailable'));
  assert.equal(await failed, null);
  await flush();
  assert.equal(h.requests.length, 3);
  h.requests[2].respond({ title: 'Still loads' });
  assert.equal((await queued).title, 'Still loads');
  h.cancel();
  await flush();
});
