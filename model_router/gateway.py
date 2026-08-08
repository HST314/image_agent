"""Workflow boundary gateway: hot reload, role checks and mandatory auditing."""
from __future__ import annotations
from typing import Any, Callable
from uuid import uuid4
from agent_core.models import ModelRole
from model_router.executor import ModelExecutor
from model_router.router import ModelRouter, ModelRoute
from storage.project_store import ProjectStore, content_hash

def _normalized_model_result(value: Any) -> Any:
    """Return the stable JSON representation used for completion hashes."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value

class RuntimeModelGateway:
    def __init__(self, store: ProjectStore, router: ModelRouter, executor: ModelExecutor[Any] | None = None, *, offline_mode: bool = False) -> None:
        self.store, self.router, self.executor, self.offline_mode = store, router, executor or ModelExecutor(), offline_mode

    def call(self, state: str, role: ModelRole, invoke: Callable[[Any], Any], *, messages: list[dict[str, Any]], variables: dict[str, Any], template_id: str, template_version: str, input_refs: list[str], parent_prompt: str | None = None, parent_call_id: str | None = None, audit_context: dict[str, Any] | None = None, round_number: int | None = None, needs_images: int = 0) -> Any:
        self.router = self.router.reload_at_boundary()
        binding = self.router.validate_capability(state, role=role, needs_images=needs_images)
        route = ModelRoute(binding=binding, mock=True) if self.offline_mode else self.router.route_for_state(state)
        snapshot = binding.model_dump(mode="json")
        trace = f"trace_{uuid4().hex}"
        audit = {"messages": messages, "template_id": template_id, "template_version": template_version,
            "template_hash": content_hash(messages), "variables": variables, "input_refs": input_refs,
            "model": {"provider": binding.provider, "name": binding.model, "role": role.value},
            "parameters": binding.parameters, "config_hash": self.router.config_hash, "state": state,
            "trace_id": trace, "parent_prompt": parent_prompt, "parent_call_id": parent_call_id,
            "capability": state, "round": round_number}
        self.store.events.append("model_config_loaded", state=state, config_hash=self.router.config_hash, binding=snapshot)
        call_id = self.store.prompts.begin(audit)
        if audit_context is not None:
            audit_context["call_id"] = call_id
        is_image = role is ModelRole.TEXT_TO_IMAGE_MODEL
        if is_image:
            self.store.prompts.status(call_id, "queued")
        route = ModelRoute(binding=route.binding, mock=route.mock, key_env=route.key_env,
                           stream_handler=lambda delta: self.store.prompts.chunk(call_id, delta))
        try:
            if is_image:
                self.store.prompts.status(call_id, "running")
            result = self.executor.run(lambda: invoke(route), trace_id=trace)
            if is_image:
                self.store.prompts.status(call_id, "provider_completed")
            self.store.prompts.complete(call_id, output_raw=result)
            normalized_result = _normalized_model_result(result)
            self.store.events.append("model_call_completed", call_id=call_id, parent_call_id=parent_call_id,
                                     state=state, trace_id=trace, output_hash=content_hash(normalized_result))
            if is_image and isinstance(result, dict):
                result = {**result, "_model_call_id": call_id}
            return result
        except Exception as exc:
            self.store.prompts.fail(call_id, {"code": type(exc).__name__, "message": str(exc)})
            raise
