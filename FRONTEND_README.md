# Image Agent Studio 前端启动与验收

## 新增范围

本交付仅新增 `main_front.py`、`frontend/`、`frontend_tests/`、`design-system/`、
`requirements-front.txt` 与本文档。生产后端的 132 个原文件均保持不变。

`main_front.py` 是薄适配层：工程创建、恢复、重试和分支分别调用生产代码中的
`WorkflowRunner` 与 `ProjectStore`。它不复制状态机、模型路由、生成、校准或最终
交付门禁。浏览器侧不保存密钥，也不会把离线模拟结果标为最终成功。

## 启动

```bash
cd image_agent_mvp
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.lock -r requirements-front.txt
uvicorn main_front:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。生产模型凭证继续使用原后端支持的环境变量，
例如 `ARK_API_KEY`；本文档和前端代码不写入任何密钥。

可选环境变量：

- `IMAGE_AGENT_FRONT_PROJECTS_ROOT`：Web 工程数据根目录。默认写入
  `frontend/data/projects`，与附件内原生产工程隔离。
- `IMAGE_AGENT_MODEL_CONFIG`：生产模型配置路径。默认使用
  `configs/model_config.yaml`。

## 测试

```bash
# 原生产测试
python3 -m pytest -q tests

# 新增适配层测试
python3 -m pytest -q frontend_tests

# P1-08 圈画微调 UI 契约测试（需要 node；含 Node DOM shim 交互驱动）
python3 -m pytest -q tests/test_p1_08_guided_edit_ui.py

# Python 语法检查
python3 -m py_compile main_front.py
```

## 圈画微调（P1-08 前端）

`waiting_human_rework` 相位渲染圈画编辑器：矩形与自由画笔、颜色、粗细、撤销/清空、
指导图预览（画布叠加在当前资产图上，与服务端合成同源数据）和自由文本 Prompt。

- **坐标语义**：标注只以 `source_image_pixels` 保存与提交。画布精确覆盖
  `object-fit: contain` 的实际内容区域，CSS 坐标按 `source/rendered` 比例反算并钳制在
  原图边界内；DPR 只影响画布 backing store，不进入提交坐标。横图、竖图、超宽图的
  letterbox 偏移由 `geFitRect` 统一处理。
- **提交契约**：`POST /api/projects/{project_id}/advance` 携带冻结的 `guided_edit`
  （当前分支、当前头资产、原图宽高、连续轮次、actor、非空 Prompt）与按载荷派生的稳定
  `idempotency_key`；重复点击在请求中被禁用，同一载荷重试键不变。项目/分支/头资产/幂等
  安全门禁由服务端独占执行，前端不复制。
- **状态**：提交中/失败/成功均有真实状态文案；成功后轮询 job 并重新拉取工程视图，
  新图进入 `waiting_reinspection`，旧质检与旧最终确认不再可用。
- **草稿**：未提交标注与 Prompt 按 `ge-draft:{project_id}:{asset_id}` 存入
  sessionStorage，刷新同资产可恢复，换图不串稿；提交成功即清除。
- **降级**：当前资产不是受控 artifact（如离线 mock）时不渲染画布，仅保留文字微调；
  图像载入失败显示可恢复提示。

## 安全边界

- 工程 ID 仅允许 2–64 位字母、数字、下划线与连字符，阻止路径穿越。
- JSON 请求上限 512 KiB；本地图片下载上限 25 MiB。
- 图片只允许从当前工程的 `artifacts/images` 目录读取，且 MIME 类型仅允许
  PNG、JPEG、WebP、GIF。
- 外部模型、密钥、配置或依赖不可用时，API 返回可恢复的真实错误；工作流已有
  检查点不会被覆盖。
- 同一工程并发推进沿用生产 `ProjectStore.lock()` 排他锁。

## 设计与状态覆盖

设计源文件位于 `design-system/image-agent-studio/MASTER.md`，工作台差异规则位于
`pages/workspace.md`。界面覆盖 loading、empty、error、disabled、success、离线提示、
长任务超时和断线恢复；支持 375、768、1024、1440px，键盘焦点、跳转链接、
语义表单、`aria-live`、44px 触控目标与 `prefers-reduced-motion`。

## 生产基线证据

- 指定附件：`image_agent_mvp_production.zip`
- 附件 ID：`019fd20a-3869-7cd2-aef2-b97f691526fe`
- ZIP SHA-256：`c9cb2760040da794789621241932424a2ed48bf14fe27b9d9a1b5d55b73509d3`
- 原文件数：132
- 交付前复核方式：对解压基线和当前树排除上述新增路径后分别执行
  `find ... -type f -print0 | sort -z | xargs -0 sha256sum`，再执行 `diff -u`；
  预期无输出且退出码为 0。

## 异步推进 API（v1.8）

`POST /api/projects/{project_id}/advance` 不再执行长耗时模型调用，只以请求中的
`idempotency_key`（未提供时由当前 checkpoint 与动作参数稳定派生）创建作业并返回
`202`、`job_id`。相同键并发提交只得到同一个作业；同键不同参数返回 `409`。

- `GET /api/projects/{project_id}/jobs/{job_id}` 查询 `queued/running/succeeded/failed/cancelled`。
- `POST /api/projects/{project_id}/jobs/{job_id}/cancel` 请求取消；已进入供应商调用的作业不会伪称立即中止，完成边界后落为 `cancelled`。
- `GET /api/projects/{project_id}/events?after=<sequence>` 提供 SSE。也可发送 `Last-Event-ID`，服务端只续传更大序号。
- worker 启动或状态查询会接管失去存活进程所有者的 `running` 作业。候选五槽、质检和返工仍复用原有稳定幂等键，因此进程退出、刷新和请求超时不会新建付费槽位。
- 人工等待通过 `business_status` 暴露，均为成功 checkpoint，不进入 `failed_step`。项目创建时固化的 real/offline 模式对所有后续作业强制生效。

作业响应契约见 `schemas/AsyncJob.v1.schema.json`。

## 已知限制

- 当前使用文件型持久作业注册表和进程内 worker；适合单机/共享文件卷部署。多节点部署需要将调度器替换为具备同等 claim 与幂等语义的队列。
- 外部 HTTP 图片由原供应商 URL 展示；本地生成图片需已进入生产 artifact store。
- 没有真实模型凭证时只能显式选择离线测试模式，模拟图片受生产最终门禁限制。
- 四档截图在当前执行容器中未能生成：系统 Firefox 无头模式报告
  `RenderCompositorSWGL failed mapping default framebuffer`；随后尝试临时安装
  Playwright Chromium，但浏览器包下载连续出现 `ECONNRESET`。因此本交付不虚报
  截图完成。页面已实现并静态核查 375/768/1024/1440px 断点，需在具备可用浏览器
  渲染环境的验收阶段补录四档视觉证据。
