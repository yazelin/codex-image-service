from __future__ import annotations

from pydantic import BaseModel, Field


class ImageGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    size: str = Field(default="1024x1024", pattern=r"^\d{3,4}x\d{3,4}$")
    quality: str = Field(default="medium", pattern=r"^(low|medium|high|auto)$")
    count: int = Field(default=1, ge=1, le=4)
    # Optional: a base64-encoded source image (PNG / JPG / WebP). When set,
    # Codex runs in edit mode against that image instead of pure text-to-image.
    # count is forced to 1 in edit mode (gpt-image-2 edit returns one image).
    reference_image_base64: str | None = Field(default=None, max_length=20_000_000)


class GeneratedImage(BaseModel):
    url: str
    expires_at: str


class ImageGenerateResponse(BaseModel):
    id: str
    status: str
    images: list[GeneratedImage]
    created_at: str

