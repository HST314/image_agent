// P2-05 运行时设置台 UI 契约 driver：由 settings_ui_harness.mjs 追加在页面内联脚本之后执行，
// 与页面脚本共享作用域，可直接使用其 state / stgsUi / stgs* 函数。
// 覆盖：schema 驱动渲染与默认值、reader/operator/admin 入口矩阵（直接请求仍由服务端拒绝）、
// 客户端校验与暂存、危险项二次确认门禁、409/403/422/503 可恢复、密钥端到端零回显、
// 按需详情与 404、项目级“从 checkpoint 新建分支并应用”预览/提交/错误矩阵（无模式切换、无原地改旧分支）、
// 刷新/首页切换与迟到响应隔离、全流程零意外写入、XSS 转义。
async function __driver() {
  const results = [];
  const check = (name, cond) => results.push({ name, pass: !!cond });
  const tick = (ms = 25) => new Promise((r) => setTimeout(r, ms));
  const ok = (data) => ({ ok: true, status: 200, json: async () => data });
  const fail = (status, detail) => ({ ok: false, status, json: async () => ({ detail }) });
  const clickEl = (id) => { const el = document.querySelector('#' + id); if (!el) { results.push({ name: 'harness: element #' + id + ' present', pass: false }); return; } for (const h of el._handlers.click || []) h({ target: el, preventDefault() {} }); };
  const fireChange = (id) => { const el = document.querySelector('#' + id); if (!el) { results.push({ name: 'harness: element #' + id + ' present', pass: false }); return; } for (const h of el._handlers.change || []) h({ target: el }); };
  const setInput = (id, v) => { const el = document.querySelector('#' + id); if (!el) { results.push({ name: 'harness: element #' + id + ' present', pass: false }); return; } el.value = v; for (const h of el._handlers.input || []) h({ target: el }); };
  const stgsHtml = () => { const el = document.querySelector('#stgs-root'); return el ? el.innerHTML : ''; };
  const dangerBody = () => __ui.byId('stgs-danger-body').innerHTML;
  const dangerConfirm = () => __ui.byId('stgs-danger-confirm');
  const previewBody = () => __ui.byId('stgs-apply-preview-body').innerHTML;
  const writes = () => __ui.fetchCalls.filter((c) => String(c.options.method || 'GET').toUpperCase() !== 'GET');
  const gets = () => __ui.fetchCalls.filter((c) => String(c.options.method || 'GET').toUpperCase() === 'GET');
  const settingsGets = () => gets().filter((c) => c.url.endsWith('/api/runtime-settings'));
  const patchCalls = () => writes().filter((c) => c.url.endsWith('/api/runtime-settings'));

  const baseFields = () => ([
    { key: 'default_question_count', scope: 'new_job', risk: 'low', role: 'operator', help: '默认澄清问题数', sensitive: false, effective_when: 'new_job', default: 3, value: 3, secret_state: null, schema: { type: 'integer', minimum: 0, maximum: 10 } },
    { key: 'question_mode', scope: 'new_job', risk: 'medium', role: 'operator', help: '澄清提问模式', sensitive: false, effective_when: 'new_job', default: 'auto', value: 'auto', secret_state: null, schema: { type: 'string', enum: ['auto', 'manual'] } },
    { key: 'watermark', scope: 'new_job', risk: 'medium', role: 'operator', help: '水印开关', sensitive: false, effective_when: 'new_job', default: false, value: false, secret_state: null, schema: { type: 'boolean' } },
    { key: 'self_check.termination', scope: 'new_job', risk: 'medium', role: 'operator', help: '质检终止策略', sensitive: false, effective_when: 'new_job', default: 'solo', value: 'solo', secret_state: null, schema: { type: 'string', enum: ['solo', 'fix'] } },
    { key: 'default_output_size', scope: 'new_job', risk: 'medium', role: 'operator', help: '默认输出尺寸', sensitive: false, effective_when: 'new_job', default: '1024x1024', value: '1024x1024', secret_state: null, schema: { type: 'string', pattern: '^\\d{2,5}x\\d{2,5}$' } },
    { key: 'model_timeout_seconds', scope: 'new_job', risk: 'medium', role: 'operator', help: '模型调用超时', sensitive: false, effective_when: 'new_job', default: 180, value: 180, secret_state: null, schema: { type: 'number', exclusiveMinimum: 0, maximum: 3600 } },
    { key: 'candidate_count', scope: 'new_job', risk: 'high', role: 'admin', help: '候选图数量/费用上限', sensitive: false, effective_when: 'new_job', default: 5, value: 5, secret_state: null, schema: { type: 'integer', minimum: 1, maximum: 10 } },
    { key: 'approval_required', scope: 'new_project', risk: 'critical', role: 'admin', help: '任务书与最终确认门禁', sensitive: false, effective_when: 'new_project', default: true, value: true, secret_state: null, schema: { type: 'boolean' } },
    { key: 'image_api_base_url', scope: 'new_project', risk: 'critical', role: 'admin', help: '图片供应商端点', sensitive: false, effective_when: 'new_project', default: '', value: '', secret_state: null, schema: { type: 'string' } },
    { key: 'skill_failure_mode', scope: 'new_project', risk: 'critical', role: 'admin', help: 'Skill 失败安全策略', sensitive: false, effective_when: 'new_project', default: 'block', value: 'block', secret_state: null, schema: { type: 'string', enum: ['block', 'allow_degraded'] } },
    { key: 'provider_api_key', scope: 'new_job', risk: 'critical', role: 'admin', help: '供应商密钥', sensitive: true, effective_when: 'new_job', default: null, value: null, secret_state: 'unset', schema: { type: 'string' } },
    { key: 'legacy_removed', scope: 'new_job', risk: 'low', role: 'operator', help: '待下线字段', sensitive: false, effective_when: 'new_job', default: 'x', value: 'x', secret_state: null, schema: { type: 'string' } },
  ]);

  // 遵循冻结契约的假服务端：describe 读取、PATCH 版本化更新、单键按需详情。
  const server = { version: 3, fields: baseFields(), patchError: null, secretSet: false };
  const describe = () => ({
    schema_version: 1, version: server.version, sha256: 'ab12cd34ef56'.padEnd(64, '0'),
    fields: server.fields.map((f) => f.key === 'provider_api_key' ? { ...f, secret_state: server.secretSet ? 'set' : 'unset' } : { ...f }),
  });
  __ui.settingsImpl = async (u, o) => {
    const method = String(o.method || 'GET').toUpperCase();
    if (method === 'PATCH') {
      if (server.patchError) return server.patchError;
      const body = JSON.parse(o.body);
      if (body.expected_version !== server.version) return fail(409, { code: 'SETTINGS_VERSION_CONFLICT', message: '设置版本冲突，请刷新后重试。' });
      for (const [k, v] of Object.entries(body.changes)) {
        if (k === 'provider_api_key') { server.secretSet = true; continue; }
        const f = server.fields.find((x) => x.key === k);
        if (f) f.value = v;
      }
      server.version += 1;
      return ok({ schema_version: 1, version: server.version, sha256: 'ff'.repeat(32), changed: true, secret_states: server.secretSet ? { provider_api_key: 'set' } : {} });
    }
    return ok(describe());
  };
  __ui.settingDetailImpl = async (u) => {
    const key = decodeURIComponent(u.split('/api/runtime-settings/')[1]);
    if (key === 'legacy_removed') return fail(404, { code: 'SETTING_NOT_FOUND', message: '设置不存在。' });
    const f = describe().fields.find((x) => x.key === key);
    if (!f) return fail(404, { code: 'SETTING_NOT_FOUND', message: '设置不存在。' });
    return ok({ schema_version: 1, version: server.version, sha256: 'ab12cd34ef56'.padEnd(64, '0'), field: f });
  };

  const setRole = (r) => { globalThis.RUNTIME_SETTINGS_ROLE = r; };
  const openSettings = async () => { clickEl('settings-button'); await tick(40); };

  await tick(); // 初始 loadProjects().then(renderHome)

  // ---------- S0：初始读取 503 → 可恢复错误态 → 重试成功 ----------
  const realImpl = __ui.settingsImpl;
  __ui.settingsImpl = async () => fail(503, { code: 'SETTINGS_UNAVAILABLE', message: '运行时设置暂不可读取。' });
  setRole('operator');
  await openSettings();
  check('load-503: error panel shows unavailable title', stgsHtml().includes('设置服务暂不可用'));
  check('load-503: recovery copy explains safe retry', stgsHtml().includes('已有设置与运行中的任务不受影响'));
  check('load-503: retry entry present', stgsHtml().includes('id="stgs-retry"'));
  __ui.settingsImpl = realImpl;
  clickEl('stgs-retry');
  await tick(40);
  check('load-503: retry recovers to ready view', stgsHtml().includes('设置版本 v3'));

  // ---------- S1：schema 驱动渲染（operator） ----------
  check('render: exactly one describe GET on open', settingsGets().length === 2); // S0 失败 1 次 + 重试 1 次
  check('render: all 12 fields rendered from schema', (stgsHtml().match(/class="stgs-field" id="stgs-field-/g) || []).length === 12);
  check('render: version badge and schema/hash chips shown', stgsHtml().includes('设置版本 v3') && stgsHtml().includes('schema v1') && stgsHtml().includes('配置哈希 ab12cd34ef56'));
  check('render: operator role chip and copy', stgsHtml().includes('角色：操作员') && stgsHtml().includes('仅可修改业务参数'));
  check('render: server-side auth disclaimer shown', stgsHtml().includes('所有修改仍由服务端最终鉴权'));
  check('render: defaults and current values from describe', stgsHtml().includes('1024x1024') && stgsHtml().includes('<dd>3</dd>'));
  check('render: enum constraint text rendered', stgsHtml().includes('枚举：auto / manual'));
  check('render: numeric range text rendered', stgsHtml().includes('整数（≥ 0，≤ 10）'));
  check('render: exclusiveMinimum rendered as strict bound', stgsHtml().includes('&gt; 0') || stgsHtml().includes('> 0，≤ 3600'));
  check('render: pattern constraint rendered', stgsHtml().includes('需匹配 ^\\d{2,5}x\\d{2,5}$'));
  check('render: help text rendered', stgsHtml().includes('默认澄清问题数'));
  check('render: scope/effective timing labels', stgsHtml().includes('新任务生效') && stgsHtml().includes('新工程生效'));
  check('render: effective timing copy for new_job', stgsHtml().includes('保存后只对之后新建的任务生效'));
  check('render: effective timing copy for new_project', stgsHtml().includes('从 checkpoint 新建分支并应用'));
  check('render: risk badges labelled', stgsHtml().includes('低风险') && stgsHtml().includes('严重风险'));
  check('render: sensitive field shows state only', stgsHtml().includes('未设置') && stgsHtml().includes('密钥不回显'));
  check('render: sensitive field has no value/default echo', !stgsHtml().includes('sk-') && stgsHtml().includes('—（密钥不回显）'));
  check('render: dotted key id sanitized', stgsHtml().includes('id="stgs-field-self_check_termination"'));
  check('render: zero writes so far', writes().length === 0);

  // ---------- S2：reader 角色矩阵 ----------
  setRole('reader');
  await openSettings();
  check('reader: no edit entries at all', !stgsHtml().includes('id="stgs-edit-') && !stgsHtml().includes('stgs-secret-start'));
  check('reader: operator fields show read-only lock', stgsHtml().includes('当前角色只读'));
  check('reader: admin fields show admin-only lock', stgsHtml().includes('仅管理员可修改'));
  check('reader: no apply entry, admin-only hint', !stgsHtml().includes('id="stgs-apply-open"') && stgsHtml().includes('仅管理员可把设置应用到既有工程的新分支'));
  check('reader: detail buttons still available', stgsHtml().includes('id="stgs-detail-question_mode"'));
  check('reader: role banner copy', stgsHtml().includes('仅可查看设置定义与当前值'));

  // ---------- S3：operator 入口矩阵 ----------
  setRole('operator');
  await openSettings();
  check('operator: edit entry on business field', stgsHtml().includes('id="stgs-edit-question_mode"'));
  check('operator: admin field locked with hint', !stgsHtml().includes('id="stgs-edit-candidate_count"') && stgsHtml().includes('仅管理员可修改'));
  check('operator: sensitive field locked for non-admin', !stgsHtml().includes('id="stgs-secret-start"'));
  check('operator: no project-apply entry', !stgsHtml().includes('id="stgs-apply-open"'));

  // ---------- S4：客户端校验、暂存、撤销、放弃 ----------
  clickEl('stgs-edit-default_question_count');
  check('validate: numeric editor rendered', stgsHtml().includes('id="stgs-edit-input"') && stgsHtml().includes('type="number"'));
  setInput('stgs-edit-input', 'abc');
  clickEl('stgs-edit-apply');
  check('validate: non-integer rejected inline', stgsHtml().includes('请输入整数'));
  setInput('stgs-edit-input', '99');
  clickEl('stgs-edit-apply');
  check('validate: above maximum rejected inline', stgsHtml().includes('不能大于 10'));
  setInput('stgs-edit-input', '7');
  clickEl('stgs-edit-apply');
  check('stage: valid value staged with chip', stgsHtml().includes('待保存 → 7'));
  clickEl('stgs-edit-question_mode');
  check('validate: enum editor renders select', stgsHtml().includes('<select class="input" id="stgs-edit-input"'));
  setInput('stgs-edit-input', 'bogus');
  clickEl('stgs-edit-apply');
  check('validate: enum membership enforced', stgsHtml().includes('取值必须是枚举之一'));
  setInput('stgs-edit-input', 'manual');
  clickEl('stgs-edit-apply');
  check('stage: enum value staged', stgsHtml().includes('待保存 → manual'));
  clickEl('stgs-edit-watermark');
  setInput('stgs-edit-input', 'true');
  clickEl('stgs-edit-apply');
  check('stage: boolean staged with human label', stgsHtml().includes('待保存 → 开启'));
  clickEl('stgs-edit-default_output_size');
  setInput('stgs-edit-input', 'abc');
  clickEl('stgs-edit-apply');
  check('validate: pattern enforced inline', stgsHtml().includes('需匹配格式'));
  setInput('stgs-edit-input', '512x512');
  clickEl('stgs-edit-apply');
  check('stage: pattern-valid value staged', stgsHtml().includes('待保存 → 512x512'));
  clickEl('stgs-edit-model_timeout_seconds');
  setInput('stgs-edit-input', '0');
  clickEl('stgs-edit-apply');
  check('validate: exclusiveMinimum enforced inline', stgsHtml().includes('必须大于 0'));
  clickEl('stgs-edit-cancel');
  check('validate: cancel discards editor without staging', !stgsHtml().includes('待保存 → 0'));
  check('stage: save bar summarizes count', stgsHtml().includes('保存 4 项修改'));
  clickEl('stgs-unstage-watermark');
  check('stage: per-field unstage works', !stgsHtml().includes('待保存 → 开启') && stgsHtml().includes('保存 3 项修改'));
  clickEl('stgs-discard');
  check('stage: discard clears all staged', stgsHtml().includes('暂存后统一保存') && !stgsHtml().includes('待保存'));
  check('stage: still zero writes after local editing', writes().length === 0);

  // ---------- S5：保存（无危险项，operator） ----------
  clickEl('stgs-edit-question_mode');
  setInput('stgs-edit-input', 'manual');
  clickEl('stgs-edit-apply');
  clickEl('stgs-save');
  check('save: actor required before any request', stgsHtml().includes('请填写操作者标识') && writes().length === 0);
  setInput('stgs-actor', 'op-1');
  clickEl('stgs-save');
  await tick(40);
  check('save: exactly one PATCH issued', patchCalls().length === 1);
  const saveBody = JSON.parse(patchCalls()[0].options.body);
  check('save: body carries expected_version/actor/changes/flag', saveBody.expected_version === 3 && saveBody.actor === 'op-1' && saveBody.changes.question_mode === 'manual' && saveBody.dangerous_confirmed === false);
  check('save: no dangerous keys so no confirm gate', Object.keys(saveBody.changes).length === 1);
  check('save: version/hash refreshed after save', stgsHtml().includes('设置版本 v4'));
  check('save: staged changes cleared after success', !stgsHtml().includes('待保存'));
  check('save: current value reflects server state', stgsHtml().includes('<dd>manual</dd>'));
  check('save: actor persisted for next session', __ui.storage.local.getItem('studio-actor') === 'op-1');

  // ---------- S6：危险项二次确认（admin） ----------
  setRole('admin');
  await openSettings(); // v4
  check('admin: secret entry visible', stgsHtml().includes('id="stgs-secret-start"'));
  check('admin: admin field editable', stgsHtml().includes('id="stgs-edit-candidate_count"'));
  check('admin: apply entry visible', stgsHtml().includes('id="stgs-apply-open"'));
  clickEl('stgs-edit-candidate_count');
  setInput('stgs-edit-input', '8');
  clickEl('stgs-edit-apply');
  check('danger: high-risk staged chip warns', stgsHtml().includes('candidate_count → 8（需二次确认）'));
  clickEl('stgs-save');
  await tick();
  check('danger: no PATCH before explicit confirmation', patchCalls().length === 1);
  check('danger: dialog lists risky change current→pending', dangerBody().includes('candidate_count') && dangerBody().includes('待保存'));
  check('danger: confirm disabled until checkbox', dangerConfirm().disabled === true);
  __ui.byId('stgs-danger-check').checked = true;
  fireChange('stgs-danger-check');
  check('danger: checkbox enables confirm', dangerConfirm().disabled === false);
  clickEl('stgs-danger-cancel');
  check('danger: cancel keeps staged and sends nothing', stgsHtml().includes('candidate_count → 8') && patchCalls().length === 1);
  clickEl('stgs-save');
  await tick();
  __ui.byId('stgs-danger-check').checked = true;
  fireChange('stgs-danger-check');
  clickEl('stgs-danger-confirm');
  await tick(40);
  check('danger: confirmed save sends dangerous_confirmed=true', patchCalls().length === 2 && JSON.parse(patchCalls()[1].options.body).dangerous_confirmed === true);
  check('danger: confirmed save carries risky change', JSON.parse(patchCalls()[1].options.body).changes.candidate_count === 8);
  check('danger: version bumped after confirmed save', stgsHtml().includes('设置版本 v5'));

  // ---------- S7：保存错误矩阵（409/403/422/503） ----------
  clickEl('stgs-edit-question_mode');
  setInput('stgs-edit-input', 'auto');
  clickEl('stgs-edit-apply');
  server.patchError = fail(409, { code: 'SETTINGS_VERSION_CONFLICT', message: '设置版本冲突，请刷新后重试。' });
  server.version = 9; // 他人已先保存
  const beforeGets = settingsGets().length;
  clickEl('stgs-save');
  await tick(40);
  check('save-409: conflict message shown', stgsHtml().includes('设置版本冲突'));
  check('save-409: staged cleared and auto-refreshed', !stgsHtml().includes('待保存 → auto') && settingsGets().length === beforeGets + 1);
  check('save-409: latest version displayed after refresh', stgsHtml().includes('设置版本 v9'));
  check('save-409: recovery copy asks re-check', stgsHtml().includes('请重新核对'));
  server.patchError = fail(403, { code: 'SETTINGS_FORBIDDEN', message: '当前角色无权修改管理员设置。' });
  clickEl('stgs-edit-question_mode');
  setInput('stgs-edit-input', 'manual');
  clickEl('stgs-edit-apply');
  clickEl('stgs-save');
  await tick(40);
  check('save-403: server rejection surfaced as final authority', stgsHtml().includes('服务端拒绝了此次修改') && stgsHtml().includes('最终以服务端鉴权为准'));
  check('save-403: staged changes retained for retry', stgsHtml().includes('待保存 → manual'));
  server.patchError = fail(422, { code: 'SETTINGS_VALIDATION_FAILED', message: '组合校验未通过。' });
  clickEl('stgs-save');
  await tick(40);
  check('save-422: validation failure copy distinct', stgsHtml().includes('输入未通过服务端校验') && stgsHtml().includes('组合校验未通过'));
  check('save-422: staged changes retained', stgsHtml().includes('待保存 → manual'));
  server.patchError = fail(503, { code: 'SETTINGS_UNAVAILABLE', message: '运行时设置暂不可读取。' });
  clickEl('stgs-save');
  await tick(40);
  check('save-503: storage failure copy safe and recoverable', stgsHtml().includes('设置存储暂时不可用，修改未保存'));
  check('save-503: staged changes retained', stgsHtml().includes('待保存 → manual'));
  server.patchError = null;
  clickEl('stgs-discard');
  await tick();

  // ---------- S8：密钥端到端零回显 ----------
  const SECRET = 'sk-live-SECRETVALUE-001';
  clickEl('stgs-secret-start');
  check('secret: editor uses password input', stgsHtml().includes('type="password"'));
  clickEl('stgs-secret-submit');
  check('secret: empty value rejected before confirm', stgsHtml().includes('请输入新的密钥值'));
  setInput('stgs-secret-input', SECRET);
  const pBeforeSecret = patchCalls().length;
  clickEl('stgs-secret-submit');
  await tick();
  check('secret: update requires secondary confirmation', dangerBody().includes('即将更新供应商密钥') && patchCalls().length === pBeforeSecret);
  check('secret: confirm copy promises no echo', dangerBody().includes('不会出现在任何界面、缓存、错误或日志中'));
  __ui.byId('stgs-danger-check').checked = true;
  fireChange('stgs-danger-check');
  clickEl('stgs-danger-confirm');
  await tick(40);
  const secretPatch = JSON.parse(patchCalls().at(-1).options.body);
  check('secret: PATCH carries new value with dangerous flag', patchCalls().length === pBeforeSecret + 1 && secretPatch.changes.provider_api_key === SECRET && secretPatch.dangerous_confirmed === true);
  check('secret: draft wiped after submit', stgsSecretDraft === '');
  check('secret: input cleared after submit', (document.querySelector('#stgs-secret-input') || { value: '' }).value === '');
  check('secret: state flips to set without echo', stgsHtml().includes('已设置') && !stgsHtml().includes(SECRET));
  check('secret: danger dialog holds no echo', !dangerBody().includes(SECRET));
  server.patchError = fail(422, { code: 'SETTINGS_VALIDATION_FAILED', message: '密钥格式无效。' });
  clickEl('stgs-secret-start');
  setInput('stgs-secret-input', SECRET);
  clickEl('stgs-secret-submit');
  await tick();
  __ui.byId('stgs-danger-check').checked = true;
  fireChange('stgs-danger-check');
  clickEl('stgs-danger-confirm');
  await tick(40);
  check('secret-422: validation error surfaced without value', stgsHtml().includes('密钥未通过校验') && !stgsHtml().includes(SECRET));
  check('secret-422: draft wiped on error path too', stgsSecretDraft === '');
  server.patchError = null;
  setInput('stgs-secret-input', SECRET); // 错误后编辑器仍打开，直接测取消路径
  clickEl('stgs-secret-cancel');
  check('secret: cancel wipes draft and closes editor', stgsSecretDraft === '' && !stgsHtml().includes('stgs-secret-input'));

  // ---------- S9：按需详情、404、未知字段、XSS ----------
  clickEl('stgs-detail-question_mode');
  await tick(30);
  check('detail: single-key GET issued on demand', gets().some((c) => c.url.endsWith('/api/runtime-settings/question_mode')));
  check('detail: region shows constraints and read version', stgsHtml().includes('读取时设置版本') && stgsHtml().includes('类型约束'));
  clickEl('stgs-detail-question_mode');
  check('detail: toggle collapses region', !stgsHtml().includes('读取时设置版本'));
  clickEl('stgs-detail-legacy_removed');
  await tick(30);
  check('detail-404: removed setting shows safe message', stgsHtml().includes('设置不存在或已下线'));
  server.fields.push({ key: 'mystery_field', scope: 'strange', risk: 'weird', role: 'strange', help: '<img src=x onerror=alert(1)>', sensitive: false, effective_when: 'strange', default: 'd', value: '<script>alert(2)</script>', secret_state: null, schema: { type: 'string' } });
  await openSettings();
  check('unknown: unrecognized meta rendered raw without crash', stgsHtml().includes('weird') && stgsHtml().includes('strange'));
  check('xss: help and value escaped, nothing executable', !stgsHtml().includes('<script>alert(2)</script>') && !stgsHtml().includes('<img src=x') && stgsHtml().includes('&lt;script&gt;'));
  server.fields.pop();

  // ---------- S10：项目级“从 checkpoint 新建分支并应用” ----------
  state.projects = [{ project_id: 'proj-a' }, { project_id: 'proj-b' }];
  const CK_HEAD = 'checkpoints/main/000005-final_confirmation.json';
  __ui.branchesImpl = async () => ok({
    project_id: 'proj-a', version: 11,
    items: [
      { branch_id: 'branch_' + 'a'.repeat(32), name: 'main', head: CK_HEAD, current: true },
      { branch_id: 'branch_' + 'b'.repeat(32), name: 'rev1', head: 'checkpoints/rev1/000002-step.json', current: false },
      { branch_id: 'branch_' + 'c'.repeat(32), name: 'empty', head: null, current: false },
    ],
  });
  let applyBody = null;
  __ui.applyImpl = async (u, o) => { applyBody = JSON.parse(o.body); return ok({ branch: 'settings-branch-1', runtime_settings_version: 9, runtime_settings_sha256: 'cd'.repeat(32), branches: { project_id: 'proj-a', version: 12, items: [] } }); };
  await openSettings();
  const applyV = stgsUi.data.version;
  clickEl('stgs-apply-open');
  await tick(40);
  check('apply: project select populated from loaded projects', __ui.byId('stgs-apply-project').innerHTML.includes('proj-a') && __ui.byId('stgs-apply-project').innerHTML.includes('proj-b'));
  check('apply: branches fetched for selected project', gets().some((c) => c.url.includes('/api/projects/proj-a/branches')));
  check('apply: preview shows source branch and checkpoint', previewBody().includes('main') && previewBody().includes(CK_HEAD));
  check('apply: preview pins settings version and hash', previewBody().includes('设置版本') && previewBody().includes('v' + applyV));
  check('apply: preview lists new_project fields only', previewBody().includes('approval_required') && previewBody().includes('skill_failure_mode') && !previewBody().includes('provider_api_key'));
  check('apply: preview states source branch/jobs/mode unchanged', previewBody().includes('源分支、既有任务与已固化的真实/离线模式保持不变'));
  check('apply: no in-place edit or mode switch control', !previewBody().includes('name="offline"') && !previewBody().includes('id="stgs-apply-mode"') && !previewBody().includes('切换为'));
  check('apply: confirm enabled once preview ready', __ui.byId('stgs-apply-confirm').disabled === false);
  setInput('stgs-apply-name', 'x');
  setInput('stgs-apply-actor', 'admin-1');
  clickEl('stgs-apply-confirm');
  await tick();
  check('apply: invalid branch name rejected client-side', __ui.byId('stgs-apply-name-error').textContent.includes('分支名称') && applyBody === null);
  setInput('stgs-apply-name', 'settings-branch-1');
  clickEl('stgs-apply-confirm');
  await tick(40);
  check('apply: POST body carries checkpoint/actor/versions', applyBody && applyBody.checkpoint === CK_HEAD && applyBody.actor === 'admin-1' && applyBody.expected_version === 11 && applyBody.settings_version === applyV);
  check('apply: POST body has exactly the frozen keys', applyBody && ['checkpoint', 'actor', 'expected_version', 'settings_version', 'name'].every((k) => k in applyBody) && Object.keys(applyBody).length === 5);
  check('apply: success note states invariants', stgsHtml().includes('已从 checkpoint ' + CK_HEAD + ' 创建新分支 settings-branch-1') && stgsHtml().includes('真实/离线模式均未改变'));
  state.projects = [{ project_id: 'proj-a' }, { project_id: 'proj-b' }]; // 成功路径的 loadProjects 会刷新工程列表
  __ui.applyImpl = async () => fail(409, { code: 'SETTINGS_VERSION_CONFLICT', message: '设置版本冲突，请刷新后重试。' });
  const gBefore = settingsGets().length;
  clickEl('stgs-apply-open');
  await tick(40);
  setInput('stgs-apply-actor', 'admin-1');
  clickEl('stgs-apply-confirm');
  await tick(60);
  check('apply-409: conflict refreshes settings and branches', __ui.byId('stgs-apply-status').textContent.includes('版本冲突') && settingsGets().length > gBefore);
  __ui.applyImpl = async () => fail(403, { code: 'SETTINGS_FORBIDDEN', message: '只有管理员可把项目级设置应用到新分支。' });
  clickEl('stgs-apply-confirm');
  await tick(40);
  check('apply-403: admin-only rejection shown safely', __ui.byId('stgs-apply-status').textContent.includes('只有管理员'));
  __ui.applyImpl = async () => fail(503, { code: 'SETTINGS_UNAVAILABLE', message: '不可用。' });
  clickEl('stgs-apply-confirm');
  await tick(40);
  check('apply-503: unavailable copy recoverable', __ui.byId('stgs-apply-status').textContent.includes('暂不可用'));
  clickEl('stgs-apply-cancel');
  __ui.applyImpl = null;

  // ---------- S11：刷新路由、首页重置与迟到响应隔离 ----------
  await openSettings();
  const fetchMark = __ui.fetchCalls.length;
  clickEl('refresh-button');
  await tick(40);
  const routed = __ui.fetchCalls.slice(fetchMark);
  check('refresh: global refresh re-reads settings when active', routed.some((c) => c.url.endsWith('/api/runtime-settings')));
  check('refresh: no project reload while settings active', !routed.some((c) => c.url.endsWith('/api/projects')));
  renderHome();
  check('home: leaving settings resets view state', stgsUi.active === false && !__ui.contentEl.innerHTML.includes('id="stgs-root"'));
  const pending = [];
  __ui.settingsImpl = async (u, o) => {
    if (String(o.method || 'GET').toUpperCase() === 'PATCH') return ok({ schema_version: 1, version: 10, sha256: 'ee'.repeat(32), changed: true, secret_states: {} });
    return new Promise((resolve) => pending.push(resolve));
  };
  await openSettings();
  check('crosstalk: first load pending', pending.length === 1 && stgsUi.status === 'loading');
  stgsReload();
  await tick();
  check('crosstalk: reload issues second request', pending.length === 2);
  pending[0](ok({ schema_version: 1, version: 99, sha256: '00'.repeat(32), fields: [] }));
  await tick(30);
  check('crosstalk: stale response dropped by view token', !stgsHtml().includes('v99'));
  pending[1](ok(describe()));
  await tick(30);
  check('crosstalk: latest response rendered', stgsHtml().includes('设置版本 v' + server.version));
  __ui.settingsImpl = realImpl;

  // ---------- S12：全流程零意外写入 ----------
  // 显式写操作：保存 1 + 危险确认 1 + 错误矩阵 4 + 密钥 2 = 8 次 PATCH；1 次 apply POST。
  const unexpected = writes().filter((c) => !c.url.endsWith('/api/runtime-settings') && !c.url.includes('apply-runtime-settings'));
  check('writes: only frozen settings endpoints were written', unexpected.length === 0);
  check('writes: PATCH count matches explicit user actions', patchCalls().length === 8);
  check('writes: apply POSTs match explicit confirmations', writes().filter((c) => c.url.includes('apply-runtime-settings')).length === 4);

  return results;
}
return __driver();
