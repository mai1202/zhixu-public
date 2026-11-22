from typing import Literal

from pydantic import Field, model_validator

from .business import ContractModel


class OcrItemInput(ContractModel):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(max_length=10_000)
    poly: tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]
    score: float | None = Field(ge=0, le=1)


class OcrResultInput(ContractModel):
    engine_version: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    execution_location: Literal["browser", "server"]
    image_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(ge=1, le=100_000)
    height: int = Field(ge=1, le=100_000)
    orientation: Literal[0, 90, 180, 270]
    elapsed_ms: int = Field(ge=0)
    items: list[OcrItemInput] = Field(max_length=10_000)
    warnings: list[str] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_items(self):
        """OCR 图框必须在同一图片范围内，且行 ID 在一份结果中不重复。"""
        identifiers = [item.id for item in self.items]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("OCR 行标识必须唯一")
        if any(
            x < 0 or y < 0 or x > self.width or y > self.height
            for item in self.items
            for x, y in item.poly
        ):
            raise ValueError("OCR 坐标超出图像边界")
        return self


class OcrSourceInput(ContractModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=500)
    corrected_items: list[OcrItemInput] = Field(max_length=10_000)
    analysis_mode: Literal["rules", "llm", "auto"] = "rules"
    provider_profile_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def check_analysis_choice(self):
        """明确 LLM 模式必须选择自己的模型配置。"""
        if self.analysis_mode == "llm" and not self.provider_profile_id:
            raise ValueError("模型模式需要指定用户自己的模型配置")
        return self
