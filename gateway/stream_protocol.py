"""Recognize a bounded OpenAI SSE completion without persisting response content."""

from __future__ import annotations

import json


class CompletionMarker:
    """Track both the OpenAI response envelope and the terminal SSE event.

    ``[DONE]`` alone is framing, not proof that the preceding stream was an
    OpenAI completion. One bounded JSON event with a ``choices`` array is the
    minimum response contract the router forwards and records as healthy.
    """

    _LINE_LIMIT = 64 * 1024
    _EVENT_LIMIT = 64 * 1024

    def __init__(self):
        self.complete = False
        self.response_envelope = False
        self._line = bytearray()
        self._overflow = False
        self._after_cr = False
        self._first_line = True
        self._data_fields = 0
        self._terminal_data = False
        self._event_data: list[bytes] = []
        self._event_bytes = 0
        self._event_overflow = False

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
            self._finish_event()
        else:
            field, _, value = line.partition(b":")
            if field == b"data":
                self._data_fields = min(2, self._data_fields + 1)
                value = value.removeprefix(b" ")
                self._terminal_data = (
                    self._data_fields == 1
                    and not self._overflow
                    and value == b"[DONE]"
                )
                if not self._terminal_data:
                    self._record_data(value)
        self._line.clear()
        self._overflow = False

    def _record_data(self, value: bytes) -> None:
        if self._overflow or self._event_overflow:
            self._event_overflow = True
            return
        if self._event_bytes + len(value) > self._EVENT_LIMIT:
            self._event_overflow = True
            return
        self._event_data.append(value)
        self._event_bytes += len(value)

    def _finish_event(self) -> None:
        if self._data_fields == 1 and self._terminal_data:
            self.complete = True
        elif self._event_data and not self._event_overflow:
            try:
                event = json.loads(b"\n".join(self._event_data))
            except (TypeError, ValueError, UnicodeDecodeError):
                event = None
            if isinstance(event, dict) and isinstance(event.get("choices"), list):
                self.response_envelope = True
        self._data_fields = 0
        self._terminal_data = False
        self._event_data.clear()
        self._event_bytes = 0
        self._event_overflow = False
