"""Shared FastAPI dependencies."""

import secrets

from fastapi import Header, HTTPException, status

from backend.config import settings


async def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """Require an API key for mutating endpoints when one is configured.

    Local development and simulation environments normally leave ``API_KEY``
    unset, which keeps the endpoints open. Any deployment exposing the API
    publicly should set ``API_KEY``.
    """
    expected = settings.API_KEY
    if not expected:
        return

    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )
