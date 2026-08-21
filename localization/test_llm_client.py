from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from localization_engine.llm.client import OpenAICompatibleLLMClient


def stream_response(*chunks: bytes, status_code: int = 200, headers: dict[str, str] | None = None) -> MagicMock:
    response = MagicMock(status_code=status_code, headers=headers or {})
    response.iter_content.return_value = list(chunks)
    return response


def sse_event(payload: object, *, line_ending: bytes = b"\n") -> bytes:
    if isinstance(payload, bytes):
        data = payload
    else:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"data: " + data + line_ending + line_ending


def complete_stream(content: str = "[]", *, line_ending: bytes = b"\n") -> bytes:
    return b"".join(
        [
            sse_event({"choices": [{"delta": {"content": content}, "finish_reason": None}]}, line_ending=line_ending),
            sse_event({"choices": [], "usage": {"completion_tokens": 1}}, line_ending=line_ending),
            sse_event({"choices": [{"delta": {}, "finish_reason": "stop"}]}, line_ending=line_ending),
            sse_event(b"[DONE]", line_ending=line_ending),
        ]
    )


class LLMClientTest(unittest.TestCase):
    @patch("localization_engine.llm.client._acquire_request_slot")
    @patch("localization_engine.llm.client._SHARED_SESSION.post")
    def test_kimi_stream_buffers_chunks_and_decodes_utf8(
        self,
        post: MagicMock,
        acquire_slot: MagicMock,
    ) -> None:
        wire = complete_stream("[中文]", line_ending=b"\r\n")
        split = wire.index("中".encode("utf-8")) + 1
        response = stream_response(wire[:17], wire[17:split], wire[split : split + 1], wire[split + 1 :])
        post.return_value = response

        client = OpenAICompatibleLLMClient(
            base_url="https://example.invalid/v1",
            access_token="token",
            model_name="kimi-k2.7-code",
            max_tokens=2048,
        )
        self.assertEqual(client.chat([{"role": "user", "content": "locate"}]), "[中文]")

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["max_completion_tokens"], 8192)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertIs(payload["stream"], True)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)
        self.assertEqual(post.call_args.kwargs["headers"]["Accept"], "text/event-stream")
        acquire_slot.assert_called_once_with(
            "https://example.invalid/v1",
            "kimi-k2.7-code",
            wait_timeout_seconds=1200.0,
        )
        acquire_slot.return_value.release.assert_called_once_with()
        response.close.assert_called_once_with()

    @patch("localization_engine.llm.client._acquire_request_slot", return_value=None)
    @patch("localization_engine.llm.client._SHARED_SESSION.post")
    def test_multiline_data_event_is_reassembled(
        self,
        post: MagicMock,
        _acquire_slot: MagicMock,
    ) -> None:
        raw = json.dumps(
            {"choices": [{"delta": {"content": "[]"}, "finish_reason": None}]},
            separators=(",", ":"),
        ).encode("utf-8")
        split = raw.index(b"[]") + 1
        wire = b"data: " + raw[:split] + b"\ndata: " + raw[split:] + b"\n\n" + sse_event(b"[DONE]")
        post.return_value = stream_response(wire)

        client = OpenAICompatibleLLMClient("https://example.invalid/v1", "token", "kimi-k2.7-code")
        self.assertEqual(client.chat([{"role": "user", "content": "locate"}]), "[]")

    @patch("localization_engine.llm.client.time.sleep")
    @patch("localization_engine.llm.client._acquire_request_slot")
    @patch("localization_engine.llm.client._SHARED_SESSION.post")
    def test_truncated_stream_retries_after_releasing_slot(
        self,
        post: MagicMock,
        acquire_slot: MagicMock,
        sleep: MagicMock,
    ) -> None:
        first = stream_response(sse_event({"choices": [{"delta": {"content": "["}}]}))
        second = stream_response(complete_stream("[]"))
        post.side_effect = [first, second]
        lock = MagicMock()
        acquire_slot.return_value = lock

        def assert_released_before_backoff(_seconds: float) -> None:
            self.assertGreaterEqual(lock.release.call_count, 1)

        sleep.side_effect = assert_released_before_backoff
        client = OpenAICompatibleLLMClient(
            "https://example.invalid/v1",
            "token",
            "kimi-k2.7-code",
            max_retries=2,
        )
        self.assertEqual(client.chat([{"role": "user", "content": "locate"}]), "[]")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(lock.release.call_count, 2)
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()
        sleep.assert_called_once_with(1)

    @patch("localization_engine.llm.client.time.sleep")
    @patch("localization_engine.llm.client._acquire_request_slot", return_value=None)
    @patch("localization_engine.llm.client._SHARED_SESSION.post")
    def test_http_400_is_not_retried(
        self,
        post: MagicMock,
        _acquire_slot: MagicMock,
        sleep: MagicMock,
    ) -> None:
        response = stream_response(b'{"error":{"message":"bad parameter"}}', status_code=400)
        post.return_value = response
        client = OpenAICompatibleLLMClient(
            "https://example.invalid/v1",
            "token",
            "kimi-k2.7-code",
            max_retries=3,
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            client.chat([{"role": "user", "content": "locate"}])
        post.assert_called_once()
        sleep.assert_not_called()
        response.close.assert_called_once_with()

    @patch("localization_engine.llm.client.time.sleep")
    @patch("localization_engine.llm.client._acquire_request_slot", return_value=None)
    @patch("localization_engine.llm.client._SHARED_SESSION.post")
    def test_http_429_honors_retry_after(
        self,
        post: MagicMock,
        _acquire_slot: MagicMock,
        sleep: MagicMock,
    ) -> None:
        limited = stream_response(b"rate limited", status_code=429, headers={"Retry-After": "2.5"})
        success = stream_response(complete_stream("[]"))
        post.side_effect = [limited, success]
        client = OpenAICompatibleLLMClient(
            "https://example.invalid/v1",
            "token",
            "kimi-k2.7-code",
            max_retries=2,
        )
        self.assertEqual(client.chat([{"role": "user", "content": "locate"}]), "[]")
        sleep.assert_called_once_with(2.5)
        limited.close.assert_called_once_with()
        success.close.assert_called_once_with()

    @patch("localization_engine.llm.client._acquire_request_slot", return_value=None)
    @patch("localization_engine.llm.client._SHARED_SESSION.post")
    def test_stream_error_event_fails(
        self,
        post: MagicMock,
        _acquire_slot: MagicMock,
    ) -> None:
        response = stream_response(
            b"event: error\n" + sse_event({"error": {"message": "upstream failed", "code": "upstream"}})
        )
        post.return_value = response
        client = OpenAICompatibleLLMClient(
            "https://example.invalid/v1",
            "token",
            "kimi-k2.7-code",
            max_retries=1,
        )
        with self.assertRaisesRegex(RuntimeError, "upstream failed"):
            client.chat([{"role": "user", "content": "locate"}])
        response.close.assert_called_once_with()

    @patch("localization_engine.llm.client._acquire_request_slot", return_value=None)
    @patch("localization_engine.llm.client._SHARED_SESSION.post")
    def test_length_finish_reason_increases_budget_and_retries(
        self,
        post: MagicMock,
        _acquire_slot: MagicMock,
    ) -> None:
        truncated = stream_response(
            sse_event({"choices": [{"delta": {"content": "partial"}, "finish_reason": "length"}]})
        )
        success = stream_response(complete_stream("[]"))
        post.side_effect = [truncated, success]
        client = OpenAICompatibleLLMClient(
            "https://example.invalid/v1",
            "token",
            "kimi-k2.7-code",
            max_retries=2,
        )
        with patch("localization_engine.llm.client.time.sleep"):
            self.assertEqual(client.chat([{"role": "user", "content": "locate"}]), "[]")
        self.assertEqual(post.call_args_list[0].kwargs["json"]["max_completion_tokens"], 8192)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["max_completion_tokens"], 16384)

    @patch("localization_engine.llm.client._acquire_request_slot", return_value=None)
    @patch("localization_engine.llm.client._SHARED_SESSION.post")
    def test_done_with_empty_content_is_rejected(
        self,
        post: MagicMock,
        _acquire_slot: MagicMock,
    ) -> None:
        post.return_value = stream_response(sse_event(b"[DONE]"))
        client = OpenAICompatibleLLMClient(
            "https://example.invalid/v1",
            "token",
            "kimi-k2.7-code",
            max_retries=1,
        )
        with self.assertRaisesRegex(RuntimeError, "empty content"):
            client.chat([{"role": "user", "content": "locate"}])

    @patch("localization_engine.llm.client._acquire_request_slot", return_value=None)
    @patch("localization_engine.llm.client._SHARED_SESSION.post")
    def test_default_models_keep_existing_parameters(
        self,
        post: MagicMock,
        _acquire_slot: MagicMock,
    ) -> None:
        response = MagicMock(status_code=200, headers={})
        response.json.return_value = {
            "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]
        }
        post.return_value = response

        client = OpenAICompatibleLLMClient(
            base_url="https://example.invalid/v1",
            access_token="token",
            model_name="gpt-5.6-sol",
            max_tokens=2048,
        )
        self.assertEqual(client.chat([{"role": "user", "content": "locate"}]), "[]")

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertEqual(payload["temperature"], 0.1)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("Accept", post.call_args.kwargs["headers"])
        response.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
