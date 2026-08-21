from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import httpx
import together
from anthropic import AI_PROMPT, HUMAN_PROMPT, Anthropic, AnthropicBedrock
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    AzureOpenAI,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from simple_parsing.helpers.serialization.serializable import FrozenSerializable, Serializable
from tenacity import (
    retry,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from sweagent.agent.commands import Command
from sweagent.utils.config import keys_config
from sweagent.utils.log import get_logger

logger = get_logger("api_models")


class _EmptyChoicesError(RuntimeError):
    pass


_OPENAI_STANDARD_WAIT = wait_random_exponential(min=1, max=30)
_OPENAI_RATE_LIMIT_WAIT = wait_random_exponential(min=5, max=120)


def _wait_openai_retry(retry_state: Any) -> float:
    outcome = getattr(retry_state, "outcome", None)
    exc = outcome.exception() if outcome is not None else None
    strategy = _OPENAI_RATE_LIMIT_WAIT if isinstance(exc, RateLimitError) else _OPENAI_STANDARD_WAIT
    return strategy(retry_state)


def _stop_openai_retry(retry_state: Any) -> bool:
    outcome = getattr(retry_state, "outcome", None)
    exc = outcome.exception() if outcome is not None else None
    text = str(exc)
    if isinstance(exc, RateLimitError) and "GoUsageLimitError" in text:
        max_attempts = 240
    elif (
        isinstance(exc, InternalServerError)
        and "kimi-k2.7-code" in text
        and "distributor" in text
    ):
        max_attempts = 30
    else:
        max_attempts = 5
    return retry_state.attempt_number >= max_attempts


def _resolve_openai_http_timeout() -> float:
    """Total HTTP timeout (connect + read) for OpenAI-compatible clients.

    Default 600s: first turn often includes long system prompt + in-history demonstrations;
    without an explicit timeout the process appears hung at ``bash-$`` with no log line.
    Override with env ``OPENAI_HTTP_TIMEOUT`` or ``keys.cfg`` key of the same name.
    """
    raw = os.environ.get("OPENAI_HTTP_TIMEOUT")
    if raw is None or str(raw).strip() == "":
        raw = keys_config.get("OPENAI_HTTP_TIMEOUT", None)
    if raw is None or str(raw).strip() == "":
        return 600.0
    return float(raw)


@dataclass(frozen=True)
class ModelArguments(FrozenSerializable):
    """Arguments configuring the model and its behavior."""

    # Name of the model to use
    model_name: str
    # Cost limit for every instance (task)
    per_instance_cost_limit: float = 0.0
    # Total cost limit
    total_cost_limit: float = 0.0
    # Sampling temperature
    temperature: float = 1.0
    # Sampling top-p
    top_p: float = 1.0
    # Path to replay file when using the replay model
    replay_path: str | None = None
    # Host URL when using Ollama model
    host_url: str = "localhost:11434"
    # Max agent turns (LLM calls) per instance; 0 = unlimited. When reached, submit is forced.
    max_steps_per_instance: int = 0


@dataclass
class APIStats(Serializable):
    total_cost: float = 0
    instance_cost: float = 0
    tokens_sent: int = 0
    tokens_received: int = 0
    api_calls: int = 0

    def __add__(self, other):
        if not isinstance(other, APIStats):
            msg = "Can only add APIStats with APIStats"
            raise TypeError(msg)

        return APIStats(
            **{field.name: getattr(self, field.name) + getattr(other, field.name) for field in fields(self)},
        )

    def replace(self, other):
        if not isinstance(other, APIStats):
            msg = "Can only replace APIStats with APIStats"
            raise TypeError(msg)

        return APIStats(**{field.name: getattr(other, field.name) for field in fields(self)})


class ContextWindowExceededError(Exception):
    pass


class CostLimitExceededError(Exception):
    pass


class BaseModel:
    MODELS = {}
    SHORTCUTS = {}

    def __init__(self, args: ModelArguments, commands: list[Command]):
        self.args = args
        self.commands = commands
        self.model_metadata = {}
        self.stats = APIStats()

        # Map `model_name` to API-compatible name `api_model`
        self.api_model = (
            self.SHORTCUTS[self.args.model_name] if self.args.model_name in self.SHORTCUTS else self.args.model_name
        )

        # Map model name to metadata (cost, context info)
        MODELS = {
            **{dest: self.MODELS[src] for dest, src in self.SHORTCUTS.items()},
            **self.MODELS,
        }
        if args.model_name in MODELS:
            self.model_metadata = MODELS[args.model_name]
        elif args.model_name.startswith("ft:"):
            ft_model = args.model_name.split(":")[1]
            self.model_metadata = MODELS[ft_model]
        elif args.model_name.startswith("ollama:"):
            self.api_model = args.model_name.split("ollama:", 1)[1]
            self.model_metadata = self.MODELS[self.api_model]
        elif args.model_name.startswith("azure:"):
            azure_model = args.model_name.split("azure:", 1)[1]
            self.model_metadata = MODELS[azure_model]
        elif args.model_name.startswith("bedrock:"):
            self.api_model = args.model_name.split("bedrock:", 1)[1]
            self.model_metadata = MODELS[self.api_model]
        else:
            msg = f"Unregistered model ({args.model_name}). Add model name to MODELS metadata to {self.__class__}"
            raise ValueError(msg)

    def reset_stats(self, other: APIStats | None = None):
        if other is None:
            self.stats = APIStats(total_cost=self.stats.total_cost)
            logger.info("Resetting model stats")
        else:
            self.stats = other

    def update_stats(self, input_tokens: int, output_tokens: int) -> float:
        """
        Calculates the cost of a response from the openai API.

        Args:
        input_tokens (int): The number of tokens in the prompt.
        output_tokens (int): The number of tokens in the response.

        Returns:
        float: The cost of the response.
        """
        # Calculate cost and update cost related fields
        cost = (
            self.model_metadata["cost_per_input_token"] * input_tokens
            + self.model_metadata["cost_per_output_token"] * output_tokens
        )
        self.stats.total_cost += cost
        self.stats.instance_cost += cost
        self.stats.tokens_sent += input_tokens
        self.stats.tokens_received += output_tokens
        self.stats.api_calls += 1

        # Log updated cost values to std. out.
        logger.info(
            f"input_tokens={input_tokens:,}, "
            f"output_tokens={output_tokens:,}, "
            f"instance_cost={self.stats.instance_cost:.2f}, "
            f"cost={cost:.2f}",
        )
        logger.info(
            f"total_tokens_sent={self.stats.tokens_sent:,}, "
            f"total_tokens_received={self.stats.tokens_received:,}, "
            f"total_cost={self.stats.total_cost:.2f}, "
            f"total_api_calls={self.stats.api_calls:,}",
        )

        # Check whether total cost or instance cost limits have been exceeded
        if 0 < self.args.total_cost_limit <= self.stats.total_cost:
            logger.warning(f"Cost {self.stats.total_cost:.2f} exceeds limit {self.args.total_cost_limit:.2f}")
            msg = "Total cost limit exceeded"
            raise CostLimitExceededError(msg)

        if 0 < self.args.per_instance_cost_limit <= self.stats.instance_cost:
            logger.warning(f"Cost {self.stats.instance_cost:.2f} exceeds limit {self.args.per_instance_cost_limit:.2f}")
            msg = "Instance cost limit exceeded"
            raise CostLimitExceededError(msg)
        return cost

    def query(self, history: list[dict[str, str]]) -> str:
        msg = "Use a subclass of BaseModel"
        raise NotImplementedError(msg)


class OpenAIModel(BaseModel):
    MODELS = {
        "gpt-3.5-turbo-0125": {
            "max_context": 16_385,
            "cost_per_input_token": 5e-07,
            "cost_per_output_token": 1.5e-06,
        },
        "gpt-3.5-turbo-1106": {
            "max_context": 16_385,
            "cost_per_input_token": 1.5e-06,
            "cost_per_output_token": 2e-06,
        },
        "gpt-3.5-turbo-16k-0613": {
            "max_context": 16_385,
            "cost_per_input_token": 1.5e-06,
            "cost_per_output_token": 2e-06,
        },
        "gpt-4-32k-0613": {
            "max_context": 32_768,
            "cost_per_input_token": 6e-05,
            "cost_per_output_token": 0.00012,
        },
        "gpt-4-0613": {
            "max_context": 8_192,
            "cost_per_input_token": 3e-05,
            "cost_per_output_token": 6e-05,
        },
        "gpt-4-1106-preview": {
            "max_context": 128_000,
            "cost_per_input_token": 1e-05,
            "cost_per_output_token": 3e-05,
        },
        "gpt-4-0125-preview": {
            "max_context": 128_000,
            "cost_per_input_token": 1e-05,
            "cost_per_output_token": 3e-05,
        },
        "gpt-4-turbo-2024-04-09": {
            "max_context": 128_000,
            "cost_per_input_token": 1e-05,
            "cost_per_output_token": 3e-05,
        },
        "gpt-4o-2024-05-13": {
            "max_context": 128_000,
            "cost_per_input_token": 5e-06,
            "cost_per_output_token": 15e-06,
        },
        "gpt-5.5": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "gpt-5.6-sol": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "gpt-5.3-codex-spark": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },

        # DashScope / OpenAI-compatible endpoints (e.g. Qwen/GLM/Kimi via OPENAI_API_BASE_URL).
        #
        # NOTE: We intentionally set costs to 0 because pricing varies by provider and
        # region; users can still enforce budgets by API-call limits outside this
        # estimator, or by updating these values later.
        "qwen3-32b": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "qwen3-coder-30b-a3b-instruct": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "qwen3-coder-480b-a35b-instruct": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "qwen3-coder-plus": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "qwen3-235b-a22b-instruct": {
            "max_context": 256_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "qwen3-235b-a22b-thinking-2507": {
            "max_context": 256_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "qwen3.5-flash": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "tongyi-xiaomi-analysis-pro": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "qwen3.5-122b-a10b": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "qwen3.5-plus-2026-02-15": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "glm-5": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "qwen3-max-2026-01-23": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "kimi-k2.5": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "kimi-k2.7-code": {
            "max_context": 128_000,
            "max_tokens": 8_192,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "qwen3-vl-flash-2026-01-22": {
            "max_context": 128_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "MiniMax-M2": {
            "max_context": 200_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "MiniMax-M2.5": {
            "max_context": 200_000,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "DeepSeek-R1-Distill-Qwen-32B": {
            "max_context": 131_072,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
        "deepseek-r1-distill-qwen-32b": {
            "max_context": 131_072,
            "cost_per_input_token": 0.0,
            "cost_per_output_token": 0.0,
        },
    }

    SHORTCUTS = {
        "qwen3-32b-instruct": "qwen3-32b",
        "minimax-m2": "MiniMax-M2",
        "minimax-m2.5": "MiniMax-M2.5",
        "minimax-2.5": "MiniMax-M2.5",
        "gpt3": "gpt-3.5-turbo-1106",
        "gpt3-legacy": "gpt-3.5-turbo-16k-0613",
        "gpt4": "gpt-4-1106-preview",
        "gpt4-legacy": "gpt-4-0613",
        "gpt4-0125": "gpt-4-0125-preview",
        "gpt3-0125": "gpt-3.5-turbo-0125",
        "gpt4-turbo": "gpt-4-turbo-2024-04-09",
        "gpt4o": "gpt-4o-2024-05-13",
        "deepseek-r1-32b": "deepseek-r1-distill-qwen-32b",
    }

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)
        if self.args.model_name in {"minimax-m2.5", "minimax-2.5"}:
            self.api_model = "minimax-m2.5"

        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)

        # Set OpenAI key
        http_timeout = _resolve_openai_http_timeout()
        client_timeout = httpx.Timeout(connect=15.0, read=http_timeout, write=30.0, pool=15.0)
        if self.args.model_name.startswith("azure"):
            self.api_model = keys_config["AZURE_OPENAI_DEPLOYMENT"]
            self.client = AzureOpenAI(
                api_key=keys_config["AZURE_OPENAI_API_KEY"],
                azure_endpoint=keys_config["AZURE_OPENAI_ENDPOINT"],
                api_version=keys_config.get("AZURE_OPENAI_API_VERSION", "2024-02-01"),
                timeout=client_timeout,
                max_retries=0,
            )
            logger.info(
                "Azure OpenAI client: deployment=%s read_timeout=%.0fs connect_timeout=15s endpoint=%s",
                self.api_model,
                http_timeout,
                keys_config.get("AZURE_OPENAI_ENDPOINT", ""),
            )
        else:
            api_base_url: str | None = keys_config.get("OPENAI_API_BASE_URL", None)
            self.client = OpenAI(
                api_key=keys_config["OPENAI_API_KEY"],
                base_url=api_base_url,
                timeout=client_timeout,
                max_retries=0,
            )
            logger.info(
                "OpenAI-compatible client: model=%s read_timeout=%.0fs connect_timeout=15s base_url=%s",
                self.api_model,
                http_timeout,
                api_base_url or "(default)",
            )

    def history_to_messages(
        self,
        history: list[dict[str, str]],
        is_demonstration: bool = False,
    ) -> str | list[dict[str, str]]:
        """
        Create `messages` by filtering out all keys except for role/content per `history` turn
        """
        # Remove system messages if it is a demonstration
        if is_demonstration:
            history = [entry for entry in history if entry["role"] != "system"]
            return "\n".join([entry["content"] for entry in history])
        # Return history components with just role, content fields
        return [{k: v for k, v in entry.items() if k in ["role", "content"]} for entry in history]

    def _create_chat_completion(self, history: list[dict[str, str]]):
        if self.api_model == "kimi-k2.7-code":
            return self.client.chat.completions.create(
                messages=self.history_to_messages(history),
                model=self.api_model,
                max_tokens=self.model_metadata["max_tokens"],
            )
        return self.client.chat.completions.create(
            messages=self.history_to_messages(history),
            model=self.api_model,
            temperature=self.args.temperature,
            top_p=self.args.top_p,
        )

    @staticmethod
    def _is_context_length_error(exc: BadRequestError) -> bool:
        body = getattr(exc, "body", None)
        error = body.get("error", body) if isinstance(body, dict) else {}
        code = getattr(exc, "code", None) or (error.get("code") if isinstance(error, dict) else None)
        if code == "context_length_exceeded":
            return True
        message = error.get("message") if isinstance(error, dict) else None
        text = " ".join(str(value) for value in (code, message, exc) if value).casefold()
        return any(
            marker in text
            for marker in (
                "context_length_exceeded",
                "maximum context length",
                "context window exceeded",
                "exceeds the context window",
            )
        )

    @staticmethod
    def _response_summary(response: Any) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "request_id": getattr(response, "_request_id", None) or getattr(response, "request_id", None),
            "id": getattr(response, "id", None),
            "object": getattr(response, "object", None),
            "model": getattr(response, "model", None),
            "created": getattr(response, "created", None),
        }
        choices = getattr(response, "choices", None)
        if choices is None:
            summary["choices_count"] = None
            summary["first_finish_reason"] = None
        else:
            summary["choices_count"] = len(choices)
            if len(choices) > 0:
                summary["first_finish_reason"] = getattr(choices[0], "finish_reason", None)
            else:
                summary["first_finish_reason"] = None
        usage = getattr(response, "usage", None)
        if usage is None:
            summary["usage"] = None
        else:
            summary["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        payload_keys = None
        if hasattr(response, "model_dump"):
            try:
                dumped = response.model_dump()
                if isinstance(dumped, dict):
                    payload_keys = sorted(dumped.keys())
            except Exception:
                payload_keys = None
        summary["payload_keys"] = payload_keys
        return summary

    def _log_response_anomaly(self, message: str, response: Any, *, attempt: int) -> None:
        summary = self._response_summary(response)
        logger.warning(
            "%s attempt=%d response_summary=%s",
            message,
            attempt,
            json.dumps(summary, ensure_ascii=False, default=str),
        )

    def _record_usage(self, response: Any, *, attempt: int) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            self._log_response_anomaly("Model response.usage is missing.", response, attempt=attempt)
            raise RuntimeError(
                "Model response.usage is missing. This is treated as a config/auth anomaly; "
                "please verify OPENAI_API_KEY and OPENAI_API_BASE_URL source (env may override keys.cfg)."
            )

        input_tokens = getattr(usage, "prompt_tokens", None)
        output_tokens = getattr(usage, "completion_tokens", None)
        if input_tokens is None or output_tokens is None:
            self._log_response_anomaly(
                "Model response.usage has missing token fields.",
                response,
                attempt=attempt,
            )
            raise RuntimeError(
                "Model response.usage token fields are missing. This is treated as a config/auth anomaly; "
                "please verify OPENAI_API_KEY and OPENAI_API_BASE_URL source (env may override keys.cfg)."
            )

        self.update_stats(int(input_tokens), int(output_tokens))

    @retry(
        # Longer backoff for TPM / 429 throttling (3×1–15s was too weak for cloud quotas).
        wait=_wait_openai_retry,
        reraise=True,
        stop=_stop_openai_retry,
        retry=retry_if_exception_type(
            (
                APIConnectionError,
                APITimeoutError,
                AuthenticationError,
                InternalServerError,
                RateLimitError,
                _EmptyChoicesError,
            )
        ),
    )
    def query(self, history: list[dict[str, str]]) -> str:
        """
        Query the OpenAI API with the given `history` and return the response.
        """
        logger.info(
            "LLM chat.completions starting: model=%s history_turns=%d",
            self.api_model,
            len(history),
        )
        t0 = time.perf_counter()
        try:
            response = self._create_chat_completion(history)
        except BadRequestError as exc:
            if not self._is_context_length_error(exc):
                raise
            msg = f"Context window ({self.model_metadata['max_context']} tokens) exceeded"
            raise CostLimitExceededError(msg) from exc
        logger.info("LLM chat.completions finished in %.2fs (attempt 1)", time.perf_counter() - t0)
        self._record_usage(response, attempt=1)

        choices = getattr(response, "choices", None) or []
        if not choices:
            self._log_response_anomaly("Model returned no choices.", response, attempt=1)
            raise _EmptyChoicesError("Model returned no choices in chat completion response.")

        choice0 = choices[0]
        message = getattr(choice0, "message", None)
        if message is None:
            raise RuntimeError("Model returned no message in first choice.")

        content = getattr(message, "content", None)
        if isinstance(content, list):
            # OpenAI-compatible endpoints may return structured content parts.
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text", "")
                else:
                    text = getattr(part, "text", "")
                if text:
                    parts.append(str(text))
            content = "".join(parts)
        elif content is not None:
            content = str(content)

        if content is None or content.strip() == "":
            reasoning_content = getattr(message, "reasoning_content", None)
            finish_reason = getattr(choice0, "finish_reason", None)
            refusal = getattr(message, "refusal", None)
            if reasoning_content:
                logger.warning(
                    "Model returned empty message.content but has reasoning_content; using reasoning_content as fallback."
                )
                content = str(reasoning_content)
            else:
                logger.warning(
                    "Model returned empty message content (finish_reason=%s, refusal=%r).",
                    finish_reason,
                    refusal,
                )
                raise RuntimeError("Model returned empty message content.")

        return content


class AnthropicModel(BaseModel):
    MODELS = {
        "claude-instant": {
            "max_context": 100_000,
            "cost_per_input_token": 1.63e-06,
            "cost_per_output_token": 5.51e-06,
        },
        "claude-2.0": {
            "max_context": 100_000,
            "cost_per_input_token": 1.102e-05,
            "cost_per_output_token": 3.268e-05,
        },
        "claude-2.1": {
            "max_context": 100_000,
            "cost_per_input_token": 1.102e-05,
            "cost_per_output_token": 3.268e-05,
        },
        "claude-3-opus-20240229": {
            "max_context": 200_000,
            "max_tokens": 4096,  # Max tokens to generate for Claude 3 models
            "cost_per_input_token": 1.5e-05,
            "cost_per_output_token": 7.5e-05,
        },
        "claude-3-sonnet-20240229": {
            "max_context": 200_000,
            "max_tokens": 4096,
            "cost_per_input_token": 3e-06,
            "cost_per_output_token": 1.5e-05,
        },
        "claude-3-haiku-20240307": {
            "max_context": 200_000,
            "max_tokens": 4096,
            "cost_per_input_token": 2.5e-07,
            "cost_per_output_token": 1.25e-06,
        },
    }

    SHORTCUTS = {
        "claude-2": "claude-2.1",
        "claude-opus": "claude-3-opus-20240229",
        "claude-sonnet": "claude-3-sonnet-20240229",
        "claude-haiku": "claude-3-haiku-20240307",
    }

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)

        # Set Anthropic key
        self.api = Anthropic(api_key=keys_config["ANTHROPIC_API_KEY"])

    def history_to_messages(
        self,
        history: list[dict[str, str]],
        is_demonstration: bool = False,
    ) -> str | list[dict[str, str]]:
        """
        Create `prompt` by filtering out all keys except for role/content per `history` turn
        Reference: https://docs.anthropic.com/claude/reference/complete_post
        """
        return anthropic_history_to_messages(self, history, is_demonstration)

    @retry(
        wait=wait_random_exponential(min=1, max=15),
        reraise=True,
        stop=stop_after_attempt(3),
        retry=retry_if_not_exception_type((CostLimitExceededError, RuntimeError)),
    )
    def query(self, history: list[dict[str, str]]) -> str:
        """
        Query the Anthropic API with the given `history` and return the response.
        """
        return anthropic_query(self, history)


class BedrockModel(BaseModel):
    MODELS = {
        "anthropic.claude-instant-v1": {
            "max_context": 100_000,
            "max_tokens_to_sample": 4096,
            "cost_per_input_token": 8e-07,
            "cost_per_output_token": 2.4e-06,
        },
        "anthropic.claude-v2": {
            "max_context": 100_000,
            "max_tokens_to_sample": 4096,
            "cost_per_input_token": 8e-06,
            "cost_per_output_token": 2.4e-05,
        },
        "anthropic.claude-v2:1": {
            "max_context": 100_000,
            "max_tokens": 4096,
            "cost_per_input_token": 8e-06,
            "cost_per_output_token": 2.4e-05,
        },
        "anthropic.claude-3-opus-20240229-v1:0": {
            "max_context": 200_000,
            "max_tokens": 4096,
            "cost_per_input_token": 1.5e-05,
            "cost_per_output_token": 7.5e-05,
        },
        "anthropic.claude-3-sonnet-20240229-v1:0": {
            "max_context": 200_000,
            "max_tokens": 4096,
            "cost_per_input_token": 3e-06,
            "cost_per_output_token": 1.5e-05,
        },
        "anthropic.claude-3-haiku-20240307-v1:0": {
            "max_context": 200_000,
            "max_tokens": 4096,
            "cost_per_input_token": 2.5e-07,
            "cost_per_output_token": 1.25e-06,
        },
    }

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)

        # Extract provider from model ID
        # https://docs.aws.amazon.com/bedrock/latest/userguide/model-ids.html
        self.model_provider = self.api_model.split(".")[0]
        if self.model_provider == "anthropic":
            # Note: this assumes AWS credentials are already configured.
            # https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html
            self.api = AnthropicBedrock()
        elif self.model_provider in ["ai21", "amazon", "cohere", "meta", "mistral"]:
            msg = f"{self.api_model} is not supported!"
            raise NotImplementedError(msg)
        else:
            msg = f"Provider {self.model_provider} is not supported by Amazon Bedrock!"
            raise ValueError(msg)

    def history_to_messages(
        self,
        history: list[dict[str, str]],
        is_demonstration: bool = False,
    ) -> str | list[dict[str, str]]:
        """
        Create `prompt` from the history of messages
        """
        if self.model_provider == "anthropic":
            return anthropic_history_to_messages(self, history, is_demonstration)
        else:
            msg = f"{self.api_model} is not supported!"
            raise NotImplementedError(msg)

    @retry(
        wait=wait_random_exponential(min=1, max=15),
        reraise=True,
        stop=stop_after_attempt(3),
        retry=retry_if_not_exception_type((CostLimitExceededError, RuntimeError)),
    )
    def query(self, history: list[dict[str, str]]) -> str:
        """
        Query Amazon Bedrock with the given `history` and return the response.
        """
        if self.model_provider == "anthropic":
            return anthropic_query(self, history)
        else:
            msg = f"{self.api_model} is not supported!"
            raise NotImplementedError(msg)


def anthropic_history_to_messages(
    model: AnthropicModel | BedrockModel,
    history: list[dict[str, str]],
    is_demonstration: bool = False,
) -> str | list[dict[str, str]]:
    """
    Create `prompt` by filtering out all keys except for role/content per `history` turn
    Reference: https://docs.anthropic.com/claude/reference/complete_post
    """
    # Preserve behavior for older models
    if model.api_model in ["claude-instant", "claude-2.0"] or (
        isinstance(model, BedrockModel) and model.api_model in ["anthropic.claude-instant-v1", "anthropic.claude-v2"]
    ):
        # Remove system messages if it is a demonstration
        if is_demonstration:
            history = [entry for entry in history if entry["role"] != "system"]
        # Map history to Claude format
        prompt = "\n\n"
        for entry in history:
            if entry["role"] in {"user", "system"}:
                prompt += f'{HUMAN_PROMPT} {entry["content"]}\n\n'
            elif entry["role"] == "assistant":
                prompt += f'{AI_PROMPT} {entry["content"]}\n\n'
        prompt += AI_PROMPT
        return prompt

    # Remove system messages if it is a demonstration
    if is_demonstration:
        history = [entry for entry in history if entry["role"] != "system"]
        return "\n".join([entry["content"] for entry in history])

    # Return history components with just role, content fields (no system message)
    messages = [
        {k: v for k, v in entry.items() if k in ["role", "content"]} for entry in history if entry["role"] != "system"
    ]
    compiled_messages = []  # Combine messages from the same role
    last_role = None
    for message in reversed(messages):
        if last_role == message["role"]:
            compiled_messages[-1]["content"] = message["content"] + "\n" + compiled_messages[-1]["content"]
        else:
            compiled_messages.append(message)
        last_role = message["role"]
    compiled_messages = list(reversed(compiled_messages))
    # Replace any empty content values with a "(No output)"
    for message in compiled_messages:
        if message["content"].strip() == "":
            message["content"] = "(No output)"
    return compiled_messages


def anthropic_query(model: AnthropicModel | BedrockModel, history: list[dict[str, str]]) -> str:
    """
    Query the Anthropic API with the given `history` and return the response.
    """
    # Preserve behavior for older models
    if model.api_model in ["claude-instant", "claude-2.0", "claude-2.1"] or (
        isinstance(model, BedrockModel) and model.api_model in ["anthropic.claude-instant-v1", "anthropic.claude-v2"]
    ):
        # Perform Anthropic API call
        prompt = anthropic_history_to_messages(model, history)
        if isinstance(model, BedrockModel):
            # Use a dummy Anthropic client since count_tokens
            # is not available in AnthropicBedrock
            # https://github.com/anthropics/anthropic-sdk-python/issues/353
            input_tokens = Anthropic().count_tokens(prompt)
        else:
            input_tokens = model.api.count_tokens(prompt)
        completion = model.api.completions.create(
            model=model.api_model,
            prompt=prompt,
            max_tokens_to_sample=model.model_metadata["max_context"] - input_tokens
            if isinstance(model, Anthropic)
            else model.model_metadata["max_tokens_to_sample"],
            temperature=model.args.temperature,
            top_p=model.args.top_p,
        )
        # Calculate + update costs, return response
        response = completion.completion
        if isinstance(model, BedrockModel):
            output_tokens = Anthropic().count_tokens(response)
        else:
            output_tokens = model.api.count_tokens(response)
        model.update_stats(input_tokens, output_tokens)
        return response

    # Get system message(s)
    system_message = "\n".join([entry["content"] for entry in history if entry["role"] == "system"])
    messages = anthropic_history_to_messages(model, history)

    # Perform Anthropic API call
    response = model.api.messages.create(
        messages=messages,
        max_tokens=model.model_metadata["max_tokens"],
        model=model.api_model,
        temperature=model.args.temperature,
        top_p=model.args.top_p,
        system=system_message,
    )

    # Calculate + update costs, return response
    model.update_stats(response.usage.input_tokens, response.usage.output_tokens)
    return "\n".join([x.text for x in response.content])


class OllamaModel(BaseModel):
    MODELS = defaultdict(
        lambda: {
            "max_context": 128_000,
            "cost_per_input_token": 0,
            "cost_per_output_token": 0,
        },
    )

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)
        from ollama import Client

        self.client = Client(host=args.host_url)

    def history_to_messages(
        self,
        history: list[dict[str, str]],
        is_demonstration: bool = False,
    ) -> str | list[dict[str, str]]:
        """
        Create `messages` by filtering out all keys except for role/content per `history` turn
        """
        # Remove system messages if it is a demonstration
        if is_demonstration:
            history = [entry for entry in history if entry["role"] != "system"]
            return "\n".join([entry["content"] for entry in history])
        # Return history components with just role, content fields
        return [{k: v for k, v in entry.items() if k in ["role", "content"]} for entry in history]

    @retry(
        wait=wait_random_exponential(min=1, max=15),
        reraise=True,
        stop=stop_after_attempt(3),
        retry=retry_if_not_exception_type((CostLimitExceededError, RuntimeError)),
    )
    def query(self, history: list[dict[str, str]]) -> str:
        """
        Query the Ollama API with the given `history` and return the response.
        """
        response = self.client.chat(
            model=self.api_model,
            messages=self.history_to_messages(history),
            options={
                "temperature": self.args.temperature,
                "top_p": self.args.top_p,
            },
        )
        # Calculate + update costs, return response
        if "prompt_eval_count" in response:
            input_tokens = response["prompt_eval_count"]
        else:
            logger.warning(
                "Prompt eval count not found in response. Using 0. "
                "This might be because the prompt has been cached. "
                "See https://github.com/princeton-nlp/SWE-agent/issues/44 "
                "and https://github.com/ollama/ollama/issues/3427.",
            )
            input_tokens = 0
        output_tokens = response["eval_count"]
        self.update_stats(input_tokens, output_tokens)
        return response["message"]["content"]


class TogetherModel(BaseModel):
    # Check https://docs.together.ai/docs/inference-models for model names, context
    # Check https://www.together.ai/pricing for pricing
    MODELS = {
        "meta-llama/Llama-2-13b-chat-hf": {
            "max_context": 4096,
            "cost_per_input_token": 2.25e-07,
            "cost_per_output_token": 2.25e-07,
        },
        "meta-llama/Llama-2-70b-chat-hf": {
            "max_context": 4096,
            "cost_per_input_token": 9e-07,
            "cost_per_output_token": 9e-07,
        },
        "mistralai/Mistral-7B-Instruct-v0.2": {
            "max_context": 32768,
            "cost_per_input_token": 2e-07,
            "cost_per_output_token": 2e-07,
        },
        "togethercomputer/RedPajama-INCITE-7B-Chat": {
            "max_context": 2048,
            "cost_per_input_token": 2e-07,
            "cost_per_output_token": 2e-07,
        },
        "mistralai/Mixtral-8x7B-Instruct-v0.1": {
            "max_context": 32768,
            "cost_per_input_token": 6e-07,
            "cost_per_output_token": 6e-07,
        },
    }

    SHORTCUTS = {
        "llama13b": "meta-llama/Llama-2-13b-chat-hf",
        "llama70b": "meta-llama/Llama-2-70b-chat-hf",
        "mistral7b": "mistralai/Mistral-7B-Instruct-v0.2",
        "mixtral8x7b": "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "redpajama7b": "togethercomputer/RedPajama-INCITE-7B-Chat",
    }

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)
        assert together.version >= "1.1.0", "Please upgrade to Together SDK v1.1.0 or later."

        # Set Together key
        together.api_key = keys_config["TOGETHER_API_KEY"]

    def history_to_messages(self, history: list[dict[str, str]], is_demonstration: bool = False) -> str:
        """
        Create `prompt` by filtering out all keys except for role/content per `history` turn
        """
        # Remove system messages if it is a demonstration
        if is_demonstration:
            history = [entry for entry in history if entry["role"] != "system"]
        # Map history to TogetherAI format
        mapping = {"user": "human", "assistant": "bot", "system": "bot"}
        prompt = [f'<{mapping[d["role"]]}>: {d["content"]}' for d in history]
        prompt = "\n".join(prompt)
        return f"{prompt}\n<bot>:"

    @retry(
        wait=wait_random_exponential(min=1, max=15),
        reraise=True,
        stop=stop_after_attempt(3),
        retry=retry_if_not_exception_type((CostLimitExceededError, RuntimeError)),
    )
    def query(self, history: list[dict[str, str]]) -> str:
        """
        Query the Together API with the given `history` and return the response.
        """
        # Perform Together API call
        prompt = self.history_to_messages(history)
        # Anthropic's count_tokens is convenient because it caches and utilizes huggingface/tokenizers, so we will use.
        max_tokens_to_sample = self.model_metadata["max_context"] - Anthropic().count_tokens(prompt)
        completion = together.Complete.create(
            model=self.api_model,
            prompt=prompt,
            max_tokens=max_tokens_to_sample,
            stop=["<human>"],
            temperature=self.args.temperature,
            top_p=self.args.top_p,
        )
        # Calculate + update costs, return response
        response = completion["choices"][0]["text"].split("<human>")[0]
        input_tokens = completion["usage"]["prompt_tokens"]
        output_tokens = completion["usage"]["completion_tokens"]
        self.update_stats(input_tokens, output_tokens)
        return response


class HumanModel(BaseModel):
    MODELS = {"human": {}}

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)

        # Determine which commands require multi-line input
        self.multi_line_command_endings = {
            command.name: command.end_name for command in commands if command.end_name is not None
        }

    def history_to_messages(
        self,
        history: list[dict[str, str]],
        is_demonstration: bool = False,
    ) -> str | list[dict[str, str]]:
        """
        Create `messages` by filtering out all keys except for role/content per `history` turn
        """
        # Remove system messages if it is a demonstration
        if is_demonstration:
            history = [entry for entry in history if entry["role"] != "system"]
            return "\n".join([entry["content"] for entry in history])
        # Return history components with just role, content fields
        return [{k: v for k, v in entry.items() if k in ["role", "content"]} for entry in history]

    def query(self, history: list[dict[str, str]], action_prompt: str = "> ") -> str:
        """
        Logic for handling user input to pass to SWEEnv
        """
        action = input(action_prompt)
        command_name = action.split()[0] if action else ""

        # Special handling for multi-line input actions (i.e. edit)
        if command_name in self.multi_line_command_endings:
            buffer = [action]
            end_keyword = self.multi_line_command_endings[command_name]
            while True:
                action = input("... ")
                buffer.append(action)
                if action.rstrip() == end_keyword:
                    # Continue reading input until terminating keyword inputted
                    break
            action = "\n".join(buffer)
        elif action.strip() == "start_multiline_command":  # do arbitrary multi-line input
            buffer = []
            while True:
                action = input("... ")
                if action.rstrip() == "end_multiline_command":
                    break
                buffer.append(action)
            action = "\n".join(buffer)
        return action


