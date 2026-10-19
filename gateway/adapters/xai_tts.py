"""Pure xAI TTS transport; this catalog declares a service, not a chat model."""

from __future__ import annotations

import base64
import binascii
import json
import logging
from urllib.parse import urlencode

SERVICE = "xai-tts"
MAX_FRAME = 64 * 1024
MAX_PROVIDER_FRAME = 256 * 1024
MAX_PCM = 60 * 24000 * 2


class SpeechFault(Exception):
    """A bounded public error code, never a provider response or selected text."""


def decode_message(raw, *, max_frame=MAX_FRAME):
    if (
        not isinstance(raw, str)
        or len(raw.encode("utf-8", errors="surrogatepass")) > max_frame
    ):
        raise SpeechFault("invalid_frame")
    try:
        raw.encode("utf-8")
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SpeechFault("invalid_frame") from None
    if not isinstance(value, dict):
        raise SpeechFault("invalid_frame")
    return value


def pcm_delta(message):
    if not isinstance(message.get("delta"), str):
        raise SpeechFault("invalid_audio")
    try:
        return base64.b64decode(message["delta"], validate=True)
    except (ValueError, binascii.Error):
        raise SpeechFault("invalid_audio") from None


async def read_catalog(http, base_url, headers):
    # No paid request or inferred provider health. This is router-declared
    # transport metadata; root must prove credentials and provider separately.
    if base_url != "https://api.x.ai/v1" or not headers.get("Authorization"):
        raise ValueError("TTS endpoint requires protected static credentials")
    return {"data": [{"id": SERVICE, "capabilities": ["speech", "streaming"]}]}


def connect_provider(engine, headers, voice):
    if engine.base_url != "https://api.x.ai/v1" or voice != "rex":
        raise SpeechFault("unsupported_provider")
    if not headers.get("Authorization"):
        raise SpeechFault("provider_unconfigured")
    from websockets.asyncio.client import connect

    class DirectTTS(connect):
        def process_redirect(self, exc):
            # This engine owns one fixed paid endpoint. Do not follow another
            # location or silently make additional upstream attempts.
            return exc

    # The default library logger can include frame contents at DEBUG. Suppress
    # it locally rather than altering logging for other transports.
    logger = logging.Logger("model-router.speech.private", level=logging.CRITICAL + 1)  # noqa: LOG001 - isolated non-propagating frame logger
    logger.propagate = False
    query = urlencode(
        {"voice": voice, "language": "en", "codec": "pcm", "sample_rate": 24000}
    )
    return DirectTTS(
        "wss://api.x.ai/v1/tts?" + query,
        additional_headers={"Authorization": headers["Authorization"]},
        proxy=None,
        compression=None,
        open_timeout=5,
        close_timeout=1,
        max_size=MAX_PROVIDER_FRAME,
        max_queue=4,
        write_limit=32768,
        logger=logger,
    )
