"""Stable errors shared by HTTP, jobs and validation."""

class StudioError(Exception):
    def __init__(self, message, code="invalid_request", status=400, details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details or {}

    def as_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class MissingRecord(StudioError):
    def __init__(self, kind, identifier):
        super().__init__(
            f"找不到{kind}：{identifier}",
            code="not_found",
            status=404,
        )


class Conflict(StudioError):
    def __init__(self, message):
        super().__init__(message, code="conflict", status=409)

