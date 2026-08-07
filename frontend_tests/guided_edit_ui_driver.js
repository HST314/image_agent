// P1-08 圈画微调 UI 契约 driver：由 guided_edit_ui_harness.mjs 追加在页面内联脚本之后执行，
// 与页面脚本共享作用域，可直接使用其 state / renderProject / guidedEdit / ge* 函数。
// 覆盖：坐标落点（横/竖/超宽 + DPR + letterbox）、越界防护、空 Prompt/空标注、撤销/清空、
// 预览与提交一致性、稳定幂等键与重复点击、请求中/失败/成功状态、新图复检且旧确认失效、
// 草稿刷新恢复、文字-only 降级、mock 资产降级与图片载入失败。
async function __driver() {
  const results = [];
  const check = (name, cond) => results.push({ name, pass: !!cond });
  const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
  const content = __ui.contentEl;
  const approx = (a, b) => Math.abs(a - b) < 1e-9;
  GE_POLL_MS = 1;
  await tick(); // 等待脚本末尾 loadProjects().then(renderHome) 完成初始渲染

  const A = 'artifact_' + 'a'.repeat(64);
  const B = 'artifact_' + 'b'.repeat(64);
  const C = 'artifact_' + 'c'.repeat(64);
  const D = 'artifact_' + 'd'.repeat(64);
  const E = 'artifact_' + 'e'.repeat(64);
  const assetOf = (id) => ({ artifact_id: id, uri: `artifact://${id}`, sha256: 'f'.repeat(64) });
  const reworkView = (id, history = []) => ({
    project_id: 'ge-proj', capabilities: [], history,
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 3 }, updated_at: '2026-08-07T00:00:00Z' },
    snapshot: { state: 'human_prompt_iteration', phase: 'waiting_human_rework', waiting: true, completed: false, asset: assetOf(id), current_asset: assetOf(id) },
  });
  const reinspView = (id) => ({
    project_id: 'ge-proj', capabilities: [], history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 4 }, updated_at: '2026-08-07T01:00:00Z' },
    snapshot: { state: 'self_check_iteration', phase: 'waiting_reinspection', waiting: true, completed: false, final_confirmation: null, latest_checked_asset_hash: null, asset: assetOf(id), current_asset: assetOf(id) },
  });
  const setView = (view) => { state.current = view; __ui.getView = () => view; renderProject(); };
  const loadImage = (nw, nh, bw, bh, dpr = 2) => {
    __ui.setDPR(dpr);
    const img = document.querySelector('#ge-image'), stage = document.querySelector('#ge-stage');
    stage.clientWidth = bw; stage.clientHeight = bh;
    img.naturalWidth = nw; img.naturalHeight = nh; img.complete = true;
    for (const h of img._handlers.load || []) h();
  };
  const canvas = () => document.querySelector('#ge-canvas');
  const fire = (t, x, y) => { for (const h of canvas()._handlers[t] || []) h({ offsetX: x, offsetY: y, pointerId: 1, preventDefault() {}, buttons: 1 }); };
  const drag = (x1, y1, x2, y2) => { fire('pointerdown', x1, y1); fire('pointermove', (x1 + x2) / 2, (y1 + y2) / 2); fire('pointermove', x2, y2); fire('pointerup', x2, y2); };
  const setField = (id, v) => { const el = document.querySelector('#' + id); el.value = v; for (const h of el._handlers.input || []) h({ target: el }); };
  const clickEl = (id) => { const el = document.querySelector('#' + id); for (const h of el._handlers.click || []) h({ target: el, preventDefault() {} }); };
  const advanceCalls = () => __ui.fetchCalls.filter((c) => /\/advance$/.test(c.url) && String(c.options.method || '').toUpperCase() === 'POST');
  const lastBody = () => JSON.parse(advanceCalls().slice(-1)[0].options.body);

  // ---------- S1：编辑器挂载与横图画布几何（1600x900，DPR=2，上下 letterbox） ----------
  setView(reworkView(A));
  check('editor: markup mounted for waiting_human_rework', content.innerHTML.includes('id="ge-canvas"') && content.innerHTML.includes('提交圈画微调'));
  loadImage(1600, 900, 800, 520, 2);
  check('editor: image ready with intrinsic size', guidedEdit.ready === true && guidedEdit.naturalW === 1600 && guidedEdit.naturalH === 900);
  check('geometry: canvas backing = content * DPR', canvas().width === 1600 && canvas().height === 900);
  check('geometry: landscape letterbox offset', canvas().style.top === '35px' && canvas().style.left === '0px' && canvas().style.width === '800px');

  // ---------- S2：坐标落点（横图，CSS → source_image_pixels，不受 DPR 影响） ----------
  drag(100, 50, 200, 150);
  const rect1 = guidedEdit.annotations[0] || {};
  check('landscape: rectangle lands on source pixels', rect1.type === 'rectangle' && rect1.x === 200 && rect1.y === 100 && rect1.width === 200 && rect1.height === 200);
  check('landscape: rectangle defaults color/width', rect1.color === '#ff2d55' && rect1.stroke_width === 6);
  clickEl('ge-tool-brush');
  check('toolbar: brush tool armed', guidedEdit.tool === 'brush' && document.querySelector('#ge-tool-brush').getAttribute('aria-pressed') === 'true' && document.querySelector('#ge-tool-rect').getAttribute('aria-pressed') === 'false');
  setField('ge-color', '#00ff00');
  setField('ge-width', '12');
  check('toolbar: color/width applied', guidedEdit.color === '#00ff00' && guidedEdit.width === 12 && document.querySelector('#ge-width-value').textContent.includes('12'));
  drag(10.5, 10.5, 20.5, 20.5);
  const brush1 = guidedEdit.annotations[1] || {};
  check('landscape: brush points land on source pixels', brush1.type === 'brush' && brush1.points[0].x === 21 && brush1.points[0].y === 21 && brush1.points.slice(-1)[0].x === 41 && brush1.points.slice(-1)[0].y === 41);
  check('landscape: brush keeps color/width', brush1.color === '#00ff00' && brush1.stroke_width === 12);
  clickEl('ge-tool-rect');
  drag(200, 150, 100, 50); // 反向拖拽
  const rect2 = guidedEdit.annotations[2] || {};
  check('landscape: reverse drag normalized', rect2.type === 'rectangle' && rect2.x === 200 && rect2.y === 100 && rect2.width === 200 && rect2.height === 200);

  // ---------- S3：撤销 / 清空 ----------
  clickEl('ge-undo');
  check('undo: last annotation removed', guidedEdit.annotations.length === 2);
  clickEl('ge-undo'); clickEl('ge-undo');
  check('undo: empties and disables', guidedEdit.annotations.length === 0 && document.querySelector('#ge-undo').disabled === true && document.querySelector('#ge-clear').disabled === true);
  check('preview: canvas cleared on undo', canvas()._ctx.calls.some((c) => c.op === 'clearRect'));
  drag(100, 50, 200, 150);
  clickEl('ge-clear');
  check('clear: removes all annotations', guidedEdit.annotations.length === 0);

  // ---------- S4：竖图 letterbox（900x1600，左右留白） ----------
  setView(reworkView(B)); loadImage(900, 1600, 800, 520, 2);
  check('geometry: portrait letterbox offset', canvas().style.left === '253.75px' && canvas().style.top === '0px' && canvas().style.width === '292.5px');
  check('geometry: portrait backing size', canvas().width === 585 && canvas().height === 1040);
  drag(0, 0, 146.25, 260);
  const rectP = guidedEdit.annotations[0] || {};
  check('portrait: rectangle lands on source pixels', rectP.x === 0 && rectP.y === 0 && rectP.width === 450 && rectP.height === 800);

  // ---------- S5：超宽图（4000x500） ----------
  setView(reworkView(C)); loadImage(4000, 500, 800, 520, 2);
  check('geometry: ultra-wide letterbox offset', canvas().style.top === '210px' && canvas().style.left === '0px');
  drag(0, 0, 800, 100);
  const rectW = guidedEdit.annotations[0] || {};
  check('ultra-wide: rectangle lands on source pixels', rectW.width === 4000 && rectW.height === 500);

  // ---------- S6：越界防护 ----------
  drag(-50, -50, 9999, 9999);
  const clamped = guidedEdit.annotations[1] || {};
  check('bounds: drag beyond canvas clamped to image bounds', clamped.x === 0 && clamped.y === 0 && clamped.width === 4000 && clamped.height === 500);
  drag(-50, -50, -10, -10);
  check('bounds: fully outside drag commits nothing', guidedEdit.annotations.length === 2);

  // ---------- S7：空 Prompt 校验 ----------
  setField('ge-prompt', '   ');
  setField('ge-actor', 'op-1');
  __ui.fetchCalls.length = 0;
  clickEl('ge-submit');
  await tick();
  check('validate: empty prompt blocked with inline error', document.querySelector('#ge-prompt-error').textContent.length > 0);
  check('validate: empty prompt sends no request', advanceCalls().length === 0);

  // ---------- S8：空标注校验 ----------
  setField('ge-prompt', '改成蓝色');
  clickEl('ge-clear');
  clickEl('ge-submit');
  await tick();
  check('validate: empty annotations blocked with inline error', document.querySelector('#ge-annotation-error').textContent.length > 0);
  check('validate: empty annotations sends no request', advanceCalls().length === 0);

  // ---------- S9：预览与提交一致性 + 冻结契约字段 + 成功流（新图复检、旧确认失效） ----------
  setView(reworkView(A)); loadImage(1600, 900, 800, 520, 2);
  clickEl('ge-tool-rect'); setField('ge-color', '#ff2d55'); setField('ge-width', '6');
  drag(100, 50, 200, 150);
  clickEl('ge-tool-brush'); setField('ge-color', '#00ff00'); setField('ge-width', '12'); drag(300, 200, 350, 260);
  const strokes = canvas()._ctx.calls.filter((c) => c.op === 'stroke');
  check('preview: canvas renders annotations with same style', strokes.some((s) => s.strokeStyle === '#00ff00' && s.lineWidth === 12) && strokes.some((s) => s.strokeStyle === '#ff2d55' && s.lineWidth === 6));
  setField('ge-prompt', '  把圈出的区域改为蓝色  ');
  setField('ge-actor', 'op-1');
  const beforeSubmit = JSON.stringify(guidedEdit.annotations);
  __ui.getView = () => reinspView(A); // 提交成功后 GET 工程返回复检视图
  __ui.fetchCalls.length = 0;
  clickEl('ge-submit');
  await tick(60);
  const body = lastBody();
  const ge = body.guided_edit || {};
  check('submit: annotations identical to preview state', JSON.stringify(ge.annotations) === beforeSubmit);
  check('submit: contract fields frozen', ge.coordinate_space === 'source_image_pixels' && ge.parent_asset_id === A && ge.branch === 'main' && ge.source_width === 1600 && ge.source_height === 900 && ge.round === 1 && ge.actor === 'op-1' && ge.prompt === '把圈出的区域改为蓝色');
  check('submit: stable idempotency key shape', /^guided-edit:[0-9a-f]{16}$/.test(body.idempotency_key || ''));
  check('success: draft cleared after submit', guidedEdit.annotations.length === 0 && !sessionStorage.getItem(`ge-draft:ge-proj:${A}`));
  check('success: project refetched', __ui.fetchCalls.some((c) => /\/api\/projects\/ge-proj$/.test(c.url)));
  check('reinspection: new image forces recheck copy', content.innerHTML.includes('开始重新质检') && content.innerHTML.includes('不会复用旧质检结论'));
  check('reinspection: editor and stale final confirmation gone', !content.innerHTML.includes('id="ge-canvas"') && !content.innerHTML.includes('确认并冻结交付'));

  // ---------- S10：轮次按当前分支连续递增 ----------
  const hist = [
    { type: 'human_rework_completed', branch: 'main', edit: { round: 1 } },
    { type: 'human_rework_completed', branch: 'other', edit: { round: 1 } },
    { type: 'human_rework_completed', branch: 'main' },
  ];
  check('round: counts only guided edits on current branch', geNextRound(hist, 'main') === 2);

  // ---------- S11：请求中 / 重复点击 / 失败 / 稳定幂等键 ----------
  setView(reworkView(A)); loadImage(1600, 900, 800, 520, 2);
  drag(100, 50, 200, 150);
  setField('ge-prompt', '把圈出的区域改为蓝色'); setField('ge-actor', 'op-1');
  __ui.fetchCalls.length = 0;
  __ui.advanceImpl = () => new Promise(() => {}); // 永不返回，保持请求中
  geSubmit(false);
  check('state: submitting shows progress copy and disables submit', guidedEdit.submitting === true && document.querySelector('#ge-submit').disabled === true && document.querySelector('#ge-status').textContent.length > 0);
  geSubmit(false);
  check('state: repeat click while in flight sends no second request', advanceCalls().length === 1);
  guidedEdit.submitting = false; // driver 级复位，进入失败场景
  __ui.advanceImpl = async () => ({ ok: false, status: 409, json: async () => ({ detail: '同一幂等键不能用于不同人工微调请求。' }) });
  await geSubmit(false);
  check('failure: http conflict shows recoverable error', document.querySelector('#ge-status').textContent.includes('同一幂等键'));
  check('failure: annotations preserved and form re-enabled', guidedEdit.annotations.length === 1 && document.querySelector('#ge-submit').disabled === false);
  const key1 = lastBody().idempotency_key;
  await geSubmit(false);
  check('idempotency: same payload keeps stable key across retries', lastBody().idempotency_key === key1);
  setField('ge-prompt', '换一种修改要求');
  await geSubmit(false);
  check('idempotency: changed payload derives new key', lastBody().idempotency_key !== key1);
  __ui.advanceImpl = null;
  __ui.jobImpl = async () => ({ ok: true, status: 200, json: async () => ({ status: 'failed', error: { message: '供应商调用失败' } }) });
  await geSubmit(false);
  check('failure: job failure surfaced without losing draft', document.querySelector('#ge-status').textContent.includes('供应商调用失败') && guidedEdit.annotations.length === 1);
  __ui.jobImpl = null;

  // ---------- S12：草稿刷新恢复（按资产隔离） ----------
  setView(reworkView(D)); loadImage(1600, 900, 800, 520, 2);
  clickEl('ge-tool-rect');
  drag(100, 50, 200, 150);
  setField('ge-prompt', '草稿内容');
  check('draft: persisted per asset', (sessionStorage.getItem(`ge-draft:ge-proj:${D}`) || '').includes('草稿内容'));
  renderProject(); // 模拟刷新后同资产重进
  check('draft: annotations restored after refresh', guidedEdit.annotations.length === 1 && guidedEdit.annotations[0].x === 200);
  check('draft: prompt restored after refresh', document.querySelector('#ge-prompt').value === '草稿内容');
  setView(reworkView(E)); loadImage(1600, 900, 800, 520, 2);
  check('draft: not leaked onto a different asset', guidedEdit.annotations.length === 0);

  // ---------- S13：文字-only 降级路径（保留既有 human_prompt 契约） ----------
  setView(reworkView(A)); loadImage(1600, 900, 800, 520, 2);
  setField('ge-prompt', '仅文字修改'); setField('ge-actor', 'op-1');
  __ui.getView = () => reinspView(A);
  __ui.fetchCalls.length = 0;
  clickEl('ge-text-submit');
  await tick(60);
  const textBody = lastBody();
  check('text-only: submits human_prompt without guided_edit', textBody.human_prompt === '仅文字修改' && !('guided_edit' in textBody));
  check('text-only: stable idempotency key', /^human-rework:[0-9a-f]{16}$/.test(textBody.idempotency_key || ''));

  // ---------- S14：mock / 非受控资产降级 ----------
  const mockView = { project_id: 'ge-proj', capabilities: [], history: [], manifest: { current_branch: 'main', current_checkpoint: { sequence: 3 }, updated_at: '2026-08-07T00:00:00Z' }, snapshot: { state: 'human_prompt_iteration', phase: 'waiting_human_rework', waiting: true, completed: false, asset: { uri: 'mock://offline/0', sha256: 'f'.repeat(64) }, current_asset: { uri: 'mock://offline/0', sha256: 'f'.repeat(64) } } };
  setView(mockView);
  check('degrade: mock asset hides canvas but keeps text rework', !content.innerHTML.includes('id="ge-canvas"') && content.innerHTML.includes('无法圈画') && content.innerHTML.includes('id="ge-text-submit"'));

  // ---------- S15：图片载入失败可恢复提示 ----------
  setView(reworkView(A));
  const img = document.querySelector('#ge-image');
  for (const h of img._handlers.error || []) h();
  check('load-failure: recoverable message shown', document.querySelector('#ge-stage-status').textContent.includes('失败'));

  // ---------- S16：letterbox 纯函数 ----------
  const f1 = geFitRect(1600, 900, 800, 520), f2 = geFitRect(900, 1600, 800, 520), f3 = geFitRect(4000, 500, 800, 520);
  check('fit: landscape', approx(f1.left, 0) && approx(f1.top, 35) && approx(f1.width, 800) && approx(f1.height, 450));
  check('fit: portrait', approx(f2.left, 253.75) && approx(f2.top, 0) && approx(f2.width, 292.5) && approx(f2.height, 520));
  check('fit: ultra-wide', approx(f3.left, 0) && approx(f3.top, 210) && approx(f3.width, 800) && approx(f3.height, 100));

  return results;
}
return __driver();
