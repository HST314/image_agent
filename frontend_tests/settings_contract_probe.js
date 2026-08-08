// P2-05 运行时设置跨栈探针：在 DOM shim 中驱动前端真实构建
// 保存 PATCH / 密钥更新 PATCH / 项目级应用 POST 三个提交体并输出，
// 由 tests/test_p2_05_settings_ui.py 用冻结的后端 RuntimeSettingsUpdateRequest /
// BranchSettingsApplyRequest 契约校验。
async function __probe() {
  const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
  const ok = (d) => ({ ok: true, status: 200, json: async () => d });
  const clickEl = (id) => { const el = document.querySelector('#' + id); for (const h of (el && el._handlers.click) || []) h({ target: el, preventDefault() {} }); };
  const fireChange = (id) => { const el = document.querySelector('#' + id); for (const h of (el && el._handlers.change) || []) h({ target: el }); };
  const setInput = (id, v) => { const el = document.querySelector('#' + id); el.value = v; for (const h of el._handlers.input || []) h({ target: el }); };
  await tick(); // 初始 loadProjects/renderHome

  globalThis.RUNTIME_SETTINGS_ROLE = 'admin';
  let version = 7;
  const fields = [
    { key: 'question_mode', scope: 'new_job', risk: 'medium', role: 'operator', help: '澄清提问模式', sensitive: false, effective_when: 'new_job', default: 'auto', value: 'auto', secret_state: null, schema: { type: 'string', enum: ['auto', 'manual'] } },
    { key: 'candidate_count', scope: 'new_job', risk: 'high', role: 'admin', help: '候选图数量/费用上限', sensitive: false, effective_when: 'new_job', default: 5, value: 5, secret_state: null, schema: { type: 'integer', minimum: 1, maximum: 10 } },
    { key: 'approval_required', scope: 'new_project', risk: 'critical', role: 'admin', help: '任务书与最终确认门禁', sensitive: false, effective_when: 'new_project', default: true, value: true, secret_state: null, schema: { type: 'boolean' } },
    { key: 'provider_api_key', scope: 'new_job', risk: 'critical', role: 'admin', help: '供应商密钥', sensitive: true, effective_when: 'new_job', default: null, value: null, secret_state: 'unset', schema: { type: 'string' } },
  ];
  let saveBody = null, secretBody = null, applyBody = null;
  __ui.settingsImpl = async (u, o) => {
    if (String(o.method || 'GET').toUpperCase() === 'PATCH') {
      const body = JSON.parse(o.body);
      if ('provider_api_key' in body.changes) secretBody = body; else saveBody = body;
      version += 1;
      return ok({ schema_version: 1, version, sha256: 'f'.repeat(64), changed: true, secret_states: {} });
    }
    return ok({ schema_version: 1, version, sha256: 'a'.repeat(64), fields });
  };
  const CK = 'checkpoints/main/000005-final_confirmation.json';
  __ui.branchesImpl = async () => ok({
    project_id: 'probe-proj', version: 11,
    items: [{ branch_id: 'branch_' + 'a'.repeat(32), name: 'main', head: CK, current: true }],
  });
  __ui.applyImpl = async (u, o) => {
    applyBody = JSON.parse(o.body);
    return ok({ branch: 'settings-branch-1', runtime_settings_version: 7, runtime_settings_sha256: 'a'.repeat(64), branches: { project_id: 'probe-proj', version: 12, items: [] } });
  };

  // 打开设置台（admin），填写操作者
  clickEl('settings-button');
  await tick(40);
  setInput('stgs-actor', 'probe-admin');

  // 1) 普通 + 高风险暂存 → 二次确认 → 保存 PATCH
  clickEl('stgs-edit-question_mode');
  setInput('stgs-edit-input', 'manual');
  clickEl('stgs-edit-apply');
  clickEl('stgs-edit-candidate_count');
  setInput('stgs-edit-input', '9');
  clickEl('stgs-edit-apply');
  clickEl('stgs-save');
  await tick();
  __ui.byId('stgs-danger-check').checked = true;
  fireChange('stgs-danger-check');
  clickEl('stgs-danger-confirm');
  await tick(40);

  // 2) 密钥更新 → 二次确认 → PATCH
  clickEl('stgs-secret-start');
  setInput('stgs-secret-input', 'sk-probe-secret-789');
  clickEl('stgs-secret-submit');
  await tick();
  __ui.byId('stgs-danger-check').checked = true;
  fireChange('stgs-danger-check');
  clickEl('stgs-danger-confirm');
  await tick(40);

  // 3) 项目级应用：从 checkpoint 新建分支并应用
  state.projects = [{ project_id: 'probe-proj' }];
  clickEl('stgs-apply-open');
  await tick(40);
  setInput('stgs-apply-name', 'settings-branch-1');
  setInput('stgs-apply-actor', 'probe-admin');
  clickEl('stgs-apply-confirm');
  await tick(60);

  return [{
    name: 'probe: settings payloads captured',
    pass: !!(saveBody && secretBody && applyBody),
    body: saveBody, secret_body: secretBody, apply_body: applyBody,
  }];
}
return __probe();
