"""Load external style cards without embedding style rules in code."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

from agent_core.models import SkillStatus, StyleCard


def load_style_card(path: str | Path) -> StyleCard:
    """Load and validate one style card JSON file."""

    return StyleCard.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_style_card_index(path: str | Path) -> dict[str, list[dict[str, str | int]]]:
    """Load a style card index as plain JSON."""

    return json.loads(Path(path).read_text(encoding="utf-8"))


class StyleCardLoader:
    """Load and select approved style cards from an external index."""

    def __init__(self, index_path: str | Path) -> None:
        self.index_path = Path(index_path)
        self.base_dir = self.index_path.parent
        self.index = load_style_card_index(self.index_path)

    def _approved_cards(self) -> list[tuple[int, StyleCard]]:
        cards: list[tuple[int, StyleCard]] = []
        seen_indexes: set[str] = set()
        for item in self.index.get("items", []):
            card_path = (self.base_dir / str(item["path"])).resolve()
            if card_path.parent != self.base_dir.resolve():
                raise ValueError("Style card path escapes the indexed Skill directory.")
            card = load_style_card(card_path)
            if card.style_id != item.get("style_id"):
                raise ValueError(f"Style index identity mismatch for {card.style_id}.")
            if card.style_index != item.get("style_index"):
                raise ValueError(f"Style index identity mismatch for {card.style_id}.")
            if card.style_index in seen_indexes:
                raise ValueError(f"Duplicate style_index: {card.style_index}")
            seen_indexes.add(card.style_index)
            reference = (self.base_dir / card.reference_image.path).resolve()
            if self.base_dir.resolve() not in reference.parents or not reference.is_file():
                raise ValueError(f"Missing controlled reference image for {card.style_index}.")
            actual_hash = hashlib.sha256(reference.read_bytes()).hexdigest()
            if actual_hash != card.reference_image.sha256:
                raise ValueError(f"Reference image hash mismatch for {card.style_index}.")
            if card.status is SkillStatus.APPROVED:
                cards.append((int(item.get("priority", 1000)), card))
        return cards

    def select_distinct(self, count: int = 5, *, task_text: str = "") -> list[StyleCard]:
        """Return relevant approved style cards in deterministic rank order.

        The index owns the style vocabulary and tie-break order. A card is
        eligible only when its name, tag, or declared use appears in the task.
        """

        selected: list[StyleCard] = []
        seen_compositions: set[str] = set()
        normalized_task = task_text.lower()
        ranked: list[tuple[int, int, StyleCard]] = []
        for priority, card in self._approved_cards():
            if any(term.lower() in normalized_task for term in card.avoid_for):
                continue
            hints = [card.style_name or "", *card.tags, *card.best_for]
            score = sum(1 for hint in hints if hint.lower() in normalized_task)
            if score == 0:
                continue
            ranked.append((-score, priority, card))
        for _, _, card in sorted(ranked, key=lambda row: (row[0], row[1], row[2].style_index)):
            if card.composition in seen_compositions:
                continue
            selected.append(card)
            seen_compositions.add(card.composition)
            if len(selected) == count:
                return selected
        raise ValueError(
            f"Style index does not contain {count} relevant approved distinct style cards."
        )
