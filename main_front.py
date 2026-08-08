"""Image Agent 的 FastAPI Web 薄适配层。

本文件只负责 HTTP 输入校验、调用生产 WorkflowRunner / ProjectStore，以及把
文件型快照转换成前端可消费的视图；工作流判断和图片生成均由现有后端完成。
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_core.models import DesignDeliveryEnvelope, DesignTaskEnvelope, ImageTaskCard
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from calibrator.calibration_loop import ManualAction
from storage.project_store import ProjectStore, content_hash
from configs.runtime_policy import RuntimePolicy
from configs.runtime_settings import RuntimeSettingsStore, SettingsConflict, SettingsForbidden
from agent_core.jobs import JobStore, WorkflowJobWorker
from agent_core.guided_edit import GuidedEditRequest
from agent_core.delivery import DeliveryService
from agent_core.observability import event_page, progress_projection
from agent_core.health import HealthService

from configs.env_loader import load_dotenv  # 引入 .env 加载器

load_dotenv(".env")  # 在程序启动时自动读取当前目录下的 .env 文件

APP_ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = APP_ROOT / "frontend"
PROJECTS_ROOT = Path(os.getenv("IMAGE_AGENT_FRONT_PROJECTS_ROOT", FRONTEND_ROOT / "data" / "projects")).resolve()
MODEL_CONFIG = Path(os.getenv("IMAGE_AGENT_MODEL_CONFIG", APP_ROOT / "configs" / "model_config.yaml")).resolve()
RUNTIME_CONFIG = Path(os.getenv("IMAGE_AGENT_RUNTIME_CONFIG", APP_ROOT / "configs" / "runtime.yaml")).resolve()
MAX_REQUEST_BYTES = 512 * 1024
MAX_ASSET_BYTES = 25 * 1024 * 1024
PROJECT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$")
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

app = FastAPI(
    title="Image Agent Studio",
    description="生产 Image Agent 的 Web 薄适配接口",
    version="1.0.0",
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(StrictRequest):
    project_id: str = Field(min_length=2, max_length=64)
    task_card: dict[str, Any] | None = None
    offline: bool = False
    envelope: dict[str, Any] | None = None


class AdvanceRequest(StrictRequest):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    selected_id: str | None = Field(default=None, max_length=128)
    clarification_answers: dict[str, Any] | None = None
    edited_markdown: str | None = Field(default=None, max_length=100_000)
    manual_action: Literal["execute", "edit_and_execute", "skip", "end", "accept_current"] | None = None
    edited_delta: str | None = Field(default=None, max_length=4_000)
    human_prompt: str | None = Field(default=None, max_length=8_000)
    guided_edit: GuidedEditRequest | None = None
    task_spec_action: Literal["confirm"] | None = None
    final_action: Literal["confirm", "continue"] | None = None
    actor: str | None = Field(default=None, min_length=1, max_length=256)
    quality_action: Literal["continue_generation", "manual_rework", "abandon"] | None = None
    expense_confirmed: bool = False
    offline: bool = False


class ManualReturnRequest(StrictRequest):
    delivery_version: int = Field(gt=0)
    actor: str = Field(min_length=1, max_length=256)
    target: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class BranchRequest(StrictRequest):
    checkpoint: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, min_length=2, max_length=64)
    actor: str = Field(default="operator", min_length=1, max_length=256)
    expected_version: int = Field(ge=1)

class BranchSwitchRequest(StrictRequest):
    branch_id: str = Field(pattern=r"^branch_[a-f0-9]{32}$")
    checkpoint: str = Field(min_length=1, max_length=256)
    expected_version: int = Field(ge=1)

class HistoryReopenPreviewRequest(StrictRequest):
    name: str | None = Field(default=None, min_length=2, max_length=64)

class RuntimeSettingsUpdateRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    changes: dict[str, Any] = Field(min_length=1, max_length=32)
    actor: str = Field(min_length=1, max_length=256)
    dangerous_confirmed: bool = False

class BranchSettingsApplyRequest(BranchRequest):
    settings_version: int = Field(ge=1)


def _trace_detail_allowed(request: Request, project_id: str) -> bool:
    """Explicit host authorization hook; secure default is deny."""
    authorizer = getattr(app.state, "model_call_detail_authorizer", None)
    return bool(authorizer and authorizer(request, project_id))

def _settings_role(request: Request) -> str:
    """Host-injected RBAC; secure default permits reads but no writes."""
    authorizer = getattr(app.state, "runtime_settings_authorizer", None)
    role = authorizer(request) if authorizer else None
    return role if role in {"operator", "admin"} else "reader"

def _settings_store() -> RuntimeSettingsStore:
    return RuntimeSettingsStore(PROJECTS_ROOT, RuntimePolicy.from_file(RUNTIME_CONFIG))


def _translate_model_call_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException): return exc
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail={"code": "MODEL_CALL_NOT_FOUND", "message": "模型调用不存在。"})
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail={"code": "MODEL_CALL_CURSOR_INVALID", "message": str(exc)})
    return HTTPException(status_code=503, detail={"code": "MODEL_CALL_AUDIT_UNAVAILABLE",
                                                   "message": "模型调用审计暂不可读取。"})


@app.middleware("http")
async def enforce_request_size(request: Request, call_next):
    """在 JSON 解析前拒绝超大请求，避免内存型拒绝服务。"""
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > MAX_REQUEST_BYTES:
                return JSONResponse(status_code=413, content={"detail": "请求内容超过 512 KiB 限制。"})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Content-Length 无效。"})
    return await call_next(request)


def _safe_project_id(value: str) -> str:
    value = value.strip()
    if not PROJECT_ID.fullmatch(value):
        raise HTTPException(status_code=422, detail="工程 ID 仅允许 2–64 位字母、数字、下划线和连字符。")
    return value


def _store(project_id: str) -> ProjectStore:
    return ProjectStore(PROJECTS_ROOT, _safe_project_id(project_id))


def _runner(store: ProjectStore, offline: bool, runtime_policy: RuntimePolicy | None = None) -> WorkflowRunner:
    if not MODEL_CONFIG.is_file():
        raise RuntimeError("模型配置文件不存在，请设置 IMAGE_AGENT_MODEL_CONFIG。")
    policy = runtime_policy or RuntimePolicy.model_validate(store.runtime_snapshot()["policy"])
    return WorkflowRunner(store, MODEL_CONFIG, offline_mode=offline, runtime_policy=policy,
                          provider_api_key=_settings_store().secret("provider_api_key"))


def _options(body: AdvanceRequest) -> RunnerOptions:
    action = None
    if body.manual_action:
        action = ManualAction(action=body.manual_action, edited_delta=body.edited_delta)
    return RunnerOptions(
        selected_id=body.selected_id,
        manual_action=action,
        human_prompt=body.human_prompt,
        guided_edit=body.guided_edit,
        edited_markdown=body.edited_markdown,
        task_spec_action=body.task_spec_action,
        final_action=body.final_action,
        actor=body.actor,
        clarification_answers=body.clarification_answers,
        quality_action=body.quality_action,
        idempotency_key=body.idempotency_key,
        expense_confirmed=body.expense_confirmed,
    )


def _project_view(store: ProjectStore) -> dict[str, Any]:
    manifest = store.manifest()
    snapshot = store.resume() or {}
    return {
        "project_id": store.project_id,
        "manifest": manifest,
        "snapshot": snapshot,
        "history": store.history(),
        "execution_cursor": store.execution_cursor(),
        "business_status": _business_status(snapshot),
        "capabilities": _capabilities(manifest, snapshot),
    }


def _business_status(snapshot: dict[str, Any]) -> str:
    phase = str(snapshot.get("phase") or "")
    waiting = {
        "waiting_clarification": "waiting_clarification",
        "waiting_task_spec_confirmation": "waiting_task_spec_confirmation",
        "waiting_master_selection": "waiting_master_selection",
        "waiting_human_approval": "waiting_quality_decision",
        "waiting_quality_disposition": "waiting_quality_disposition",
        "waiting_human_rework": "waiting_human_rework",
        "waiting_reinspection": "waiting_reinspection",
        "waiting_final_confirmation": "waiting_final_confirmation",
    }
    if phase in waiting:
        return waiting[phase]
    if snapshot.get("delivery_frozen"):
        return "delivery_frozen"
    return str(snapshot.get("state") or "received")


def _job_store(project_id: str) -> JobStore:
    store = _store(project_id)
    store.manifest()
    snapshot = store.runtime_snapshot()
    policy = RuntimePolicy.model_validate(snapshot["policy"])
    return JobStore(store.root, max_attempts=policy.max_render_retries + 1)


def _execute_job(project_id: str, reference: dict[str, Any]) -> None:
    jobs = _job_store(project_id)
    job = jobs.claim(reference["job_id"])
    if job is None:
        return
    store = _store(project_id)
    store.events.append("job_started", job_id=job["job_id"], attempt=job["attempt"])
    try:
        jobs.heartbeat(job["job_id"], completed=0, total=1, unit="workflow")
        payload = job["payload"]
        body = AdvanceRequest.model_validate(payload["options"])
        snapshot = store.resume()
        if snapshot is None:
            raise ValueError("工程还没有可恢复节点。")
        frozen_settings = payload.get("runtime_settings") or store.runtime_snapshot()
        runner = _runner(store, payload["mode"] == "offline",
                         RuntimePolicy.model_validate(frozen_settings["policy"]))
        runner.should_cancel = lambda: jobs.cancellation_requested(job["job_id"])
        runner.progress = lambda completed, total, unit: jobs.heartbeat(
            job["job_id"], completed=completed, total=total, unit=unit
        )
        runner.run(snapshot, _options(body))
        from agent_core.error_taxonomy import JobCancelledError
        if jobs.cancellation_requested(job["job_id"]):
            raise JobCancelledError("作业在完成前收到取消请求。")
        if not jobs.cancellation_requested(job["job_id"]):
            current = jobs.get(job["job_id"])["progress"]
            jobs.heartbeat(job["job_id"], completed=current["total"], total=current["total"], unit=current["unit"])
        finished = jobs.finish(job["job_id"])
        store.events.append("job_finished", job_id=job["job_id"], status=finished["status"])
    except Exception as exc:
        from agent_core.error_taxonomy import error_record
        stage = str((store.execution_cursor() or {}).get("handler") or "workflow")
        finished = jobs.finish(job["job_id"], error=error_record(exc, stage=stage))
        store.events.append("job_finished", job_id=job["job_id"], status=finished["status"], error=finished.get("error"))


JOB_WORKER = WorkflowJobWorker(_execute_job)
HEALTH_SERVICE: HealthService | None = None


def _provider_configured_readonly() -> bool:
    """Inspect secret state without invoking RuntimeSettingsStore's lock-file write path."""
    path = PROJECTS_ROOT / ".runtime-settings.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool((data.get("secrets") or {}).get("provider_api_key"))
    except (OSError, ValueError, TypeError):
        return False


