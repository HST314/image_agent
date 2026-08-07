"""Versioned, atomic, file-backed project workspace."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

FORMAT_VERSION = 1
CHECKPOINT_ENVELOPE_VERSION = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def content_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("wb") as stream:
        stream.write(_canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class CorruptProjectError(ValueError):
    pass


class ProjectLockError(RuntimeError):
    pass

class ProjectExistsError(FileExistsError):
    pass

class ImmutableRecordError(FileExistsError):
    pass


class LegacyCheckpointReadOnlyError(ValueError):
    """A legacy checkpoint is intact but cannot safely drive execution."""


class EventStore:
    _guards: dict[str, threading.Lock] = {}
    _guards_guard = threading.Lock()
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event_type: str, **payload: Any) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.path.resolve())
        with self._guards_guard:
            guard = self._guards.setdefault(key, threading.Lock())
        with guard:
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                existing = self.read_all()
                event = {"format_version": FORMAT_VERSION, "event_id": uuid4().hex,
                         "sequence": int(existing[-1].get("sequence", len(existing))) + 1 if existing else 1,
                         "timestamp": _now(), "type": event_type, **payload}
                encoded = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode()
                if os.write(descriptor, encoded) != len(encoded):
                    raise OSError("事件日志写入不完整")
                os.fsync(descriptor)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        return event

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _LegacyPromptStore:
    SECRET_WORDS = ("api_key", "apikey", "authorization", "token", "secret")

    def __init__(self, root: Path) -> None:
        self.root = root

    REQUIRED = {"messages", "template_id", "template_version", "template_hash", "variables", "input_refs", "model", "parameters", "config_hash", "state", "trace_id"}

    def begin(self, record: dict[str, Any]) -> str:
        missing = self.REQUIRED - record.keys()
        if missing:
            raise ValueError(f"Prompt 审计记录缺少必填项：{', '.join(sorted(missing))}")
        prompt_id = str(record.get("prompt_id") or f"prompt_{uuid4().hex}")
        sanitized = self._redact(record)
        data = {"format_version": FORMAT_VERSION, "prompt_id": prompt_id, "created_at": _now(), "status": "started", **sanitized}
        data["record_hash"] = content_hash(data)
        path = self.root / f"{prompt_id}.json"
        if path.exists():
            raise ImmutableRecordError("Prompt 记录不可覆盖。")
        atomic_json(path, data)
        return prompt_id

    def complete(self, prompt_id: str, *, output_raw: Any, output_parsed: Any = None, output_ref: str | None = None) -> str:
        original = self.get(prompt_id)
        record = {**original, "parent_record_hash": original["record_hash"], "status": "completed", "completed_at": _now(), "output_raw": self._redact(output_raw), "output_parsed": self._redact(output_parsed), "output_ref": output_ref}
        record.pop("record_hash", None)
        record["record_hash"] = content_hash(record)
        result_id = f"{prompt_id}.result"
        path = self.root / f"{result_id}.json"
        if path.exists():
            raise ImmutableRecordError("Prompt 输出审计记录不可覆盖。")
        atomic_json(path, record)
        return result_id

    def save(self, record: dict[str, Any]) -> str:
        """Compatibility entry point; still enforces the strong contract."""
        return self.begin(record)

    def get(self, prompt_id: str) -> dict[str, Any]:
        data = json.loads((self.root / f"{prompt_id}.json").read_text(encoding="utf-8"))
        checksum = data.pop("record_hash", None)
        if data.get("format_version") != FORMAT_VERSION or checksum != content_hash(data):
            raise CorruptProjectError("Prompt 记录版本或完整性校验失败。")
        data["record_hash"] = checksum
        return data

    @classmethod
    def _redact(cls, value: Any) -> Any:
        # 新加这 2 行：如果是 Pydantic 对象，自动转为字典
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")

        if isinstance(value, dict):
            return {k: "[REDACTED]" if any(word in k.lower() for word in cls.SECRET_WORDS) else cls._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.metadata = root / "metadata.jsonl"

    def save_bytes(self, content: bytes, *, suffix: str, metadata: dict[str, Any]) -> dict[str, Any]:
        digest = hashlib.sha256(content).hexdigest()
        suffix = suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            raise ValueError("不支持的图片文件类型。")
        filename = f"{digest}{suffix}"
        path = self.root / "images" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
        artifact_id = f"artifact_{digest}"
        record = {"format_version": FORMAT_VERSION, "artifact_id": artifact_id,
                  "uri": f"artifact://{artifact_id}", "sha256": digest,
                  "content_sha256": digest, "filename": filename, **metadata}
        EventStore(self.metadata).append("artifact_saved", **record)
        return record

    def resolve(self, artifact_id: str) -> Path:
        """Resolve a project-scoped artifact id without accepting paths."""
        if not artifact_id.startswith("artifact_") or len(artifact_id) != 73:
            raise FileNotFoundError("图片资源不存在。")
        records = EventStore(self.metadata).read_all()
        record = next((item for item in reversed(records)
                       if item.get("type") == "artifact_saved" and item.get("artifact_id") == artifact_id), None)
        if record is None:
            raise FileNotFoundError("图片资源不存在。")
        filename = str(record.get("filename", ""))
        candidate = (self.root / "images" / filename).resolve()
        allowed = (self.root / "images").resolve()
        if allowed not in candidate.parents or not candidate.is_file():
            raise FileNotFoundError("图片资源不存在。")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != record.get("sha256"):
            raise CorruptProjectError("图片资源内容哈希校验失败。")
        return candidate


class CheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, branch: str, sequence: int, state: str, data: dict[str, Any],
             *, execution_cursor: dict[str, Any] | None = None) -> tuple[str, str]:
        if execution_cursor is None:
            from agent_core.workflow import project_execution_cursor
            execution_cursor = project_execution_cursor(state, data)
        if execution_cursor is None:
            raise LegacyCheckpointReadOnlyError(f"状态 {state!r} 无法映射到版本化执行游标；仅允许审计读取。")
        envelope = {"format_version": FORMAT_VERSION, "checkpoint_envelope_version": CHECKPOINT_ENVELOPE_VERSION,
                    "branch": branch, "sequence": sequence, "state": state,
                    "execution_cursor": execution_cursor,
                    "compatibility_projection": {"state": state, "phase": data.get("phase")}, "data": data}
        envelope["checksum"] = content_hash(envelope)
        relative = f"checkpoints/{branch}/{sequence:06d}-{state}.json"
        path = self.root / relative
        if path.exists():
            raise ImmutableRecordError("成功检查点不可覆盖。")
        atomic_json(path, envelope)
        return relative, envelope["checksum"]

    def load(self, relative: str) -> dict[str, Any]:
        envelope = json.loads((self.root / relative).read_text(encoding="utf-8"))
        checksum = envelope.pop("checksum", None)
        if envelope.get("format_version") != FORMAT_VERSION or checksum != content_hash(envelope):
            raise CorruptProjectError("检查点版本或完整性校验失败。")
        version = envelope.get("checkpoint_envelope_version")
        if version is None:
            from agent_core.workflow import project_execution_cursor
            cursor = project_execution_cursor(str(envelope.get("state") or ""), envelope.get("data") or {})
            envelope["checkpoint_envelope_version"] = 1
            envelope["execution_cursor"] = cursor
            envelope["legacy_read_only"] = cursor is None
        elif version != CHECKPOINT_ENVELOPE_VERSION:
            raise CorruptProjectError("检查点 envelope 版本不受支持。")
        envelope["checksum"] = checksum
        return envelope


class ProjectStore:
    """Own project manifest, branches, prompts, events, artifacts and checkpoints."""

    def __init__(self, projects_root: str | Path, project_id: str) -> None:
        self.root = Path(projects_root) / project_id
        self.project_id = project_id
        self.events = EventStore(self.root / "events/events.jsonl")
        from storage.prompt_store import PromptStore
        self.prompts = PromptStore(self.root / "runtime/prompts.jsonl")
        self.artifacts = ArtifactStore(self.root / "artifacts")
        self.checkpoints = CheckpointStore(self.root)
        self._lock_depth = 0
        self._lock_owner: int | None = None
        self._lock_guard = threading.RLock()
        self._lock_descriptor: int | None = None

    def create(self, config: dict[str, Any] | None = None,
               recovery_claim: tuple[str, str] | None = None) -> dict[str, Any]:
        if self.root.exists():
            if recovery_claim is None:
                raise ProjectExistsError("工程已存在；请使用 resume、retry 或 rewind，禁止重复 new。")
            self._assert_empty_directory_owned_by_claim(*recovery_claim)
        else:
            self.root.mkdir(parents=True, exist_ok=False)
        manifest = {"format_version": FORMAT_VERSION, "project_id": self.project_id, "current_branch": "main", "current_checkpoint": None, "failed_step": None, "created_at": _now(), "updated_at": _now()}
        atomic_json(self.root / "manifest.json", manifest)
        if config is None:
            from configs.runtime_policy import RuntimePolicy
            config = {"runtime_policy": RuntimePolicy.from_file(Path("configs/runtime.yaml")).snapshot("offline")}
        atomic_json(self.root / "project.yaml", config)
        atomic_json(self.root / "branches.json", {"format_version": FORMAT_VERSION, "branches": {"main": {"parent": None, "from_checkpoint": None, "created_at": _now()}}})
        self.events.append("project_created", branch="main")
        return manifest

    def _assert_empty_directory_owned_by_claim(self, key: str, raw_hash: str) -> None:
        """Allow takeover only for this live claim's empty pre-manifest directory."""
        projects_root = self.root.parent
        descriptor = os.open(projects_root / ".design-task-idempotency.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            registry_path = projects_root / ".design-task-idempotency.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            record = registry.get(key)
            if (not record or record.get("raw_hash") != raw_hash
                    or record.get("project_id") != self.project_id
                    or record.get("status") != "pending"
                    or record.get("owner_pid") != os.getpid()
                    or record.get("owner_start") != _process_start_time(os.getpid())):
                raise ProjectExistsError("残留目录不属于当前幂等登记，拒绝接管。")
            if (self.root / "manifest.json").exists() or any(self.root.iterdir()):
                raise ProjectExistsError("残留目录包含工程或未知数据，拒绝接管。")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @classmethod
    def claim_design_task(cls, projects_root: str | Path, project_id: str, key: str, raw_hash: str) -> tuple[str, bool]:
        """Atomically bind an inbound idempotency key before any paid work starts."""
        root = Path(projects_root)
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / ".design-task-idempotency.lock"
        registry_path = root / ".design-task-idempotency.json"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
            existing = registry.get(key)
            if existing:
                if existing.get("raw_hash") != raw_hash:
                    raise ValueError("同一幂等键不能提交不同的原始任务。")
                canonical = str(existing["project_id"])
                manifest_exists = (root / canonical / "manifest.json").is_file()
                owner_alive = _same_process(existing.get("owner_pid"), existing.get("owner_start"))
                if existing.get("status") == "committed" or manifest_exists:
                    return canonical, False
                if existing.get("status") == "pending" and owner_alive:
                    return canonical, False
                existing.update(status="pending", owner_pid=os.getpid(),
                                owner_start=_process_start_time(os.getpid()), claimed_at=_now())
                atomic_json(registry_path, registry)
                return canonical, True
            if any(
                record.get("project_id") == project_id
                for registered_key, record in registry.items()
                if registered_key != key
            ):
                raise ProjectExistsError("工程目录已绑定其他幂等登记，拒绝接管。")
            registry[key] = {"project_id": project_id, "raw_hash": raw_hash, "status": "pending",
                             "owner_pid": os.getpid(), "owner_start": _process_start_time(os.getpid()),
                             "claimed_at": _now()}
            atomic_json(registry_path, registry)
            return project_id, True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @classmethod
    def finish_design_task(cls, projects_root: str | Path, key: str, raw_hash: str, project_id: str) -> None:
        cls._set_design_task_claim_status(projects_root, key, raw_hash, project_id, "committed")

    @classmethod
    def abandon_design_task(cls, projects_root: str | Path, key: str, raw_hash: str, project_id: str) -> None:
        cls._set_design_task_claim_status(projects_root, key, raw_hash, project_id, "abandoned")

    @classmethod
    def _set_design_task_claim_status(cls, projects_root: str | Path, key: str, raw_hash: str,
                                      project_id: str, status: str) -> None:
        root = Path(projects_root)
        descriptor = os.open(root / ".design-task-idempotency.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            path = root / ".design-task-idempotency.json"
            registry = json.loads(path.read_text(encoding="utf-8"))
            record = registry.get(key)
            if (not record or record.get("raw_hash") != raw_hash
                    or record.get("project_id") != project_id):
                raise ValueError("幂等登记与待更新任务不一致。")
            record["status"] = status
            record[f"{status}_at"] = _now()
            atomic_json(path, registry)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def runtime_snapshot(self) -> dict[str, Any]:
        value = json.loads((self.root / "project.yaml").read_text(encoding="utf-8"))
        if not value.get("runtime_policy"):
            raise CorruptProjectError("工程缺少运行策略快照。")
        return value["runtime_policy"]

    def assert_runtime_mode(self, mode: str) -> None:
        configured = self.runtime_snapshot().get("mode")
        if configured != mode:
            raise ValueError(f"工程运行模式已固化为 {configured}，不能切换为 {mode}。")

    def manifest(self) -> dict[str, Any]:
        self._recover_checkpoint_commit()
        data = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        if data.get("format_version") != FORMAT_VERSION:
            raise CorruptProjectError("工程版本不受支持。")
        return data

    def _recover_checkpoint_commit(self) -> None:
        """Finish an interrupted checkpoint/event/manifest commit from its WAL."""
        pending_path = self.root / "runtime/checkpoint-commit.json"
        if not pending_path.exists():
            return
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        pointer = pending["pointer"]
        checkpoint_path = self.root / pointer["path"]
        if not checkpoint_path.exists():
            # Intent was durable but the immutable record was never installed;
            # no event/manifest may point at it, so rollback is safe.
            pending_path.unlink()
            return
        # The immutable checkpoint must exist and validate before it can become visible.
        envelope = self.checkpoints.load(pointer["path"])
        if envelope["checksum"] != pointer["checksum"]:
            raise CorruptProjectError("待恢复检查点哈希不一致。")
        transaction_id = pending["transaction_id"]
        if not any(event.get("transaction_id") == transaction_id for event in self.events.read_all()):
            self.events.append("step_succeeded", branch=pointer["branch"], state=pointer["state"],
                               checkpoint=pointer["path"], transaction_id=transaction_id)
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        current = manifest.get("current_checkpoint") or {}
        if current.get("branch") != pointer["branch"] or int(current.get("sequence", 0)) < int(pointer["sequence"]):
            manifest.update(current_branch=pointer["branch"], current_checkpoint=pointer,
                            failed_step=None, updated_at=pending["updated_at"])
            atomic_json(manifest_path, manifest)
        pending_path.unlink()

    def checkpoint_context(self, state: str, context: Any, *, branch: str | None = None) -> str:
        return self.checkpoint(state, context.dump_snapshot(), branch=branch)

    def checkpoint(self, state: str, data: dict[str, Any], *, branch: str | None = None) -> str:
        manifest = self.manifest()
        active = branch or manifest["current_branch"]
        previous = manifest.get("current_checkpoint")
        sequence = 1 if not previous or previous.get("branch") != active else int(previous["sequence"]) + 1
        relative = f"checkpoints/{active}/{sequence:06d}-{state}.json"
        transaction_id = uuid4().hex
        # Write-ahead intent makes the three-file logical commit crash-recoverable.
        pending_path = self.root / "runtime/checkpoint-commit.json"
        from agent_core.workflow import project_execution_cursor
        cursor = project_execution_cursor(state, data)
        if cursor is None:
            raise LegacyCheckpointReadOnlyError(f"状态 {state!r} 无法映射到版本化执行游标；拒绝写入。")
        expected = {"format_version": FORMAT_VERSION, "checkpoint_envelope_version": CHECKPOINT_ENVELOPE_VERSION,
                    "branch": active, "sequence": sequence, "state": state,
                    "execution_cursor": cursor,
                    "compatibility_projection": {"state": state, "phase": data.get("phase")}, "data": data}
        checksum = content_hash(expected)
        pointer = {"path": relative, "checksum": checksum, "branch": active, "sequence": sequence, "state": state}
        updated_at = _now()
        atomic_json(pending_path, {"format_version": FORMAT_VERSION, "transaction_id": transaction_id,
                                   "pointer": pointer, "updated_at": updated_at})
        self.checkpoints.save(active, sequence, state, data, execution_cursor=cursor)
        self._recover_checkpoint_commit()
        return relative

    def start_step(self, state: str, **details: Any) -> None:
        self.events.append("step_started", branch=self.manifest()["current_branch"], state=state, **details)

    def fail_step(self, state: str, error: dict[str, Any]) -> None:
        manifest = self.manifest()
        manifest["failed_step"] = {"state": state, "error": error, "at": _now()}
        manifest["updated_at"] = _now()
        atomic_json(self.root / "manifest.json", manifest)
        self.events.append("step_failed", branch=manifest["current_branch"], state=state, error=error)

    def resume(self) -> dict[str, Any] | None:
        pointer = self.manifest().get("current_checkpoint")
        if not pointer:
            return None
        checkpoint = self.checkpoints.load(pointer["path"])
        if checkpoint.get("legacy_read_only"):
            raise LegacyCheckpointReadOnlyError("旧 checkpoint 状态不可安全映射；工程仅允许 inspect/history，禁止 resume/retry/branch。")
        return checkpoint["data"]

    def execution_cursor(self) -> dict[str, Any] | None:
        """Return the canonical cursor without leaking it into legacy snapshot data."""
        pointer = self.manifest().get("current_checkpoint")
        if not pointer:
            return None
        return self.checkpoints.load(pointer["path"]).get("execution_cursor")

    def retry(self, execute: Any, *, name: str | None = None) -> Any:
        manifest = self.manifest()
        failure = manifest.get("failed_step")
        pointer = manifest.get("current_checkpoint")
        if not failure:
            raise ValueError("当前没有失败步骤需要重试。")
        if not pointer:
            raise ValueError("失败步骤之前没有成功检查点，无法安全重试。")
        branch = self.branch_from(pointer["path"], name=name or f"retry-{uuid4().hex[:8]}")
        self.events.append("retry_started", branch=branch, state=failure["state"], from_checkpoint=pointer["path"])
        return execute(failure["state"], self.resume())

    def branch_from(self, checkpoint: str, *, name: str | None = None) -> str:
        source = self.checkpoints.load(checkpoint)
        if source.get("legacy_read_only"):
            raise LegacyCheckpointReadOnlyError("旧 checkpoint 状态不可安全映射；禁止从只读记录创建执行分支。")
        branches_path = self.root / "branches.json"
        branches = json.loads(branches_path.read_text(encoding="utf-8"))
        branch = name or f"branch-{uuid4().hex[:8]}"
        if branch in branches["branches"]:
            raise ValueError("分支名称已存在。")
        branches["branches"][branch] = {"parent": source["branch"], "from_checkpoint": checkpoint, "created_at": _now()}
        atomic_json(branches_path, branches)
        manifest = self.manifest()
        relative, checksum = self.checkpoints.save(branch, 1, source["state"], source["data"])
        manifest.update(current_branch=branch, current_checkpoint={"path": relative, "checksum": checksum, "branch": branch, "sequence": 1, "state": source["state"]}, failed_step=None, updated_at=_now())
        atomic_json(self.root / "manifest.json", manifest)
        self.events.append("branch_created", branch=branch, parent=source["branch"], from_checkpoint=checkpoint)
        return branch

    @contextmanager
    def lock(self) -> Iterator[None]:
        # The runner owns the project transaction. Helpers such as the candidate
        # batch may enter it again on the same store instance without deadlock.
        owner = threading.get_ident()
        if self._lock_depth and self._lock_owner == owner:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        lock_path = self.root / ".lock"
        with self._lock_guard:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise ProjectLockError("该工程正在由另一个进程处理，请稍后重试。") from exc
            info = {"pid": os.getpid(), "process_start": _process_start_time(os.getpid()), "thread_id": owner, "acquired_at": _now()}
            os.ftruncate(descriptor, 0)
            os.write(descriptor, _canonical(info))
            os.fsync(descriptor)
            self._lock_descriptor = descriptor
        try:
            self._lock_depth = 1
            self._lock_owner = owner
            yield
        finally:
            self._lock_depth = 0
            self._lock_owner = None
            descriptor = self._lock_descriptor
            self._lock_descriptor = None
            if descriptor is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
    def idempotency_key(self, state: str, checkpoint_hash: str, prompt_hash: str, model_hash: str, reference_hash: str = "") -> str:
        return content_hash([state, checkpoint_hash, prompt_hash, model_hash, reference_hash])

    def history(self) -> list[dict[str, Any]]:
        return self.events.read_all()


def _process_start_time(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[21]
    except (OSError, IndexError):
        return "unknown"


def _same_process(pid: Any, start: Any) -> bool:
    if not isinstance(pid, int) or not isinstance(start, str):
        return False
    actual = _process_start_time(pid)
    return actual != "unknown" and actual == start
