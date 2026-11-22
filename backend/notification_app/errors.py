from typing import Any


class ApiProblem(Exception):
    """可预期的 API 错误；details 不包含输入原文或凭证。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        self.retry_after = retry_after