def _health_service() -> HealthService:
    global HEALTH_SERVICE
    if HEALTH_SERVICE is None:
        HEALTH_SERVICE = HealthService(
            PROJECTS_ROOT, MODEL_CONFIG,
            provider_configured=_provider_configured_readonly,
            asset_proxy_configured=lambda: any(
                getattr(route, "path", None) == "/api/projects/{project_id}/assets/{artifact_id}"
                for route in app.routes
            ),
        )
    return HEALTH_SERVICE


def _recover_jobs(project_id: str) -> None:
    store = _store(project_id)
    for job_id in _job_store(project_id).recoverable():
        store.events.append("job_recovered", job_id=job_id, status="recovered")
        JOB_WORKER.submit(project_id, job_id)


def _capabilities(manifest: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    """Project canonical state actions into the legacy HTTP view."""
    if manifest.get("failed_step"):
        return ["retry"]
    if snapshot.get("completed"):
        return ["inspect", "branch"]
    from agent_core.workflow import allowed_actions, project_execution_cursor
    cursor = project_execution_cursor(str(snapshot.get("state") or ""), snapshot)
    if cursor:
        actions = list(allowed_actions(str(cursor["product_state"])))
        if cursor["product_state"] == "human_rework":
            actions.append("branch")
        return actions
    if snapshot:
        return ["resume", "branch"]
    return []


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=422, detail=exc.errors(include_url=False))
    if isinstance(exc, SettingsForbidden):
        return HTTPException(status_code=403, detail={"code": "SETTINGS_FORBIDDEN", "message": str(exc)})
    if isinstance(exc, SettingsConflict):
        return HTTPException(status_code=409, detail={"code": "SETTINGS_VERSION_CONFLICT", "message": str(exc)})
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return HTTPException(status_code=404, detail="工程或资源不存在。")
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail=str(exc))
    if "正在由另一个进程处理" in str(exc):
        return HTTPException(status_code=423, detail=str(exc))
    return HTTPException(
        status_code=503,
        detail=f"后端能力暂不可用：{exc}。已有进度已保留，可修正配置后恢复或重试。",
    )


