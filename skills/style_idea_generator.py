"""Generate style idea cards from style references."""

from __future__ import annotations

import json
import base64
import mimetypes
from pathlib import Path
from typing import Any

from agent_core.models import ImageTaskCard, StyleCard, StyleIdeaCard, TaskConfirmationDoc
from model_router.clients import VisionLanguageModelClient


class StyleIdeaGenerator:
    """Create human-readable style direction cards before image rendering."""

    def __init__(self, client: VisionLanguageModelClient | None = None, model_name: str | None = None, *, offline_mode: bool = False,
                 reference_root: Path | None = None) -> None:
        self.client = client
        self.offline_mode = offline_mode
        self.model_name = model_name or ("offline_style_builder" if offline_mode else "style_vlm")
        self.reference_root = (reference_root or Path(__file__).parent / "style_cards").resolve()

    def generate(
        self,
        *,
        task_card: ImageTaskCard,
        confirmation_doc: TaskConfirmationDoc,
        style_cards: list[StyleCard],
        count: int = 5,
    ) -> list[StyleIdeaCard]:
        """Generate one idea card for each selected style card."""

        if len(style_cards) != count:
            raise ValueError(f"Exactly {count} validated style directory entries are required.")
        identities = {(card.style_id, card.style_index) for card in style_cards}
        if len(identities) != count or len({card.style_id for card in style_cards}) != count or len({card.style_index for card in style_cards}) != count:
            raise ValueError("Style directory selection contains duplicate entries.")
        cards: list[StyleIdeaCard] = []
        for style_card in style_cards:
            cards.append(
                self._generate_one(
                    task_card=task_card,
                    confirmation_doc=confirmation_doc,
                    style_card=style_card,
                )
            )
        return cards

    def _generate_one(
        self,
        *,
        task_card: ImageTaskCard,
        confirmation_doc: TaskConfirmationDoc,
        style_card: StyleCard,
    ) -> StyleIdeaCard:
        """Generate one style idea card through VLM when possible."""

        reference_asset = style_card.reference_image.path
        if self.client is not None and reference_asset:
            try:
                payload = self.client.inspect(
                    self._reference_data_uri(style_card),
                    self._prompt(task_card, confirmation_doc, style_card),
                )
                return self._from_payload(task_card, style_card, reference_asset, payload)
            except Exception as exc:
                raise RuntimeError(
                    f"风格 VLM 调用、style_index 绑定或固定结构输出失败（{style_card.style_index}）。"
                ) from exc
        if not self.offline_mode:
            raise RuntimeError("未配置风格模型；只有显式离线模式允许规则化风格卡。")
        return self._offline_card(task_card, style_card, reference_asset)

    def _from_payload(
        self,
        task_card: ImageTaskCard,
        style_card: StyleCard,
        reference_asset: str,
        payload: dict[str, Any],
    ) -> StyleIdeaCard:
        """Validate a model payload into a style idea card."""

        if payload.get("style_index") != style_card.style_index:
            raise ValueError("VLM style_index 与受控目录绑定不一致。")
        return StyleIdeaCard(
            task_id=task_card.task_id,
            source_style_id=style_card.style_id,
            style_index=style_card.style_index,
            style_summary=payload["style_summary"],
            title=payload["title"],
            composition=payload["composition"],
            material=payload["material"],
            lighting=payload["lighting"],
            narrative=payload["narrative"],
            graphic_language=payload["graphic_language"],
            fit_reason=payload["fit_reason"],
            artistic_philosophy=payload["artistic_philosophy"],
            adaptable_mechanism=payload["adaptable_mechanism"],
            prohibited_copy_elements=payload["prohibited_copy_elements"],
            major_risk=payload["major_risk"],
            prompt_supplement=payload["prompt_supplement"],
            reference_asset=reference_asset,
            generated_by=self.model_name,
        )

    def _offline_card(
        self,
        task_card: ImageTaskCard,
        style_card: StyleCard,
        reference_asset: str | None,
    ) -> StyleIdeaCard:
        """Build a deterministic idea card from approved style-card data."""

        material = "、".join(style_card.visual_language.materiality) or style_card.visual_language.scheme or "通用材质语言"
        risk = "；".join(style_card.risk_notes[:2]) or "主要风险来自未确认信息和风格过度延展。"
        return StyleIdeaCard(
            task_id=task_card.task_id,
            source_style_id=style_card.style_id,
            style_index=style_card.style_index,
            style_summary=style_card.summary,
            title=style_card.style_name or style_card.style_id,
            composition=style_card.composition,
            material=material,
            lighting=style_card.visual_language.lighting or "均匀柔和照明",
            narrative=f"以{style_card.composition}组织从主信息到辅助信息的阅读叙事。",
            graphic_language=style_card.visual_language.scheme or material,
            fit_reason=f"{style_card.summary}；适用于任务目标与使用场景；目录标注适用范围为：{'、'.join(style_card.best_for)}。",
            artistic_philosophy=f"以“{style_card.style_name}”建立信息秩序，在表达效率与视觉辨识度之间保持平衡。",
            adaptable_mechanism=f"借鉴{style_card.composition}，以及{material}；不复制参考图的具体主体或独特表达。",
            prohibited_copy_elements=["参考图主体", "参考图构图", "参考图文字", "参考图标识", "参考图独特表达"],
            major_risk=risk,
            prompt_supplement=(
                f"构图方向：{style_card.composition}\n"
                f"材质与视觉语言：{material}\n"
                "保持项目内容、已确认事实、颜色条件和空间条件一致；只改变风格机制。"
            ),
            reference_asset=reference_asset,
            generated_by=self.model_name,
        )

    @staticmethod
    def _prompt(
        task_card: ImageTaskCard,
        confirmation_doc: TaskConfirmationDoc,
        style_card: StyleCard,
    ) -> str:
        """Build a JSON-only VLM prompt for interpreting a style reference."""

        return (
            "请阅读且仅分析这张受控风格参考图，为通用图片生成流程输出一个中文风格理念卡。"
            "不得加入任务卡和确认书以外的具体业务事实。只返回 JSON："
            '{"style_index":"string","style_summary":"string","title":"string","composition":"string","material":"string",'
            '"lighting":"string","narrative":"string","graphic_language":"string",'
            '"fit_reason":"string","artistic_philosophy":"string","adaptable_mechanism":"string",'
            '"prohibited_copy_elements":["string"],"major_risk":"string","prompt_supplement":"string"}\n'
            f"绑定身份：{json.dumps({'style_id': style_card.style_id, 'style_index': style_card.style_index}, ensure_ascii=False)}\n"
            "禁止复刻的具体元素必须覆盖参考图主体、构图、文字、标识和独特表达。\n"
            f"任务卡：{json.dumps(task_card.model_dump(mode='json'), ensure_ascii=False)}\n"
            f"确认书：{json.dumps(confirmation_doc.model_dump(mode='json'), ensure_ascii=False)}\n"
            f"风格卡：{json.dumps(style_card.model_dump(mode='json'), ensure_ascii=False)}"
        )

    def _reference_data_uri(self, style_card: StyleCard) -> str:
        path = (self.reference_root / style_card.reference_image.path).resolve()
        if not path.is_relative_to(self.reference_root):
            raise ValueError("受控风格参考图路径越界。")
        payload = path.read_bytes()
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return f"data:{media_type};base64,{base64.b64encode(payload).decode('ascii')}"
