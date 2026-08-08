// P2-06 健康与诊断台 UI 契约 driver：由 health_ui_harness.mjs 追加在页面内联脚本之后执行，
// 与页面脚本共享作用域，可直接使用其 state / hlthUi / hlth* 函数与 HLTH_POLL_MS。
// 覆盖：六组件及组合状态映射、liveness/readiness 差异、离线/供应商未配置提示、
// 关键 503 与局部退化 200、trace 关联、reader/operator 无诊断入口且直接调用零请求、
// admin 受控读取、敏感信息负向用例、限频轮询去重、错误恢复、页面/项目切换停止与迟到响应隔离、
// 全流程零业务写入。
async function __driver() {
  const results = [];
  const check = (name, cond) => results.push({ name, pass: !!cond });
  const tick = (ms = 25) => new Promise((r) => setTimeout(r, ms));
  const ok = (data) => ({ ok: true, status: 200, json: async () => data });
  const ok503 = (data) => ({ ok: false, status: 503, json: async () => data });
  const fail = (status, detail) => ({ ok: false, status, json: async () => ({ detail }) });
  const clickEl = (id) => { const el = document.querySelector('#' + id); if (!el) { results.push({ name: 'harness: element #' + id + ' present', pass: false }); return; } for (const h of el._handlers.click || []) h({ target: el, preventDefault() {} }); };
  const hlthHtml = () => { const el = __ui.hlthRoot(); return el ? el.innerHTML : ''; };
  const liveCard = () => { const h = hlthHtml(); const i = h.indexOf('id="hlth-live"'); const j = h.indexOf('id="hlth-ready"'); return i >= 0 && j > i ? h.slice(i, j) : ''; };
  const compCard = (name) => { const m = hlthHtml().match(new RegExp('data-hlth-component="' + name + '"[\\s\\S]*?(?=<div class="hlth-component"|$)')); return m ? m[0] : ''; };
  const overallBox = () => { const m = hlthHtml().match(/class="hlth-overall"[\s\S]*?<\/div>/); return m ? m[0] : ''; };
  const writes = () => __ui.fetchCalls.filter((c) => String(c.options.method || 'GET').toUpperCase() !== 'GET');
  const liveGets = () => __ui.fetchCalls.filter((c) => c.url.endsWith('/api/health/live'));
  const readyGets = () => __ui.fetchCalls.filter((c) => c.url.endsWith('/api/health/ready'));
  const diagGets = () => __ui.fetchCalls.filter((c) => c.url.includes('/api/internal/diagnostics/'));
  const setRole = (r) => { globalThis.RUNTIME_SETTINGS_ROLE = r; };
  const T = (seed) => __ui.mkTrace(seed);
  const NOW = '2026-08-08T01:02:03+00:00';
  const comp = (status, code, impact, extras) => ({ status, error_code: code, checked_at: NOW, business_impact: impact, ...(extras || {}) });
  const healthyComps = () => ({
    model_router: comp('healthy', 'OK', '模型路由配置可用于真实调用'),
    worker: comp('healthy', 'OK', 'worker 心跳正常或当前空闲'),
    queue: comp('healthy', 'OK', '队列无超时滞留'),
    storage: comp('healthy', 'OK', '存储可写且容量充足'),
    event_append: comp('healthy', 'OK', '业务事件可持久化'),
    asset_proxy: comp('healthy', 'OK', '受控资产读取可用'),
  });
  const readyPayload = (overall, trace, components) => ({ status: overall, checked_at: NOW, trace_id: trace, components });
  const setReady = (payload, http) => { __ui.readyImpl = async () => (http === 503 ? ok503(payload) : ok(payload)); };
  const openHealth = async () => { clickEl('health-button'); await tick(60); };

  await tick(); // 初始 loadProjects().then(renderHome)

  // ---------- S0：reader 基线——liveness/readiness 分层 + 六组件 + 零写入 ----------
  setRole('reader');
  setReady(readyPayload('ready', T('b0'), healthyComps()), 200);
  await openHealth();
  check('mount: health view mounted as global section', __ui.contentEl.innerHTML.includes('id="hlth-root"'));
  check('mount: page title set', __ui.byId('page-title').textContent === '系统健康与诊断');
  check('load: exactly one liveness GET', liveGets().length === 1);
  check('load: exactly one readiness GET', readyGets().length === 1);
  check('load: zero diagnostics GET', diagGets().length === 0);
  check('live: alive badge and label', liveCard().includes('liveness（进程存活）') && liveCard().includes('>存活</span>'));
  check('live: checked time and trace shown', liveCard().includes(T('b0')) === false && liveCard().includes(T('1')));
  check('live: liveness-vs-readiness difference copy', liveCard().includes('只表示进程存活，不代表依赖可用'));
  check('live: no diagnostics entry inside liveness card', !liveCard().includes('hlth-diag-open'));
  check('ready: overall ready badge with success class', overallBox().includes('全部就绪') && overallBox().includes('badge--success'));
  check('ready: http 200 semantics chip', hlthHtml().includes('HTTP 200 · 局部退化仍返回 200'));
  check('ready: trace_id shown', hlthHtml().includes(T('b0')));
  check('ready: six component cards', (hlthHtml().match(/data-hlth-component="/g) || []).length === 6);
  check('ready: component labels', ['模型路由', 'Worker 心跳', '队列滞留', '存储可写/容量', '事件追加', '资产代理'].every((x) => hlthHtml().includes(x)));
  check('ready: healthy status, code, impact, time per component', compCard('worker').includes('正常') && compCard('worker').includes('OK') && compCard('worker').includes('worker 心跳正常或当前空闲') && compCard('worker').includes('检查时间'));
  check('reader: no diagnostics entry', !hlthHtml().includes('hlth-diag-open'));
  check('reader: admin-only note', hlthHtml().includes('内部诊断详情仅管理员可见'));
  check('reader: role chip', hlthHtml().includes('角色：只读'));
  check('baseline: zero writes so far', writes().length === 0);

  // ---------- S1：六组件独立故障与组合状态映射（关键 503 vs 局部退化 200） ----------
  const CODES = { model_router: 'PROBE_FAILED', worker: 'PROBE_TIMEOUT', queue: 'PROBE_FAILED', storage: 'STORAGE_NOT_WRITABLE', event_append: 'EVENT_APPEND_UNAVAILABLE', asset_proxy: 'ASSET_PROXY_UNAVAILABLE' };
  for (const name of ['model_router', 'worker', 'queue', 'storage', 'event_append', 'asset_proxy']) {
    const critical = name === 'storage' || name === 'event_append';
    const comps = healthyComps();
    comps[name] = comp('unhealthy', CODES[name], '该组件暂不可用');
    setReady(readyPayload(critical ? 'not_ready' : 'degraded', T('f' + name.length + name.charCodeAt(0)), comps), critical ? 503 : 200);
    clickEl('hlth-refresh');
    await tick(60);
    check(`map ${name}: overall ${critical ? 'not_ready' : 'degraded'}`, overallBox().includes(critical ? '未就绪' : '局部退化'));
    check(`map ${name}: overall severity class ${critical ? 'danger' : 'warning'}`, critical ? overallBox().includes('badge--danger') : (overallBox().includes('badge--warning') && !overallBox().includes('badge--danger')));
    check(`map ${name}: http ${critical ? '503' : '200'} chip`, hlthHtml().includes(critical ? 'HTTP 503 · 关键组件（存储/事件追加）不可用' : 'HTTP 200 · 局部退化仍返回 200'));
    check(`map ${name}: failing card unhealthy with stable code`, compCard(name).includes('不可用') && compCard(name).includes(CODES[name]));
    check(`map ${name}: sibling component still healthy (no global crash)`, compCard(name === 'queue' ? 'worker' : 'queue').includes('正常'));
    check(`map ${name}: banner copy ${critical ? 'critical' : 'degraded'}`, hlthHtml().includes(critical ? '关键组件不可用：业务持久化已暂停保护' : '局部退化：部分组件异常，其余能力仍可用'));
  }
  check('map: degraded never rendered as global crash copy', !hlthHtml().includes('全部不可用') && !hlthHtml().includes('服务完全崩溃'));

  // ---------- S2：离线与供应商未配置提示（均不得冒充生产健康） ----------
  const offlineComps = healthyComps();
  offlineComps.model_router = comp('offline', 'OFFLINE_ONLY', '仅离线项目可运行，不代表生产供应商健康');
  setReady(readyPayload('degraded', T('0ff10e'), offlineComps), 200);
  clickEl('hlth-refresh');
  await tick(60);
  check('offline: component badge 离线', compCard('model_router').includes('>离线</span>'));
  check('offline: stable code and label', compCard('model_router').includes('OFFLINE_ONLY') && compCard('model_router').includes('仅离线环境'));
  check('offline: explicit not-production hint', compCard('model_router').includes('这是明确的离线标识，不代表生产供应商健康'));
  check('offline: overall degraded not ready', overallBox().includes('局部退化') && !overallBox().includes('全部就绪'));
  const unconfComps = healthyComps();
  unconfComps.model_router = comp('not_configured', 'MODEL_PROVIDER_NOT_CONFIGURED', '真实模型调用不可用');
  setReady(readyPayload('degraded', T('0c0f16'), unconfComps), 200);
  clickEl('hlth-refresh');
  await tick(60);
  check('not_configured: component badge 未配置', compCard('model_router').includes('>未配置</span>'));
  check('not_configured: stable code and label', compCard('model_router').includes('MODEL_PROVIDER_NOT_CONFIGURED') && compCard('model_router').includes('供应商未配置'));
  check('not_configured: never-misreport hint', compCard('model_router').includes('不会被误报为生产健康'));

  // ---------- S3：liveness/readiness 相互独立 + 非法 trace 不出入口不发请求 ----------
  __ui.liveImpl = async () => { throw new TypeError('network down'); };
  setReady(readyPayload('ready', T('3a'), healthyComps()), 200);
  clickEl('hlth-refresh');
  await tick(60);
  check('independence: liveness error card while readiness ready', liveCard().includes('服务不可达') && overallBox().includes('全部就绪'));
  check('independence: liveness retry entry', liveCard().includes('id="hlth-live-retry"'));
  __ui.liveImpl = null;
  clickEl('hlth-refresh');
  await tick(60);
  check('independence: liveness recovers after manual retry', liveCard().includes('>存活</span>'));
  setRole('admin');
  setReady(readyPayload('ready', 'trace_NOT_A_VALID_ONE', healthyComps()), 200);
  clickEl('hlth-refresh');
  await tick(60);
  const diagBeforeInvalid = diagGets().length;
  check('invalid-trace: no diagnostics entry for malformed trace_id', !hlthHtml().includes('hlth-diag-open'));
  check('invalid-trace: malformed trace not rendered as trace', !hlthHtml().includes('trace_NOT_A_VALID_ONE'));
  await hlthOpenDiagnostics('trace_still_not_valid');
  await tick(30);
  check('invalid-trace: direct call blocked before request', diagGets().length === diagBeforeInvalid);

  // ---------- S4：admin 受控读取 + trace 关联 + 内部内容 XSS 转义 ----------
  const T4 = T('4ad');
  setReady(readyPayload('degraded', T4, { ...healthyComps(), storage: comp('degraded', 'STORAGE_CAPACITY_LOW', '新资产写入存在容量风险') }), 200);
  __ui.diagImpl = async (u) => ok({
    trace_id: T4, checked_at: NOW,
    components: { storage: { exception_type: 'RuntimeError', exception: '/srv/private/projects sk-internal-9 provider raw failure <img src=x onerror=alert(1)>', anchor: '/srv/private/projects', free_bytes: 1024 } },
  });
  clickEl('hlth-refresh');
  await tick(60);
  check('admin: diagnostics entry visible', hlthHtml().includes('id="hlth-diag-open"'));
  check('admin: server ACL disclaimer chip', hlthHtml().includes('仍以服务端 ACL 为最终裁决'));
  clickEl('hlth-diag-open');
  await tick(60);
  check('admin: exactly one diagnostics GET bound to readiness trace', diagGets().length === diagBeforeInvalid + 1 && diagGets().every((c) => c.url.endsWith('/api/internal/diagnostics/' + T4)));
  check('admin: internal detail rendered for authorized read', hlthHtml().includes('内部诊断') && hlthHtml().includes('/srv/private/projects'));
  check('admin: internal content HTML-escaped', hlthHtml().includes('&lt;img') && !hlthHtml().includes('<img src=x'));
  check('admin: diag region labels trace snapshot', hlthHtml().includes('trace ' + T4 + ' 的该次检查快照'));
  clickEl('hlth-diag-close');
  await tick(30);
  check('admin: diag region collapses', !hlthHtml().includes('的该次检查快照') && !hlthHtml().includes('class="hlth-diag"'));

  // ---------- S5：reader/operator 门禁矩阵（直接调用也零请求） ----------
  for (const role of ['reader', 'operator']) {
    setRole(role);
    await openHealth();
    const n = diagGets().length;
    check(`gate ${role}: no diagnostics entry in DOM`, !hlthHtml().includes('hlth-diag-open'));
    check(`gate ${role}: admin-only note shown`, hlthHtml().includes('内部诊断详情仅管理员可见'));
    await hlthOpenDiagnostics(T('5' + role.length + 'e'));
    await tick(30);
    check(`gate ${role}: direct openDiagnostics call issues zero requests`, diagGets().length === n);
  }

  // ---------- S6：admin 诊断 403/404/503 与过期 trace 安全处理 ----------
  setRole('admin');
  await openHealth();
  __ui.diagImpl = async () => fail(403, { code: 'DIAGNOSTICS_FORBIDDEN', message: '无内部诊断权限。' });
  const d403 = diagGets().length;
  clickEl('hlth-diag-open');
  await tick(60);
  check('diag-403: server ACL rejection surfaced', hlthHtml().includes('服务端 ACL 拒绝了诊断读取') && hlthHtml().includes('无内部诊断权限'));
  check('diag-403: no auto retry or bypass copy, single request', hlthHtml().includes('不会重试或绕过') && diagGets().length === d403 + 1);
  __ui.diagImpl = async () => fail(404, { code: 'DIAGNOSTIC_TRACE_NOT_FOUND', message: '诊断记录不存在或已过期。' });
  clickEl('hlth-diag-open');
  await tick(60);
  check('diag-404: expired trace handled', hlthHtml().includes('诊断记录不存在或已过期'));
  check('diag-404: recovery copy points to fresh trace', hlthHtml().includes('重新执行健康检查获取新的 trace_id'));
  __ui.diagImpl = async () => fail(503, { code: 'HEALTH_UNAVAILABLE', message: '诊断暂不可用。' });
  clickEl('hlth-diag-open');
  await tick(60);
  check('diag-503: failure recoverable and scoped', hlthHtml().includes('诊断读取失败') && hlthHtml().includes('公共健康视图不受影响'));
  clickEl('hlth-diag-close');
  await tick(30);

  // ---------- S7：敏感信息负向用例——白名单渲染丢弃非契约字段 ----------
  setRole('admin');
  const junk = { anchor: '/srv/private/projects', free_bytes: 7, stale_jobs: ['job-secret-1'], exception: 'provider raw failure sk-leak-9', stack: 'Traceback (most recent call last)', nested: { token: 'sk-leak-9' } };
  const junkComps = healthyComps();
  junkComps.model_router = comp('healthy', 'OK', '可用<svg onload=alert(1)>', junk);
  junkComps.storage = comp('healthy', 'OK', '存储可写且容量充足', junk);
  setReady(readyPayload('ready', T('7ec'), junkComps), 200);
  await openHealth();
  const pub = hlthHtml();
  check('negative: no absolute paths in public DOM', !pub.includes('/srv/private'));
  check('negative: no secret/token in public DOM', !pub.includes('sk-leak-9'));
  check('negative: no provider raw exception in public DOM', !pub.includes('provider raw failure'));
  check('negative: no stack trace in public DOM', !pub.includes('Traceback'));
  check('negative: no internal-only field names rendered', !pub.includes('stale_jobs') && !pub.includes('free_bytes') && !pub.includes('job-secret-1'));
  check('negative: business_impact XSS escaped', pub.includes('&lt;svg') && !pub.includes('<svg onload'));
  check('negative: whitelisted fields still render correctly', overallBox().includes('全部就绪') && compCard('storage').includes('存储可写且容量充足'));
  check('negative: no diagnostics fetch fired for junk payload', diagGets().filter((c) => c.url.endsWith(T('7ec'))).length === 0);

  // ---------- S8：限频轮询、并发去重与重复点击保护 ----------
  HLTH_POLL_MS = 100000; // 先关闭自动轮询干扰，专测去重与单击语义
  let flights = 0, maxFlight = 0;
  __ui.liveImpl = async () => ok({ status: 'alive', checked_at: NOW, trace_id: T('1') });
  __ui.readyImpl = async () => { flights++; maxFlight = Math.max(maxFlight, flights); await tick(50); flights--; return ok(readyPayload('ready', T('p' + readyGets().length), healthyComps())); };
  clickEl('health-button');
  await tick(20); // 初次加载仍在途
  const inFlightBefore = readyGets().length;
  clickEl('hlth-refresh');
  clickEl('hlth-refresh');
  await tick(120);
  check('poll: concurrent clicks collapsed into the in-flight request', readyGets().length === inFlightBefore);
  check('poll: no concurrency amplification (max in-flight = 1)', maxFlight === 1);
  const beforeSingle = readyGets().length;
  clickEl('hlth-refresh');
  check('poll: manual refresh shows busy state while in flight', hlthHtml().includes('正在刷新…'));
  await tick(120);
  check('poll: single manual refresh issues exactly one readiness GET', readyGets().length === beforeSingle + 1);
  HLTH_POLL_MS = 30; // 恢复自动轮询，验证限频节律
  clickEl('hlth-refresh');
  await tick(80);
  const c1 = readyGets().length;
  await tick(320);
  const gained = readyGets().length - c1;
  check('poll: auto polling continues at bounded cadence', gained >= 1 && gained <= 8);
  check('poll: zero writes during polling', writes().length === 0);

  // ---------- S9：错误恢复（自动轮询自愈） ----------
  __ui.readyImpl = async () => { throw new TypeError('network down'); };
  clickEl('hlth-refresh');
  await tick(80);
  check('recover: readiness error panel', hlthHtml().includes('就绪检查未完成'));
  check('recover: auto-retry copy', hlthHtml().includes('自动轮询会继续重试'));
  check('recover: liveness still alive independently', liveCard().includes('>存活</span>'));
  __ui.readyImpl = async () => ok(readyPayload('ready', T('9ec' + readyGets().length), healthyComps()));
  await tick(200);
  check('recover: auto polling self-heals without manual action', overallBox().includes('全部就绪'));

  // ---------- S10：页面/项目/设置切换停止轮询 + 迟到响应隔离 ----------
  let pendingResolve = null;
  __ui.readyImpl = async () => new Promise((resolve) => { pendingResolve = resolve; });
  clickEl('hlth-refresh');
  await tick(30);
  check('switch: slow readiness request pending', pendingResolve !== null);
  renderHome();
  check('switch: leaving view deactivates health module', hlthUi.active === false && !__ui.contentEl.innerHTML.includes('id="hlth-root"'));
  pendingResolve(ok(readyPayload('not_ready', T('1a7e'), healthyComps())));
  await tick(50);
  check('switch: late response dropped by view token', hlthUi.ready === null && !__ui.contentEl.innerHTML.includes('未就绪'));
  const stoppedAt = readyGets().length + liveGets().length;
  await tick(200);
  check('switch: polling stopped after leaving view', readyGets().length + liveGets().length === stoppedAt);
  __ui.readyImpl = async () => ok(readyPayload('ready', T('a1'), healthyComps()));
  await openHealth();
  const beforeProject = readyGets().length + liveGets().length;
  __ui.getView = () => ({ project_id: 'proj-x', snapshot: { state: 'intake_clarify' }, manifest: {}, capabilities: [], history: [] });
  await openProject('proj-x');
  await tick(200);
  check('switch: project switch stops health polling', readyGets().length + liveGets().length === beforeProject && hlthUi.active === false);
  __ui.getView = null;
  renderHome();
  await tick(30);
  await openHealth();
  const beforeSettings = readyGets().length + liveGets().length;
  clickEl('settings-button');
  await tick(200);
  check('switch: settings view stops health polling', readyGets().length + liveGets().length === beforeSettings && hlthUi.active === false);

  // ---------- S11：全局刷新路由到健康台 ----------
  HLTH_POLL_MS = 15000;
  await openHealth();
  const mark = __ui.fetchCalls.length;
  clickEl('refresh-button');
  await tick(80);
  const routed = __ui.fetchCalls.slice(mark);
  check('refresh: global refresh re-reads health when active', routed.some((c) => c.url.endsWith('/api/health/ready')) && routed.some((c) => c.url.endsWith('/api/health/live')));
  check('refresh: no project reload while health active', !routed.some((c) => c.url.endsWith('/api/projects')));

  // ---------- S12：全流程零业务写入 + 仅触达白名单端点 ----------
  check('writes: zero non-GET requests across all scenarios', writes().length === 0);
  const allowed = (u) => u.endsWith('/api/health') || u.endsWith('/api/health/live') || u.endsWith('/api/health/ready') || u.includes('/api/internal/diagnostics/') || u.endsWith('/api/projects') || u.includes('/api/projects/proj-x') || u.includes('/api/runtime-settings') || u.includes('/event-log') || u.includes('/progress') || u.includes('/model-calls') || u.includes('/history');
  check('writes: health module never touched model/job/asset/business endpoints', __ui.fetchCalls.every((c) => allowed(c.url)));

  return results;
}
return __driver();
