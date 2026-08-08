"""P1-08 canonical annotation contract and server-side guidance compositor."""
from __future__ import annotations

import io
from typing import Annotated, Literal

from PIL import Image, ImageColor, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PixelPoint(_Strict):
    x: float = Field(ge=0)
    y: float = Field(ge=0)


class RectangleAnnotation(_Strict):
    type: Literal["rectangle"]
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    color: str
    stroke_width: float = Field(gt=0, le=256)


class BrushAnnotation(_Strict):
    type: Literal["brush"]
    points: list[PixelPoint] = Field(min_length=2, max_length=20_000)
    color: str
    stroke_width: float = Field(gt=0, le=256)


Annotation = Annotated[RectangleAnnotation | BrushAnnotation, Field(discriminator="type")]


class GuidedEditRequest(_Strict):
    """Coordinates are intrinsic source-image pixels, independent of CSS/DPR."""

    parent_asset_id: str = Field(pattern=r"^artifact_[0-9a-f]{64}$")
    branch: str = Field(min_length=1, max_length=64)
    coordinate_space: Literal["source_image_pixels"]
    source_width: int = Field(gt=0, le=100_000)
    source_height: int = Field(gt=0, le=100_000)
    annotations: list[Annotation] = Field(min_length=1, max_length=2_000)
    prompt: str = Field(min_length=1, max_length=8_000)
    actor: str = Field(min_length=1, max_length=256)
    round: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_content(self) -> "GuidedEditRequest":
        self.prompt = self.prompt.strip()
        if not self.prompt:
            raise ValueError("圈画微调 Prompt 不能为空。")
        for annotation in self.annotations:
            try:
                ImageColor.getrgb(annotation.color)
            except ValueError as exc:
                raise ValueError("标注颜色必须是 Pillow 支持的颜色值。") from exc
            if isinstance(annotation, RectangleAnnotation):
                if annotation.x + annotation.width > self.source_width or annotation.y + annotation.height > self.source_height:
                    raise ValueError("矩形标注超出原图像素边界。")
            elif any(point.x > self.source_width or point.y > self.source_height for point in annotation.points):
                raise ValueError("画笔标注超出原图像素边界。")
        return self


def compose_guidance(source: bytes, request: GuidedEditRequest) -> tuple[bytes, str, int, int]:
    """Burn annotations into a new immutable PNG without mutating the source."""
    with Image.open(io.BytesIO(source)) as opened:
        opened.load()
        if opened.size != (request.source_width, request.source_height):
            raise ValueError("提交的原图像素尺寸与受控资产不一致。")
        image = opened.convert("RGBA")
    draw = ImageDraw.Draw(image)
    for annotation in request.annotations:
        width = max(1, round(annotation.stroke_width))
        if isinstance(annotation, RectangleAnnotation):
            draw.rectangle((annotation.x, annotation.y, annotation.x + annotation.width,
                            annotation.y + annotation.height), outline=annotation.color, width=width)
        else:
            draw.line([(point.x, point.y) for point in annotation.points], fill=annotation.color,
                      width=width, joint="curve")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), "image/png", image.width, image.height
