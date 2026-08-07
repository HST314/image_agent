// P2-02 历史时间线 UI 契约 driver：由 history_ui_harness.mjs 追加在页面内联脚本之后执行，
// 与页面脚本共享作用域，可直接使用其 state / renderProject / historyUi / hist* 函数。
// 覆盖：概要分页稳定顺序与按需详情、连续切换零写入、固化内容不回填当前值、
// missing/migration_failed/无事实/错误态、历史任务书与图片只读、资产项目作用域代理与降级、
// 重开只读预览/取消无副作用/确认建分支携带 expected_version/409 版本冲突刷新重试/404/422。
async function __driver() {
  const results = [];
  const check = (name, cond) => results.push({ name, pass: !!cond });
  const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
  const content = __ui.contentEl;
  const previewBody = () => __ui.byId('reopen-preview-body').innerHTML;
  await tick(); // 等待脚本末尾 loadProjects().then(renderHome) 完成初始渲染

  const CK1 = 'checkpoints/main/000001-confirmation_build.json';
  const CK2 = 'checkpoints/main/000002-initial_candidate_generation.json';
  const CK3 = 'checkpoints/main/000003-self_check_iteration.json';
  const CKM = 'checkpoints/main/000004-human_prompt_iteration.json';
  const CKX = 'checkpoints/main/000005-final_approval.json';
  const NODE1 = 'history_' + 'a'.repeat(32), NODE2 = 'history_' + 'b'.repeat(32), NODE3 = 'history_' + 'c'.repeat(32);
  const NODEM = 'history_' + 'd'.repeat(32), NODEX = 'history_' + 'e'.repeat(32);
  const A1 = 'artifact_' + '1'.repeat(64), A2 = 'artifact_' + '2'.repeat(64);
  const ok = (data) => ({ ok: true, status: 200, json: async () => data });
  const fail = (status, detail) => ({ ok: false, status, json: async () => ({ detail }) });

  const summary = (node_id, checkpoint, over = {}) => ({
    node_id, checkpoint, availability: 'available', error: null,
    branch: 'main', sequence: 1, state: 'confirmation_build', checksum: 'c'.repeat(64),
    summary: { task_spec_version: 1, fact_kinds: ['task_specification'], asset_count: 1 }, ...over,
  });
  const S1 = summary(NODE1, CK1, {
    sequence: 1, state: 'confirmation_build',
    summary: { task_spec_version: 1, asset_count: 2, fact_kinds: ['task_specification', 'task_spec_confirmation', 'style_cards', 'style_slot_audit', 'candidate_assets', 'inspection', 'model_output_summary', 'human_decision', 'final_confirmation', 'frozen_delivery'] },
  });
  const S2 = summary(NODE2, CK2, { sequence: 2, state: 'initial_candidate_generation', summary: { task_spec_version: 2, fact_kinds: ['task_specification', 'inspection', 'human_decision'], asset_count: 0 } });
  const S3 = summary(NODE3, CK3, { sequence: 3, state: 'self_check_iteration', summary: { task_spec_version: null, fact_kinds: [], asset_count: 0 } });
  const SM = { node_id: NODEM, checkpoint: CKM, availability: 'missing', error: '固化历史文件缺失。' };
  const SX = { node_id: NODEX, checkpoint: CKX, availability: 'migration_failed', error: '不支持的固化格式版本。' };

  const detail = (node_id, checkpoint, over = {}) => ({
    schema_version: 1, project_id: 'hist-proj', node_id, checkpoint,
    availability: 'available', error: null, branch: 'main', sequence: 1,
    state: 'confirmation_build', checksum: 'c'.repeat(64), facts: {}, assets: [], ...over,
  });
  const D1 = detail(NODE1, CK1, {
    sequence: 1, state: 'confirmation_build',
    facts: {
      task_specification: { version: 1, title: 'past <script>alert(1)</script>', markdown: '# 历史任务书\n\n当时固化的正文内容，只允许只读查看。' },
      task_spec_confirmation: { actor: 'planner-1', confirmed_at: '2026-08-07T10:00:00Z', subject_sha256: 'b'.repeat(64), task_spec_version: 1 },
      style_cards: [{ style_index: 'STYLE-001', version: '1', title: '清透夏日', artifact_id: A1 }],
      style_slot_audit: [{ slot: 'STYLE-001', verdict: 'pass', note: '风格与任务匹配' }],
      candidate_assets: [{ artifact_id: A1, id: 'cand-1' }, { artifact_id: A2, id: 'cand-2' }, { artifact_id: 'artifact_越权' }],
      inspection: { passed: false, failed_items: ['主体比例失衡'], round: 2 },
      model_output_summary: { model: 'm1', summary: 'old-model-summary' },
      human_decision: { action: 'rework', actor: 'op-1' },
      final_confirmation: { actor: 'reviewer-1', confirmed_at: '2026-08-07T11:00:00Z' },
      frozen_delivery: { delivery_version: 1 },
    },
    assets: [
      { artifact_id: A1, uri: `artifact://${A1}`, download_path: `/api/projects/hist-proj/assets/${A1}` },
      { artifact_id: A2, uri: `artifact://${A2}`, download_path: `/api/projects/hist-proj/assets/${A2}` },
    ],
  });
  const D2 = detail(NODE2, CK2, { sequence: 2, state: 'initial_candidate_generation', facts: { task_specification: { version: 2, title: 'later' }, inspection: { passed: false }, human_decision: { action: 'rework' } } });
  const D3 = detail(NODE3, CK3, { sequence: 3, state: 'self_check_iteration', facts: {} });
  const DM = { schema_version: 1, project_id: 'hist-proj', node_id: NODEM, checkpoint: CKM, availability: 'missing', error: '固化历史文件缺失。', facts: null, assets: [] };
  const DX = { schema_version: 1, project_id: 'hist-proj', node_id: NODEX, checkpoint: CKX, availability: 'migration_failed', error: '不支持的固化格式版本。', facts: null, assets: [] };
  const details = { [NODE1]: D1, [NODE2]: D2, [NODE3]: D3, [NODEM]: DM, [NODEX]: DX };

  const indexCalls = [];
  let indexFail = false;
  __ui.historyIndexImpl = async (u) => {
    indexCalls.push(u);
    if (indexFail) return fail(503, '后端能力暂不可用：history store down。');
    const cursor = new URLSearchParams(u.split('?')[1]).get('cursor');
    if (!cursor) return ok({ schema_version: 1, project_id: 'hist-proj', items: [S1, S2, S3], next_cursor: 'cursor-2' });
    return ok({ schema_version: 1, project_id: 'hist-proj', items: [SM, SX], next_cursor: null });
  };
  const detailCalls = [];
  let detailOverride = null;
  __ui.historyDetailImpl = async (u) => {
    detailCalls.push(u);
    if (detailOverride) return detailOverride(u);
    const node = u.split('/history/')[1];
    return ok(details[node]);
  };
  const preview = { schema_version: 1, node_id: NODE1, checkpoint: CK1, parent_branch_id: 'branch_' + 'f'.repeat(32), parent_branch: 'main', new_branch: { name: '自动生成名称', parent_branch_id: 'branch_' + 'f'.repeat(32), fork_checkpoint: CK1 }, invalidated_confirmations: ['task_spec_confirmation', 'inspection', 'inspection_history', 'final_confirmation', 'frozen_delivery', 'delivery_frozen', 'quality_disposition'], execution_contract: 'POST /api/projects/{project_id}/branches' };
  const previewCalls = [];
  let previewOverride = null;
  __ui.historyPreviewImpl = async (u, o) => { previewCalls.push({ u, body: JSON.parse(o.body || '{}') }); if (previewOverride) return previewOverride(u, o); return ok(preview); };
  const branchesGetCalls = [];
  let branchVersion = 7;
  __ui.branchesGetImpl = async () => { branchesGetCalls.push(branchVersion); return ok({ project_id: 'hist-proj', version: branchVersion, items: [] }); };
  const branchPostCalls = [];
  let createImpl = null;
  __ui.branchesCreateImpl = async (u, o) => { branchPostCalls.push(JSON.parse(o.body)); if (createImpl) return createImpl(u, o); return fail(500, '未配置创建结果。'); };

  const mkView = (branch) => ({
    project_id: 'hist-proj', capabilities: ['resume', 'branch'], history: [],
    manifest: { current_branch: branch, current_checkpoint: { sequence: 5 }, updated_at: '2026-08-07T12:00:00Z' },
    snapshot: { state: 'confirmation_build', phase: 'waiting_task_spec_confirmation', waiting: true, task_specification: { version: 99, title: 'CURRENT-SHOULD-NOT-LEAK' } },
  });
  const setView = (view) => { state.current = view; __ui.getView = () => view; renderProject(); };
  const setField = (id, v) => { const el = document.querySelector('#' + id); el.value = v; for (const h of el._handlers.input || []) h({ target: el }); };
  const clickEl = (id) => { const el = document.querySelector('#' + id); if (!el) { results.push({ name: 'harness: element #' + id + ' present', pass: false }); return; } for (const h of el._handlers.click || []) h({ target: el, preventDefault() {} }); };
  const histHtml = () => { const h = __ui.histRoot(); return h ? h.innerHTML : ''; };
  const detailHtml = () => histHtml().split('id="hist-detail"')[1] || '';
  const posts = () => __ui.fetchCalls.filter((c) => String(c.options.method || 'GET').toUpperCase() === 'POST');

  // ---------- S1：首页概要按稳定顺序分页加载，不发生详情请求 ----------
  setView(mkView('main'));
  await tick(40);
  check('list: first page fetched with limit and no cursor', indexCalls.length === 1 && indexCalls[0].includes('/history?limit=25') && !indexCalls[0].includes('cursor='));
  check('list: no detail requested before selection', detailCalls.length === 0);
  check('list: stable server order preserved', histHtml().indexOf('hist-node-' + NODE1) > -1 && histHtml().indexOf('hist-node-' + NODE1) < histHtml().indexOf('hist-node-' + NODE2) && histHtml().indexOf('hist-node-' + NODE2) < histHtml().indexOf('hist-node-' + NODE3));
  check('list: node state label shown', histHtml().includes('任务书') && histHtml().includes('候选生成'));
  check('list: branch and sequence shown', histHtml().includes('分支 main') && histHtml().includes('序号 1'));
  check('list: task spec version shown', histHtml().includes('任务书 v1') && histHtml().includes('任务书 v2'));
  check('list: asset count shown', histHtml().includes('2 项资产'));
  check('list: fact kind chips shown', histHtml().includes('VLM 审计') && histHtml().includes('人工决策') && histHtml().includes('冻结交付'));
  check('list: availability badge shown', histHtml().includes('>可用</span>'));
  check('list: more-pages entry offered', histHtml().includes('id="hist-more"') && histHtml().includes('加载更早节点'));
  check('list: current branch labeled as now-context', histHtml().includes('当前分支 main'));

  // ---------- S2：选中节点才按需加载详情，固化内容不回填当前值 ----------
  clickEl('hist-node-' + NODE1);
  await tick();
  check('detail: fetched exactly once on selection', detailCalls.length === 1 && detailCalls[0].includes(NODE1));
  const dh1 = detailHtml();
  check('detail: frozen task spec shown', dh1.includes('past') && dh1.includes('当时固化的正文内容'));
  check('detail: never backfills current manifest/snapshot value', !dh1.includes('CURRENT-SHOULD-NOT-LEAK'));
  check('detail: node meta shown', dh1.includes('所属分支') && dh1.includes('main') && dh1.includes('checksum') && dh1.includes(CK1));
  check('detail: task spec confirmation shown', dh1.includes('planner-1') && dh1.includes('任务书确认'));
  check('detail: style card shown', dh1.includes('STYLE-001') && dh1.includes('清透夏日'));
  check('detail: VLM audit shown', dh1.includes('VLM 审计') && dh1.includes('风格与任务匹配'));
  check('detail: candidates shown', dh1.includes('cand-1') && dh1.includes('cand-2'));
  check('detail: inspection shown', dh1.includes('主体比例失衡'));
  check('detail: model summary shown', dh1.includes('old-model-summary'));
  check('detail: human decision shown', dh1.includes('rework') && dh1.includes('op-1'));
  check('detail: final confirmation and frozen delivery shown', dh1.includes('reviewer-1') && dh1.includes('冻结交付'));
  check('detail: assets via project-scoped proxy', dh1.includes(`/api/projects/hist-proj/assets/${A1}`));
  check('detail: asset image has controlled failure fallback', dh1.includes('onerror=') && dh1.includes('资产加载失败或不可见'));
  check('detail: invalid artifact reference degraded not proxied', dh1.includes('资产引用无效或越权不可见'));
  check('detail: historical task spec read-only (no edit entry)', !/<textarea|<input|contenteditable/i.test(dh1));
  check('detail: reopen entry available for frozen node', dh1.includes('id="hist-reopen"'));
  check('xss: raw script tag never injected', !content.innerHTML.includes('<script>alert(1)</script>') && histHtml().includes('&lt;script&gt;'));

  // ---------- S3：连续切换节点只读零写入 ----------
  clickEl('hist-node-' + NODE2);
  await tick();
  check('switch: second node fetched on demand', detailCalls.length === 2 && detailCalls[1].includes(NODE2));
  check('switch: detail replaced with node-2 frozen facts', detailHtml().includes('later') && !detailHtml().includes('old-model-summary'));
  clickEl('hist-node-' + NODE1);
  await tick();
  check('switch: reselect refetches frozen detail', detailCalls.length === 3 && detailHtml().includes('past'));
  const dcBefore = detailCalls.length, icBefore = indexCalls.length;
  renderProject();
  await tick();
  check('rerender: project re-render never refetches list or detail', detailCalls.length === dcBefore && indexCalls.length === icBefore);

  // ---------- S4：分页加载更多与结束态 ----------
  clickEl('hist-more');
  await tick();
  check('paging: second page requested with opaque cursor', indexCalls.length === 2 && indexCalls[1].includes('cursor=cursor-2'));
  check('paging: items appended in stable order', histHtml().indexOf('hist-node-' + NODE3) < histHtml().indexOf('hist-node-' + NODEM) && histHtml().indexOf('hist-node-' + NODEM) < histHtml().indexOf('hist-node-' + NODEX));
  check('paging: end-of-history state shown', histHtml().includes('已到最早的历史节点') && !histHtml().includes('id="hist-more"'));
  check('paging: no further index request without action', indexCalls.length === 2);

  // ---------- S5/S6/S7：missing / migration_failed / 无事实 ----------
  clickEl('hist-node-' + NODEM);
  await tick();
  const dhM = detailHtml();
  check('missing: explicit missing state shown', dhM.includes('该历史节点已缺失') && dhM.includes('固化历史文件缺失'));
  check('missing: no facts rendered and no current-value fill', dhM.includes('不会用当前数据补画历史') && !dhM.includes('hist-fact'));
  check('missing: no reopen entry', !dhM.includes('id="hist-reopen"'));
  clickEl('hist-node-' + NODEX);
  await tick();
  const dhX = detailHtml();
  check('migration-failed: explicit state and reason shown', dhX.includes('该历史节点迁移失败') && dhX.includes('不支持的固化格式版本'));
  check('migration-failed: no reopen entry', !dhX.includes('id="hist-reopen"'));
  clickEl('hist-node-' + NODE3);
  await tick();
  check('empty-facts: explicit no-fact state shown', detailHtml().includes('该节点没有固化任务书'));
  check('empty-facts: available node still offers reopen', detailHtml().includes('id="hist-reopen"'));

  // ---------- S8：详情接口失败可恢复 ----------
  detailOverride = async () => fail(503, '后端能力暂不可用：detail store down。');
  clickEl('hist-node-' + NODE2);
  await tick();
  check('detail-error: failure state shown', detailHtml().includes('节点详情加载失败') && detailHtml().includes('detail store down'));
  detailOverride = null;
  clickEl('hist-detail-retry');
  await tick();
  check('detail-error: explicit retry recovers', detailHtml().includes('later'));

  // ---------- S9：概要接口失败可恢复 ----------
  indexFail = true;
  clickEl('hist-reload');
  await tick();
  check('list-error: failure state shown', histHtml().includes('历史概要加载失败') && histHtml().includes('history store down'));
  indexFail = false;
  clickEl('hist-retry');
  await tick(40);
  check('list-error: explicit retry recovers first page', histHtml().includes('hist-node-' + NODE1) && histHtml().includes('id="hist-more"'));

  // ---------- S10：浏览/选择全程零写入 ----------
  check('read-only: browsing and selecting issued no write request', posts().length === 0);

  // ---------- S11：重开只读预览与取消无副作用 ----------
  clickEl('hist-node-' + NODE1);
  await tick();
  clickEl('hist-reopen');
  await tick();
  check('preview: read-only preview requested once', previewCalls.length === 1 && previewCalls[0].u.includes(NODE1));
  check('preview: empty name omitted from preview body', Object.keys(previewCalls[0].body).length === 0);
  check('preview: branch version fetched for expected_version gate', branchesGetCalls.length === 1);
  check('preview: parent branch relation shown', previewBody().includes('父分支') && previewBody().includes('main'));
  check('preview: fork checkpoint shown', previewBody().includes(CK1));
  check('preview: invalidated confirmations shown', previewBody().includes('任务书确认') && previewBody().includes('最终确认') && previewBody().includes('冻结交付') && previewBody().includes('质量处置'));
  check('preview: no-side-effect copy shown', previewBody().includes('取消此对话框不产生任何写入'));
  check('preview: confirm enabled after version loaded', document.querySelector('#reopen-confirm').disabled === false);
  clickEl('reopen-cancel');
  await tick(5);
  check('preview-cancel: no branch creation request', branchPostCalls.length === 0);
  check('preview-cancel: no write at all after cancel', posts().length === previewCalls.length);

  // ---------- S12：确认后携带 expected_version 创建子分支并刷新 ----------
  clickEl('hist-reopen');
  await tick();
  setField('reopen-name', 'revision');
  setField('reopen-actor', 'op-9');
  const idxBeforeCreate = indexCalls.length;
  let resolveCreate;
  createImpl = () => new Promise((r) => { resolveCreate = r; });
  clickEl('reopen-confirm');
  await tick(5);
  check('confirm: in-flight copy and duplicate click guard', document.querySelector('#reopen-confirm').disabled === true);
  clickEl('reopen-confirm');
  await tick(5);
  check('confirm: duplicate click sends one request', branchPostCalls.length === 1);
  createImpl = null;
  resolveCreate(ok({ branches: { project_id: 'hist-proj', version: 8, items: [{ name: 'main', current: false }, { name: 'revision', current: true }] }, project: mkView('revision') }));
  await tick(60);
  check('confirm: posts frozen branch contract', branchPostCalls[0].checkpoint === CK1 && branchPostCalls[0].name === 'revision' && branchPostCalls[0].actor === 'op-9' && branchPostCalls[0].expected_version === 7);
  check('confirm: exactly the contract keys sent', Object.keys(branchPostCalls[0]).sort().join(',') === 'actor,checkpoint,expected_version,name');
  check('success: project view refreshed to new branch', histHtml().includes('当前分支 revision'));
  check('success: timeline reloaded from first page', indexCalls.length === idxBeforeCreate + 1 && !indexCalls[indexCalls.length - 1].includes('cursor='));
  check('success: project list refreshed', __ui.fetchCalls.some((c) => /\/api\/projects$/.test(c.url)));

  // ---------- S13：版本冲突提示、自动刷新版本后重试成功 ----------
  clickEl('hist-node-' + NODE1);
  await tick();
  clickEl('hist-reopen');
  await tick();
  setField('reopen-name', 'rev2');
  setField('reopen-actor', 'op-9');
  createImpl = async () => { branchVersion = 9; return fail(409, '分支版本冲突，请刷新后重试。'); };
  clickEl('reopen-confirm');
  await tick();
  check('conflict: version conflict surfaced in dialog', document.querySelector('#reopen-status').textContent.includes('版本冲突'));
  check('conflict: latest version refetched after 409', branchesGetCalls[branchesGetCalls.length - 1] === 9);
  check('conflict: confirm re-enabled for retry', document.querySelector('#reopen-confirm').disabled === false);
  createImpl = async (u, o) => ok({ branches: { project_id: 'hist-proj', version: 10, items: [] }, project: mkView('rev2') });
  clickEl('reopen-confirm');
  await tick(60);
  check('conflict: retry carries refreshed expected_version', branchPostCalls[branchPostCalls.length - 1].expected_version === 9);
  check('conflict: retry success refreshes branch context', histHtml().includes('当前分支 rev2'));

  // ---------- S14：404 / 422 明确提示且对话框保持可恢复 ----------
  clickEl('hist-node-' + NODE1);
  await tick();
  clickEl('hist-reopen');
  await tick();
  setField('reopen-actor', 'op-9');
  createImpl = async () => fail(404, '工程或资源不存在。');
  clickEl('reopen-confirm');
  await tick();
  check('not-found: 404 surfaced and dialog stays recoverable', document.querySelector('#reopen-status').textContent.includes('不存在') && previewBody().includes('父分支'));
  createImpl = async () => fail(422, '请求体未通过 schema 校验。');
  clickEl('reopen-confirm');
  await tick();
  check('invalid: 422 surfaced without closing dialog', document.querySelector('#reopen-status').textContent.includes('校验') && previewBody().includes('父分支'));
  clickEl('reopen-cancel');

  // ---------- S15：预览本身失败的明确状态与重试 ----------
  previewOverride = async () => fail(409, '该历史节点不可用于重开。');
  clickEl('hist-reopen');
  await tick();
  check('preview-error: failure state shown with server reason', previewBody().includes('预览生成失败') && previewBody().includes('不可用于重开'));
  previewOverride = null;
  clickEl('reopen-preview-retry');
  await tick();
  check('preview-error: explicit retry recovers preview', previewBody().includes('父分支'));
  clickEl('reopen-cancel');

  // ---------- S16：表单校验（非法名称/空 actor 零请求） ----------
  clickEl('hist-reopen');
  await tick();
  const postsBefore = branchPostCalls.length;
  setField('reopen-name', 'bad name!');
  setField('reopen-actor', '');
  clickEl('reopen-confirm');
  await tick(5);
  check('validate: invalid name and empty actor blocked inline', document.querySelector('#reopen-name-error').textContent.length > 0 && document.querySelector('#reopen-actor-error').textContent.length > 0);
  check('validate: invalid form sends no creation request', branchPostCalls.length === postsBefore);
  clickEl('reopen-cancel');

  return results;
}
return __driver();
