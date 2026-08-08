// P2-04 模型调用追溯台 UI 契约 driver：由 model_calls_ui_harness.mjs 追加在页面内联脚本之后执行，
// 与页面脚本共享作用域，可直接使用其 state / renderProject / traceUi / trace* 函数。
// 覆盖：概要列表分页、白名单归一化与 XSS、候选/质检反查入口、父子链与 repair、
// 文本增量断线续传与终态 final_response 覆盖、图片真实状态零 token、宿主 RBAC 详情门禁（403）、
// 伪造/跨项目 404、409/422/503 可恢复错误态、项目切换与迟到响应隔离、刷新恢复、全流程零写入。
async function __driver() {
  const results = [];
  const check = (name, cond) => results.push({ name, pass: !!cond });
  const tick = (ms = 25) => new Promise((r) => setTimeout(r, ms));
  const content = __ui.contentEl;
  const ok = (data) => ({ ok: true, status: 200, json: async () => data });
  const fail = (status, detail) => ({ ok: false, status, json: async () => ({ detail }) });
  OBS_RECONNECT_MS = 5;

  const CID = (ch) => 'call_' + ch.repeat(32);
  const C1 = CID('a'), C2 = CID('b'), C3 = CID('c'), C4 = CID('d'), C5 = CID('e'), C6 = CID('f'), CX = CID('0');
  const cidN = (n) => 'call_' + String(n).padStart(32, '0');
  const enc = (offset) => Buffer.from('model-calls-v1:' + offset).toString('base64url');
  const mkSummary = (id, over = {}) => ({
    call_id: id, capability: 'initial_candidate_generation', call_type: 'text', status: 'completed',
    started_at: '2026-08-07T12:00:00Z', completed_at: '2026-08-07T12:00:04Z', duration_ms: 4200,
    config_hash: 'c'.repeat(64), parent_call_id: null, trace_id: 'trace_x',
    model: { provider: 'fixture', name: 'm', version: '2026-08' },
    template: { id: 't', version: 'v1' },
    input_asset_hashes: ['b'.repeat(64)],
    prompt_summary: { sha256: 'd'.repeat(64), characters: 128 },
    result_summary: { sha256: 'e'.repeat(64), characters: 256 },
    error: null, status_events: [], ...over,
  });

  // 遵循冻结契约的假服务端：概要/详情/文本增量三个只读入口，cursor 为版本化不透明 offset。
  const server = { calls: new Map(), order: [], deltas: new Map(), details: new Map(), rbac: false };
  const mcCalls = [];
  let mcOverride = null;
  const baseImpl = async (u) => {
    const [path, qs] = u.split('?');
    const q = new URLSearchParams(qs || '');
    const single = /\/model-calls\/(call_[a-f0-9]{32})(\/text-deltas)?$/.exec(path);
    if (single && single[2]) {
      const id = single[1];
      if (!server.calls.has(id)) return fail(404, { code: 'MODEL_CALL_NOT_FOUND', message: '模型调用不存在。' });
      const all = server.deltas.get(id) || { items: [], complete: false, final: null };
      const after = parseInt(q.get('after') || '0', 10);
      const limit = parseInt(q.get('limit') || '100', 10);
      const items = all.items.filter((x) => x.sequence > after).slice(0, limit);
      return ok({ call_id: id, items, next_after: items.length ? items[items.length - 1].sequence : after, complete: all.complete, final_response: all.complete ? all.final : null });
    }
    if (single) {
      const id = single[1];
      if (q.get('detail') === 'true') {
        if (!server.rbac || !server.details.has(id)) return fail(403, { code: 'MODEL_CALL_DETAIL_FORBIDDEN', message: '无权读取模型调用详情。' });
        return ok({ view: 'detail', retention: 'append_only_audit', call: server.details.get(id) });
      }
      const c = server.calls.get(id);
      if (!c) return fail(404, { code: 'MODEL_CALL_NOT_FOUND', message: '模型调用不存在。' });
      return ok({ view: 'summary', retention: 'append_only_audit', call: c });
    }
    const limit = parseInt(q.get('limit') || '25', 10);
    if (!(limit >= 1 && limit <= 100)) return fail(422, [{ loc: ['query', 'limit'], msg: 'Input should be less than or equal to 100' }]);
    const cursor = q.get('cursor');
    let start = 0;
    if (cursor != null) {
      try {
        const raw = Buffer.from(cursor, 'base64url').toString();
        if (!raw.startsWith('model-calls-v1:')) throw new Error('bad');
        start = parseInt(raw.split(':')[1], 10);
      } catch { return fail(409, { code: 'MODEL_CALL_CURSOR_INVALID', message: '模型调用游标无效或版本不兼容。' }); }
    }
    const page = server.order.slice(start, start + limit).map((id) => server.calls.get(id));
    const next = start + limit < server.order.length ? enc(start + limit) : null;
    return ok({ items: page, next_cursor: next });
  };
  __ui.modelCallsImpl = async (u) => { mcCalls.push(u); return (mcOverride || baseImpl)(u); };

  const mkView = (pid) => ({
    project_id: pid, capabilities: ['resume'], history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 5 }, updated_at: '2026-08-07T12:00:00Z' },
    snapshot: { state: 'initial_candidate_generation', phase: 'five_candidate_generation', waiting: false },
  });
  const setView = (view) => { state.current = view; __ui.getView = () => view; renderProject(); };
  const clickEl = (id) => { const el = document.querySelector('#' + id); if (!el) { results.push({ name: 'harness: element #' + id + ' present', pass: false }); return; } for (const h of el._handlers.click || []) h({ target: el, preventDefault() {} }); };
  const traceHtml = () => { const h = __ui.traceRoot(); return h ? h.innerHTML : ''; };
  const listHtml = () => { const h = traceHtml(); const i = h.indexOf('id="trace-list"'); const j = h.indexOf('id="trace-detail"'); return i > -1 ? h.slice(i, j > i ? j : undefined) : ''; };
  const panelHtml = () => { const h = traceHtml(); const i = h.indexOf('id="trace-detail"'); return i > -1 ? h.slice(i) : ''; };
  const nodeCount = () => (listHtml().match(/class="hist-node trace-node"/g) || []).length;
  const listCalls = () => mcCalls.filter((u) => u.includes('/model-calls?'));
  const listCallsFor = (pid) => listCalls().filter((u) => u.includes('/projects/' + pid + '/model-calls'));
  const deltaCalls = () => mcCalls.filter((u) => u.includes('/text-deltas'));
  const detailCalls = () => mcCalls.filter((u) => u.includes('detail=true'));
  const summaryCalls = () => mcCalls.filter((u) => !u.includes('/model-calls?') && !u.includes('/text-deltas') && !u.includes('detail=true'));
  const writes = () => __ui.fetchCalls.filter((c) => String(c.options.method || 'GET').toUpperCase() !== 'GET');

  await tick(); // 初始 loadProjects().then(renderHome)

  // ---------- S1：首页概要列表、按需加载、无详情/文本预取、XSS 转义 ----------
  server.calls.set(C1, mkSummary(C1));
  server.calls.set(C2, mkSummary(C2, { capability: 'self_check_inspection', status: 'failed', error: { code: 'PARSE', message: 'invalid' } }));
  server.calls.set(C6, mkSummary(C6, { capability: '<script>alert(1)</script>' }));
  server.order = [C6, C2, C1];
  setView(mkView('trace-proj-a'));
  await tick(50);
  check('init: first list page requested with frozen limit and no cursor', listCalls().length === 1 && listCalls()[0].includes('/model-calls?limit=25') && !listCalls()[0].includes('cursor='));
  check('init: summary nodes rendered in server order', nodeCount() === 3);
  check('init: capability labels rendered', listHtml().includes('候选生成') && listHtml().includes('画面质检'));
  check('init: failed call carries danger badge', listHtml().includes('badge--danger'));
  check('init: zero summary/detail/delta prefetch before selection', summaryCalls().length === 0 && detailCalls().length === 0 && deltaCalls().length === 0);
  check('init: panel shows idle hint', panelHtml().includes('选择一条调用记录'));
  check('init: host RBAC absent so no detail entry anywhere', !traceHtml().includes('查看授权详情'));
  check('init: xss capability escaped not executable', !traceHtml().includes('<script>alert(1)</script>') && traceHtml().includes('&lt;script&gt;'));

  // ---------- S2：游标分页、到底态、409/422/503 可恢复 ----------
  server.calls.clear(); server.order = [];
  for (let i = 0; i < 30; i++) { const id = cidN(i + 1); server.calls.set(id, mkSummary(id)); server.order.push(id); }
  setView(mkView('trace-proj-b'));
  await tick(50);
  check('paging: first page holds frozen page size', nodeCount() === 25);
  clickEl('trace-more');
  await tick();
  check('paging: second page passes opaque cursor', listCallsFor('trace-proj-b').length === 2 && listCallsFor('trace-proj-b')[1].includes('cursor='));
  const decoded = Buffer.from(new URLSearchParams(listCallsFor('trace-proj-b')[1].split('?')[1]).get('cursor'), 'base64url').toString();
  check('paging: cursor pins offset window', decoded === 'model-calls-v1:25');
  check('paging: all 30 calls loaded across pages', nodeCount() === 30);
  check('paging: end-of-list state shown', listHtml().includes('已加载全部调用记录') && !listHtml().includes('id="trace-more"'));

  mcOverride = async (u) => (u.includes('cursor=') ? fail(409, { code: 'MODEL_CALL_CURSOR_INVALID', message: '模型调用游标无效或版本不兼容。' }) : baseImpl(u));
  server.order.push(cidN(31)); server.calls.set(cidN(31), mkSummary(cidN(31)));
  clickEl('trace-reload');
  await tick(40);
  clickEl('trace-more');
  await tick();
  check('error-409: cursor failure surfaced with distinct title', listHtml().includes('调用列表游标已失效') && listHtml().includes('模型调用游标无效'));
  check('error-409: loaded rows preserved on cursor failure', nodeCount() === 25);
  check('error-409: reload action offered', listHtml().includes('id="trace-retry"'));
  mcOverride = null;
  clickEl('trace-retry');
  await tick(40);
  check('error-409: reload recovers from first page', nodeCount() === 25 && !listCalls()[listCalls().length - 1].includes('cursor='));

  mcOverride = async () => fail(422, [{ loc: ['query', 'limit'], msg: 'Input should be less than or equal to 100' }]);
  setView(mkView('trace-proj-b2'));
  await tick(50);
  check('error-422: parameter error surfaced distinctly', listHtml().includes('查询参数未通过校验') && listHtml().includes('limit'));
  mcOverride = null;

  mcOverride = async () => fail(503, { code: 'MODEL_CALL_AUDIT_UNAVAILABLE', message: '模型调用审计暂不可读取。' });
  setView(mkView('trace-proj-b3'));
  await tick(50);
  check('error-503: unavailable message surfaced', listHtml().includes('调用列表读取失败') && listHtml().includes('模型调用审计暂不可读取'));
  mcOverride = null;
  clickEl('trace-retry');
  await tick(40);
  check('error-503: retry recovers list', nodeCount() === 25);

  // ---------- S3：选中概要、敏感字段白名单、父子链与 repair ----------
  server.calls.clear(); server.deltas.clear(); server.details.clear(); server.rbac = false;
  server.calls.set(C1, mkSummary(C1, {
    capability: 'self_check_inspection', status: 'failed', error: { code: 'PARSE', message: 'invalid output' },
    raw_payload: 'sk-secret', provider_response: { signed_url: 'https://vendor.example/x?signature=abc' },
    local_path: '/srv/private/secret/file.png', messages: [{ role: 'user', content: 'LEAKMSG' }],
    output_raw: 'RAWLEAK', person: { email: 'alice@example.com', phone: '13800138000' },
  }));
  server.calls.set(C2, mkSummary(C2, { capability: 'self_check_inspection', parent_call_id: C1 }));
  server.calls.set(C3, mkSummary(C3, { call_type: 'image', parent_call_id: C2 }));
  server.order = [C3, C2, C1];
  setView(mkView('trace-proj-c'));
  await tick(50);
  clickEl('trace-node-' + C2);
  await tick(40);
  check('select: summary fetched on demand for picked call', summaryCalls().some((u) => u.includes(C2)));
  check('select: summary meta rendered', panelHtml().includes('画面质检') && panelHtml().includes('文本生成') && panelHtml().includes('4200 毫秒'));
  check('select: repair badge shown when parent failed', panelHtml().includes('repair 修复调用'));
  check('select: parent chain walked via summary only', summaryCalls().some((u) => u.includes(C1)) && detailCalls().length === 0);
  check('select: chain renders current and parent chips', panelHtml().includes('trace-chain-' + C2) && panelHtml().includes('trace-chain-' + C1) && panelHtml().includes('← 父调用'));
  check('sensitive: raw payload and supplier url never enter DOM', !traceHtml().includes('sk-secret') && !traceHtml().includes('vendor.example') && !traceHtml().includes('signature=abc'));
  check('sensitive: local path and messages never enter DOM', !traceHtml().includes('/srv/private') && !traceHtml().includes('LEAKMSG') && !traceHtml().includes('RAWLEAK'));
  check('sensitive: pii never enters DOM', !traceHtml().includes('alice@example.com') && !traceHtml().includes('13800138000'));
  check('sensitive: unknown fields dropped at normalize', !('raw_payload' in traceUi.summary) && !('messages' in traceUi.summary) && !('output_raw' in traceUi.summary));
  clickEl('trace-chain-' + C1);
  await tick(40);
  check('chain: clicking parent chip selects it', traceUi.selected === C1 && panelHtml().includes('invalid output'));
  check('chain: root call has no parent note', panelHtml().includes('该调用没有父调用'));
  // 父链断裂：父调用不存在时链路明示而不是崩溃
  server.calls.set(CX, mkSummary(CX, { parent_call_id: cidN(99) }));
  server.order.push(CX);
  clickEl('trace-reload');
  await tick(40);
  clickEl('trace-node-' + CX);
  await tick(40);
  check('chain: missing parent surfaced as explicit note', panelHtml().includes('父调用记录不存在或不可见'));

  // ---------- S4：文本增量断线续传、去重排序、错误恢复、终态 final_response 覆盖 ----------
  server.calls.set(C4, mkSummary(C4, { capability: 'confirmation_build' }));
  server.deltas.set(C4, { items: [{ sequence: 1, delta: 'alpha' }, { sequence: 2, delta: '-beta' }, { sequence: 3, delta: '-gamma' }], complete: false, final: null });
  server.order = [C4, C3, C2, C1];
  clickEl('trace-reload');
  await tick(40);
  clickEl('trace-node-' + C4);
  await tick(40);
  check('text: no delta prefetch before explicit load', deltaCalls().length === 0 && panelHtml().includes('加载文本输出'));
  clickEl('trace-text-load');
  await tick(30);
  check('text: first load resumes from zero with frozen limit', deltaCalls().length === 1 && deltaCalls()[0].includes('after=0') && deltaCalls()[0].includes('limit=100'));
  check('text: joined deltas rendered in sequence order', panelHtml().includes('alpha-beta-gamma'));
  check('text: running call offers resume actions', panelHtml().includes('id="trace-text-pull"'));
  mcOverride = async (u) => (u.includes('/text-deltas') ? fail(503, { code: 'MODEL_CALL_AUDIT_UNAVAILABLE', message: '模型调用审计暂不可读取。' }) : baseImpl(u));
  clickEl('trace-text-pull');
  await tick(30);
  check('text: 503 surfaced with resume-from hint', panelHtml().includes('文本增量读取失败') && panelHtml().includes('从序号 #3 续传重试'));
  mcOverride = null;
  server.deltas.set(C4, { items: [{ sequence: 1, delta: 'alpha' }, { sequence: 2, delta: '-beta' }, { sequence: 3, delta: '-gamma' }, { sequence: 4, delta: '-delta' }, { sequence: 5, delta: '-epsilon' }], complete: false, final: null });
  clickEl('trace-text-retry');
  await tick(30);
  check('text: retry resumes from last confirmed sequence', deltaCalls()[deltaCalls().length - 1].includes('after=3'));
  check('text: resume appends without duplication', panelHtml().includes('alpha-beta-gamma-delta-epsilon') && (panelHtml().match(/-gamma/g) || []).length === 1);
  mcOverride = async (u) => (u.includes('/text-deltas') ? ok({ call_id: C4, items: [{ sequence: 6, delta: '-zeta' }, { sequence: 5, delta: '-epsilon' }], next_after: 6, complete: false, final_response: null }) : baseImpl(u));
  clickEl('trace-text-pull');
  await tick(30);
  mcOverride = null;
  check('text: duplicate and out-of-order deltas deduped and sorted', (panelHtml().match(/-epsilon/g) || []).length === 1 && panelHtml().indexOf('-epsilon') < panelHtml().indexOf('-zeta'));
  mcOverride = async (u) => (u.includes('/text-deltas') ? ok({ call_id: C4, items: [], next_after: 6, complete: true, final_response: 'FINAL-AUTHORITATIVE' }) : baseImpl(u));
  clickEl('trace-text-pull');
  await tick(30);
  mcOverride = null;
  check('text: terminal final_response overrides incremental display', panelHtml().includes('FINAL-AUTHORITATIVE') && !panelHtml().includes('alpha-beta-gamma'));
  check('text: completed call offers no more resume actions', !panelHtml().includes('id="trace-text-pull"') && !panelHtml().includes('id="trace-text-more"'));
  check('text: authority note explains override', panelHtml().includes('已覆盖增量拼接展示'));

  // ---------- S5：图片调用只展示真实状态事件，零 token 增量 ----------
  const deltasBeforeImage = deltaCalls().length;
  server.calls.set(C5, mkSummary(C5, {
    call_type: 'image', capability: 'initial_candidate_generation',
    status_events: [
      { status: 'queued', at: '2026-08-07T12:00:00Z' },
      { status: 'running', at: '2026-08-07T12:00:01Z' },
      { status: 'provider_completed', at: '2026-08-07T12:00:05Z' },
      { status: 'ingested', at: '2026-08-07T12:00:06Z' },
    ],
  }));
  server.order = [C5, C4, C3, C2, C1];
  clickEl('trace-reload');
  await tick(40);
  clickEl('trace-node-' + C5);
  await tick(40);
  const flow = panelHtml();
  check('image: real status events rendered in truthful order', flow.indexOf('已入队') > -1 && flow.indexOf('已入队') < flow.indexOf('生成中') && flow.indexOf('生成中') < flow.indexOf('供应商已返回') && flow.indexOf('供应商已返回') < flow.indexOf('资产已入库'));
  check('image: zero token stream requested for image call', deltaCalls().length === deltasBeforeImage);
  check('image: no text viewer offered for image call', !flow.includes('id="trace-text-load"'));
  check('image: truthful no-token note shown', flow.includes('不产生文本 token 增量'));
  check('image: no fabricated percent or eta', !flow.includes('%') && !flow.includes('预计'));

  // ---------- S6：宿主 RBAC 详情门禁（未接入不请求、403 可恢复、授权后脱敏渲染） ----------
  check('gate: detail entry hidden while host RBAC undeclared', !flow.includes('查看授权详情') && detailCalls().length === 0);
  globalThis.MODEL_CALL_DETAIL_RBAC = true;
  clickEl('trace-node-' + C4);
  await tick(40);
  check('gate: detail entry appears once host RBAC declared', panelHtml().includes('查看授权详情'));
  clickEl('trace-detail-load');
  await tick(30);
  check('gate: detail request carries detail=true', detailCalls().length === 1 && detailCalls()[0].includes('detail=true'));
  check('gate: 403 surfaces forbidden copy without bypass', panelHtml().includes('未获服务端授权') && panelHtml().includes('MODEL_CALL_DETAIL_FORBIDDEN'));
  await tick(60);
  check('gate: no automatic retry after 403', detailCalls().length === 1);
  check('gate: forbidden state offers no further detail action', !panelHtml().includes('id="trace-detail-load"') && !panelHtml().includes('id="trace-detail-retry"'));
  server.rbac = true;
  server.details.set(C4, {
    ...mkSummary(C4, { capability: 'confirmation_build' }),
    messages: [{ role: 'user', content: 'safe prompt' }, { role: 'system', content: 'use Bearer topsecret now' }],
    variables: { api_key: 'sk-live-secret', note: 'ok' },
    parameters: { temperature: 0.7 },
    input_refs: ['artifact_' + 'b'.repeat(64), 'https://vendor.example/x?signature=abc'],
    output_raw: { body: 'done', token: 'hidden' },
    output_parsed: { ok: true }, output_ref: null, error: null,
    text_deltas: [{ sequence: 1, delta: 'alpha' }],
  });
  clickEl('trace-node-' + C5);
  await tick(30);
  clickEl('trace-node-' + C4);
  await tick(40);
  clickEl('trace-detail-load');
  await tick(30);
  check('gate: authorized detail renders redacted content', panelHtml().includes('safe prompt') && panelHtml().includes('已脱敏'));
  check('gate: bearer token never enters DOM', !panelHtml().includes('topsecret'));
  check('gate: secret key name and value dropped', !panelHtml().includes('api_key') && !panelHtml().includes('sk-live-secret'));
  check('gate: signed url redacted in detail', !panelHtml().includes('signature=abc') && !panelHtml().includes('vendor.example'));
  check('gate: token field dropped from output detail', !panelHtml().includes('hidden'));
  check('gate: retention and no-delete copy shown', panelHtml().includes('append_only_audit') && panelHtml().includes('不提供删除'));
  check('gate: delta count summarized without dumping stream', panelHtml().includes('共 1 条'));

  // ---------- S7：伪造/跨项目 ID 与非法 ID ----------
  await traceFocusCall(cidN(77));
  await tick(40);
  check('forged: unknown call id surfaced as not-found', panelHtml().includes('调用记录不存在或不可见') && panelHtml().includes('伪造或跨项目'));
  const summariesBeforeInvalid = summaryCalls().length;
  await traceFocusCall('call_NOTVALID');
  await tick(20);
  check('forged: malformed id rejected without any request', summaryCalls().length === summariesBeforeInvalid);

  // ---------- S8：候选与质检反查入口 ----------
  setView({
    ...mkView('trace-proj-d'),
    snapshot: {
      state: 'master_candidate_selection', phase: 'waiting_master_selection', waiting: true, completed: false,
      candidates: [{ id: 'cand-1', uri: 'u1', model_call_id: C3 }, { id: 'cand-2', uri: 'u2' }],
      candidate_slots: { succeeded: [0, 1, 2, 3, 4], failed: [], pending_retry: [] },
    },
  });
  await tick(50);
  const traceRefs = document.querySelectorAll('[data-trace-call]');
  check('lookup: candidate with model_call_id offers trace entry', traceRefs.length === 1 && traceRefs[0].dataset.traceCall === C3);
  for (const h of __ui.docHandlers.click || []) h({ target: { closest: (sel) => (sel === '[data-trace-call]' ? { dataset: { traceCall: C3 } } : null) } });
  await tick(50);
  check('lookup: clicking candidate trace entry loads that call summary', summaryCalls().some((u) => u.includes(C3)) && traceUi.selected === C3);
  check('lookup: focused call shows provenance note', panelHtml().includes('已从候选或质检入口定位'));
  setView({
    ...mkView('trace-proj-d'),
    snapshot: {
      state: 'self_check_iteration', phase: 'waiting_human_approval', waiting: true, completed: false,
      inspection: { passed: true, rework_prompt_delta: '', model_call_id: C2 },
      asset: { artifact_id: 'artifact_' + 'b'.repeat(64) },
    },
  });
  await tick(50);
  const qcRefs = document.querySelectorAll('[data-trace-call]');
  check('lookup: inspection with model_call_id offers chain entry', qcRefs.length === 1 && qcRefs[0].dataset.traceCall === C2 && content.innerHTML.includes('查看本次质检调用链'));

  // ---------- S9：项目切换取消旧响应，迟到响应不串页 ----------
  let resolveSlow;
  mcOverride = async (u) => {
    if (u.includes(C1) && !u.includes('/text-deltas')) return new Promise((r) => { resolveSlow = () => r(ok({ view: 'summary', retention: 'append_only_audit', call: mkSummary(C1, { capability: 'intake_clarify' }) })); });
    return baseImpl(u);
  };
  setView(mkView('trace-proj-e'));
  await tick(50);
  clickEl('trace-node-' + C1);
  await tick(20);
  check('switch: slow summary in flight before switching', resolveSlow != null);
  setView(mkView('trace-proj-f'));
  await tick(50);
  check('switch: fresh project reloads list from first page', listCalls()[listCalls().length - 1].includes('trace-proj-f') && !listCalls()[listCalls().length - 1].includes('cursor='));
  resolveSlow();
  await tick(30);
  check('switch: late summary from old project dropped', traceUi.summary === null && panelHtml().includes('选择一条调用记录'));
  mcOverride = null;
  clickEl('trace-node-' + C2);
  await tick(40);
  check('switch: new project selection works after isolation', traceUi.selected === C2 && panelHtml().includes('画面质检'));

  // ---------- S10：同项目刷新恢复 ----------
  const listBefore = listCalls().length;
  await openProject('trace-proj-f');
  await tick(60);
  check('refresh: same-project reopen reloads first page', listCalls().length > listBefore && listCalls()[listCalls().length - 1].includes('limit=25') && !listCalls()[listCalls().length - 1].includes('cursor='));
  check('refresh: selection and detail state reset', traceUi.selected === '' && panelHtml().includes('选择一条调用记录'));

  // ---------- S11：全流程零业务写入与路由语义 ----------
  check('read-only: no non-GET request during all trace operations', writes().length === 0);
  check('read-only: every list call uses frozen page size', listCalls().every((u) => u.includes('limit=25')));
  check('read-only: every delta call resumes via stable after with frozen limit', deltaCalls().every((u) => /after=\d+&limit=100/.test(u)));
  check('gate: detail=true requested only under declared host RBAC', detailCalls().length === 2);
  check('gate: trace module never opens an event stream', __ui.eventSources.every((s) => s.url.includes('/events')));

  return results;
}
return __driver();
