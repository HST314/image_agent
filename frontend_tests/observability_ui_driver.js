// P2-03 运行监督台 UI 契约 driver：由 observability_ui_harness.mjs 追加在页面内联脚本之后执行，
// 与页面脚本共享作用域，可直接使用其 state / renderProject / obsUi / obs* 函数。
// 覆盖：稳定分页与冻结高水位、since 仅取新增、重复/乱序/SSE 重连去重、项目切换取消旧响应、
// 五槽部分成功/失败、单槽 retry、质检多轮/到限、取消、worker 恢复、人工等待非失败、
// 409/422/OBSERVABILITY_UNAVAILABLE/断线态、敏感嵌套样本不进入 DOM、全流程零业务写入。
async function __driver() {
  const results = [];
  const check = (name, cond) => results.push({ name, pass: !!cond });
  const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
  const content = __ui.contentEl;
  const ok = (data) => ({ ok: true, status: 200, json: async () => data });
  const fail = (status, detail) => ({ ok: false, status, json: async () => ({ detail }) });
  OBS_RECONNECT_MS = 5;

  const TRACE = 'trace_' + 'a'.repeat(32);
  const ev = (seq, over = {}) => ({
    event_id: 'evt_' + String(seq).padStart(6, '0'), sequence: seq, event_type: 'step_started',
    phase: 'initial_candidate_generation', job_id: null, slot: null, round: null, status: 'running',
    timestamp: '2026-08-07T12:00:00Z', trace_id: TRACE, retry_count: 0, error_code: null, message: null, ...over,
  });
  const mkEvents = (n, start = 1, over = {}) => Array.from({ length: n }, (_, i) => ev(start + i, over));
  const enc = (after, through) => Buffer.from(JSON.stringify({ v: 1, after, through })).toString('base64url');

  // 遵循冻结契约的假服务端：cursor 固定高水位窗口；since 以当前高水位为上界。
  const logCalls = [];
  let serverEvents = [];
  let logOverride = null;
  __ui.eventLogImpl = async (u) => {
    logCalls.push(u);
    if (logOverride) return logOverride(u);
    const q = new URLSearchParams(u.split('?')[1] || '');
    const limit = parseInt(q.get('limit') || '50', 10);
    const cursor = q.get('cursor');
    const since = q.get('since');
    let after, through;
    if (cursor != null) { const c = JSON.parse(Buffer.from(cursor, 'base64url').toString()); after = c.after; through = c.through; }
    else { after = since != null ? parseInt(since, 10) : 0; through = serverEvents.length; }
    const win = serverEvents.filter((e) => e.sequence > after && e.sequence <= through);
    const page = win.slice(0, limit);
    const next = win.length > limit ? enc(page[page.length - 1].sequence, through) : null;
    return ok({ schema_version: 1, items: page, next_cursor: next, through_sequence: through });
  };
  const prog = (over = {}) => ({
    schema_version: 1, phase: 'initial_candidate_generation', status: 'running',
    job: { job_id: 'job_' + 'a'.repeat(32), status: 'running', attempt: 3, retry_count: 2 },
    work: { completed: 3, total: 5, unit: 'candidate' },
    candidates: { completed_slots: [0, 1, 2], failed_slots: [3], total: 5 },
    quality: { current_round: 2, max_rounds: 4, at_limit: false },
    waiting_for_human: false, through_sequence: 120, ...over,
  });
  const progressCalls = [];
  let progressOverride = null;
  let progressData = prog();
  __ui.progressImpl = async (u) => {
    progressCalls.push(u);
    if (progressOverride) return progressOverride(u);
    return ok(progressData);
  };
  const mkView = (pid) => ({
    project_id: pid, capabilities: ['resume'], history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 5 }, updated_at: '2026-08-07T12:00:00Z' },
    snapshot: { state: 'initial_candidate_generation', phase: 'five_candidate_generation', waiting: false },
  });
  const setView = (view) => { state.current = view; __ui.getView = () => view; renderProject(); };
  const clickEl = (id) => { const el = document.querySelector('#' + id); if (!el) { results.push({ name: 'harness: element #' + id + ' present', pass: false }); return; } for (const h of el._handlers.click || []) h({ target: el, preventDefault() {} }); };
  const obsHtml = () => { const h = __ui.obsRoot(); return h ? h.innerHTML : ''; };
  const logHtml = () => { const h = obsHtml(); const i = h.indexOf('id="obs-log"'); return i > -1 ? h.slice(i) : ''; };
  const progHtml = () => { const h = obsHtml(); const i = h.indexOf('id="obs-progress"'); const j = h.indexOf('id="obs-log"'); return i > -1 ? h.slice(i, j > i ? j : undefined) : ''; };
  const rowCount = () => (logHtml().match(/class="obs-event"/g) || []).length;
  const seqOnce = (n) => (logHtml().match(new RegExp('<span>#' + n + '</span>', 'g')) || []).length;
  const sse = () => __ui.eventSources[__ui.eventSources.length - 1];
  const writes = () => __ui.fetchCalls.filter((c) => String(c.options.method || 'GET').toUpperCase() !== 'GET');
  const logGets = () => logCalls.slice();

  await tick(); // 初始 loadProjects().then(renderHome)

  // ---------- S1：首页稳定分页、冻结高水位捕获、进度与 SSE 建连 ----------
  serverEvents = mkEvents(120);
  setView(mkView('obs-proj-a'));
  await tick(50);
  check('init: first page requested with limit and no cursor/since', logCalls.length === 1 && logCalls[0].includes('/event-log?limit=50') && !logCalls[0].includes('cursor=') && !logCalls[0].includes('since='));
  check('init: progress requested once', progressCalls.length === 1 && progressCalls[0].includes('/progress'));
  check('init: sse opened from zero', __ui.eventSources.length === 1 && sse().url.includes('/events?after=0'));
  check('init: stream connecting badge shown', obsHtml().includes('正在连接实时事件'));
  check('init: 50 rows in stable ascending order', rowCount() === 50 && seqOnce(1) === 1 && seqOnce(50) === 1);
  check('init: frozen high watermark captured from first page', obsUi.throughSequence === 120);
  check('init: load-more offered while pages remain', logHtml().includes('id="obs-more"'));
  check('init: progress renders server work units', progHtml().includes('3/5 候选图'));
  check('init: progress renders slot set summary', progHtml().includes('已完成 3 个 · 失败 1 个 · 共 5 个'));
  check('init: progress renders quality rounds', progHtml().includes('第 2 轮 / 上限 4 轮'));
  check('init: progress renders attempt and retry count', progHtml().includes('第 3 次尝试 · 已重试 2 次'));
  check('init: worker recovery hint shown for resumed job', progHtml().includes('worker 恢复后继续执行'));

  sse().__open();
  await tick(40);
  check('stream: live badge after open', obsHtml().includes('实时事件已连接'));
  check('stream: no since pull before frozen pages exhausted', logCalls.length === 1);

  // ---------- S2：游标分页至冻结水位，期间并发追加不进入窗口 ----------
  serverEvents = serverEvents.concat(mkEvents(10, 121)); // 并发追加 121..130
  clickEl('obs-more');
  await tick();
  check('paging: second page passes opaque cursor and no since', logCalls.length === 2 && logCalls[1].includes('cursor=') && !logCalls[1].includes('since='));
  const decoded = JSON.parse(Buffer.from(new URLSearchParams(logCalls[1].split('?')[1]).get('cursor'), 'base64url').toString());
  check('paging: cursor pins frozen window (after=50 through=120)', decoded.after === 50 && decoded.through === 120);
  check('paging: 100 rows after second page', rowCount() === 100);
  clickEl('obs-more');
  await tick();
  check('paging: all 120 frozen events loaded without gap or dup', rowCount() === 120 && obsUi.seenSeq.size === 120);
  check('paging: end-of-pages state shows frozen watermark', logHtml().includes('已加载到冻结水位 #120') && !logHtml().includes('id="obs-more"'));
  check('paging: concurrently appended events stay out of frozen pages', !logHtml().includes('<span>#121</span>') && obsUi.lastSeq === 120);

  // ---------- S3：since 仅取新增，重复事件被去重 ----------
  clickEl('obs-pull');
  await tick();
  check('since: pull uses since=last confirmed sequence', logCalls[logCalls.length - 1].includes('since=120') && !logCalls[logCalls.length - 1].includes('cursor='));
  check('since: new events appended and watermark advanced', rowCount() === 130 && logHtml().includes('已加载到冻结水位 #130'));
  const dupImpl = async () => ok({ schema_version: 1, items: [ev(125), ev(131)], next_cursor: null, through_sequence: 131 });
  logOverride = dupImpl;
  clickEl('obs-pull');
  await tick();
  logOverride = null;
  check('dedup: duplicate sequence never re-enters list', seqOnce(125) === 1 && obsUi.seenSeq.size === 131 && rowCount() === 131);

  // ---------- S4：SSE 推送去重与乱序插入 ----------
  const beforeSse = rowCount();
  sse().__emit('step_started', ev(132), '132');
  await tick(5);
  check('sse: pushed event appended', rowCount() === beforeSse + 1 && seqOnce(132) === 1);
  check('sse: push triggers progress refresh', progressCalls.length >= 3);
  sse().__emit('step_started', ev(132), '132');
  await tick(5);
  check('sse: duplicate push dropped', rowCount() === beforeSse + 1);
  sse().__emit('step_started', ev(134), '134');
  sse().__emit('step_started', ev(133), '133');
  await tick(5);
  check('sse: out-of-order unseen events inserted in sequence order', obsUi.items[obsUi.items.length - 2].sequence === 133 && obsUi.items[obsUi.items.length - 1].sequence === 134);
  check('sse: last confirmed sequence advanced', obsUi.lastSeq === 134);

  // ---------- S5：断线重连从最后已确认 sequence 续传 ----------
  const oldSrc = sse();
  oldSrc.__error();
  check('reconnect: disconnect state shown', obsHtml().includes('连接已断开，等待自动重连') && logHtml().includes('id="obs-reconnect"'));
  check('reconnect: broken source closed', oldSrc.closed === true);
  await tick(30); // OBS_RECONNECT_MS=5 自动重连
  check('reconnect: new stream resumes from last confirmed sequence', sse() !== oldSrc && sse().url.includes('/events?after=134'));
  oldSrc.__emit('step_started', ev(135), '135'); // 旧流推送不得生效（已关闭）
  await tick(5);
  check('reconnect: stale stream events ignored', rowCount() === 134);
  sse().__open();
  await tick(40);
  check('reconnect: catch-up pull after resume', logCalls[logCalls.length - 1].includes('since=134'));
  sse().__emit('step_started', ev(135), '135');
  await tick(5);
  check('reconnect: post-resume push accepted once', seqOnce(135) === 1 && obsUi.lastSeq === 135);
  check('reconnect: manual reconnect button hidden while live', !logHtml().includes('id="obs-reconnect"'));

  // ---------- S6：项目切换取消旧响应、关闭旧流、旧项目不串页 ----------
  let resolveSlow;
  logOverride = async (u) => {
    if (u.includes('since=')) return new Promise((r) => { resolveSlow = () => r(ok({ schema_version: 1, items: mkEvents(5, 136, { message: 'A-MSG' }), next_cursor: null, through_sequence: 140 })); });
    return ok({ schema_version: 1, items: mkEvents(3, 1, { message: 'B-MSG' }), next_cursor: null, through_sequence: 3 });
  };
  clickEl('obs-pull'); // A 的慢速增量请求，挂起至切换后才返回
  await tick(5);
  check('switch: slow pull in flight before switching', obsUi.pulling === true || resolveSlow != null);
  setView(mkView('obs-proj-b'));
  await tick(50);
  check('switch: previous stream closed on project change', sse().url.includes('after=0') && __ui.eventSources.filter((s) => !s.closed).length === 1);
  check('switch: fresh project loads first page without cursor/since', logCalls[logCalls.length - 1].includes('/event-log?limit=50') && !logCalls[logCalls.length - 1].includes('cursor=') && !logCalls[logCalls.length - 1].includes('since='));
  check('switch: new project rows rendered', rowCount() === 3 && logHtml().includes('B-MSG'));
  if (resolveSlow) { resolveSlow(); await tick(10); }
  check('switch: late response from old project dropped', rowCount() === 3 && !logHtml().includes('A-MSG') && obsUi.lastSeq === 3);
  check('switch: progress refetched for new project', progressCalls[progressCalls.length - 1].includes('obs-proj-b'));
  logOverride = null;

  // ---------- S7：概要进度投影（部分成功/失败槽、retry、质检到限、取消、人工等待） ----------
  progressData = prog({ quality: { current_round: 4, max_rounds: 4, at_limit: true } });
  clickEl('obs-progress-refresh');
  await tick();
  check('progress: quality round limit shown as disposition wait', progHtml().includes('第 4 轮 / 上限 4 轮') && progHtml().includes('已到返工上限，等待人工处置'));
  progressData = prog({ phase: 'waiting_quality_disposition', status: 'waiting', waiting_for_human: true });
  clickEl('obs-progress-refresh');
  await tick();
  check('progress: human wait is a normal waiting state, not failure', progHtml().includes('等待人工处理') && progHtml().includes('人工等待是正常等待态，不是失败') && !progHtml().includes('badge--danger'));
  progressData = prog({ job: { job_id: 'job_' + 'b'.repeat(32), status: 'cancelled', attempt: 1, retry_count: 0 } });
  clickEl('obs-progress-refresh');
  await tick();
  check('progress: cancelled job shown as cancelled', progHtml().includes('已取消'));
  progressData = prog({ job: null, status: 'idle' });
  clickEl('obs-progress-refresh');
  await tick();
  check('progress: no-job state shown', progHtml().includes('当前没有后台任务记录'));
  progressData = prog({ work: { completed: 1, total: 5, unit: 'candidate' }, candidates: { completed_slots: [2], failed_slots: [0, 4], total: 5 } });
  clickEl('obs-progress-refresh');
  await tick();
  const slotOk = (progHtml().match(/obs-slot is-ok/g) || []).length;
  const slotFail = (progHtml().match(/obs-slot is-fail/g) || []).length;
  check('progress: partial success with failed slots rendered truthfully', slotOk === 1 && slotFail === 2 && progHtml().includes('1/5 候选图'));
  check('progress: no fabricated percent/eta/99 anywhere', !progHtml().includes('%') && !progHtml().includes('预计') && !progHtml().includes('99'));
  progressData = prog();

  // ---------- S8：409 / 422 / OBSERVABILITY_UNAVAILABLE / 恢复 ----------
  logOverride = async (u) => u.includes('cursor=') ? fail(409, '事件游标超出允许查询范围。') : ok({ schema_version: 1, items: mkEvents(5, 1), next_cursor: enc(5, 12), through_sequence: 12 });
  setView(mkView('obs-proj-c'));
  await tick(50);
  clickEl('obs-more');
  await tick();
  check('error-409: cursor/range failure surfaced with distinct title', logHtml().includes('事件游标或查询范围已失效') && logHtml().includes('事件游标超出允许查询范围'));
  check('error-409: loaded rows preserved and stale more-button removed', rowCount() === 5 && !logHtml().includes('id="obs-more"'));
  check('error-409: reload action offered', logHtml().includes('id="obs-log-retry"') && logHtml().includes('重载事件日志'));
  logOverride = null;
  serverEvents = mkEvents(5, 1);
  clickEl('obs-log-retry');
  await tick(40);
  check('error-409: reload recovers from first page', rowCount() === 5 && seqOnce(1) === 1 && logCalls[logCalls.length - 1].includes('/event-log?limit=50'));

  logOverride = async () => fail(422, [{ loc: ['query', 'limit'], msg: 'Input should be less than or equal to 100' }]);
  setView(mkView('obs-proj-c2'));
  await tick(50);
  check('error-422: parameter error surfaced distinctly', logHtml().includes('事件查询参数未通过校验') && logHtml().includes('limit'));
  logOverride = null;

  const UNAVAILABLE = { code: 'OBSERVABILITY_UNAVAILABLE', trace_id: TRACE, message: '事件或进度数据暂不可读取。' };
  logOverride = async () => fail(503, UNAVAILABLE);
  progressOverride = async () => fail(503, UNAVAILABLE);
  setView(mkView('obs-proj-c3'));
  await tick(50);
  check('error-503: log shows unavailable message with trace_id', logHtml().includes('事件或进度数据暂不可读取') && logHtml().includes(TRACE));
  check('error-503: progress shows unavailable message with trace_id', progHtml().includes('进度读取失败') && progHtml().includes(TRACE));
  logOverride = null;
  progressOverride = null;
  progressData = prog();
  clickEl('obs-progress-retry');
  await tick();
  check('error-503: progress retry recovers', progHtml().includes('3/5 候选图'));

  // ---------- S9：敏感嵌套样本不进入 DOM，未知字段被白名单丢弃 ----------
  serverEvents = [ev(1, {
    message: '现场标记 <script>alert(1)</script> 已脱敏',
    raw_payload: 'sk-super-secret',
    provider_response: { signed_url: 'https://vendor.example/x?signature=secret', body: 'raw' },
    local_path: '/srv/private/projects/customer/file.png',
    person: { email: 'alice@example.com', phone: '13800138000' },
    error_detail: 'Authorization: Bearer hidden /etc/passwd',
  })];
  progressData = prog({ internal_note: 'sk-prog-secret', vendor_raw: 'https://vendor.example/raw' });
  setView(mkView('obs-proj-d'));
  await tick(50);
  const all = content.innerHTML + obsHtml();
  check('sensitive: xss payload escaped not executable', !all.includes('<script>alert(1)</script>') && all.includes('&lt;script&gt;'));
  check('sensitive: api key never enters DOM', !all.includes('sk-super-secret') && !all.includes('sk-prog-secret'));
  check('sensitive: supplier payload/url never enters DOM', !all.includes('vendor.example') && !all.includes('signature=secret'));
  check('sensitive: local path never enters DOM', !all.includes('/srv/') && !all.includes('/etc/passwd'));
  check('sensitive: pii and authorization never enter DOM', !all.includes('alice@example.com') && !all.includes('13800138000') && !all.includes('Bearer'));
  check('sensitive: unknown event fields dropped at normalize', !('raw_payload' in obsUi.items[0]) && !('provider_response' in obsUi.items[0]) && !('local_path' in obsUi.items[0]));

  // ---------- S10：刷新同项目重建读取状态 ----------
  const esBefore = __ui.eventSources.length;
  const logsBefore = logCalls.length;
  await openProject('obs-proj-d');
  await tick(60);
  check('refresh: same-project reopen rebuilds log from first page', logCalls.length > logsBefore && logCalls[logCalls.length - 1].includes('/event-log?limit=50'));
  check('refresh: stream recreated for reopened project', __ui.eventSources.length > esBefore && __ui.eventSources.filter((s) => !s.closed).length === 1);

  // ---------- S11：全流程零业务写入 ----------
  check('read-only: no non-GET request during all browse/paging/pull/sse operations', writes().length === 0);
  check('read-only: every event-log call is a bounded GET', logGets().every((u) => u.includes('limit=50')));

  return results;
}
return __driver();