class HumanThoughtModel(HumanModel):
    MODELS = {"human_thought": {}}

    def query(self, history: list[dict[str, str]]) -> str:
        """
        Logic for handling user input (both thought + action) to pass to SWEEnv
        """
        thought_all = ""
        thought = input("Thought (end w/ END_THOUGHT): ")
        while True:
            if "END_THOUGHT" in thought:
                thought = thought.split("END_THOUGHT")[0]
                thought_all += thought
                break
            thought_all += thought
            thought = input("... ")

        action = super().query(history, action_prompt="Action: ")

        return f"{thought_all}\n```\n{action}\n```"


class ReplayModel(BaseModel):
    MODELS = {"replay": {}}

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)

        if self.args.replay_path is None or not os.path.exists(self.args.replay_path):
            msg = "--replay_path must point to a file that exists to run a replay policy"
            raise ValueError(msg)

        self.replays = [
            list(json.loads(x).values())[0] for x in Path(self.args.replay_path).read_text().splitlines(keepends=True)
        ]
        self.replay_idx = 0
        self.action_idx = 0

    def _next_replay(self) -> None:
        """Called after last action"""
        self.replay_idx += 1
        self.action_idx = 0

    def query(self, history: list[dict[str, str]]) -> str:
        """
        Logic for tracking which replay action to pass to SWEEnv
        """
        actions = self.replays[self.replay_idx]
        try:
            action = actions[self.action_idx]
        except IndexError:
            msg = (
                "This seems to be an incomplete trajectory. "
                "We reached the end of it, but `submit` was not called. "
                "Calling it now."
            )
            logger.warning(msg)
            action = "```\nsubmit\n```"

        self.action_idx += 1

        # Assuming `submit` is always last action of replay trajectory
        if action == "submit":
            self._next_replay()

        return action


