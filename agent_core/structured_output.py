"""Strict VLM output validation with one targeted repair attempt."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

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

    def __init__(self, output_kind: str, validation_error: str, redacted_output: str) -> None:
        super().__init__(f"{output_kind} 结构化输出修复后仍无效（{validation_error}），可重试。")
        self.output_kind = output_kind
        self.validation_error = validation_error
        self.redacted_output = redacted_output
        self.retryable = True


class ModelOutputParseError(ValueError):
    """Parse failure that retains raw provider output without exposing it in the message."""

    def __init__(self, raw_output: str, cause: Exception) -> None:
        super().__init__(f"模型响应不是合法 JSON：{type(cause).__name__}")
        self.raw_output = raw_output


def _validate(model: type[T], raw: Any, expected_values: dict[str, Any] | None = None) -> T:
    if isinstance(raw, str):
        raw = json.loads(raw)
    parsed = model.model_validate(raw)
    for field, expected in (expected_values or {}).items():
        if getattr(parsed, field) != expected:
            raise ValueError(f"{field} 与受控绑定不一致。")
    return parsed


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
    try:
        original = invoke(prompt)
        return _validate(model, original, expected_values)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError, KeyError) as first:
        original = getattr(first, "raw_output", original)
        original_text = original if isinstance(original, str) else json.dumps(original, ensure_ascii=False, sort_keys=True)
        repair_prompt = (
            f"{prompt}\n\n你正在修复一个不符合契约的模型响应。只返回修复后的纯 JSON 对象；不得补造未知事实。\n"
            f"输出类型：{output_kind}\nJSON Schema：{json.dumps(schema, ensure_ascii=False, sort_keys=True)}\n"
            f"校验错误：{first}\n原响应：{original_text}"
        )
        repaired: Any = None
        try:
            repaired = invoke(repair_prompt)
            return _validate(model, repaired, expected_values)
        except Exception as second:
            error = RecoverableStructuredOutputError(
                output_kind,
                str(second),
                redact_model_output(repaired if repaired is not None else original),
            )
            if on_failure:
                on_failure(error)
            raise error from second
