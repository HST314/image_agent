from __future__ import annotations

import hashlib
import json
from pathlib import Path

from storage import file_lock


def test_runtime_sources_do_not_import_posix_only_fcntl() -> None:
    roots = (Path("agent_core"), Path("configs"), Path("storage"))
    offenders = [
        str(path)
        for root in roots
        for path in root.rglob("*.py")
        if "import fcntl" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_portalocker_facade_accepts_existing_descriptors(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    with path.open("a+b") as stream:
        file_lock.lock(stream.fileno(), file_lock.LOCK_EX)
        file_lock.unlock(stream.fileno())


def test_all_declared_reference_hashes_match_byte_exact_assets() -> None:
    cards = Path("skills/style_cards")
    index = json.loads((cards / "index.json").read_text(encoding="utf-8"))
    for item in index["items"]:
        card = json.loads((cards / item["path"]).read_text(encoding="utf-8"))
        reference = cards / card["reference_image"]["path"]
        assert hashlib.sha256(reference.read_bytes()).hexdigest() == card["reference_image"]["sha256"]
    assert "skills/style_cards/references/** -text" in Path(".gitattributes").read_text(encoding="utf-8")
