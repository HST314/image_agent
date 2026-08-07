// P1-09 人工回传跨栈探针：在 DOM shim 中驱动前端真实构建回传提交体并输出，
// 由 tests/test_p1_09_delivery_ui.py 用冻结的后端 ManualReturnRequest 契约校验。
async function __probe() {
  const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
  await tick(); // 初始 loadProjects/renderHome

  const ASSET = 'artifact_' + 'f'.repeat(64);
  const SHA = 'a'.repeat(64);
  const delivery = {
    schema_version: '1.1', delivery_version: 1, task_id: 'task-1', design_job_id: 'dlv-proj',
    status: 'ready', return_status: 'pending_return',
    final_image: { artifact_id: ASSET, uri: `artifact://${ASSET}`, sha256: SHA, format: 'png', media_type: 'image/png', width: 37, height: 19, size_bytes: 1234 },
    design_note: '设计理念：留白聚焦\n选择理由：突出清爽\n任务适配点：适配社交媒体投放。',
    design_note_sources: { task: { deliverable_goal: '夏季新品主视觉' }, style: { title: '清透夏日' }, quality_sha256: 'e'.repeat(64) },
    task_confirmation: { actor: 'planner', confirmed_at: '2026-08-07T10:00:00Z', task_spec_version: 2 },
    final_confirmation: { actor: 'reviewer', confirmed_at: '2026-08-07T11:00:00Z', asset_sha256: SHA },
    trace_refs: ['evt-task-1', 'evt-quality-1', 'evt-final-1'],
    source_sha256: 'b'.repeat(64), payload_sha256: 'c'.repeat(64), created_at: '2026-08-07T12:00:00Z',
  };
  __ui.deliveryGetImpl = async () => ({ ok: true, status: 200, json: async () => delivery });
  __ui.deliveryReturnImpl = async (u, o) => ({
    ok: true, status: 200,
    json: async () => ({ delivery_version: 1, actor: 'op-1', target: 'parent-agent', idempotency_key: JSON.parse(o.body).idempotency_key, payload_sha256: 'd'.repeat(64), delivery_payload_sha256: 'c'.repeat(64), returned_at: '2026-08-07T13:00:00Z' }),
  });
  state.current = {
    project_id: 'dlv-proj', capabilities: ['inspect'], history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 9 }, updated_at: '2026-08-07T12:00:00Z' },
    snapshot: { state: 'final_approval', phase: 'delivery_frozen', completed: true, waiting: false, delivery_frozen: true },
  };
  __ui.getView = () => state.current;
  renderProject();
  await tick();

  const set = (id, v) => { const el = document.querySelector('#' + id); el.value = v; for (const h of el._handlers.input || []) h({ target: el }); };
  set('dlv-actor', 'op-1');
  set('dlv-target', 'parent-agent');
  const btn = document.querySelector('#dlv-return-submit');
  for (const h of btn._handlers.click || []) h({ target: btn, preventDefault() {} });
  await tick(60);

  const call = __ui.fetchCalls.filter((c) => /\/delivery\/return$/.test(c.url)).slice(-1)[0];
  const body = call ? JSON.parse(call.options.body) : null;
  return [{ name: 'probe: return payload captured', pass: !!body, body }];
}
return __probe();
