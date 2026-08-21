from __future__ import annotations

"""LLM client for OpenAI-compatible chat completions."""

import hashlib
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from filelock import FileLock, Timeout as FileLockTimeout


_SHARED_SESSION = requests.Session()
_KIMI_REQUEST_SLOTS = 4


class _RetryableLLMError(RuntimeError):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class _CompletionLengthError(_RetryableLLMError):
    pass


class _NonRetryableLLMError(RuntimeError):
    pass


def _acquire_request_slot(
    base_url: str,
    model_name: str,
    *,
    wait_timeout_seconds: float = 1200.0,
) -> FileLock | None:
    if model_name.casefold() != "kimi-k2.7-code":
        return None
    namespace = hashlib.sha256(f"{base_url}|{model_name}".encode("utf-8")).hexdigest()[:12]
    deadline = time.monotonic() + max(1.0, wait_timeout_seconds)
    while True:
        for index in range(_KIMI_REQUEST_SLOTS):
            lock = FileLock(str(Path(tempfile.gettempdir()) / f"arkeval_kimi_{namespace}_{index}.lock"))
            try:
                lock.acquire(timeout=0)
                return lock
            except FileLockTimeout:
                continue
        if time.monotonic() >= deadline:
            raise _RetryableLLMError(
                f"timed out waiting for a Kimi request slot after {wait_timeout_seconds:.0f}s"
            )
        time.sleep(0.1)


def _response_excerpt(response: requests.Response, limit: int = 500) -> str:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=min(4096, limit)):
        if not chunk:
            continue
        raw = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        remaining = limit - size
        chunks.append(raw[:remaining])
        size += min(len(raw), remaining)
        if size >= limit:
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


