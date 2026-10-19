"""Distinct instructions-only JevOS wire; no V1 or calibrated-confidence claims."""
import json
import math
import re

PROTOCOL = "jevos_native"
CONTRACT = "jevos.noul.v4"  # Consumer-owned identifier, not upstream api_version.
MAX_BODY = 8192
MAX_REPLY = 4096


class NativeFault(Exception):
    """Only constant bounded codes may leave this protocol boundary."""


def request_body(payload):
    if not isinstance(payload, dict) or set(payload) != {"model", "state", "questions"}:
        raise NativeFault("invalid_native_request")
    if not isinstance(payload["model"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}", payload["model"]):
        raise NativeFault("invalid_native_model")
    questions = payload["questions"]
    if not isinstance(questions, dict) or len(questions) != 1:
        raise NativeFault("one_noul_required")
    qid, question = next(iter(questions.items()))
    if not isinstance(qid, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", qid):
        raise NativeFault("invalid_question_id")
    if (not isinstance(question, dict) or set(question) != {"type", "instructions"}
            or question["type"] != "noul"):
        raise NativeFault("instructions_only_noul_required")
    text = question["instructions"]
    if not isinstance(text, str) or not text.strip():
        raise NativeFault("invalid_instructions")
    state = payload["state"]
    if not isinstance(state, (str, dict, list)) or not state:
        raise NativeFault("invalid_state")
    remaining = 256

    def visit(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 8:
            raise NativeFault("state_structure_limit")
        if isinstance(value, dict):
            if any(not isinstance(k, str) for k in value):
                raise NativeFault("invalid_state")
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
        elif value is not None and type(value) not in (str, bool, int, float):
            raise NativeFault("invalid_state")

    visit(state)
    try:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(text.encode("utf-8")) > 2048 or len(raw) > MAX_BODY:
            raise NativeFault("native_body_limit")
    except (ValueError, UnicodeError, OverflowError):
        raise NativeFault("invalid_native_request") from None
    return qid


def response_body(raw, qid, model, max_tokens):
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise NativeFault("invalid_native_response") from None
    if not isinstance(body, dict) or set(body) != {"model", "answers", "usage"}:
        raise NativeFault("invalid_native_response")
    if body["model"] != model or not isinstance(body["answers"], dict):
        raise NativeFault("native_identity_mismatch")
    if set(body["answers"]) != {qid}:
        raise NativeFault("native_question_mismatch")
    answer, usage = body["answers"][qid], body["usage"]
    if not isinstance(answer, dict) or set(answer) != {"type", "noul"}:
        raise NativeFault("invalid_native_answer")
    p = answer["noul"]
    if answer["type"] != "noul" or type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
        raise NativeFault("invalid_native_probability")
    if (not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"}
            or type(usage["input_tokens"]) is not int
            or not 1 <= usage["input_tokens"] <= max_tokens
            or type(usage["output_tokens"]) is not int or usage["output_tokens"] != 0):
        raise NativeFault("invalid_native_usage")
    return body


def server_timing(value):
    if not isinstance(value, str) or len(value) > 512:
        raise NativeFault("missing_native_timing")
    result = {}
    for part in value.split(","):
        match = re.fullmatch(r"\s*([a-z_]+);dur=([0-9]+(?:\.[0-9]+)?)\s*", part)
        if not match or match[1] in result:
            raise NativeFault("invalid_native_timing")
        result[match[1]] = float(match[2])
    if not {"queue", "inference", "total"} <= result.keys() or any(not math.isfinite(v) for v in result.values()):
        raise NativeFault("invalid_native_timing")
    return result


async def read_health(http, base_url, headers):
    raw = bytearray()
    async with http.stream("GET", base_url + "/health", headers=headers, timeout=2) as response:
        if response.status_code != 200:
            raise NativeFault("native_catalog_unavailable")
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise NativeFault("native_catalog_limit")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError):
        raise NativeFault("invalid_native_catalog") from None
    if (not isinstance(document, dict) or document.get("status") != "ready"
            or not isinstance(document.get("model"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", document["model"])
            or not isinstance(document.get("fingerprint"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", document["fingerprint"])):
        raise NativeFault("invalid_native_catalog")
    return document


async def read_catalog(http, base_url, headers):
    health = await read_health(http, base_url, headers)
    # /health states the actual served name. jev-* aliases are not extra engines.
    return {"data": [{"id": health["model"], "capabilities": ["decision"]}]}
