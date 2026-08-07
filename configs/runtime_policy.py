"""Strict, immutable runtime behavior policy."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SelfCheckRuntimePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    termination: Literal["solo", "fix"]
    fixed_rounds: int = Field(ge=1, le=20)
    max_rounds: int = Field(ge=1, le=20)
    stop_early_on_pass: bool
    release: Literal["auto", "manual"]
    rule_version: str = Field(default="visual-inspection-v2", min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_rounds(self):
        if self.fixed_rounds > self.max_rounds:
            raise ValueError("self_check.fixed_rounds 不能大于 max_rounds")
        return self


class RuntimePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    default_question_count: int = Field(ge=0, le=10)
    question_mode: Literal["auto", "manual"]
    max_auto_questions: int = Field(ge=0, le=10)
    max_clarify_rounds: int = Field(ge=0, le=20)
    clarification_total_budget: int = Field(ge=0, le=100)
    candidate_count: int = Field(default=5, ge=1, le=10)
    candidate_concurrency: int = Field(default=5, ge=1, le=10)
    candidate_min_mechanism_differences: int = Field(default=3, ge=1, le=5)
    stream_model_output: bool
    self_check: SelfCheckRuntimePolicy
    approval_required: bool
    max_render_retries: int = Field(ge=0, le=10)
    max_calibration_retries: int = Field(ge=0, le=20)
    model_timeout_seconds: float = Field(default=180, gt=0, le=3600)
    image_api_base_url: str
    default_output_size: str = Field(pattern=r"^\d{2,5}x\d{2,5}$")
    response_format: Literal["url", "b64_json"]
    watermark: bool
    skill_failure_mode: Literal["block", "allow_degraded"] = "block"

    @model_validator(mode="after")
    def validate_combinations(self):
        if self.max_auto_questions > self.clarification_total_budget:
            raise ValueError("max_auto_questions 不能超过 clarification_total_budget")
        if self.question_mode == "manual" and self.max_auto_questions:
            raise ValueError("question_mode=manual 时 max_auto_questions 必须为 0")
        if not self.approval_required:
            raise ValueError("approval_required=false 与任务书及最终确认强制门禁冲突")
        if self.image_api_base_url:
            parsed = urlparse(self.image_api_base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("image_api_base_url 必须为空或有效的 HTTP(S) 地址")
        return self

    @classmethod
    def from_file(cls, path: Path) -> "RuntimePolicy":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("runtime.yaml 顶层必须是对象")
        return cls.model_validate(raw)

    def snapshot(self, mode: Literal["offline", "real"]) -> dict:
        value = {"schema_version": 1, "mode": mode, "policy": self.model_dump(mode="json")}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return {**value, "sha256": hashlib.sha256(encoded).hexdigest()}