class InstantEmptySubmitTestModel(BaseModel):
    MODELS = {"instant_empty_submit": {}}

    def __init__(self, args: ModelArguments, commands: list[Command]):
        """This model immediately submits. Useful for testing purposes"""
        super().__init__(args, commands)
        self._action_idx = 0

    def query(self, history: list[dict[str, str]]) -> str:
        # Need to at least do _something_ to submit
        if self._action_idx == 0:
            self._action_idx = 1
            action = "DISCUSSION\nLet's reproduce the bug by creating a `reproduce.py` file.\n\n```\ncreate reproduce.py\n```\n"
        elif self._action_idx == 1:
            self._action_idx = 0
            action = "DISCUSSION\nThe task should be resolved, so let's submit the patch.\n\n```\nsubmit\n```\n"
        return action


def get_model(args: ModelArguments, commands: list[Command] | None = None):
    """
    Returns correct model object given arguments and commands
    """
    if commands is None:
        commands = []
    if args.model_name == "instant_empty_submit":
        return InstantEmptySubmitTestModel(args, commands)
    if args.model_name == "human":
        return HumanModel(args, commands)
    if args.model_name == "human_thought":
        return HumanThoughtModel(args, commands)
    if args.model_name == "replay":
        return ReplayModel(args, commands)
    elif (
        args.model_name.startswith("gpt")
        or args.model_name.startswith("ft:gpt")
        or args.model_name.startswith("azure:gpt")
    ):
        return OpenAIModel(args, commands)
    elif args.model_name in OpenAIModel.MODELS or args.model_name in OpenAIModel.SHORTCUTS:
        # OpenAI-compatible providers (e.g. DashScope/Qwen) can use arbitrary model names
        # as long as they're registered in OpenAIModel.MODELS.
        return OpenAIModel(args, commands)
    elif args.model_name.startswith("claude"):
        return AnthropicModel(args, commands)
    elif args.model_name.startswith("bedrock"):
        return BedrockModel(args, commands)
    elif args.model_name.startswith("ollama"):
        return OllamaModel(args, commands)
    elif args.model_name in TogetherModel.SHORTCUTS:
        return TogetherModel(args, commands)
    elif args.model_name == "instant_empty_submit":
        return InstantEmptySubmitTestModel(args, commands)
    else:
        msg = f"Invalid model name: {args.model_name}"
        raise ValueError(msg)