def _translate_observability_error(exc: Exception) -> HTTPException:
    """Keep storage paths, parser details and raw exceptions out of public telemetry APIs."""
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return HTTPException(status_code=404, detail="工程或事件资源不存在。")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=503, detail={
        "code": "OBSERVABILITY_UNAVAILABLE", "trace_id": f"trace_{uuid4().hex}",
        "message": "事件或进度数据暂不可读取。",
    })


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_ROOT / "index.html", media_type="text/html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Compatibility liveness endpoint; no dependency probes or internal paths."""
    result = _health_service().liveness()
    return {**result, "status": "ok"}


@app.get("/api/health/live")
async def health_live() -> dict[str, Any]:
    return _health_service().liveness()


@app.get("/api/health/ready")
async def health_ready() -> JSONResponse:
    result = await asyncio.to_thread(_health_service().readiness)
    return JSONResponse(status_code=503 if result["status"] == "not_ready" else 200, content=result)


@app.get("/api/internal/diagnostics/{trace_id}")
async def health_diagnostics(trace_id: str, request: Request) -> dict[str, Any]:
    """Raw probe details are isolated behind the host application's admin hook."""
    if _settings_role(request) != "admin":
        raise HTTPException(status_code=403, detail={"code": "DIAGNOSTICS_FORBIDDEN", "message": "无内部诊断权限。"})
    result = _health_service().diagnostics(trace_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "DIAGNOSTIC_TRACE_NOT_FOUND", "message": "诊断记录不存在或已过期。"})
    return result

