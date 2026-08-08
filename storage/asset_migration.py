"""Audit/migrate legacy temporary image URLs without mutating old checkpoints."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterator

from storage.assets import AssetPersistenceError, persist_image_asset
from storage.project_store import ProjectStore


def _legacy_assets(value: Any, location: str = "snapshot") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        uri = value.get("uri") or value.get("url")
        if isinstance(uri, str) and uri.startswith(("http://", "https://")):
            yield location, value
        for key, child in value.items():
            yield from _legacy_assets(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _legacy_assets(child, f"{location}[{index}]")


def migrate_current_snapshot(store: ProjectStore) -> list[dict[str, Any]]:
    """Persist reachable URLs and audit irrecoverable ones explicitly."""
    results: list[dict[str, Any]] = []
    for location, legacy in _legacy_assets(store.resume() or {}):
        try:
            asset = persist_image_asset(legacy, store.artifacts)
            result = {"location": location, "status": "migrated", "artifact_id": asset["artifact_id"],
                      "uri": asset["uri"], "sha256": asset["sha256"]}
            store.events.append("legacy_asset_migrated", **result)
        except AssetPersistenceError as exc:
            result = {"location": location, "status": "invalid", "reason": str(exc)}
            store.events.append("legacy_asset_invalidated", **result)
        results.append(result)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移或标记当前检查点中的临时图片 URL")
    parser.add_argument("projects_root", type=Path)
    parser.add_argument("project_id")
    args = parser.parse_args()
    print(json.dumps(migrate_current_snapshot(ProjectStore(args.projects_root, args.project_id)),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
