import http


class CustomHTTPException(Exception):
    def __init__(
        self,
        status_code: int,
        phrase: str | None = None,
        detail: str | None = None,
        error_code: int | None = None,
        headers=None
    ) -> None:

        if phrase is None:
            phrase = http.HTTPStatus(status_code).phrase
        if error_code is None:
            error_code = status_code
        self.status_code = status_code
        self.phrase = phrase
        self.detail = detail
        self.error_code = error_code
        self.headers = headers

    def __str__(self) -> str:
        return f"{self.status_code}: {self.phrase}"

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        return f"{class_name}(status_code={self.status_code!r}, detail={self.phrase!r})"
