// P1-10 统一错误呈现 UI 契约 driver：由 error_ui_harness.mjs 追加在页面内联脚本之后执行，
// 与页面脚本共享作用域，可直接使用其 state / renderProject / errInfo / jobUi / advance 等。
// 覆盖：全部稳定 code 与五类 suggested_action 映射、429/Retry-After、可重试与不可重试按钮差异、
// 部分槽位成功后的恢复呈现、刷新/worker 重启后状态一致、超过上限不再给重试、取消与人工等待差异、
// 缺失可选字段与旧错误对象兼容、敏感详情不进入 DOM、重复点击仅发一次请求、
// HTTP 422 与异步 job failed 的呈现边界。
async function __driver() {
  const results = [];
  const check = (name, cond) => results.push({ name, pass: !!cond });
  const tick = (ms = 25) => new Promise((r) => setTimeout(r, ms));
  const content = __ui.contentEl;
  const toasts = () => document.querySelector('#toasts').children.map((c) => c.textContent || '');
  const clearToasts = () => { document.querySelector('#toasts').children.length = 0; };
  await tick(); // 等待脚本末尾 loadProjects().then(renderHome) 完成初始渲染
  JOB_POLL_MS = 1; // 驱动级加速：页面默认值 800ms

  const PID = 'err-proj';
  const TRACE = 'trace_' + 'a'.repeat(32);
  const JOB_ID = 'job_' + 'b'.repeat(32);
  const JOB_URL = `/api/projects/${PID}/jobs/${JOB_ID}`;
  const stable = (over = {}) => ({
    code: 'UPSTREAM_TIMEOUT', stage: 'initial_candidate_generation', retryable: true,
    suggested_action: 'retry', trace_id: TRACE, detail: '供应商响应超时', ...over,
  });
  const failedView = (err) => ({
    project_id: PID, capabilities: ['retry'], history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 7 }, updated_at: '2026-08-07T12:00:00Z',
      failed_step: { state: 'initial_candidate_generation', error: err, at: '2026-08-07T12:00:00Z' } },
    snapshot: { state: 'initial_candidate_generation', phase: 'generating', waiting: false, completed: false },
  });
  const waitingSpecView = () => ({
    project_id: PID, capabilities: ['confirm_task_spec'], history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 3 }, updated_at: '2026-08-07T12:00:00Z' },
    snapshot: { state: 'confirmation_build', phase: 'waiting_task_spec_confirmation', waiting: true, completed: false },
  });
  const partialSlotsView = () => ({
    project_id: PID, capabilities: [], history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 7 }, updated_at: '2026-08-07T12:00:00Z' },
    snapshot: { state: 'initial_candidate_generation', phase: 'waiting_candidate_retry', waiting: true, completed: false,
      candidates: [1, 2, 3].map((i) => ({ id: 'candidate-' + i, style_name: '方向' + i, uri: 'u' + i })),
      candidate_slots: { succeeded: [0, 1, 2], failed: [3, 4], pending_retry: [3, 4] } },
  });
  const failedJob = (over = {}) => ({
    job_id: JOB_ID, idempotency_key: 'key-12345678', status: 'failed', cancel_requested: false,
    progress: { completed: 2, total: 5, unit: 'candidate' }, heartbeat_at: null,
    created_at: '2026-08-07T12:00:00Z', updated_at: '2026-08-07T12:01:00Z',
    attempt: 1, max_attempts: 3, error: stable({ code: 'PROVIDER_UNAVAILABLE' }), ...over,
  });
  const resetJob = () => {
    jobUi.key = ''; jobUi.jobId = ''; jobUi.statusUrl = ''; jobUi.payload = null;
    jobUi.job = null; jobUi.busy = false; jobUi.cancelling = false;
    jobNoteClear(); sessionStorage.clear(); clearToasts();
  };
  const setView = (view) => { state.current = view; __ui.getView = () => view; renderProject(); };
  const plantWatch = (payload) => {
    jobUi.key = PID; jobUi.jobId = JOB_ID; jobUi.statusUrl = JOB_URL; jobUi.payload = payload;
    jobWatchSave(PID);
    jobUi.key = ''; jobUi.jobId = ''; jobUi.statusUrl = ''; jobUi.payload = null; jobUi.job = null;
  };
  const clickJobRetry = () => {
    const el = document.querySelector('#job-retry');
    for (const h of (el && el._handlers.click) || []) h({ target: el, preventDefault() {} });
  };
  const advancePosts = () => __ui.fetchCalls.filter((c) => /\/advance$/.test(c.url) && String(c.options.method || '').toUpperCase() === 'POST');
  const cancelPosts = () => __ui.fetchCalls.filter((c) => /\/cancel$/.test(c.url) && String(c.options.method || '').toUpperCase() === 'POST');

  // ---------- S1：全部稳定 code × 五类建议动作映射 ----------
  const CODES = [
    ['UPSTREAM_TIMEOUT', 'retry', true], ['RATE_LIMITED', 'retry', true],
    ['AUTHENTICATION_FAILED', 'contact_admin', false], ['CONTENT_REJECTED', 'modify_input', false],
    ['PROVIDER_UNAVAILABLE', 'retry', true], ['ASSET_INGESTION_FAILED', 'retry', true],
    ['STRUCTURED_OUTPUT_INVALID', 'retry', true], ['INVALID_INPUT', 'modify_input', false],
    ['CONFIGURATION_OR_SKILL', 'contact_admin', false], ['CANCELLED', 'none', false],
    ['INTERNAL_ERROR', 'contact_admin', false],
  ];
  for (const [code, action, retryable] of CODES) {
    resetJob();
    setView(failedView(stable({ code, suggested_action: action, retryable })));
    const html = content.innerHTML;
    check(`map ${code}: code rendered`, html.includes(code));
    check(`map ${code}: zh label rendered`, html.includes(ERR_CODE_LABELS[code]));
    check(`map ${code}: stage rendered`, html.includes('候选生成'));
    check(`map ${code}: trace rendered`, html.includes(TRACE));
    check(`map ${code}: sanitized detail rendered`, html.includes('供应商响应超时'));
    check(`map ${code}: action copy rendered`, html.includes(ERR_ACTION_COPY[action]));
    check(`map ${code}: retry button ${action === 'retry' ? 'present' : 'absent'}`,
      html.includes('data-action="retry"') === (action === 'retry'));
  }
  resetJob();
  setView(failedView(stable({ code: 'CANCELLED', suggested_action: 'none', retryable: false, detail: '作业已请求取消' })));
  check('map CANCELLED: neutral cancelled title', content.innerHTML.includes('任务已取消'));

  // ---------- S2：429 / Retry-After 等待提示 ----------
  resetJob();
  setView(failedView(stable({ code: 'RATE_LIMITED', retry_after_seconds: 7.5 })));
  check('429: policy-bounded wait hint with seconds', content.innerHTML.includes('等待约 8 秒'));
  check('429: retry still offered after wait hint', content.innerHTML.includes('data-action="retry"'));

  // ---------- S3：可重试与不可重试按钮差异（显式对照） ----------
  resetJob();
  setView(failedView(stable({ code: 'UPSTREAM_TIMEOUT' })));
  const retryableHtml = content.innerHTML;
  setView(failedView(stable({ code: 'AUTHENTICATION_FAILED', suggested_action: 'contact_admin', retryable: false })));
  const nonRetryableHtml = content.innerHTML;
  check('diff: retryable shows retry entry', retryableHtml.includes('data-action="retry"'));
  check('diff: non-retryable shows no retry entry', !nonRetryableHtml.includes('data-action="retry"'));
  check('diff: non-retryable shows admin copy', nonRetryableHtml.includes(ERR_ACTION_COPY.contact_admin));

  // ---------- S4：部分槽位成功后的恢复呈现 + 槽位/轮次展示 ----------
  resetJob();
  setView(partialSlotsView());
  const partialHtml = content.innerHTML;
  check('slots: partial success renders slot-retry domain action', partialHtml.includes('仅补跑失败槽位'));
  check('slots: partial success is not an error panel', !partialHtml.includes('错误代码'));
  check('slots: succeeded assets are read-only (no selection)', !partialHtml.includes('data-candidate='));
  setView(failedView(stable({ candidate_slot: 3, rework_round: 2 })));
  check('slots: candidate_slot rendered', content.innerHTML.includes('候选槽位') && content.innerHTML.includes('<dd>3</dd>'));
  check('slots: rework_round rendered', content.innerHTML.includes('返工轮次') && content.innerHTML.includes('<dd>2</dd>'));

  // ---------- S5：刷新后状态一致（checkpoint 通道重渲染 + watch 续接为 job 通道） ----------
  resetJob();
  setView(failedView(stable()));
  const firstRender = content.innerHTML;
  renderProject();
  check('refresh: re-render keeps identical error facts', content.innerHTML.includes(TRACE) && content.innerHTML.includes('UPSTREAM_TIMEOUT') === firstRender.includes('UPSTREAM_TIMEOUT'));
  plantWatch({ task_spec_action: 'confirm', actor: 'op', idempotency_key: 'key-12345678' });
  __ui.jobImpl = async () => ({ ok: true, status: 200, json: async () => failedJob() });
  await jobWatchResume(PID);
  check('refresh: watch resume upgrades to job-channel panel', content.innerHTML.includes('id="job-retry"'));
  check('refresh: server attempt counters shown, not inferred', content.innerHTML.includes('已尝试 1/3'));
  check('refresh: failed watch retained for later retry', (sessionStorage.getItem(`job-watch:${PID}`) || '').includes(JOB_ID));

  // ---------- S6：worker 重启后续跑，终态呈现一致 ----------
  resetJob();
  setView(waitingSpecView());
  plantWatch({ task_spec_action: 'confirm', actor: 'op', idempotency_key: 'key-12345678' });
  let restartCalls = 0;
  __ui.jobImpl = async () => {
    restartCalls += 1;
    const job = restartCalls === 1
      ? { status: 'running', cancel_requested: false, progress: { completed: 2, total: 5, unit: 'candidate' } }
      : failedJob({ attempt: 2, max_attempts: 3 });
    return { ok: true, status: 200, json: async () => job };
  };
  await jobWatchResume(PID);
  check('restart: orphaned job re-polled past running state', restartCalls >= 2);
  check('restart: terminal failure rendered after resume', content.innerHTML.includes('错误代码') && content.innerHTML.includes('已尝试 2/3'));
  check('restart: job-channel retry entry available', content.innerHTML.includes('id="job-retry"'));
  __ui.jobImpl = null;

  // ---------- S7：超过上限不再给重试 ----------
  resetJob();
  setView(failedView(stable()));
  plantWatch({ task_spec_action: 'confirm', actor: 'op', idempotency_key: 'key-12345678' });
  __ui.jobImpl = async () => ({ ok: true, status: 200, json: async () => failedJob({ attempt: 3, max_attempts: 3 }) });
  await jobWatchResume(PID);
  check('cap: exhausted attempts hide retry entry', !content.innerHTML.includes('id="job-retry"') && !content.innerHTML.includes('data-action="retry"'));
  check('cap: exhausted copy shown', content.innerHTML.includes('已达到重试次数上限'));
  __ui.jobImpl = null;
  __ui.fetchCalls.length = 0;
  __ui.advanceImpl = async () => ({ ok: true, status: 202, json: async () => ({ job_id: JOB_ID, status: 'queued', created: false, status_url: JOB_URL }) });
  let capPolls = 0;
  __ui.jobImpl = async () => { capPolls += 1; return { ok: true, status: 200, json: async () => failedJob({ attempt: 3, max_attempts: 3 }) }; };
  await advance({ task_spec_action: 'confirm', actor: 'op', idempotency_key: 'key-12345678' }, 'advance');
  check('cap: in-session capped retry shows exhausted panel', content.innerHTML.includes('已达到重试次数上限') && !content.innerHTML.includes('id="job-retry"'));
  check('cap: exactly one re-post, no blind loop', advancePosts().length === 1 && capPolls >= 1);
  __ui.advanceImpl = null; __ui.jobImpl = null;

  // ---------- S8：取消流（cancel_requested → cancelled）与人工等待差异 ----------
  resetJob();
  setView(waitingSpecView());
  let jobState = 'running';
  __ui.jobImpl = async () => ({ ok: true, status: 200, json: async () => ({ status: jobState, cancel_requested: jobState !== 'running', progress: { completed: 1, total: 5, unit: 'candidate' } }) });
  __ui.cancelImpl = async () => { jobState = 'cancel_requested'; return { ok: true, status: 200, json: async () => ({ status: 'cancel_requested' }) }; };
  const cancelFlow = advance({ task_spec_action: 'confirm', actor: 'op', idempotency_key: 'key-cancel-001' }, 'advance');
  for (let i = 0; i < 100 && !(jobUi.noteText && jobUi.noteText.textContent.includes('后端正在处理')); i++) await tick(2);
  check('cancel: real progress note while running', !!jobUi.noteText && jobUi.noteText.textContent.includes('1/5'));
  check('cancel: cancel control offered during run', !!jobUi.cancelEl);
  jobCancel(); jobCancel(); // 重复点击仅发一次请求
  await tick(10);
  check('cancel: double click sends one cancel request', cancelPosts().length === 1);
  jobState = 'cancelled';
  await cancelFlow;
  check('cancel: terminal cancelled toast, no error framing', toasts().some((t) => t.includes('任务已取消')));
  check('cancel: watch cleared after cancellation', sessionStorage.getItem(`job-watch:${PID}`) === null);
  check('cancel: progress note dismissed', jobUi.noteEl === null);
  check('cancel: waiting UI restored without error panel', content.innerHTML.includes('确认任务书并继续') && !content.innerHTML.includes('错误代码'));
  __ui.jobImpl = null; __ui.cancelImpl = null;
  resetJob();
  setView(waitingSpecView());
  const waitingHtml = content.innerHTML;
  check('waiting: shown as normal human todo', waitingHtml.includes('等待你的决定') && waitingHtml.includes('确认任务书并继续'));
  check('waiting: no failure or retry semantics', !waitingHtml.includes('错误代码') && !waitingHtml.includes('data-action="retry"') && !waitingHtml.includes('任务已取消'));

  // ---------- S9：缺失可选字段不破坏呈现 ----------
  resetJob();
  setView(failedView(stable()));
  const minimalHtml = content.innerHTML;
  check('minimal: no slot/round/wait rows when absent', !minimalHtml.includes('候选槽位') && !minimalHtml.includes('返工轮次') && !minimalHtml.includes('等待约'));
  check('minimal: no undefined/null leakage', !minimalHtml.includes('undefined') && !minimalHtml.includes('>null<'));
  const noTrace = stable(); delete noTrace.trace_id;
  setView(failedView(noTrace));
  check('minimal: missing trace_id renders without trace row', !content.innerHTML.includes('<dt>trace</dt>') && content.innerHTML.includes('UPSTREAM_TIMEOUT'));

  // ---------- S10：旧错误对象兼容 ----------
  resetJob();
  setView(failedView({ code: 'ProviderError', message: '供应商返回 500', retryable: true }));
  check('legacy: message rendered with retry entry', content.innerHTML.includes('供应商返回 500') && content.innerHTML.includes('data-action="retry"'));
  setView(failedView({ message: '旧式错误' }));
  check('legacy: message-only object renders with retry entry', content.innerHTML.includes('旧式错误') && content.innerHTML.includes('data-action="retry"'));
  setView(failedView({ code: 'Fatal', message: '不可恢复', retryable: false }));
  check('legacy: non-retryable hides retry entry', content.innerHTML.includes('不可恢复') && !content.innerHTML.includes('data-action="retry"'));
  check('legacy: non-retryable maps to admin copy', content.innerHTML.includes(ERR_ACTION_COPY.contact_admin));

  // ---------- S11：敏感/不可信详情不进入 DOM ----------
  resetJob();
  setView(failedView(stable({ detail: '鉴权失败 <img src=x onerror=alert(1)> token=abc.def', trace_id: 'trace_<script>alert(2)</script>' })));
  const xssHtml = content.innerHTML;
  check('xss: raw img handler never injected', !xssHtml.includes('<img src=x onerror'));
  check('xss: detail escaped', xssHtml.includes('&lt;img'));
  check('xss: raw script in trace never injected', !xssHtml.includes('<script>alert(2)</script>'));

  // ---------- S12：重复点击仅发一次请求 ----------
  resetJob();
  setView(waitingSpecView());
  let releaseAccept;
  __ui.advanceImpl = () => new Promise((r) => { releaseAccept = r; });
  __ui.fetchCalls.length = 0;
  const click1 = advance({ task_spec_action: 'confirm', actor: 'op', idempotency_key: 'key-double-01' }, 'advance');
  const click2 = advance({ task_spec_action: 'confirm', actor: 'op', idempotency_key: 'key-double-01' }, 'advance');
  await tick(10);
  check('double-click: second advance blocked while in flight', advancePosts().length === 1);
  releaseAccept({ ok: true, status: 202, json: async () => ({ job_id: JOB_ID, status: 'queued', created: true, status_url: JOB_URL }) });
  __ui.jobImpl = async () => ({ ok: true, status: 200, json: async () => ({ status: 'succeeded', progress: { completed: 1, total: 1, unit: 'workflow' } }) });
  await Promise.all([click1, click2]);
  __ui.advanceImpl = null; __ui.jobImpl = null;
  resetJob();
  setView(failedView(stable()));
  plantWatch({ task_spec_action: 'confirm', actor: 'op', idempotency_key: 'key-12345678' });
  __ui.jobImpl = async () => ({ ok: true, status: 200, json: async () => failedJob() });
  await jobWatchResume(PID);
  let releaseRetry;
  __ui.advanceImpl = () => new Promise((r) => { releaseRetry = r; });
  __ui.fetchCalls.length = 0;
  clickJobRetry(); clickJobRetry();
  await tick(10);
  check('double-click: retry of original action posts once', advancePosts().length === 1);
  releaseRetry({ ok: true, status: 202, json: async () => ({ job_id: JOB_ID, status: 'queued', created: false, status_url: JOB_URL }) });
  __ui.jobImpl = async () => ({ ok: true, status: 200, json: async () => ({ status: 'succeeded', progress: { completed: 1, total: 1, unit: 'workflow' } }) });
  await tick(30);
  __ui.advanceImpl = null; __ui.jobImpl = null;

  // ---------- S13：HTTP 422 与异步 job failed 的呈现边界 ----------
  resetJob();
  setView(waitingSpecView());
  __ui.advanceImpl = async () => ({ ok: false, status: 422, json: async () => ({ detail: [{ loc: ['body', 'actor'], msg: 'field required' }] }) });
  await advance({ task_spec_action: 'confirm' }, 'advance');
  check('422: surfaced as immediate request error', toasts().some((t) => t.includes('actor') && t.includes('field required')));
  check('422: no job watch persisted', sessionStorage.getItem(`job-watch:${PID}`) === null);
  check('422: no failure panel; waiting UI preserved', !content.innerHTML.includes('错误代码') && content.innerHTML.includes('确认任务书并继续'));
  __ui.advanceImpl = async () => ({ ok: true, status: 202, json: async () => ({ job_id: JOB_ID, status: 'queued', created: true, status_url: JOB_URL }) });
  __ui.jobImpl = async () => ({ ok: true, status: 200, json: async () => failedJob({ error: stable({ code: 'CONTENT_REJECTED', suggested_action: 'modify_input', retryable: false, detail: '包含受限内容' }) }) });
  clearToasts();
  await advance({ task_spec_action: 'confirm', actor: 'op', idempotency_key: 'key-job-fail-1' }, 'advance');
  check('job-failed: failure panel rendered after polling', content.innerHTML.includes('错误代码') && content.innerHTML.includes('CONTENT_REJECTED'));
  check('job-failed: modify_input copy, no retry entry', content.innerHTML.includes(ERR_ACTION_COPY.modify_input) && !content.innerHTML.includes('id="job-retry"'));
  check('job-failed: error toast raised', toasts().some((t) => t.includes('受限内容')));
  __ui.advanceImpl = null; __ui.jobImpl = null;

  // ---------- S14：统一 advance 轮询成功流 + 真实进度 ----------
  resetJob();
  setView(waitingSpecView());
  clearToasts();
  __ui.advanceImpl = async () => ({ ok: true, status: 202, json: async () => ({ job_id: JOB_ID, status: 'queued', created: true, status_url: JOB_URL }) });
  let pollCalls = 0;
  let releaseSecond;
  const secondGate = new Promise((r) => { releaseSecond = r; });
  __ui.jobImpl = async () => {
    pollCalls += 1;
    if (pollCalls >= 2) await secondGate;
    const job = pollCalls >= 2
      ? { status: 'succeeded', progress: { completed: 5, total: 5, unit: 'candidate' } }
      : { status: 'running', cancel_requested: false, progress: { completed: 2, total: 5, unit: 'candidate' } };
    return { ok: true, status: 200, json: async () => job };
  };
  const successFlow = advance({ task_spec_action: 'confirm', actor: 'op', idempotency_key: 'key-success-01' }, 'advance');
  for (let i = 0; i < 100 && !(pollCalls >= 2 && jobUi.noteText && jobUi.noteText.textContent.includes('2/5')); i++) await tick(2);
  check('progress: real server progress shown (2/5)', !!jobUi.noteText && jobUi.noteText.textContent.includes('2/5'));
  releaseSecond();
  await successFlow;
  check('success: saved toast after terminal state', toasts().some((t) => t.includes('已保存到新的工作流检查点')));
  check('success: watch cleared', sessionStorage.getItem(`job-watch:${PID}`) === null);
  check('success: note dismissed and no error panel', jobUi.noteEl === null && !content.innerHTML.includes('错误代码'));
  __ui.advanceImpl = null; __ui.jobImpl = null;

  // ---------- S15：重试复用原 job/动作及幂等语义 ----------
  resetJob();
  setView(failedView(stable()));
  plantWatch({ task_spec_action: 'confirm', actor: 'op', idempotency_key: 'key-12345678' });
  __ui.jobImpl = async () => ({ ok: true, status: 200, json: async () => failedJob() });
  await jobWatchResume(PID);
  check('retry: job-channel retry entry rendered', content.innerHTML.includes('id="job-retry"'));
  clearToasts();
  __ui.fetchCalls.length = 0;
  __ui.advanceImpl = async () => ({ ok: true, status: 202, json: async () => ({ job_id: JOB_ID, status: 'queued', created: false, status_url: JOB_URL }) });
  let releasePoll;
  const pollGate = new Promise((r) => { releasePoll = r; });
  __ui.jobImpl = async () => { await pollGate; return { ok: true, status: 200, json: async () => ({ status: 'succeeded', progress: { completed: 1, total: 1, unit: 'workflow' } }) }; };
  clickJobRetry();
  await tick(15);
  check('retry: re-posts to advance endpoint (same job channel)', advancePosts().length === 1);
  const retryBody = JSON.parse(advancePosts()[0].options.body);
  check('retry: original idempotency key reused', retryBody.idempotency_key === 'key-12345678');
  check('retry: original action payload reused', retryBody.task_spec_action === 'confirm' && retryBody.actor === 'op');
  check('retry: no per-slot replay fields fabricated', !('candidate_slot' in retryBody) && !('slots' in retryBody));
  __ui.getView = () => waitingSpecView();
  releasePoll();
  await tick(40);
  check('retry: success clears watch and confirms save', sessionStorage.getItem(`job-watch:${PID}`) === null && toasts().some((t) => t.includes('已保存到新的工作流检查点')));
  __ui.advanceImpl = null; __ui.jobImpl = null;

  // ---------- S16：无后台轮询 / 无虚假进度 ----------
  resetJob();
  setView(waitingSpecView());
  __ui.fetchCalls.length = 0;
  await tick(60);
  check('no-polling: zero requests without explicit action', __ui.fetchCalls.length === 0);

  return results;
}
return __driver();
