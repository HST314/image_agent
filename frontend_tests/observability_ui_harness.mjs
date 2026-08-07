// P2-03 运行监督台 UI 契约测试 Harness（零 npm 依赖）。
// 用最小 DOM shim 在 Node 中真实执行 frontend/index.html 的内联脚本，
// 再由 driver 驱动事件日志分页/since 增量/SSE 续传去重/概要进度投影与错误态，
// 验证冻结高水位、断线重连、项目切换隔离、敏感字段不进入 DOM 与全程零写入。
// 用法: node observability_ui_harness.mjs <index.html 路径> <driver.js 路径>
//  stdout: JSON 数组 [{name, pass}]；退出码 0=全部通过，1=有失败，2= harness 错误。
import { readFileSync, writeSync } from 'node:fs';

const [htmlPath, driverPath] = process.argv.slice(2);
const html = readFileSync(htmlPath, 'utf8');
const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) { writeSync(2, 'no inline <script> found\n'); process.exit(2); }

// ---- 最小 DOM shim：只实现页面脚本真实用到的 API ----
// 区域感知动态缓存：#content 全量重渲、#obs-root / #hist-root 定向重渲分别持有独立缓存。
const contentCache = new Map();
const obsCache = new Map();
const histCache = new Map();
const previewCache = new Map();
let obsRootEl = null;
let histRootEl = null;
function makeCtx() {
  const ctx = {
    calls: [],
    lineWidth: 1, strokeStyle: '#000', lineCap: 'butt', lineJoin: 'miter',
    setTransform(...a) { ctx.calls.push({ op: 'setTransform', args: a }); },
    clearRect(...a) { ctx.calls.push({ op: 'clearRect', args: a }); },
    beginPath() { ctx.calls.push({ op: 'beginPath', args: [] }); },
    moveTo(...a) { ctx.calls.push({ op: 'moveTo', args: a }); },
    lineTo(...a) { ctx.calls.push({ op: 'lineTo', args: a }); },
    rect(...a) { ctx.calls.push({ op: 'rect', args: a }); },
    stroke() { ctx.calls.push({ op: 'stroke', args: [], lineWidth: ctx.lineWidth, strokeStyle: ctx.strokeStyle }); },
  };
  return ctx;
}
function makeEl(name, onSetHtml) {
  const el = {
    name, _html: '', _text: '', _handlers: {}, style: {}, dataset: {}, attributes: {},
    disabled: false, value: '', checked: false,
    width: 0, height: 0, clientWidth: 0, clientHeight: 0,
    naturalWidth: 0, naturalHeight: 0, complete: false, src: '',
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c) { const h = this._s.has(c); if (h) this._s.delete(c); else this._s.add(c); return !h; },
      contains(c) { return this._s.has(c); },
    },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; },
    addEventListener(t, f) { (this._handlers[t] = this._handlers[t] || []).push(f); },
    append() {}, remove() {}, focus() {}, showModal() {}, close() {},
    getContext() { if (!el._ctx) el._ctx = makeCtx(); return el._ctx; },
    setPointerCapture() {}, releasePointerCapture() {},
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = String(v); if (onSetHtml) onSetHtml(); },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return this._text; }, set(v) { this._text = String(v); },
  });
  return el;
}
const staticIds = ['toasts', 'health-text', 'health-dot', 'project-list', 'page-title',
  'context-label', 'content', 'new-button', 'refresh-button', 'project-form', 'project-dialog',
  'menu-button', 'sidebar', 'project-id', 'task-json', 'offline', 'project-error', 'task-error',
  'reopen-dialog', 'reopen-preview-body', 'reopen-name', 'reopen-actor', 'reopen-name-error',
  'reopen-actor-error', 'reopen-status', 'reopen-confirm', 'reopen-cancel', 'reopen-close'];
