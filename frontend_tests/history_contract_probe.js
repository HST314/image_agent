// P2-02 历史重开跨栈探针：在 DOM shim 中驱动前端真实构建“从此重开”提交体并输出，
// 由 tests/test_p2_02_history_ui.py 用冻结的后端 BranchRequest / HistoryReopenPreviewRequest 契约校验。
async function __probe() {
  const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
  await tick(); // 初始 loadProjects/renderHome

  const NODE1 = 'history_' + 'a'.repeat(32);
  const CK1 = 'checkpoints/main/000001-confirmation_build.json';
  const A1 = 'artifact_' + '1'.repeat(64);
  const ok = (d) => ({ ok: true, status: 200, json: async () => d });
  __ui.historyIndexImpl = async () => ok({
    schema_version: 1, project_id: 'hist-proj', next_cursor: null,
    items: [{ node_id: NODE1, checkpoint: CK1, availability: 'available', error: null, branch: 'main', sequence: 1, state: 'confirmation_build', checksum: 'c'.repeat(64), summary: { task_spec_version: 1, fact_kinds: ['task_specification'], asset_count: 1 } }],
  });
  __ui.historyDetailImpl = async () => ok({
    schema_version: 1, project_id: 'hist-proj', node_id: NODE1, checkpoint: CK1, availability: 'available', error: null,
    branch: 'main', sequence: 1, state: 'confirmation_build', checksum: 'c'.repeat(64),
    facts: { task_specification: { version: 1, title: 'past' } },
    assets: [{ artifact_id: A1, uri: `artifact://${A1}`, download_path: `/api/projects/hist-proj/assets/${A1}` }],
  });
  __ui.historyPreviewImpl = async () => ok({
    schema_version: 1, node_id: NODE1, checkpoint: CK1,
    parent_branch_id: 'branch_' + 'f'.repeat(32), parent_branch: 'main',
    new_branch: { name: '自动生成名称', parent_branch_id: 'branch_' + 'f'.repeat(32), fork_checkpoint: CK1 },
    invalidated_confirmations: ['task_spec_confirmation', 'inspection', 'final_confirmation'],
    execution_contract: 'POST /api/projects/{project_id}/branches',
  });
  __ui.branchesGetImpl = async () => ok({ project_id: 'hist-proj', version: 7, items: [] });
  let captured = null;
  __ui.branchesCreateImpl = async (u, o) => {
    captured = JSON.parse(o.body);
    return ok({ branches: { project_id: 'hist-proj', version: 8, items: [] }, project: { project_id: 'hist-proj', capabilities: [], history: [], manifest: { current_branch: 'revision', current_checkpoint: { sequence: 1 } }, snapshot: { state: 'confirmation_build' } } });
  };

  const view = { project_id: 'hist-proj', capabilities: ['branch'], history: [], manifest: { current_branch: 'main', current_checkpoint: { sequence: 1 } }, snapshot: { state: 'confirmation_build', phase: 'waiting_task_spec_confirmation' } };
  state.current = view;
  __ui.getView = () => view;
  renderProject();
  await tick(40);

  const node = document.querySelector('#hist-node-' + NODE1);
  for (const h of node._handlers.click || []) h({ target: node, preventDefault() {} });
  await tick();
  const reopen = document.querySelector('#hist-reopen');
  for (const h of reopen._handlers.click || []) h({ target: reopen, preventDefault() {} });
  await tick();
  const set = (id, v) => { const el = document.querySelector('#' + id); el.value = v; for (const h of el._handlers.input || []) h({ target: el }); };
  set('reopen-name', 'revision');
  set('reopen-actor', 'op-9');
  const btn = document.querySelector('#reopen-confirm');
  for (const h of btn._handlers.click || []) h({ target: btn, preventDefault() {} });
  await tick(60);

  const previewCall = __ui.fetchCalls.filter((c) => /reopen-preview$/.test(c.url)).slice(-1)[0];
  const previewBody = previewCall ? JSON.parse(previewCall.options.body || '{}') : null;
  return [{ name: 'probe: reopen payloads captured', pass: !!captured && previewBody !== null, body: captured, preview_body: previewBody }];
}
return __probe();