@app.get("/api/runtime-settings")
async def list_runtime_settings() -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_settings_store().describe)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "SETTINGS_UNAVAILABLE", "message": "运行时设置暂不可读取。"}) from exc

@app.get("/api/runtime-settings/{key}")
async def get_runtime_setting(key: str) -> dict[str, Any]:
    result = await list_runtime_settings()
    field = next((item for item in result["fields"] if item["key"] == key), None)
    if field is None:
        raise HTTPException(status_code=404, detail={"code": "SETTING_NOT_FOUND", "message": "设置不存在。"})
    return {"schema_version": result["schema_version"], "version": result["version"],
            "sha256": result["sha256"], "field": field}

@app.patch("/api/runtime-settings")
async def update_runtime_settings(body: RuntimeSettingsUpdateRequest, request: Request) -> dict[str, Any]:
    try:
        role = _settings_role(request)
        if role == "reader": raise SettingsForbidden("当前主体没有设置写权限。")
        return await asyncio.to_thread(_settings_store().update, body.changes,
            expected_version=body.expected_version, actor=body.actor, role=role,
            dangerous_confirmed=body.dangerous_confirmed)
    except Exception as exc:
        if isinstance(exc, ValidationError):
            raise HTTPException(status_code=422, detail={"code": "SETTINGS_VALIDATION_FAILED",
                                "message": str(exc)}) from exc
        translated = _translate_error(exc)
        if translated.status_code == 409 and not isinstance(exc, SettingsConflict):
            translated.status_code = 422
            translated.detail = {"code": "SETTINGS_VALIDATION_FAILED", "message": str(exc)}
        raise translated from exc

