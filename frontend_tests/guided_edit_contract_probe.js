// P1-08 跨栈契约探针：由 guided_edit_ui_harness.mjs 追加执行，
// 用真实页面脚本走一遍圈画流程，输出 geBuildBody 产出的提交载荷，
// 供 pytest 用冻结的后端 GuidedEditRequest / AdvanceRequest 校验。
// 返回 [{name, pass, body}]，body 即前端真实提交体（不含 offline 字段，由 pytest 侧补齐）。
async function __probe() {
  const tick = (ms = 25) => new Promise((r) => setTimeout(r, ms));
  await tick();
  const A = 'artifact_' + 'a'.repeat(64);
  const asset = { artifact_id: A, uri: `artifact://${A}`, sha256: 'f'.repeat(64) };
  state.current = {
    project_id: 'ge-proj', capabilities: [],
    history: [{ type: 'human_rework_completed', branch: 'main', edit: { round: 1 } }],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 3 }, updated_at: '2026-08-07T00:00:00Z' },
    snapshot: { state: 'human_prompt_iteration', phase: 'waiting_human_rework', waiting: true, completed: false, asset, current_asset: asset },
  };
  __ui.getView = () => state.current;
  renderProject();
  __ui.setDPR(2);
  const img = document.querySelector('#ge-image'), stage = document.querySelector('#ge-stage');
  stage.clientWidth = 800; stage.clientHeight = 520;
  img.naturalWidth = 1600; img.naturalHeight = 900; img.complete = true;
  for (const h of img._handlers.load || []) h();
  const canvas = document.querySelector('#ge-canvas');
  const fire = (t, x, y) => { for (const h of canvas._handlers[t] || []) h({ offsetX: x, offsetY: y, pointerId: 1, preventDefault() {}, buttons: 1 }); };
  fire('pointerdown', 100, 50); fire('pointermove', 200, 150); fire('pointerup', 200, 150);
  for (const h of document.querySelector('#ge-tool-brush')._handlers.click || []) h({ target: {}, preventDefault() {} });
  fire('pointerdown', 300, 200); fire('pointermove', 350, 260); fire('pointerup', 350, 260);
  const pe = document.querySelector('#ge-prompt'); pe.value = '把圈出的区域改为蓝色';
  for (const h of pe._handlers.input || []) h({ target: pe });
  const ac = document.querySelector('#ge-actor'); ac.value = 'op-1';
  for (const h of ac._handlers.input || []) h({ target: ac });
  const body = geBuildBody(false);
  return [{ name: 'probe', pass: !!body, body }];
}
return __probe();
