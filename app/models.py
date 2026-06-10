from __future__ import annotations

from pydantic import BaseModel, Field


class ImageGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=32000)
    size: str = Field(default="1024x1024", pattern=r"^\d{3,4}x\d{3,4}$")
    quality: str = Field(default="medium", pattern=r"^(low|medium|high|auto)$")
    count: int = Field(default=1, ge=1, le=4)
    # Deprecated: kept so existing callers (ctos-lite, catime) keep working.
    # Resolved through resolved_reference_images alongside the plural field.
    reference_image_base64: str | None = Field(default=None, max_length=20_000_000)
    reference_images_base64: list[str] | None = Field(default=None)
    # Note: no client-side count cap. OpenAI's gpt-image edit API
    # accepts ~16 images and will reject anything beyond that with a
    # clear error which we surface verbatim. The 4-cap from PR #1 was
    # arbitrary; lifting it for consistency across ctos-lite and
    # ching-tech-os, which deliberately set no cap either.

    @property
    def resolved_reference_images(self) -> list[str]:
        """Unify singular + plural inputs into one list the service consumes.

        Plural wins when both are set — explicit beats legacy.
        """
        if self.reference_images_base64:
            return list(self.reference_images_base64)
        if self.reference_image_base64:
            return [self.reference_image_base64]
        return []


class GeneratedImage(BaseModel):
    url: str
    expires_at: str


class ImageGenerateResponse(BaseModel):
    id: str
    status: str
    images: list[GeneratedImage]
    created_at: str


class ImageJobSubmitResponse(BaseModel):
    id: str
    status: str  # "queued"


class ImageJobStatusResponse(BaseModel):
    id: str
    status: str  # queued|running|succeeded|failed|expired
    images: list[GeneratedImage] = []
    error: str | None = None
    created_at: str
    expires_at: str | None = None
