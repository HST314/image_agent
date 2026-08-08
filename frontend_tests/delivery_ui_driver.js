// P1-09 交付与人工回传 UI 契约 driver：由 delivery_ui_harness.mjs 追加在页面内联脚本之后执行，
// 与页面脚本共享作用域，可直接使用其 state / renderProject / deliveryUi / dlv* 函数。
// 覆盖：未生成/生成中/失败可重试/待回传/已回传、三段说明与资产元数据完整呈现、
// checkpoint 不可读时仅靠 Delivery 渲染、重复点击单请求与同载荷稳定幂等键、
// 回传成功展示 actor/时间/目标/版本、409 与网络失败可恢复、版本变化不沿用旧回传 UI 状态、
// XSS 转义与仅显式动作触发请求（无后台轮询）。
async function __driver() {
  const results = [];
  const check = (name, cond) => results.push({ name, pass: !!cond });
  const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
  const content = __ui.contentEl;
  await tick(); // 等待脚本末尾 loadProjects().then(renderHome) 完成初始渲染

  const ASSET = 'artifact_' + 'f'.repeat(64);
  const SHA = 'a'.repeat(64);
  const mkDelivery = (version, over = {}) => ({
    schema_version: '1.1', delivery_version: version, task_id: 'task-1', design_job_id: 'dlv-proj',
    status: 'ready', return_status: 'pending_return',
    final_image: { artifact_id: ASSET, uri: `artifact://${ASSET}`, sha256: SHA, format: 'png', media_type: 'image/png', width: 37, height: 19, size_bytes: 1234 },
    design_note: '设计理念：用留白和冷暖对比聚焦新品\n选择理由：突出清爽与新品感\n任务适配点：围绕夏季新品主视觉，适配社交媒体投放；最终图已通过质检。',
    design_note_sources: { task: { deliverable_goal: '夏季新品主视觉' }, style: { title: '清透夏日' }, quality_sha256: 'e'.repeat(64) },
    task_confirmation: { actor: 'planner', confirmed_at: '2026-08-07T10:00:00Z', task_spec_version: 2 },
    final_confirmation: { actor: 'reviewer', confirmed_at: '2026-08-07T11:00:00Z', asset_sha256: SHA },
    trace_refs: ['evt-task-1', 'evt-quality-1', 'evt-final-1'],
    source_sha256: 'b'.repeat(64), payload_sha256: 'c'.repeat(64), created_at: '2026-08-07T12:00:00Z',
    ...over,
  });
  const recordOf = (version, actor, target) => ({ delivery_version: version, actor, target, idempotency_key: 'delivery-return:0000000000000000', payload_sha256: 'd'.repeat(64), delivery_payload_sha256: 'c'.repeat(64), returned_at: '2026-08-07T13:00:00Z' });
  const completedView = () => ({
    project_id: 'dlv-proj', capabilities: ['inspect', 'branch'], history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 9 }, updated_at: '2026-08-07T12:00:00Z' },
    snapshot: {
      state: 'final_approval', phase: 'delivery_frozen', completed: true, waiting: false, delivery_frozen: true,
      final_asset: { artifact_id: ASSET, uri: `artifact://${ASSET}`, sha256: SHA },
      final_confirmation: { actor: 'reviewer', confirmed_at: '2026-08-07T11:00:00Z', asset_sha256: SHA },
    },
  });
  const minimalView = () => ({ // checkpoint 缺失任务书/最终资产等明细，仅剩完成事实
    project_id: 'dlv-proj', capabilities: ['inspect'], history: [],
    manifest: { current_branch: 'main', current_checkpoint: { sequence: 9 }, updated_at: '2026-08-07T12:00:00Z' },
    snapshot: { state: 'final_approval', phase: 'delivery_frozen', completed: true, waiting: false, delivery_frozen: true },
  });
  const setView = (view) => { state.current = view; __ui.getView = () => view; renderProject(); };
  const freshView = (view) => { dlvReset(); setView(view); };
  const setField = (id, v) => { const el = document.querySelector('#' + id); el.value = v; for (const h of el._handlers.input || []) h({ target: el }); };
  const clickEl = (id) => { const el = document.querySelector('#' + id); for (const h of el._handlers.click || []) h({ target: el, preventDefault() {} }); };
  const dlvGets = () => __ui.fetchCalls.filter((c) => /\/delivery$/.test(c.url) && String(c.options.method || 'GET').toUpperCase() === 'GET');
  const genCalls = () => __ui.fetchCalls.filter((c) => /\/delivery\/generate$/.test(c.url));
  const retCalls = () => __ui.fetchCalls.filter((c) => /\/delivery\/return$/.test(c.url));
  const lastRetBody = () => JSON.parse(retCalls().slice(-1)[0].options.body);

  // ---------- S1：未生成状态（GET 409 → 显式生成入口，最终图与确认事实保留） ----------
  freshView(completedView());
  await tick();
  check('empty: generate entry mounted', content.innerHTML.includes('id="dlv-generate"'));
  check('empty: not-generated copy shown', content.innerHTML.includes('尚未生成'));
  check('empty: final image still visible', content.innerHTML.includes(`/assets/${ASSET}`));
  check('empty: final confirmation facts still visible', content.innerHTML.includes('reviewer'));
  check('empty: delivery fetched exactly once', dlvGets().length === 1);
  renderProject();
  check('empty: re-render does not refetch delivery', dlvGets().length === 1);
  check('empty: no return form before generation', !content.innerHTML.includes('dlv-return-submit'));

  // ---------- S2：生成中防重复点击 + 生成成功完整呈现 ----------
  let resolveGen;
  __ui.deliveryGenerateImpl = () => new Promise((r) => { resolveGen = r; });
  clickEl('dlv-generate');
  await tick(5);
  check('generating: progress copy and disabled button', content.innerHTML.includes('正在生成说明') && /id="dlv-generate" disabled/.test(content.innerHTML));
  clickEl('dlv-generate');
  check('generating: repeat click sends no second request', genCalls().length === 1);
  __ui.deliveryGenerateImpl = null;
  resolveGen({ ok: true, status: 200, json: async () => mkDelivery(1) });
  await tick();
  check('ready: real SHA-256 rendered', content.innerHTML.includes(SHA));
  check('ready: media type rendered', content.innerHTML.includes('image/png'));
  check('ready: dimensions rendered', content.innerHTML.includes('37×19'));
  check('ready: byte size rendered', content.innerHTML.includes('1234 字节'));
  check('ready: stable artifact image via controlled API', content.innerHTML.includes(`/assets/${ASSET}`));
  check('ready: note section 设计理念', content.innerHTML.includes('设计理念') && content.innerHTML.includes('用留白和冷暖对比聚焦新品'));
  check('ready: note section 选择理由', content.innerHTML.includes('选择理由') && content.innerHTML.includes('突出清爽与新品感'));
  check('ready: note section 任务适配点', content.innerHTML.includes('任务适配点') && content.innerHTML.includes('夏季新品主视觉'));
  check('ready: task confirmation summary', content.innerHTML.includes('planner'));
  check('ready: final confirmation summary', content.innerHTML.includes('reviewer'));
  check('ready: trace refs summary', content.innerHTML.includes('evt-task-1'));
  check('ready: pending_return badge and return form', content.innerHTML.includes('待回传') && content.innerHTML.includes('id="dlv-return-submit"'));

  // ---------- S3：checkpoint 不可读时仅靠 Delivery 渲染 ----------
  __ui.deliveryGetImpl = async () => ({ ok: true, status: 200, json: async () => mkDelivery(1) });
  freshView(minimalView());
  await tick();
  check('checkpoint-independent: asset metadata from delivery alone', content.innerHTML.includes(SHA) && content.innerHTML.includes('37×19'));
  check('checkpoint-independent: note and trace from delivery alone', content.innerHTML.includes('用留白和冷暖对比聚焦新品') && content.innerHTML.includes('evt-task-1'));
  check('checkpoint-independent: confirmations from delivery alone', content.innerHTML.includes('planner') && content.innerHTML.includes('reviewer'));
  check('checkpoint-independent: image resolved from delivery uri', content.innerHTML.includes(`/assets/${ASSET}`));

  // ---------- S4：回传表单校验（空 actor/空目标零请求） ----------
  __ui.fetchCalls.length = 0;
  clickEl('dlv-return-submit');
  await tick();
  check('validate: empty actor/target blocked with inline errors', document.querySelector('#dlv-actor-error').textContent.length > 0 && document.querySelector('#dlv-target-error').textContent.length > 0);
  check('validate: empty form sends no request', retCalls().length === 0);
  setField('dlv-actor', 'op-1');
  clickEl('dlv-return-submit');
  await tick();
  check('validate: missing target still sends no request', retCalls().length === 0);

  // ---------- S5：回传成功展示 actor/时间/目标/版本 ----------
  __ui.deliveryReturnImpl = async () => ({ ok: true, status: 200, json: async () => recordOf(1, 'op-1', 'parent-agent') });
  __ui.deliveryGetImpl = async () => ({ ok: true, status: 200, json: async () => mkDelivery(1, { return_status: 'returned', return_record: recordOf(1, 'op-1', 'parent-agent') }) });
  const getsBefore = dlvGets().length;
  setField('dlv-target', 'parent-agent');
  clickEl('dlv-return-submit');
  await tick(60);
  const body1 = lastRetBody();
  check('return: posts frozen contract fields', body1.delivery_version === 1 && body1.actor === 'op-1' && body1.target === 'parent-agent');
  check('return: stable idempotency key shape', /^delivery-return:[0-9a-f]{16}$/.test(body1.idempotency_key || ''));
  check('return: delivery refetched after success', dlvGets().length === getsBefore + 1);
  check('returned: badge switches to 已回传', content.innerHTML.includes('已回传') && !content.innerHTML.includes('待回传'));
  check('returned: actor and target shown', content.innerHTML.includes('op-1') && content.innerHTML.includes('parent-agent'));
  check('returned: time and version shown', content.innerHTML.includes('回传时间') && content.innerHTML.includes('回传版本'));
  check('returned: return form dismissed', !content.innerHTML.includes('id="dlv-return-submit"'));

  // ---------- S6：版本变化不沿用旧回传状态 + 重复点击单请求 + 409 可恢复 + 同载荷稳定键 ----------
  __ui.deliveryGenerateImpl = async () => ({ ok: true, status: 200, json: async () => mkDelivery(2, { payload_sha256: '1'.repeat(64) }) });
  clickEl('dlv-regenerate');
  await tick();
  check('version-change: new version back to pending_return', content.innerHTML.includes('待回传') && !content.innerHTML.includes('已回传'));
  check('version-change: return form offered for new version', content.innerHTML.includes('id="dlv-return-submit"'));
  let resolveRet;
  __ui.deliveryReturnImpl = () => new Promise((r) => { resolveRet = r; });
  setField('dlv-actor', 'op-2');
  setField('dlv-target', 'target-B');
  const retsBefore = retCalls().length;
  clickEl('dlv-return-submit');
  await tick(5);
  clickEl('dlv-return-submit');
  await tick(5);
  check('return: duplicate click in flight sends one request', retCalls().length === retsBefore + 1);
  check('return: in-flight button disabled with progress copy', document.querySelector('#dlv-return-submit').disabled === true && document.querySelector('#dlv-status').textContent.includes('提交回传'));
  resolveRet({ ok: false, status: 409, json: async () => ({ detail: '同一回传幂等键不能用于不同载荷。' }) });
  await tick();
  check('conflict: 409 shows recoverable error', document.querySelector('#dlv-status').textContent.includes('同一回传幂等键'));
  check('conflict: form re-enabled and draft preserved', document.querySelector('#dlv-return-submit').disabled === false && document.querySelector('#dlv-actor').value === 'op-2');
  const key1 = lastRetBody().idempotency_key;
  __ui.deliveryReturnImpl = async () => ({ ok: false, status: 409, json: async () => ({ detail: '同一回传幂等键不能用于不同载荷。' }) });
  setField('dlv-target', 'target-C');
  clickEl('dlv-return-submit');
  await tick();
  check('idempotency: changed payload derives new key', lastRetBody().idempotency_key !== key1);
  setField('dlv-target', 'target-B');
  clickEl('dlv-return-submit');
  await tick();
  check('idempotency: same payload keeps stable key across retries', lastRetBody().idempotency_key === key1);
  __ui.deliveryReturnImpl = async () => ({ ok: true, status: 200, json: async () => recordOf(2, 'op-2', 'target-B') });
  __ui.deliveryGetImpl = async () => ({ ok: true, status: 200, json: async () => mkDelivery(2, { payload_sha256: '1'.repeat(64), return_status: 'returned', return_record: recordOf(2, 'op-2', 'target-B') }) });
  clickEl('dlv-return-submit');
  await tick(60);
  check('returned v2: shows new actor/target after conflict recovery', content.innerHTML.includes('op-2') && content.innerHTML.includes('target-B'));
  check('returned v2: version follows new delivery', content.innerHTML.includes('回传版本</dt><dd>v2</dd>'));

  // ---------- S7：网络失败保留可恢复状态 ----------
  __ui.deliveryGenerateImpl = async () => ({ ok: true, status: 200, json: async () => mkDelivery(3, { payload_sha256: '2'.repeat(64) }) });
  clickEl('dlv-regenerate');
  await tick();
  setField('dlv-actor', 'op-3');
  setField('dlv-target', 'target-D');
  __ui.deliveryReturnImpl = async () => { throw new TypeError('Failed to fetch'); };
  clickEl('dlv-return-submit');
  await tick();
  check('network: failure shows recoverable error', document.querySelector('#dlv-status').textContent.includes('无法连接服务'));
  check('network: form re-enabled and draft preserved', document.querySelector('#dlv-return-submit').disabled === false && document.querySelector('#dlv-actor').value === 'op-3');
  __ui.deliveryReturnImpl = async () => ({ ok: true, status: 200, json: async () => recordOf(3, 'op-3', 'target-D') });
  __ui.deliveryGetImpl = async () => ({ ok: true, status: 200, json: async () => mkDelivery(3, { payload_sha256: '2'.repeat(64), return_status: 'returned', return_record: recordOf(3, 'op-3', 'target-D') }) });
  clickEl('dlv-return-submit');
  await tick(60);
  check('network: retry after outage succeeds', content.innerHTML.includes('已回传') && content.innerHTML.includes('op-3'));

  // ---------- S8：说明生成失败保留最终图与确认事实，可独立重试 ----------
  __ui.deliveryGetImpl = null; // 回到默认 409 尚未生成
  freshView(completedView());
  await tick();
  __ui.deliveryGenerateImpl = async () => ({ ok: false, status: 503, json: async () => ({ detail: '后端能力暂不可用：model down。' }) });
  clickEl('dlv-generate');
  await tick();
  check('gen-failure: error surfaced', content.innerHTML.includes('model down'));
  check('gen-failure: final image preserved', content.innerHTML.includes(`/assets/${ASSET}`));
  check('gen-failure: confirmation facts preserved', content.innerHTML.includes('reviewer'));
  check('gen-failure: retry entry still available', content.innerHTML.includes('id="dlv-generate"'));
  __ui.deliveryGenerateImpl = async () => ({ ok: true, status: 200, json: async () => mkDelivery(1) });
  clickEl('dlv-generate');
  await tick();
  check('gen-failure: independent retry succeeds', content.innerHTML.includes('待回传'));

  // ---------- S9：Delivery 字段按不可信内容转义 ----------
  const xssNote = '设计理念：无害\n选择理由：无害\n任务适配点：<img src=x onerror=alert(1)>';
  __ui.deliveryGetImpl = async () => ({ ok: true, status: 200, json: async () => mkDelivery(4, { design_note: xssNote, return_status: 'returned', return_record: recordOf(4, '<script>alert(1)</script>', 'parent-agent') }) });
  freshView(completedView());
  await tick();
  check('xss: raw script tag never injected', !content.innerHTML.includes('<script>alert(1)</script>'));
  check('xss: actor escaped in returned view', content.innerHTML.includes('&lt;script&gt;'));
  check('xss: raw img handler never injected', !content.innerHTML.includes('<img src=x onerror'));

  // ---------- S10：仅显式动作触发请求（无后台轮询） ----------
  const getsNow = dlvGets().length;
  await tick(80);
  check('no-polling: delivery GET count stable without explicit action', dlvGets().length === getsNow);

  return results;
}
return __driver();