@app.post("/api/projects/{project_id}/branches/apply-runtime-settings")
async def apply_runtime_settings_to_new_branch(project_id: str, body: BranchSettingsApplyRequest,
                                                request: Request) -> dict[str, Any]:
    """Explicitly fork before applying project-level settings; mode is never part of this DTO."""
    try:
        role = _settings_role(request)
        if role != "admin": raise SettingsForbidden("只有管理员可把项目级设置应用到新分支。")
        current = _settings_store().snapshot()
        if current["version"] != body.settings_version: raise SettingsConflict("设置版本冲突，请刷新后重试。")
        store = _store(project_id)
        before_project = (store.root / "project.yaml").read_bytes()
        branch = await asyncio.to_thread(store.branch_from, body.checkpoint, name=body.name,
            actor=body.actor, expected_version=body.expected_version, runtime_settings=current)
        if (store.root / "project.yaml").read_bytes() != before_project:
            raise RuntimeError("项目不可变运行模式快照被意外修改。")
        return {"branch": branch, "runtime_settings_version": current["version"],
                "runtime_settings_sha256": current["sha256"], "branches": store.list_branches()}
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects")
async def list_projects() -> dict[str, Any]:
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    projects: list[dict[str, Any]] = []
    for child in sorted(PROJECTS_ROOT.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not child.is_dir() or not PROJECT_ID.fullmatch(child.name) or not (child / "manifest.json").is_file():
            continue
        try:
            view = _project_view(ProjectStore(PROJECTS_ROOT, child.name))
            projects.append({
                "project_id": child.name,
                "state": view["snapshot"].get("state"),
                "phase": view["snapshot"].get("phase"),
                "completed": bool(view["snapshot"].get("completed")),
                "failed_step": view["manifest"].get("failed_step"),
                "updated_at": view["manifest"].get("updated_at"),
            })
        except (OSError, ValueError):
            continue
    return {"items": projects}


@app.post("/api/projects", status_code=status.HTTP_201_CREATED)
async def create_project(body: CreateProjectRequest) -> dict[str, Any]:
    project_id = _safe_project_id(body.project_id)
    try:
        envelope = DesignTaskEnvelope.model_validate(body.envelope) if body.envelope else None
        if not envelope and body.task_card is None:
            raise ValueError("task_card 或 envelope 必须提供一个。")
        task = envelope.task if envelope else ImageTaskCard.model_validate(body.task_card)
        if task.project_id != project_id:
            task = task.model_copy(update={"project_id": project_id})

        def execute() -> dict[str, Any]:
            raw_hash = content_hash(body.envelope) if envelope else None
            claimed_project = project_id
            newly_claimed = False
            if envelope:
                claimed_project, newly_claimed = ProjectStore.claim_design_task(PROJECTS_ROOT, project_id, envelope.idempotency_key, raw_hash)
                claimed_store = _store(claimed_project)
                if not newly_claimed and (claimed_store.root / "manifest.json").exists():
                    return _project_view(claimed_store)
                if not newly_claimed:
                    raise ValueError("同一幂等任务正在创建中，请稍后重试。")
            store = _store(claimed_project)
            try:
                claimed_task = task if task.project_id == claimed_project else task.model_copy(update={"project_id": claimed_project})
                policy = RuntimePolicy.model_validate(_settings_store().snapshot()["policy"])
                metadata = {"runtime_policy": policy.snapshot("offline" if body.offline else "real")}
                if envelope:
                    metadata.update({"design_task_schema_version": envelope.schema_version,
                                     "idempotency_key": envelope.idempotency_key,
                                     "raw_task_sha256": raw_hash})
                recovery_claim = (envelope.idempotency_key, raw_hash) if envelope and store.root.exists() else None
                store.create(metadata, recovery_claim=recovery_claim)
                if envelope:
                    ProjectStore.finish_design_task(PROJECTS_ROOT, envelope.idempotency_key, raw_hash, claimed_project)
                _runner(store, body.offline).run({"task_card": claimed_task.model_dump(mode="json"),
                    "raw_design_task_envelope": body.envelope if envelope else None}, RunnerOptions())
                return _project_view(store)
            except Exception:
                if envelope and newly_claimed and not (store.root / "manifest.json").is_file():
                    ProjectStore.abandon_design_task(PROJECTS_ROOT, envelope.idempotency_key, raw_hash, claimed_project)
                raise

        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}/delivery", response_model=DesignDeliveryEnvelope)
