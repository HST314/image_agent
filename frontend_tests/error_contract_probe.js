// P1-10 跨栈探针：页面真实 errInfo/renderErrPanel 消费全量稳定错误对象 fixture，
// 输出 fixtures 与映射结果，由 pytest 校验 fixture 满足冻结 AsyncJob v1 schema，
// 并证明前端映射覆盖 schema 枚举的全部 code 与 suggested_action。
async function __probe() {
  const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));
  await tick();
  const TRACE = 'trace_' + 'abcdef0123456789'.repeat(2);
  const fixtures = [
    { code: 'UPSTREAM_TIMEOUT', stage: 'five_candidate_generation', candidate_slot: 2, retryable: true, suggested_action: 'retry', trace_id: TRACE, detail: '供应商响应超时' },
    { code: 'RATE_LIMITED', stage: 'five_candidate_generation', candidate_slot: 0, retryable: true, suggested_action: 'retry', trace_id: TRACE, detail: '请求频率超限', retry_after_seconds: 4 },
    { code: 'AUTHENTICATION_FAILED', stage: 'self_check_iteration', retryable: false, suggested_action: 'contact_admin', trace_id: TRACE, detail: '凭证无效' },
    { code: 'CONTENT_REJECTED', stage: 'guided_edit', rework_round: 2, retryable: false, suggested_action: 'modify_input', trace_id: TRACE, detail: '包含受限内容' },
    { code: 'PROVIDER_UNAVAILABLE', stage: 'five_candidate_generation', candidate_slot: 4, retryable: true, suggested_action: 'retry', trace_id: TRACE, detail: '网络不可达' },
    { code: 'ASSET_INGESTION_FAILED', stage: 'asset_ingestion', retryable: true, suggested_action: 'retry', trace_id: TRACE, detail: '资产字节读取失败' },
    { code: 'STRUCTURED_OUTPUT_INVALID', stage: 'self_check_iteration', retryable: true, suggested_action: 'retry', trace_id: TRACE, detail: '模型输出不是合法 JSON' },
    { code: 'INVALID_INPUT', stage: 'intake_clarify', retryable: false, suggested_action: 'modify_input', trace_id: TRACE, detail: '输入不合法' },
    { code: 'CONFIGURATION_OR_SKILL', stage: 'master_candidate_selection', retryable: false, suggested_action: 'contact_admin', trace_id: TRACE, detail: '必需 Skill 加载失败' },
    { code: 'CANCELLED', stage: 'workflow', retryable: false, suggested_action: 'none', trace_id: TRACE, detail: '作业已请求取消' },
    { code: 'INTERNAL_ERROR', stage: 'workflow', retryable: false, suggested_action: 'contact_admin', trace_id: TRACE, detail: '未分类异常' },
  ];
  const mapped = fixtures.map((fixture) => {
    const info = errInfo(fixture);
    return {
      code: info && info.code,
      action: info && info.action,
      retryable: info && info.retryable,
      stable: info && info.stable,
      slot: info ? info.slot : undefined,
      round: info ? info.round : undefined,
      retryAfter: info ? info.retryAfter : undefined,
      panelHasDetail: renderErrPanel(fixture).includes('dt>错误代码'),
    };
  });
  return [{ name: 'probe', pass: mapped.every((m) => m.stable && m.panelHasDetail), fixtures, mapped }];
}
return __probe();
