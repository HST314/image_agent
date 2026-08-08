from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.health import HealthPolicy, HealthService


def _result(status="healthy", code="OK", impact="available"):
    return {"status": status, "error_code": code, "checked_at": "2026-08-08T00:00:00+00:00",
            "business_impact": impact}


def _service(tmp_path: Path, probes, **policy):
    return HealthService(tmp_path / "projects", tmp_path / "models.yaml",
        policy=HealthPolicy(cache_ttl_seconds=policy.get("ttl", 10),
                            probe_timeout_seconds=policy.get("timeout", .1),
                            worker_stale_seconds=10, queue_stale_seconds=10,
                            storage_min_free_bytes=1), probes=probes)


def test_liveness_is_independent_and_readiness_preserves_partial_failures(tmp_path: Path):
    probes = {name: (lambda: _result()) for name in
              ("model_router", "worker", "queue", "storage", "event_append", "asset_proxy")}
    probes["worker"] = lambda: _result("degraded", "WORKER_HEARTBEAT_STALE", "jobs delayed")
    service = _service(tmp_path, probes)
    assert service.liveness()["status"] == "alive"
    ready = service.readiness()
    assert ready["status"] == "degraded"
    assert ready["components"]["worker"]["error_code"] == "WORKER_HEARTBEAT_STALE"
    assert ready["components"]["storage"]["status"] == "healthy"
    assert set(ready) == {"status", "checked_at", "trace_id", "components"}


def test_critical_failure_is_503_but_degraded_is_200_and_dto_does_not_leak(tmp_path: Path, monkeypatch):
    secret = "sk-super-secret"
    probes = {name: (lambda: _result()) for name in
              ("model_router", "worker", "queue", "storage", "event_append", "asset_proxy")}
    def broken():
        raise RuntimeError(f"/srv/private/projects {secret} provider raw failure")
    probes["storage"] = broken
    service = _service(tmp_path, probes)
    monkeypatch.setattr(main_front, "HEALTH_SERVICE", service)
    client = TestClient(main_front.app)
    response = client.get("/api/health/ready")
    assert response.status_code == 503 and response.json()["status"] == "not_ready"
    public = response.text
    assert "/srv/private" not in public and secret not in public and "provider raw" not in public
    trace_id = response.json()["trace_id"]
    assert client.get(f"/api/internal/diagnostics/{trace_id}").status_code == 403
    main_front.app.state.runtime_settings_authorizer = lambda request: "admin"
    try:
        internal = client.get(f"/api/internal/diagnostics/{trace_id}")
        assert internal.status_code == 200 and internal.json()["trace_id"] == trace_id
    finally:
        del main_front.app.state.runtime_settings_authorizer


def test_timeout_is_masked_cached_and_concurrent_requests_are_coalesced(tmp_path: Path):
    calls = 0
    lock = threading.Lock()
    def slow():
        nonlocal calls
        with lock: calls += 1
        time.sleep(.15)
        return _result()
    service = _service(tmp_path, {"storage": slow, "event_append": lambda: _result()}, timeout=.03)
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.readiness())) for _ in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert calls == 1
    assert all(item["components"]["storage"]["error_code"] == "PROBE_TIMEOUT" for item in results)
    assert service.readiness()["trace_id"] == results[0]["trace_id"]


def test_real_provider_unconfigured_and_offline_are_never_production_healthy(tmp_path: Path):
    config = tmp_path / "models.yaml"
    config.write_text("state_bindings:\n  - state: x\n    provider: fake\n", encoding="utf-8")
    projects = tmp_path / "projects"; (projects / "offline").mkdir(parents=True)
    (projects / "offline/project.yaml").write_text(json.dumps({"runtime_policy": {"mode": "offline"}}))
    service = HealthService(projects, config, policy=HealthPolicy(storage_min_free_bytes=1))
    offline = service._probe_model_router()
    assert offline["status"] == "offline" and offline["error_code"] == "OFFLINE_ONLY"
    (projects / "real").mkdir()
    (projects / "real/project.yaml").write_text(json.dumps({"runtime_policy": {"mode": "real"}}))
    real = service._probe_model_router()
    assert real["status"] == "not_configured" and real["error_code"] == "MODEL_PROVIDER_NOT_CONFIGURED"


def test_default_probes_do_not_create_jobs_projects_assets_or_business_events(tmp_path: Path):
    config = tmp_path / "models.yaml"
    config.write_text("state_bindings:\n  - state: x\n    provider: fake\n", encoding="utf-8")
    projects = tmp_path / "projects"; projects.mkdir()
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    service = HealthService(projects, config, policy=HealthPolicy(storage_min_free_bytes=1))
    service.readiness()
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert before == after


def test_http_health_is_read_only_even_without_settings_store(tmp_path: Path, monkeypatch):
    projects = tmp_path / "absent-projects"
    config = tmp_path / "models.yaml"
    config.write_text("state_bindings:\n  - state: x\n    provider: fake\n", encoding="utf-8")
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", projects)
    monkeypatch.setattr(main_front, "MODEL_CONFIG", config)
    monkeypatch.setattr(main_front, "HEALTH_SERVICE", None)
    response = TestClient(main_front.app).get("/api/health/ready")
    assert response.status_code in {200, 503}
    assert not projects.exists()


def test_restart_has_same_safe_semantics(tmp_path: Path):
    probes = {"storage": lambda: _result(), "event_append": lambda: _result()}
    first = _service(tmp_path, probes).readiness()
    second = _service(tmp_path, probes).readiness()
    assert first["status"] == second["status"] == "ready"
    assert first["components"] == second["components"]


@pytest.mark.parametrize("failed", ["model_router", "worker", "queue", "storage", "event_append", "asset_proxy"])
def test_each_component_failure_is_independent_and_public_schema_is_stable(tmp_path: Path, failed: str):
    names = ("model_router", "worker", "queue", "storage", "event_append", "asset_proxy")
    probes = {name: (lambda: _result()) for name in names}
    probes[failed] = lambda: _result("unhealthy", f"{failed.upper()}_UNAVAILABLE", "component unavailable")
    result = _service(tmp_path, probes).readiness()
    expected = "not_ready" if failed in {"storage", "event_append"} else "degraded"
    assert result["status"] == expected
    assert result["components"][failed]["status"] == "unhealthy"
    assert all(result["components"][name]["status"] == "healthy" for name in names if name != failed)
    schema = json.loads((Path(__file__).parents[1] / "schemas/HealthStatus.v1.schema.json").read_text())
    jsonschema.validate(result, schema)


def test_combined_degradation_keeps_all_component_results(tmp_path: Path):
    probes = {
        "model_router": lambda: _result("not_configured", "MODEL_PROVIDER_NOT_CONFIGURED", "real unavailable"),
        "worker": lambda: _result("degraded", "WORKER_HEARTBEAT_STALE", "jobs delayed"),
        "queue": lambda: _result("degraded", "QUEUE_BACKLOG_STALE", "queue delayed"),
        "storage": lambda: _result(), "event_append": lambda: _result(), "asset_proxy": lambda: _result(),
    }
    result = _service(tmp_path, probes).readiness()
    assert result["status"] == "degraded" and len(result["components"]) == 6
