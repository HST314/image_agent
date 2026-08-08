// P1-05 UI 契约测试 Harness（零 npm 依赖）。
// 用最小 DOM shim 在 Node 中真实执行 frontend/index.html 的内联脚本，
// 再由 driver 文件驱动 waiting_candidate_retry / waiting_master_selection 两个相位，
// 验证部分候选态不存在 selected_id 提交通路且补跑动作仍可用。
// 用法: node candidate_retry_ui_harness.mjs <index.html 路径> <driver.js 路径>
//  stdout: JSON 数组 [{name, pass}]；退出码 0=全部通过，1=有失败，2= harness 错误。
import { readFileSync, writeSync } from 'node:fs';

const [htmlPath, driverPath] = process.argv.slice(2);
const html = readFileSync(htmlPath, 'utf8');
const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) { writeSync(2, 'no inline <script> found\n'); process.exit(2); }

// ---- 最小 DOM shim：只实现页面脚本真实用到的 API ----
const dynamicCache = new Map(); // 由 #content innerHTML 驱动的动态元素（如 #select-button）
function makeEl(name) {
  const el = {
    name, _html: '', _text: '', _handlers: {}, style: {}, dataset: {}, attributes: {},
    disabled: false, value: '', checked: false,
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
const fetchCalls = [];
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.prompt = () => null;
globalThis.confirm = () => true;
globalThis.fetch = async (url, options = {}) => {
  fetchCalls.push({ url: String(url), options });
  const u = String(url); let body = {};
  if (u.endsWith('/api/health')) body = { model_config_available: true };
  else if (u.endsWith('/api/projects')) body = { items: [] };
  else body = (globalThis.__ui.getView ? globalThis.__ui.getView() : {}) || {};
  return { ok: true, status: 200, json: async () => body };
};
globalThis.__ui = { docHandlers, fetchCalls, makeEl, contentEl, getView: null };

// ---- 在同一函数作用域内依次执行页面脚本与 driver（driver 可访问页面内部 state/renderProject） ----
const driver = readFileSync(driverPath, 'utf8');
const run = new Function(match[1] + '\n;\n' + driver);
Promise.resolve(run()).then((results) => {
  writeSync(1, JSON.stringify(results, null, 1) + '\n');
  process.exit(results.some((r) => !r.pass) ? 1 : 0);
}).catch((err) => { writeSync(2, String((err && err.stack) || err) + '\n'); process.exit(2); });
