from typing import Optional

from fastapi.responses import JSONResponse


def error_response(
    status_code: int,
    error: str,
    message: str = "",
    extra: Optional[dict] = None,
    retry_after: Optional[int] = None,
) -> JSONResponse:
    body = {"error": error}
    if message:
        body["message"] = message
    if extra:
        body.update(extra)
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
        body["retry_after"] = retry_after
    return JSONResponse(status_code=status_code, content=body, headers=headers)