async def get_delivery(project_id: str) -> DesignDeliveryEnvelope:
    """Read the latest standalone Delivery without consulting a checkpoint."""
    try:
        return DesignDeliveryEnvelope.model_validate(DeliveryService(_store(project_id)).get())
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.post("/api/projects/{project_id}/delivery/generate", response_model=DesignDeliveryEnvelope)
async def generate_delivery(project_id: str) -> DesignDeliveryEnvelope:
    """Explicit retry boundary for note and immutable Delivery generation."""
    try:
        store = _store(project_id)
        result = await asyncio.to_thread(DeliveryService(store).generate, store.resume() or {})
        return DesignDeliveryEnvelope.model_validate(result)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.post("/api/projects/{project_id}/delivery/return")
async def return_delivery(project_id: str, body: ManualReturnRequest) -> dict[str, Any]:
    """Record a human return; no notification, polling, or webhook is performed."""
    try:
        service = DeliveryService(_store(project_id))
        return await asyncio.to_thread(service.record_return, body.delivery_version,
            actor=body.actor, target=body.target, idempotency_key=body.idempotency_key)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_project_view, _store(project_id))
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.post("/api/projects/{project_id}/advance", status_code=status.HTTP_202_ACCEPTED)
async def advance_project(project_id: str, body: AdvanceRequest) -> dict[str, Any]:
    try:
        store = _store(project_id)
        snapshot = store.resume()
        if snapshot is None:
            raise ValueError("工程还没有可恢复节点。")
        mode = "offline" if body.offline else "real"
        store.assert_runtime_mode(mode)
        options = body.model_dump(mode="json")
        supplied_key = options.get("idempotency_key")
        key = supplied_key or content_hash([store.manifest()["current_checkpoint"], options])
        settings = _settings_store().snapshot()
        jobs = JobStore(store.root, max_attempts=int(settings["policy"]["max_render_retries"]) + 1)
        job, created = jobs.create(key, {"options": options, "mode": mode, "runtime_settings": settings})
        if created:
            store.events.append("job_queued", job_id=job["job_id"], idempotency_key=key)
        JOB_WORKER.submit(project_id, job["job_id"])
        return {"job_id": job["job_id"], "status": job["status"], "created": created,
                "status_url": f"/api/projects/{project_id}/jobs/{job['job_id']}"}
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}/jobs/{job_id}")
async def get_job(project_id: str, job_id: str) -> dict[str, Any]:
    try:
        _recover_jobs(project_id)
        return _job_store(project_id).get(job_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.post("/api/projects/{project_id}/jobs/{job_id}/cancel")
async def cancel_job(project_id: str, job_id: str) -> dict[str, Any]:
    try:
        result = _job_store(project_id).cancel(job_id)
        _store(project_id).events.append("job_cancel_requested", job_id=job_id, status=result["status"])
        return result
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}/events")
async def project_events(project_id: str, request: Request, after: int = 0) -> StreamingResponse:
    """SSE stream with monotonic sequence resume (`after` or Last-Event-ID)."""
    try:
        store = _store(project_id)
        if not (store.root / "manifest.json").is_file():
            raise FileNotFoundError("工程不存在。")
        cursor = max(after, int(request.headers.get("last-event-id", "0") or 0))
    except Exception as exc:
        raise _translate_observability_error(exc) from exc

    async def stream():
        nonlocal cursor
        idle = 0
        while idle < 150 and not await request.is_disconnected():
            try:
                page = event_page(store.events, limit=100, since=cursor)
            except Exception:
                trace_id = f"trace_{uuid4().hex}"
                error = {"code": "OBSERVABILITY_UNAVAILABLE", "trace_id": trace_id,
                         "message": "事件数据暂不可读取。"}
                yield f"event: observability_error\ndata: {json.dumps(error, ensure_ascii=False)}\n\n"
                return
            events = page["items"]
            if events:
                idle = 0
                for event in events:
                    cursor = int(event["sequence"])
                    yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            else:
                idle += 1
                yield ": keepalive\n\n"
            await asyncio.sleep(.2)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/projects/{project_id}/event-log")
