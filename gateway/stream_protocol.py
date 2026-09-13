"""Recognize OpenAI SSE completion without retaining response payloads."""


class CompletionMarker:
    """A terminal event has exactly one data field containing ``[DONE]``."""

    _LINE_LIMIT = 32

    def __init__(self):
        self.complete = False
        self._line = bytearray()
        self._overflow = False
        self._after_cr = False
        self._first_line = True
        self._data_fields = 0
        self._terminal_data = False

    def feed(self, chunk: bytes) -> None:
        for value in chunk:
            if self.complete:
                return
            if self._after_cr:
                self._after_cr = False
                if value == 10:
                    continue
            if value in (10, 13):
                self._finish_line()
                self._after_cr = value == 13
            elif len(self._line) < self._LINE_LIMIT:
                self._line.append(value)
            else:
                self._overflow = True

    def _finish_line(self) -> None:
        line = bytes(self._line)
        if self._first_line:
            line = line.removeprefix(b"\xef\xbb\xbf")
            self._first_line = False
        if not line:
            self.complete = self._data_fields == 1 and self._terminal_data
            self._data_fields = 0
            self._terminal_data = False
        else:
            field, _, value = line.partition(b":")
            if field == b"data":
                self._data_fields = min(2, self._data_fields + 1)
                value = value.removeprefix(b" ")
                self._terminal_data = (
                    self._data_fields == 1 and not self._overflow and value == b"[DONE]"
                )
        self._line.clear()
        self._overflow = False
