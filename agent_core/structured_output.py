"""Strict VLM output validation with one targeted repair attempt."""

from __future__ import annotations

import json
import re
import hashlib
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

T = TypeVar("T", bound=BaseModel)
_SECRET = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|token|secret|password)(\s*[:=]\s*|\s+)([^\s,;\"']+)"
)


def redact_model_output(value: Any) -> str:
    """Serialize an untrusted response and remove common credential forms."""

    if isinstance(value, str):
        raw = value
    else:
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            raw = repr(value)
    return _SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", raw)[:12000]


class RecoverableStructuredOutputError(RuntimeError):
    """The original response and the single repair response were both invalid."""

    def __init__(self, output_kind: str, validation_error: str, redacted_output: str, *, recovery_id: str, error_paths: list[str]) -> None:
        super().__init__(f"{output_kind} 结构化输出修复后仍无效（{validation_error}），可重试。")
        self.output_kind = output_kind
        self.validation_error = validation_error
        self.redacted_output = redacted_output
        self.retryable = True
        self.recovery_id = recovery_id
        self.error_paths = error_paths


class ModelOutputParseError(ValueError):
    """Parse failure that retains raw provider output without exposing it in the message."""

    def __init__(self, raw_output: str, cause: Exception) -> None:
        super().__init__(f"模型响应不是合法 JSON：{type(cause).__name__}")
        self.raw_output = raw_output


def extract_json_object(raw: str) -> Any:
    """Extract exactly one JSON object from fences or surrounding provider prose."""
    decoder = json.JSONDecoder()
    for offset, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(raw[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise json.JSONDecodeError("no JSON object found", raw, 0)


def _validate(model: type[T], raw: Any, expected_values: dict[str, Any] | None = None) -> T:
    if isinstance(raw, str):
        raw = extract_json_object(raw)
    parsed = model.model_validate(raw)
    for field, expected in (expected_values or {}).items():
        if getattr(parsed, field) != expected:
            raise ValueError(f"{field} 与受控绑定不一致。")
    return parsed


def _error_paths(error: Exception) -> list[str]:
    if isinstance(error, ValidationError):
        return ["$." + ".".join(map(str, item["loc"])) if item["loc"] else "$" for item in error.errors()]
    return ["$"]


def _model_error_mutable_fields(model: type[T], error: Exception) -> set[str]:
    """Return only fields that a known model-level invariant must coordinate."""

    if model.__name__ != "VisualInspectionOutput":
        return set()
    message = str(error)
    if "passed 与 decision" in message:
        return {"passed", "decision"}
    if "通过结论不得同时包含偏差或返工 Prompt" in message:
        return {"passed", "decision", "deviations", "rework_prompt_delta"}
    if "未通过结论必须包含至少一个具体偏差" in message:
        return {"passed", "decision", "deviations"}
    if "继续返工必须包含非空返工 Prompt" in message:
        return {"decision", "rework_prompt_delta"}
    return set()


def _valid_original_fields(model: type[T], raw: Any, expected: dict[str, Any], error: Exception) -> dict[str, Any]:
    try:
        value = extract_json_object(raw) if isinstance(raw, str) else raw
    except Exception:
        return {}
    if not isinstance(value, dict):
        return {}
    paths = _error_paths(error)
    invalid = {path.removeprefix("$.").split(".", 1)[0] for path in paths}
    if "$" in paths:
        invalid = _model_error_mutable_fields(model, error)
    frozen: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if name not in value or name in invalid or (name in expected and value[name] != expected[name]):
            continue
        try:
            frozen[name] = TypeAdapter(field.annotation).validate_python(value[name])
        except Exception:
            pass
    return frozen


def validate_with_one_repair(
    *,
    output_kind: str,
    model: type[T],
    invoke: Callable[[str], Any],
    prompt: str,
    schema: dict[str, Any],
    expected_values: dict[str, Any] | None = None,
    on_failure: Callable[[RecoverableStructuredOutputError], None] | None = None,
) -> T:
    """Validate once, then send the exact error and response through one repair prompt."""

    original: Any = None
    recovery_id = hashlib.sha256(f"{output_kind}\0{prompt}".encode()).hexdigest()[:24]
    try:
        original = invoke(prompt)
        return _validate(model, original, expected_values)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError, KeyError) as first:
        original = getattr(first, "raw_output", original)
        original_text = original if isinstance(original, str) else json.dumps(original, ensure_ascii=False, sort_keys=True)
        paths = _error_paths(first)
        frozen = _valid_original_fields(model, original, expected_values or {}, first)
        repair_prompt = (
            f"{prompt}\n\n你正在修复一个不符合契约的模型响应。只返回修复后的纯 JSON 对象；不得补造未知事实。\n"
            f"输出类型：{output_kind}\nJSON Schema：{json.dumps(schema, ensure_ascii=False, sort_keys=True)}\n"
            f"校验错误路径：{json.dumps(paths, ensure_ascii=False)}\n校验错误：{first}\n"
            f"不得改变的已合法字段：{redact_model_output(json.dumps(frozen, ensure_ascii=False, sort_keys=True))}\n"
            f"脱敏原响应：{redact_model_output(original_text)}\n修复关联 ID：{recovery_id}"
        )
        repaired: Any = None
        try:
            repaired = invoke(repair_prompt)
            parsed = _validate(model, repaired, expected_values)
            for name, value in frozen.items():
                if getattr(parsed, name) != value:
                    raise ValueError(f"$.{name}：repair 改变了已合法字段。")
            return parsed
        except Exception as second:
            error = RecoverableStructuredOutputError(
                output_kind,
                str(second),
                redact_model_output(repaired if repaired is not None else original),
                recovery_id=recovery_id, error_paths=_error_paths(second),
            )
            if on_failure:
                on_failure(error)
            raise error from second
