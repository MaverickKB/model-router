"""Recognize OpenAI SSE completion without retaining response payloads."""

import json


class StreamProgress:
    """Remember only whether a caller received output that must never replay."""

    def __init__(self):
        self.has_output = False
        self.line = bytearray()

    def feed(self, chunk):
        for value in chunk:
            if self.has_output:
                return
            if value in (10, 13):
                line = bytes(self.line).strip()
                self.line.clear()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    continue
                try:
                    document = json.loads(data)
                except ValueError:
                    self.has_output = True
                    return
                if not isinstance(document, dict):
                    self.has_output = True
                    return
                choices = document.get("choices") or []
                if not isinstance(choices, list):
                    self.has_output = True
                    return
                for choice in choices:
                    if not isinstance(choice, dict):
                        self.has_output = True
                        return
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        self.has_output = True
                        return
                    for name, content in delta.items():
                        if name != "role" and content:
                            self.has_output = True
                    if choice.get("text") or choice.get("finish_reason"):
                        self.has_output = True
            elif len(self.line) < 65536:
                self.line.append(value)
            else:
                # An oversized event is conservatively treated as delivered.
                self.has_output = True


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
