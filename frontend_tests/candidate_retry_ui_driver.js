// P1-05 UI 契约 driver：由 candidate_retry_ui_harness.mjs 追加在页面内联脚本之后执行，
// 与页面脚本共享作用域，可直接使用其 state / renderProject / document。
// 覆盖：3/5 部分候选态无 selected_id 提交通路（含伪造 DOM 交互负向）、补跑仍可用、
// 以及 waiting_master_selection 正常选图通路未被破坏（正向对照）。
async function __driver() {
  const results = [];
  const check = (name, cond) => results.push({ name, pass: !!cond });
  const tick = (ms = 25) => new Promise((r) => setTimeout(r, ms));
  const content = __ui.contentEl;
  const bodiesWithSelection = () =>
    __ui.fetchCalls.filter((c) => String((c.options && c.options.body) || '').includes('selected_id'));
  await tick(); // 等待脚本末尾 loadProjects().then(renderHome) 完成初始渲染

  // ---------- 相位一：waiting_candidate_retry（3/5 成功，2 槽待补） ----------
  state.current = {
    project_id: 'ui-partial', capabilities: [], history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 7 }, updated_at: '2026-08-07T00:00:00Z' },
    snapshot: {
      state: 'initial_candidate_generation', phase: 'waiting_candidate_retry', waiting: true, completed: false,
      candidates: [
        { id: 'candidate-1', style_name: '方向一', uri: 'u1' },
        { id: 'candidate-2', style_name: '方向二', uri: 'u2' },
        { id: 'candidate-3', style_name: '方向三', uri: 'u3' },
      ],
      candidate_slots: { succeeded: [0, 1, 2], failed: [3, 4], pending_retry: [3, 4] },
    },
  };
  __ui.getView = () => state.current;
  renderProject();
  const html = content.innerHTML;
  check('partial: renders retry-only action', html.includes('data-action="retry"') && html.includes('仅补跑失败槽位'));
  check('partial: renders 3 succeeded assets as read-only preview', (html.match(/candidate__image/g) || []).length === 3);
  check('partial: no #select-button markup', !html.includes('id="select-button"'));
  check('partial: no [data-candidate] markup', !html.includes('data-candidate='));
  check('partial: no master-selection copy', !html.includes('确认当前主图') && !html.includes('选择一张当前主图'));
  check('partial: #select-button absent from DOM', document.querySelector('#select-button') === null);
  check('partial: zero [data-candidate] nodes', document.querySelectorAll('[data-candidate]').length === 0);

  // 交互负向：伪造 #select-button 点击（如陈旧/注入 DOM）也不得提交 selected_id
  __ui.fetchCalls.length = 0;
  const forged = __ui.makeEl('button'); forged.dataset.selected = 'candidate-1';
  const forgedEvt = { target: { closest: (sel) => (sel === '#select-button' ? forged : null) } };
  for (const h of content._handlers.click || []) await h(forgedEvt);
  for (const h of __ui.docHandlers.click || []) await h(forgedEvt);
  await tick();
  check('partial: forged #select-button click submits no selected_id', bodiesWithSelection().length === 0);

  // 补跑仍可用：点击“仅补跑失败槽位”路由到 retry 端点
  __ui.fetchCalls.length = 0;
  const retryEvt = { target: { closest: (sel) => (sel === '[data-action]' ? { dataset: { action: 'retry' } } : null) } };
  for (const h of __ui.docHandlers.click || []) await h(retryEvt);
  await tick();
  const retryCall = __ui.fetchCalls.find((c) => c.url.endsWith('/api/projects/ui-partial/retry'));
  check('partial: retry click posts to retry endpoint', !!retryCall && String(retryCall.options.method || 'POST').toUpperCase() === 'POST');
  check('partial: retry flow carries no selected_id', bodiesWithSelection().length === 0);

  // ---------- 相位二（正向对照）：waiting_master_selection 选图通路保持可用 ----------
  state.current = {
    project_id: 'ui-master', capabilities: [], history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 9 }, updated_at: '2026-08-07T00:00:00Z' },
    snapshot: {
      state: 'master_candidate_selection', phase: 'waiting_master_selection', waiting: true, completed: false,
      candidates: [0, 1, 2, 3, 4].map((i) => ({ id: 'candidate-' + (i + 1), style_name: '方向' + (i + 1), uri: 'u' + (i + 1) })),
      candidate_slots: { succeeded: [0, 1, 2, 3, 4], failed: [], pending_retry: [] },
    },
  };
  __ui.getView = () => state.current;
  renderProject();
  const masterHtml = content.innerHTML;
  check('master: selection UI rendered', masterHtml.includes('id="select-button"') && masterHtml.includes('选择一张当前主图'));
  const candEls = document.querySelectorAll('[data-candidate]');
  check('master: five selectable candidates', candEls.length === 5);
  __ui.fetchCalls.length = 0;
  const pickEvt = { target: { closest: (sel) => (sel === '[data-candidate]' ? candEls[1] : null) } };
  for (const h of __ui.docHandlers.click || []) await h(pickEvt);
  const btn = document.querySelector('#select-button');
  check('master: picking candidate arms confirm with its id', !!btn && btn.disabled === false && btn.dataset.selected === 'candidate-2');
  const confirmEvt = { target: { closest: (sel) => (sel === '#select-button' ? btn : null) } };
  for (const h of content._handlers.click || []) await h(confirmEvt);
  await tick();
  const selCall = bodiesWithSelection()[0];
  check('master: confirm submits picked selected_id', !!selCall && JSON.parse(selCall.options.body).selected_id === 'candidate-2');
  return results;
}
return __driver();
