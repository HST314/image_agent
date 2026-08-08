"""Versioned, schema-driven runtime settings with secure secret handling."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from configs.runtime_policy import RuntimePolicy
from storage.project_store import atomic_json


class SettingsConflict(ValueError): pass
class SettingsForbidden(PermissionError): pass
class SettingsUnavailable(RuntimeError): pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


FIELD_META: dict[str, dict[str, Any]] = {
    "default_question_count": {"scope": "new_job", "risk": "low", "role": "operator", "help": "默认澄清问题数"},
    "question_mode": {"scope": "new_job", "risk": "medium", "role": "operator", "help": "澄清提问模式"},
    "max_auto_questions": {"scope": "new_job", "risk": "medium", "role": "operator", "help": "单轮自动问题上限"},
    "max_clarify_rounds": {"scope": "new_job", "risk": "medium", "role": "operator", "help": "澄清轮数上限"},
    "clarification_total_budget": {"scope": "new_job", "risk": "high", "role": "admin", "help": "澄清总费用预算"},
    "candidate_count": {"scope": "new_job", "risk": "high", "role": "admin", "help": "候选图数量/费用上限"},
    "candidate_concurrency": {"scope": "new_job", "risk": "high", "role": "admin", "help": "候选生成并发上限"},
    "candidate_min_mechanism_differences": {"scope": "new_job", "risk": "medium", "role": "operator", "help": "候选最低机制差异数"},
    "stream_model_output": {"scope": "new_job", "risk": "low", "role": "operator", "help": "文本模型流式输出"},
    "self_check.termination": {"scope": "new_job", "risk": "medium", "role": "operator", "help": "质检终止策略"},
    "self_check.fixed_rounds": {"scope": "new_job", "risk": "high", "role": "admin", "help": "固定质检轮数"},
    "self_check.max_rounds": {"scope": "new_job", "risk": "high", "role": "admin", "help": "质检费用轮数上限"},
    "self_check.stop_early_on_pass": {"scope": "new_job", "risk": "medium", "role": "operator", "help": "通过后提前停止"},
    "self_check.release": {"scope": "new_job", "risk": "high", "role": "admin", "help": "质检发布安全策略"},
    "self_check.rule_version": {"scope": "new_job", "risk": "high", "role": "admin", "help": "质检规则版本"},
    "approval_required": {"scope": "new_project", "risk": "critical", "role": "admin", "help": "任务书与最终确认门禁"},
    "max_render_retries": {"scope": "new_job", "risk": "high", "role": "admin", "help": "生图重试费用上限"},
    "max_calibration_retries": {"scope": "new_job", "risk": "high", "role": "admin", "help": "质检重试费用上限"},
    "retry_base_delay_seconds": {"scope": "new_job", "risk": "medium", "role": "operator", "help": "重试基础退避"},
    "retry_max_delay_seconds": {"scope": "new_job", "risk": "medium", "role": "operator", "help": "重试最大退避"},
    "model_timeout_seconds": {"scope": "new_job", "risk": "medium", "role": "operator", "help": "模型调用超时"},
    "image_api_base_url": {"scope": "new_project", "risk": "critical", "role": "admin", "help": "图片供应商端点"},
    "default_output_size": {"scope": "new_job", "risk": "medium", "role": "operator", "help": "默认输出尺寸"},
    "response_format": {"scope": "new_project", "risk": "high", "role": "admin", "help": "供应商响应格式"},
    "watermark": {"scope": "new_job", "risk": "medium", "role": "operator", "help": "水印开关"},
    "skill_failure_mode": {"scope": "new_project", "risk": "critical", "role": "admin", "help": "Skill 失败安全策略"},
    "provider_api_key": {"scope": "new_job", "risk": "critical", "role": "admin", "help": "供应商密钥", "sensitive": True},
}


def _get(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        current = current[part]
    return current


def _set(value: dict[str, Any], path: str, setting: Any) -> None:
    current = value
    parts = path.split(".")
    for part in parts[:-1]: current = current[part]
    current[parts[-1]] = setting


class RuntimeSettingsStore:
    def __init__(self, root: Path, defaults: RuntimePolicy):
        self.root, self.defaults = root, defaults
        self.path = root / ".runtime-settings.json"
        self.audit_path = root / ".runtime-settings.audit.jsonl"
        self.lock_path = root / ".runtime-settings.lock"

    def _locked(self, operation: Callable[[dict[str, Any]], Any], *, write: bool) -> Any:
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else self._initial()
            data.setdefault("secret_revisions", {})
            return operation(data)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)

    def _initial(self) -> dict[str, Any]:
        policy = self.defaults.model_dump(mode="json")
        return {"schema_version": 1, "version": 1, "policy": policy, "secrets": {}, "secret_revisions": {},
                "sha256": self._hash(policy, {}), "updated_at": None}

    @staticmethod
    def _hash(policy: dict[str, Any], secret_revisions: dict[str, int]) -> str:
        raw = json.dumps({"policy": policy, "secret_revisions": secret_revisions},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    def snapshot(self) -> dict[str, Any]:
        return self._locked(lambda d: {"schema_version": d["schema_version"], "version": d["version"],
            "sha256": d["sha256"], "policy": d["policy"]}, write=False)

    def describe(self) -> dict[str, Any]:
        def read(data: dict[str, Any]) -> dict[str, Any]:
            schema = RuntimePolicy.model_json_schema()
            defaults = self.defaults.model_dump(mode="json")
            fields = []
            for key, meta in FIELD_META.items():
                sensitive = bool(meta.get("sensitive"))
                fields.append({"key": key, **meta, "sensitive": sensitive,
                    "effective_when": meta["scope"], "default": None if sensitive else _get(defaults, key),
                    "value": None if sensitive else _get(data["policy"], key),
                    "secret_state": ("set" if data["secrets"].get(key) else "unset") if sensitive else None,
                    "schema": {"type": "string"} if sensitive else self._schema_for(schema, key)})
            return {"schema_version": 1, "version": data["version"], "sha256": data["sha256"], "fields": fields}
        return self._locked(read, write=False)

    @staticmethod
    def _schema_for(schema: dict[str, Any], path: str) -> dict[str, Any]:
        node = schema
        for part in path.split("."):
            if "$ref" in node: node = schema["$defs"][node["$ref"].split("/")[-1]]
            node = node["properties"][part]
        if "$ref" in node: node = schema["$defs"][node["$ref"].split("/")[-1]]
        return {k: v for k, v in node.items() if k in {"type", "enum", "minimum", "maximum", "exclusiveMinimum", "pattern"}}

    def update(self, changes: dict[str, Any], *, expected_version: int, actor: str,
               role: str, dangerous_confirmed: bool) -> dict[str, Any]:
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            if expected_version != data["version"]: raise SettingsConflict("设置版本冲突，请刷新后重试。")
            unknown = sorted(set(changes) - set(FIELD_META))
            if unknown: raise ValueError(f"未知或未接线设置：{', '.join(unknown)}")
            restricted = [key for key in changes if FIELD_META[key]["role"] == "admin" and role != "admin"]
            if restricted: raise SettingsForbidden("当前角色无权修改管理员设置。")
            if any(FIELD_META[k]["risk"] in {"high", "critical"} for k in changes) and not dangerous_confirmed:
                raise ValueError("高风险设置需要明确确认。")
            policy = json.loads(json.dumps(data["policy"]))
            secret_updates = {}
            for key, value in changes.items():
                if FIELD_META[key].get("sensitive"):
                    if not isinstance(value, str) or not value.strip(): raise ValueError("密钥更新值不能为空。")
                    secret_updates[key] = value
                else: _set(policy, key, value)
            validated = RuntimePolicy.model_validate(policy).model_dump(mode="json")
            before = data["version"]
            for key in secret_updates:
                data["secret_revisions"][key] = int(data["secret_revisions"].get(key, 0)) + 1
            data.update(version=before + 1, policy=validated,
                        sha256=self._hash(validated, data["secret_revisions"]), updated_at=_now())
            data["secrets"].update(secret_updates)
            atomic_json(self.path, data); os.chmod(self.path, 0o600)
            event = {"event": "runtime_settings_updated", "actor": actor, "role": role,
                     "before_version": before, "version": data["version"], "sha256": data["sha256"],
                     "changed_keys": sorted(changes), "effective_scopes": sorted({FIELD_META[k]["scope"] for k in changes}),
                     "at": data["updated_at"]}
            with self.audit_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush(); os.fsync(stream.fileno())
            return self.describe_unlocked(data)
        return self._locked(mutate, write=True)

    def describe_unlocked(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"schema_version": 1, "version": data["version"], "sha256": data["sha256"],
                "changed": True, "secret_states": {k: "set" for k in data["secrets"]}}

    def audit(self) -> list[dict[str, Any]]:
        def read(_: dict[str, Any]) -> list[dict[str, Any]]:
            if not self.audit_path.exists(): return []
            return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines() if line]
        return self._locked(read, write=False)
