// P1-08 圈画微调 UI 契约测试 Harness（零 npm 依赖）。
// 用最小 DOM shim 在 Node 中真实执行 frontend/index.html 的内联脚本，
// 再由 driver 文件驱动 waiting_human_rework 相位的圈画编辑器，
// 验证坐标反算、越界防护、撤销/清空、预览与提交一致性、稳定幂等键与请求状态。
// 用法: node guided_edit_ui_harness.mjs <index.html 路径> <driver.js 路径>
//  stdout: JSON 数组 [{name, pass}]；退出码 0=全部通过，1=有失败，2= harness 错误。
import { readFileSync, writeSync } from 'node:fs';

const [htmlPath, driverPath] = process.argv.slice(2);
const html = readFileSync(htmlPath, 'utf8');
const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) { writeSync(2, 'no inline <script> found\n'); process.exit(2); }

// ---- 最小 DOM shim：只实现页面脚本真实用到的 API ----
const dynamicCache = new Map(); // 由 #content innerHTML 驱动的动态元素（如 #ge-canvas）
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
function makeEl(name) {
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
    set(v) { this._html = String(v); if (el === contentEl) dynamicCache.clear(); },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return this._text; }, set(v) { this._text = String(v); },
  });
  return el;
}
const staticIds = ['toasts', 'health-text', 'health-dot', 'project-list', 'page-title',
  'context-label', 'content', 'new-button', 'refresh-button', 'project-form', 'project-dialog',
  'menu-button', 'sidebar', 'project-id', 'task-json', 'offline', 'project-error', 'task-error'];
const staticEls = new Map();
const byId = (id) => { if (!staticEls.has(id)) staticEls.set(id, makeEl('#' + id)); return staticEls.get(id); };
const contentEl = byId('content');
function dynamicById(id) {
  const re = new RegExp('id="' + id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '"');
  if (!re.test(contentEl.innerHTML)) return null;
  if (!dynamicCache.has(id)) dynamicCache.set(id, makeEl('dynamic#' + id));
  return dynamicCache.get(id);
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
globalThis.fetch = async (url, options = {}) => {
  fetchCalls.push({ url: String(url), options });
  const u = String(url);
  if (u.endsWith('/api/health')) return { ok: true, status: 200, json: async () => ({ model_config_available: true }) };
  if (u.endsWith('/api/projects')) return { ok: true, status: 200, json: async () => ({ items: [] }) };
  if (u.includes('/advance') && String(options.method || '').toUpperCase() === 'POST') {
    const impl = globalThis.__ui.advanceImpl;
    if (impl) return impl(u, options);
    return { ok: true, status: 202, json: async () => ({ job_id: 'job-default', status: 'queued', created: true, status_url: u.replace(/\/advance$/, '/jobs/job-default') }) };
  }
  if (u.includes('/jobs/')) {
    const impl = globalThis.__ui.jobImpl;
    if (impl) return impl(u, options);
    return { ok: true, status: 200, json: async () => ({ status: 'succeeded' }) };
  }
  const view = (globalThis.__ui.getView ? globalThis.__ui.getView() : {}) || {};
  return { ok: true, status: 200, json: async () => view };
};
globalThis.__ui = {
  docHandlers, fetchCalls, makeEl, contentEl,
  getView: null, advanceImpl: null, jobImpl: null,
  setDPR(v) { globalThis.devicePixelRatio = v; },
  storage: { local: globalThis.localStorage, session: globalThis.sessionStorage },
};

// ---- 在同一函数作用域内依次执行页面脚本与 driver（driver 可访问页面内部 state/renderProject/guidedEdit） ----
const driver = readFileSync(driverPath, 'utf8');
const run = new Function(match[1] + '\n;\n' + driver);
Promise.resolve(run()).then((results) => {
  writeSync(1, JSON.stringify(results, null, 1) + '\n');
  process.exit(results.some((r) => !r.pass) ? 1 : 0);
}).catch((err) => { writeSync(2, String((err && err.stack) || err) + '\n'); process.exit(2); });