def _iter_sse_events(response: requests.Response):
    buffer = bytearray()
    event_lines: list[bytes] = []
    for chunk in response.iter_content(chunk_size=4096):
        if not chunk:
            continue
        buffer.extend(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if line:
                event_lines.append(line)
            elif event_lines:
                yield event_lines
                event_lines = []
    if buffer:
        line = bytes(buffer)
        if line.endswith(b"\r"):
            line = line[:-1]
        if line:
            event_lines.append(line)
    if event_lines:
        yield event_lines


def _sse_event_data(event_lines: list[bytes]) -> tuple[str, list[bytes]]:
    event_type = ""
    data_lines: list[bytes] = []
    raw_json_line: bytes | None = None
    for index, line in enumerate(event_lines):
        if index == 0:
            line = line.removeprefix(b"\xef\xbb\xbf")
        if line.startswith(b":"):
            continue
        field, separator, value = line.partition(b":")
        if separator and value.startswith(b" "):
            value = value[1:]
        if field == b"event":
            event_type = value.decode("utf-8", errors="strict")
        elif field == b"data":
            data_lines.append(value)
        elif not separator and line.lstrip().startswith((b"{", b"[")):
            raw_json_line = line.strip()
    if not data_lines and raw_json_line is not None:
        data_lines.append(raw_json_line)
    return event_type, data_lines


def _parse_sse_json(data_lines: list[bytes], event_index: int) -> dict:
    candidates = [b"\n".join(data_lines)]
    compact = b"".join(data_lines)
    if compact != candidates[0]:
        candidates.append(compact)
    last_error: Exception | None = None
    for raw in candidates:
        try:
            value = json.loads(raw.decode("utf-8", errors="strict"))
            if isinstance(value, dict):
                return value
            raise TypeError(f"expected object, got {type(value).__name__}")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            last_error = exc
    excerpt = compact[:300].decode("utf-8", errors="replace")
    raise _RetryableLLMError(
        f"malformed SSE event {event_index}: {last_error}; data={excerpt!r}"
    )


def _api_error_text(error: object) -> str:
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        code = str(error.get("code") or "").strip()
        if message and code:
            return f"{message} (code={code})"
        if message:
            return message
    return json.dumps(error, ensure_ascii=False)[:500]


def _stream_content(response: requests.Response, inactivity_timeout_seconds: float) -> str:
    content_parts: list[str] = []
    saw_terminal = False
    last_json_event = time.monotonic()
    for event_index, event_lines in enumerate(_iter_sse_events(response), start=1):
        if time.monotonic() - last_json_event > inactivity_timeout_seconds:
            raise _RetryableLLMError(
                f"stream produced no JSON event for {inactivity_timeout_seconds:.0f}s"
            )
        event_type, data_lines = _sse_event_data(event_lines)
        if not data_lines:
            continue
        joined = b"\n".join(data_lines).strip()
        if joined == b"[DONE]":
            saw_terminal = True
            break
        data = _parse_sse_json(data_lines, event_index)
        last_json_event = time.monotonic()
        if event_type.casefold() == "error" or data.get("error") is not None:
            raise _RetryableLLMError(
                f"LLM API stream error: {_api_error_text(data.get('error', data))}"
            )
        choices = data.get("choices")
        if choices is None:
            if data.get("usage") is not None:
                continue
            raise _RetryableLLMError(f"SSE event {event_index} has no choices")
        if not isinstance(choices, list):
            raise _RetryableLLMError(f"SSE event {event_index} choices is not a list")
        if not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            raise _RetryableLLMError(f"SSE event {event_index} choice is not an object")
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise _RetryableLLMError(f"SSE event {event_index} delta is not an object")
        content = delta.get("content")
        if content is None and isinstance(choice.get("message"), dict):
            content = choice["message"].get("content")
        if content is not None:
            if not isinstance(content, str):
                raise _RetryableLLMError(f"SSE event {event_index} content is not text")
            content_parts.append(content)
        finish_reason = choice.get("finish_reason")
        if finish_reason:
            if finish_reason == "stop":
                saw_terminal = True
            elif finish_reason == "length":
                raise _CompletionLengthError("LLM completion exhausted its output budget")
            else:
                raise _NonRetryableLLMError(
                    f"LLM completion ended with finish_reason={finish_reason}"
                )
    if not saw_terminal:
        raise _RetryableLLMError("LLM stream ended before [DONE] or finish_reason=stop")
    content = "".join(content_parts).strip()
    if not content:
        raise _RetryableLLMError("LLM stream completed with empty content")
    return content


@dataclass
class OpenAICompatibleLLMClient:
    base_url: str
    access_token: str
    model_name: str
    endpoint_path: str = "chat/completions"
    timeout_seconds: float = 120.0
    max_retries: int = 3
    max_tokens: int = 2048

    def chat(self, messages: list[dict[str, str]]) -> str:
        base = self.base_url.rstrip("/")
        url = f"{base}/{self.endpoint_path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model_name,
            "messages": messages,
        }
        if self.model_name.casefold() == "kimi-k2.7-code":
            payload["max_completion_tokens"] = max(self.max_tokens, 8192)
            payload["reasoning_effort"] = "low"
            payload["stream"] = True
            headers["Accept"] = "text/event-stream"
        else:
            payload["max_tokens"] = self.max_tokens
            payload["temperature"] = 0.1
        last_error: Exception | None = None
        no_proxy = {"http": None, "https": None}
        attempts = max(1, self.max_retries)
        for attempt in range(1, attempts + 1):
            request_slot: FileLock | None = None
            response: requests.Response | None = None
            retry_after: float | None = None
            try:
                request_slot = _acquire_request_slot(
                    self.base_url,
                    self.model_name,
                    wait_timeout_seconds=max(600.0, self.timeout_seconds * 10),
                )
                response = _SHARED_SESSION.post(
                    url,
                    headers=headers,
                    json=dict(payload),
                    timeout=self.timeout_seconds,
                    proxies=no_proxy,
                    stream=bool(payload.get("stream")),
                )
                if response.status_code >= 400:
                    excerpt = _response_excerpt(response)
                    headers_map = getattr(response, "headers", {}) or {}
                    raw_retry_after = headers_map.get("Retry-After", "")
                    try:
                        retry_after = max(0.0, float(raw_retry_after))
                    except (TypeError, ValueError):
                        retry_after = None
                    message = f"LLM API HTTP {response.status_code}: {excerpt}"
                    if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
                        raise _RetryableLLMError(message, retry_after=retry_after)
                    raise _NonRetryableLLMError(message)
                if payload.get("stream"):
                    return _stream_content(response, self.timeout_seconds)
                else:
                    data = response.json()
                    if data.get("error") is not None:
                        raise _RetryableLLMError(f"LLM API error: {_api_error_text(data['error'])}")
                    choices = data.get("choices")
                    if not choices or not isinstance(choices, list):
                        raise _RetryableLLMError(f"invalid chat response: {data}")
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason")
                    if finish_reason and finish_reason != "stop":
                        raise _NonRetryableLLMError(
                            f"LLM completion ended with finish_reason={finish_reason}"
                        )
                    content = (choice.get("message") or {}).get("content") or ""
                    content = content.strip()
                    if not content:
                        raise _RetryableLLMError("LLM response completed with empty content")
                    return content
            except _NonRetryableLLMError as exc:
                raise RuntimeError(f"LLM API failed: {exc}") from exc
            except Exception as exc:
                last_error = exc
                retry_after = getattr(exc, "retry_after", retry_after)
                if isinstance(exc, _CompletionLengthError):
                    payload["max_completion_tokens"] = min(
                        int(payload["max_completion_tokens"]) * 2,
                        32768,
                    )
            finally:
                if response is not None:
                    response.close()
                if request_slot is not None:
                    request_slot.release()
            if attempt < attempts:
                time.sleep(retry_after if retry_after is not None else min(2 ** (attempt - 1), 30))
        raise RuntimeError(f"LLM API failed after {attempts} attempts: {last_error}")


ModelScopeLLMClient = OpenAICompatibleLLMClient
