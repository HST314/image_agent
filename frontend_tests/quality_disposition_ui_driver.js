// P1-07 轮数上限分流 UI 契约 driver：由 quality_disposition_ui_harness.mjs 追加在页面内联脚本之后执行，
// 与页面脚本共享作用域，可直接使用其 state / renderProject / JOB_POLL_MS。
// 覆盖：三个分流按钮真实点击的请求体契约（quality_action/actor/expense_confirmed/idempotency_key）、
// continue 费用确认的确认/取消路径、操作者录入取消路径、重复点击保护，以及全程零异常/零控制台错误。
// 回归目标：click 委托闭包内局部常量遮蔽全局 confirm 导致 continue_generation 不可达（TypeError）。
async function __driver() {
  const results = [];
  const check = (name, cond) => results.push({ name, pass: !!cond });
  const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
  const content = __ui.contentEl;
  JOB_POLL_MS = 1;

  const consoleErrors = [];
  const origConsoleError = console.error;
  console.error = (...args) => { consoleErrors.push(args.map(String).join(' ')); };

  let promptResponse = 'op-1';
  const confirmCalls = [];
  let confirmResponse = true;
  globalThis.prompt = () => promptResponse;
  globalThis.confirm = (msg) => { confirmCalls.push(String(msg)); return confirmResponse; };

  await tick(); // 等待脚本末尾 loadProjects().then(renderHome) 完成初始渲染

  const A = 'artifact_' + 'a'.repeat(64);
  const assetOf = (id) => ({ artifact_id: id, uri: `artifact://${id}`, sha256: 'f'.repeat(64) });
  const dispView = () => ({
    project_id: 'qd-proj',
    capabilities: ['continue_generation', 'manual_rework', 'abandon'],
    history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 2 }, updated_at: '2026-08-08T00:00:00Z' },
    snapshot: {
      state: 'self_check_iteration', phase: 'waiting_quality_disposition', waiting: true, completed: false,
      round: 2, cumulative_rounds: 2, quality_cycle: 1,
      failed_items: ['标题对比度不足'],
      asset: assetOf(A), current_asset: assetOf(A),
    },
  });
  const setView = (view) => { state.current = view; __ui.getView = () => view; renderProject(); };

  // 真实 click 委托：构造 target.closest 命中 [data-quality] 按钮的合成事件，捕获同步异常
  const clickQuality = async (action) => {
    const btn = __ui.makeEl('quality-button');
    btn.dataset.quality = action;
    const evt = { target: { closest: (sel) => (sel === '[data-quality]' ? btn : null) } };
    let thrown = null;
    for (const h of __ui.docHandlers.click || []) {
      try { await h(evt); } catch (e) { thrown = thrown || e; }
    }
    await tick();
    return thrown;
  };
  const advanceCalls = () => __ui.fetchCalls.filter((c) => /\/advance$/.test(c.url) && String(c.options.method || '').toUpperCase() === 'POST');
  const lastBody = () => { const c = advanceCalls(); return c.length ? JSON.parse(c.slice(-1)[0].options.body) : null; };

  // ---------- S1：分流面板渲染 ----------
  setView(dispView());
  const html = content.innerHTML;
  check('panel: renders all three disposition actions', html.includes('data-quality="continue_generation"') && html.includes('data-quality="manual_rework"') && html.includes('data-quality="abandon"'));
  check('panel: continue action marked as requiring new fee confirmation', html.includes('继续生成（需确认新费用）'));
  check('panel: failed items listed', html.includes('标题对比度不足'));

  // ---------- S2：manual_rework 真实点击 ----------
  __ui.fetchCalls.length = 0; confirmCalls.length = 0;
  let thrown = await clickQuality('manual_rework');
  check('manual_rework: click raises no error', thrown === null);
  check('manual_rework: exactly one advance request', advanceCalls().length === 1);
  let body = lastBody();
  check('manual_rework: request body contract', !!body && body.quality_action === 'manual_rework' && body.actor === 'op-1' && body.expense_confirmed === true && /^quality-manual_rework-\d+$/.test(body.idempotency_key || ''));
  check('manual_rework: fee dialog not shown', confirmCalls.length === 0);

  // ---------- S3：abandon 真实点击 ----------
  __ui.fetchCalls.length = 0; confirmCalls.length = 0;
  thrown = await clickQuality('abandon');
  check('abandon: click raises no error', thrown === null);
  check('abandon: exactly one advance request', advanceCalls().length === 1);
  body = lastBody();
  check('abandon: request body contract', !!body && body.quality_action === 'abandon' && body.actor === 'op-1' && body.expense_confirmed === true && /^quality-abandon-\d+$/.test(body.idempotency_key || ''));
  check('abandon: fee dialog not shown', confirmCalls.length === 0);

  // ---------- S4：continue_generation 费用确认·确认路径 ----------
  __ui.fetchCalls.length = 0; confirmCalls.length = 0; confirmResponse = true;
  thrown = await clickQuality('continue_generation');
  check('continue: click raises no error', thrown === null);
  check('continue: fee dialog shown once with paid-budget copy', confirmCalls.length === 1 && confirmCalls[0].includes('付费预算段'));
  check('continue: exactly one advance request after confirm', advanceCalls().length === 1);
  body = lastBody();
  check('continue: request body contract', !!body && body.quality_action === 'continue_generation' && body.actor === 'op-1' && body.expense_confirmed === true && /^quality-continue_generation-\d+$/.test(body.idempotency_key || ''));

  // ---------- S5：continue_generation 费用确认·取消路径 ----------
  __ui.fetchCalls.length = 0; confirmCalls.length = 0; confirmResponse = false;
  thrown = await clickQuality('continue_generation');
  check('continue cancel: click raises no error', thrown === null);
  check('continue cancel: fee dialog shown once', confirmCalls.length === 1);
  check('continue cancel: no advance request sent', advanceCalls().length === 0);
  confirmResponse = true;

  // ---------- S6：操作者录入取消（任何分流动作都不发请求、不弹费用确认） ----------
  promptResponse = '';
  __ui.fetchCalls.length = 0; confirmCalls.length = 0;
  thrown = await clickQuality('continue_generation');
  check('actor cancel: click raises no error', thrown === null);
  check('actor cancel: no advance request sent', advanceCalls().length === 0);
  check('actor cancel: fee dialog never reached', confirmCalls.length === 0);
  thrown = await clickQuality('manual_rework');
  check('actor cancel: manual_rework also sends no request', thrown === null && advanceCalls().length === 0);
  promptResponse = 'op-1';

  // ---------- S7：重复点击保护（首个请求在途时第二次点击不产生新请求） ----------
  __ui.fetchCalls.length = 0; confirmCalls.length = 0;
  __ui.advanceImpl = () => new Promise(() => {}); // 永不返回，保持在途
  thrown = await clickQuality('manual_rework');
  const thrown2 = await clickQuality('manual_rework');
  check('double click: no error on either click', thrown === null && thrown2 === null);
  check('double click: exactly one advance request while in flight', advanceCalls().length === 1);
  __ui.advanceImpl = null;
  jobUi.busy = false; // driver 级复位（在途守卫由 advance 负责，本场景仅验证不产生第二请求）

  // ---------- S8：全程零控制台错误 ----------
  check('console: zero console.error across all scenarios', consoleErrors.length === 0);
  console.error = origConsoleError;

  return results;
}
return __driver();
