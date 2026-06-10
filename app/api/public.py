from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app import db
from app.models import (
    ImageGenerateRequest,
    ImageGenerateResponse,
    ImageJobStatusResponse,
    ImageJobSubmitResponse,
)
from app.services import storage
from app.services.job_queue import GenerationJobFailed, GenerationQueueUnavailable


router = APIRouter()


def _extract_bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer API key",
        )
    return token.strip()


async def require_api_key(request: Request) -> dict:
    settings = request.app.state.settings
    token = _extract_bearer_token(request)
    api_key = db.get_api_key_by_token(settings, token)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    if not api_key["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key is disabled",
        )
    db.mark_api_key_used(settings, api_key["id"])
    return api_key


@router.post("/v1/images/generate", response_model=ImageGenerateResponse)
async def generate_image(
    payload: ImageGenerateRequest,
    request: Request,
    api_key: dict = Depends(require_api_key),
) -> dict:
    queue = request.app.state.job_queue
    try:
        return await queue.submit_and_wait(api_key_id=api_key["id"], payload=payload)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Image generation is still running or waiting in queue",
        ) from exc
    except GenerationQueueUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except GenerationJobFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@router.post(
    "/v1/images/jobs",
    response_model=ImageJobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_image_job(
    payload: ImageGenerateRequest,
    request: Request,
    api_key: dict = Depends(require_api_key),
) -> dict:
    """Submit a generation job and return immediately (async variant)."""
    queue = request.app.state.job_queue
    try:
        request_id = await queue.submit_only(api_key_id=api_key["id"], payload=payload)
    except GenerationQueueUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return {"id": request_id, "status": "queued"}


@router.get("/v1/images/jobs/{request_id}", response_model=ImageJobStatusResponse)
async def get_image_job(
    request_id: str,
    request: Request,
    api_key: dict = Depends(require_api_key),
) -> dict:
    """Poll a submitted job. Jobs are only visible to the key that created them."""
    settings = request.app.state.settings
    row = db.get_image_request(settings, request_id)
    if not row or row.get("api_key_id") != api_key["id"]:
        # 404 (not 403) so other keys cannot probe job-id existence.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )
    images = []
    if row["status"] == "succeeded":
        images = [
            {
                "url": storage.public_image_url(settings, Path(path)),
                "expires_at": row["expires_at"],
            }
            for path in row["image_paths"]
        ]
    return {
        "id": request_id,
        "status": row["status"],
        "images": images,
        "error": row.get("error"),
        "created_at": row["created_at"],
        "expires_at": row.get("expires_at"),
    }

