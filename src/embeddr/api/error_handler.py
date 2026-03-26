"""
Standardized error response handler for the Embeddr API.

Registers exception handlers on the FastAPI app so that all error responses
follow the ErrorResponse contract:

    {
        "ok": false,
        "error_code": "not_found",
        "message": "Artifact not found",
        "details": null
    }

Usage::

    from embeddr.api.error_handler import install_error_handlers
    install_error_handlers(app)
"""

import json
import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from embeddr_core.models.api_responses import ErrorCode

logger = logging.getLogger("embeddr.api.errors")


def _error_response(
    status_code: int,
    error_code: str,
    message: str,
    details: Optional[dict] = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error_code": error_code,
            "message": message,
            "details": details,
        },
    )


# Map well-known detail strings to error codes for backwards compat.
# Routes that already raise HTTPException(detail="...") will get a
# proper error_code without changing every call site.
_STATUS_TO_DEFAULT_CODE = {
    400: ErrorCode.INVALID_INPUT,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.INVALID_INPUT,
    500: ErrorCode.INTERNAL_ERROR,
    501: ErrorCode.NOT_IMPLEMENTED,
}


def _extract_error_code(status_code: int, detail: str) -> str:
    """
    If the detail is already a JSON-encoded ErrorResponse, extract its
    error_code. Otherwise, infer from the status code.
    """
    if detail and detail.startswith("{"):
        try:
            parsed = json.loads(detail)
            if "error_code" in parsed:
                return parsed["error_code"]
        except (json.JSONDecodeError, TypeError):
            pass

    return _STATUS_TO_DEFAULT_CODE.get(status_code, ErrorCode.INTERNAL_ERROR)


def _extract_message(detail) -> str:
    """Safely extract a string message from HTTPException.detail."""
    if isinstance(detail, str):
        # Check if it's JSON-encoded ErrorResponse
        if detail.startswith("{"):
            try:
                parsed = json.loads(detail)
                if "message" in parsed:
                    return parsed["message"]
            except (json.JSONDecodeError, TypeError):
                pass
        return detail
    if isinstance(detail, list):
        # Pydantic validation errors
        return "Validation error"
    return str(detail) if detail else "Unknown error"


def install_error_handlers(app: FastAPI) -> None:
    """Register error handlers that normalize all errors to ErrorResponse."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail_str = _extract_message(exc.detail)
        error_code = _extract_error_code(exc.status_code, str(exc.detail or ""))

        # Don't log 4xx client errors at warning level
        if exc.status_code >= 500:
            logger.error(
                "http_error status=%d code=%s path=%s message=%s",
                exc.status_code, error_code, request.url.path, detail_str,
            )

        return _error_response(
            status_code=exc.status_code,
            error_code=error_code,
            message=detail_str,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Build structured details from Pydantic errors
        errors = []
        for err in exc.errors():
            errors.append({
                "field": ".".join(str(loc) for loc in err.get("loc", [])),
                "message": err.get("msg", ""),
                "type": err.get("type", ""),
            })

        return _error_response(
            status_code=422,
            error_code=ErrorCode.INVALID_INPUT,
            message="Request validation failed",
            details={"errors": errors},
        )