async def project_event_log(project_id: str,
                            limit: int = Query(default=50, ge=1, le=100),
                            cursor: str | None = Query(default=None, max_length=512),
                            since: int | None = Query(default=None, ge=0)) -> dict[str, Any]:
    """Bounded, read-only event pages with a frozen high-water mark."""
    try:
        store = _store(project_id)
        if not (store.root / "manifest.json").is_file():
            raise FileNotFoundError("工程不存在。")
        return await asyncio.to_thread(event_page, store.events, limit=limit, cursor=cursor, since=since)
    except Exception as exc:
        raise _translate_observability_error(exc) from exc


@app.get("/api/projects/{project_id}/progress")
async def project_progress(project_id: str) -> dict[str, Any]:
    """Read-only progress derived exclusively from durable jobs/checkpoints/events."""
    try:
        store = _store(project_id)
        if not (store.root / "manifest.json").is_file():
            raise FileNotFoundError("工程不存在。")
        return await asyncio.to_thread(progress_projection, store.root, store.events)
    except Exception as exc:
        raise _translate_observability_error(exc) from exc


@app.get("/api/projects/{project_id}/model-calls")
async def list_model_calls(project_id: str, limit: int = Query(50, ge=1, le=100),
                           cursor: str | None = Query(None, max_length=512)) -> dict[str, Any]:
    """Read-only, redacted model-call summaries."""
    try:
        store = _store(project_id)
        if not (store.root / "manifest.json").is_file(): raise FileNotFoundError(project_id)
        return await asyncio.to_thread(store.prompts.list_calls, limit=limit, cursor=cursor)
    except Exception as exc:
        raise _translate_model_call_error(exc) from exc


@app.get("/api/projects/{project_id}/model-calls/{call_id}")
async def get_model_call(project_id: str, call_id: str, request: Request,
                         detail: bool = False) -> dict[str, Any]:
    try:
        if not re.fullmatch(r"call_[a-f0-9]{32}", call_id): raise FileNotFoundError(call_id)
        store = _store(project_id)
        if not (store.root / "manifest.json").is_file(): raise FileNotFoundError(project_id)
        if detail and not _trace_detail_allowed(request, project_id):
            raise HTTPException(status_code=403, detail={"code": "MODEL_CALL_DETAIL_FORBIDDEN",
                                                         "message": "无权读取模型调用详情。"})
        value = await asyncio.to_thread(store.prompts.detail if detail else store.prompts.summary, call_id)
        return {"view": "detail" if detail else "summary", "retention": "append_only_audit", "call": value}
    except Exception as exc:
        raise _translate_model_call_error(exc) from exc


@app.get("/api/projects/{project_id}/model-calls/{call_id}/text-deltas")
async def get_model_call_deltas(project_id: str, call_id: str, after: int = Query(0, ge=0),
                                limit: int = Query(100, ge=1, le=100)) -> dict[str, Any]:
    try:
        if not re.fullmatch(r"call_[a-f0-9]{32}", call_id): raise FileNotFoundError(call_id)
        store = _store(project_id)
        if not (store.root / "manifest.json").is_file(): raise FileNotFoundError(project_id)
        return await asyncio.to_thread(store.prompts.chunks, call_id, after=after, limit=limit)
    except Exception as exc:
        raise _translate_model_call_error(exc) from exc


@app.post("/api/projects/{project_id}/retry")
async def retry_project(project_id: str, body: AdvanceRequest) -> dict[str, Any]:
    try:
        def execute() -> dict[str, Any]:
            store = _store(project_id)
            runner = _runner(store, body.offline)
            store.retry(lambda state_name, snapshot: runner.run(snapshot, _options(body), only_state=state_name))
            return _project_view(store)

        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.post("/api/projects/{project_id}/branches")
async def create_branch(project_id: str, body: BranchRequest) -> dict[str, Any]:
    try:
        def execute() -> dict[str, Any]:
            store = _store(project_id)
            if body.name:
                _safe_project_id(body.name)
            with store.lock():
                store.branch_from(body.checkpoint, name=body.name, actor=body.actor,
                                  expected_version=body.expected_version)
            return {"branches": store.list_branches(), "project": _project_view(store)}

        return await asyncio.to_thread(execute)
    except Exception as exc:
        raise _translate_error(exc) from exc

@app.get("/api/projects/{project_id}/branches")
async def list_project_branches(project_id: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_store(project_id).list_branches)
    except Exception as exc:
        raise _translate_error(exc) from exc

@app.get("/api/projects/{project_id}/history")
async def list_project_history(project_id: str, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_store(project_id).history_index, limit=limit, cursor=cursor)
    except Exception as exc:
        raise _translate_error(exc) from exc

@app.get("/api/projects/{project_id}/history/{node_id}")
async def get_project_history(project_id: str, node_id: str) -> dict[str, Any]:
    try:
        if not re.fullmatch(r"history_[a-f0-9]{32}", node_id):
            raise FileNotFoundError("历史节点不存在。")
        return await asyncio.to_thread(_store(project_id).history_detail, node_id)
    except Exception as exc:
        raise _translate_error(exc) from exc

@app.post("/api/projects/{project_id}/history/{node_id}/reopen-preview")
async def preview_history_reopen(project_id: str, node_id: str,
                                 body: HistoryReopenPreviewRequest) -> dict[str, Any]:
    try:
        if not re.fullmatch(r"history_[a-f0-9]{32}", node_id):
            raise FileNotFoundError("历史节点不存在。")
        return await asyncio.to_thread(_store(project_id).history_reopen_preview, node_id, name=body.name)
    except Exception as exc:
        raise _translate_error(exc) from exc

@app.get("/api/projects/{project_id}/checkpoints/{branch}/{filename}")
async def inspect_project_checkpoint(project_id: str, branch: str, filename: str) -> dict[str, Any]:
    """P2-01 read-only inspection; it deliberately has no state mutation path."""
    try:
        return await asyncio.to_thread(_store(project_id).inspect_checkpoint,
                                       f"checkpoints/{branch}/{filename}")
    except Exception as exc:
        raise _translate_error(exc) from exc

@app.post("/api/projects/{project_id}/branches/switch")
async def switch_project_branch(project_id: str, body: BranchSwitchRequest) -> dict[str, Any]:
    try:
        store = _store(project_id)
        await asyncio.to_thread(store.switch_branch, body.branch_id, body.checkpoint,
                                expected_version=body.expected_version)
        return {"branches": store.list_branches(), "project": _project_view(store)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@app.get("/api/projects/{project_id}/assets/{artifact_id}")
async def get_asset(project_id: str, artifact_id: str) -> FileResponse:
    """Resolve a stable, project-scoped artifact id to a controlled response."""
    if not re.fullmatch(r"artifact_[a-f0-9]{64}", artifact_id):
        raise HTTPException(status_code=422, detail="资源标识无效。")
    store = _store(project_id)
    try:
        asset = store.artifacts.resolve(artifact_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="图片资源不存在。")
    if asset.stat().st_size > MAX_ASSET_BYTES:
        raise HTTPException(status_code=413, detail="图片超过 25 MiB 下载限制。")
    media_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
    if media_type not in IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="资源类型不受支持。")
    return FileResponse(asset, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})