const staticEls = new Map();
const byId = (id) => { if (!staticEls.has(id)) staticEls.set(id, makeEl('#' + id)); return staticEls.get(id); };
const contentEl = makeEl('#content', () => { contentCache.clear(); obsCache.clear(); histCache.clear(); obsRootEl = null; histRootEl = null; });
staticEls.set('content', contentEl);
function dynamicById(id) {
  const re = new RegExp('id="' + id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '"');
  if (obsRootEl && re.test(obsRootEl.innerHTML)) {
    if (!obsCache.has(id)) obsCache.set(id, makeEl('obs#' + id));
    return obsCache.get(id);
  }
  if (histRootEl && re.test(histRootEl.innerHTML)) {
    if (!histCache.has(id)) histCache.set(id, makeEl('hist#' + id));
    return histCache.get(id);
  }
  if (re.test(byId('reopen-preview-body').innerHTML)) {
    if (!previewCache.has(id)) previewCache.set(id, makeEl('preview#' + id));
    return previewCache.get(id);
  }
  if (re.test(contentEl.innerHTML)) {
    if (id === 'obs-root') {
      if (!obsRootEl) obsRootEl = makeEl('#obs-root', () => { obsCache.clear(); });
      return obsRootEl;
    }
    if (id === 'hist-root') {
      if (!histRootEl) histRootEl = makeEl('#hist-root', () => { histCache.clear(); });
      return histRootEl;
    }
    if (!contentCache.has(id)) contentCache.set(id, makeEl('content#' + id));
    return contentCache.get(id);
  }
  return null;
}
const docHandlers = {};
globalThis.document = {
  addEventListener(t, f) { (docHandlers[t] = docHandlers[t] || []).push(f); },
  createElement(tag) { return makeEl(tag); },
  querySelector(sel) {
    if (sel.startsWith('#')) { const id = sel.slice(1); return staticIds.includes(id) ? byId(id) : dynamicById(id); }
    return null;
  },
  querySelectorAll(sel) {
    if (sel === '[data-candidate]') {
      const out = []; const re = /data-candidate="([^"]*)"/g; let m;
      while ((m = re.exec(contentEl.innerHTML))) { const el = makeEl('candidate'); el.dataset.candidate = m[1]; out.push(el); }
      return out;
    }
    return [];
  },
};
function makeStorage() {
  const map = new Map();
  return {
    _map: map,
    getItem(k) { return map.has(k) ? map.get(k) : null; },
    setItem(k, v) { map.set(String(k), String(v)); },
    removeItem(k) { map.delete(k); },
    clear() { map.clear(); },
  };
}
const fetchCalls = [];
globalThis.localStorage = makeStorage();
globalThis.sessionStorage = makeStorage();
globalThis.devicePixelRatio = 1;
globalThis.addEventListener = () => {};
globalThis.prompt = () => null;
globalThis.confirm = () => true;

// ---- FakeEventSource：记录实例与 URL，driver 显式触发 open/event/error ----
const eventSources = [];
class FakeEventSource {
  constructor(url) {
    this.url = String(url);
    this._listeners = {};
    this.onmessage = null; this.onopen = null; this.onerror = null;
    this.closed = false;
    eventSources.push(this);
  }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  close() { this.closed = true; }
  __open() { if (!this.closed && this.onopen) this.onopen(); }
  __emit(type, data, id) {
    if (this.closed) return;
    const ev = { data: typeof data === 'string' ? data : JSON.stringify(data), lastEventId: id == null ? '' : String(id) };
    if (type && this._listeners[type]) for (const fn of this._listeners[type]) fn(ev);
    if (!type && this.onmessage) this.onmessage(ev);
  }
  __error() { if (!this.closed && this.onerror) this.onerror({}); }
}
globalThis.EventSource = FakeEventSource;

globalThis.fetch = async (url, options = {}) => {
  fetchCalls.push({ url: String(url), options });
  const u = String(url);
  const method = String(options.method || 'GET').toUpperCase();
  if (u.endsWith('/api/health')) return { ok: true, status: 200, json: async () => ({ model_config_available: true }) };
  if (u.endsWith('/api/projects')) return { ok: true, status: 200, json: async () => ({ items: [] }) };
  if (u.includes('/event-log') && method === 'GET') {
    const impl = globalThis.__ui.eventLogImpl;
    if (impl) return impl(u, options);
    return { ok: true, status: 200, json: async () => ({ schema_version: 1, items: [], next_cursor: null, through_sequence: 0 }) };
  }
  if (u.includes('/progress') && method === 'GET') {
    const impl = globalThis.__ui.progressImpl;
    if (impl) return impl(u, options);
    return { ok: true, status: 200, json: async () => ({ schema_version: 1, phase: 'received', status: 'idle', job: null, work: { completed: 0, total: 1, unit: 'workflow' }, candidates: { completed_slots: [], failed_slots: [], total: 5 }, quality: { current_round: 0, max_rounds: 1, at_limit: false }, waiting_for_human: false, through_sequence: 0 }) };
  }
  if (u.includes('/jobs/')) {
    return { ok: true, status: 200, json: async () => ({ status: 'succeeded' }) };
  }
  const view = (globalThis.__ui.getView ? globalThis.__ui.getView() : {}) || {};
  return { ok: true, status: 200, json: async () => view };
};
globalThis.__ui = {
  docHandlers, fetchCalls, makeEl, contentEl, byId, eventSources,
  obsRoot: () => obsRootEl,
  getView: null,
  eventLogImpl: null, progressImpl: null,
  setDPR(v) { globalThis.devicePixelRatio = v; },
  storage: { local: globalThis.localStorage, session: globalThis.sessionStorage },
};

// ---- 在同一函数作用域内依次执行页面脚本与 driver（driver 可访问页面内部 state/renderProject/obs* 函数） ----
const driver = readFileSync(driverPath, 'utf8');
const run = new Function(match[1] + '\n;\n' + driver);
Promise.resolve(run()).then((results) => {
  writeSync(1, JSON.stringify(results, null, 1) + '\n');
  process.exit(results.some((r) => !r.pass) ? 1 : 0);
}).catch((err) => { writeSync(2, String((err && err.stack) || err) + '\n'); process.exit(2); });
