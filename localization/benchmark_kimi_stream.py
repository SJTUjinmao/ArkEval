from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

import requests


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def iter_sse_events(response: requests.Response):
    lines: list[bytes] = []
    for line in response.iter_lines(chunk_size=4096, decode_unicode=False):
        if line:
            lines.append(line)
        elif lines:
            yield lines
            lines = []
    if lines:
        yield lines


def event_data(lines: list[bytes]) -> bytes:
    data_lines: list[bytes] = []
    for line in lines:
        if line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip())
        elif line.lstrip().startswith(b"{"):
            data_lines.append(line.strip())
    return b"\n".join(data_lines).strip()


def localization_prompt(row_dir: Path) -> tuple[str, int]:
    from localization_engine.locate_flow import _LLM_PREVIEW_MAX_CHARS, _read_preview

    row = int(row_dir.name.rsplit("_", 1)[-1])
    result_path = row_dir.parent.parent / "localization_results.jsonl"
    result = next(
        json.loads(line)
        for line in result_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and int(json.loads(line)["row"]) == row
    )
    candidates = [
        json.loads(line)["file_path"]
        for line in (row_dir / "embedding_candidates.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    total_chars = 0
    previews: dict[str, str] = {}
    for path in candidates:
        if total_chars >= _LLM_PREVIEW_MAX_CHARS:
            previews[path] = "(内容过长已省略)"
            continue
        preview = _read_preview(path)
        previews[path] = preview
        total_chars += len(preview)
    per_file_cap = max(800, _LLM_PREVIEW_MAX_CHARS // max(1, len(candidates)))
    blocks = []
    for index, path in enumerate(candidates, 1):
        preview = previews[path]
        if len(preview) > per_file_cap:
            preview = preview[:per_file_cap] + "\n...(已截断)"
        blocks.append(f"文件 {index}: {path}\n```\n{preview}\n```")
    prompt = (
        "用户需求：\n"
        f"{result['query']}\n\n"
        "以下是语义检索得到的候选文件（路径 + 内容预览）。请判断其中哪些文件**需要被修改**才能满足上述需求。\n"
        "只输出一个 JSON 数组，元素为需要修改的文件的**完整路径**，与上面列出的完全一致。必须至少选择一个最可能需要修改的文件，禁止输出空数组。不要输出其他解释。\n\n"
        + "\n\n".join(blocks)
    )
    return prompt, row


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure raw Kimi SSE latency and token throughput.")
    parser.add_argument("--reasoning-effort", choices=("omit", "none", "minimal", "low"), default="low")
    parser.add_argument("--enable-thinking", choices=("omit", "true", "false"), default="omit")
    parser.add_argument("--max-completion-tokens", type=int, default=2048)
    parser.add_argument("--read-timeout", type=float, default=600.0)
    parser.add_argument("--localization-row-dir", type=Path)
    args = parser.parse_args()

    load_env(Path(__file__).resolve().parents[1] / ".env")
    base_url = os.environ["OPENAI_API_BASE_URL"].rstrip("/")
    api_key = os.environ["OPENAI_API_KEY"]
    model = os.environ.get("MODEL", "kimi-k2.7-code")
    url = f"{base_url}/chat/completions"
    host = requests.utils.urlparse(base_url).hostname or ""

    prompt = "Output integers 1 through 200 separated by one space. Output nothing else."
    benchmark_row = None
    if args.localization_row_dir:
        prompt, benchmark_row = localization_prompt(args.localization_row_dir.resolve())

    payload: dict[str, object] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "max_completion_tokens": args.max_completion_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if args.reasoning_effort != "omit":
        payload["reasoning_effort"] = args.reasoning_effort
    if args.enable_thinking != "omit":
        payload["enable_thinking"] = args.enable_thinking == "true"

    started = time.perf_counter()
    first_event: float | None = None
    first_reasoning: float | None = None
    first_content: float | None = None
    reasoning_parts: list[str] = []
    content_parts: list[str] = []
    usage: dict[str, object] = {}
    finish_reason = ""
    event_count = 0

    print(
        json.dumps(
            {
                "model": model,
                "reasoning_effort": args.reasoning_effort,
                "enable_thinking": args.enable_thinking,
                "endpoint_host": host,
                "resolved_ip": socket.gethostbyname(host),
                "read_timeout_seconds": args.read_timeout,
                "benchmark_row": benchmark_row,
                "prompt_chars": len(prompt),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    try:
        with requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json=payload,
            stream=True,
            timeout=(30.0, args.read_timeout),
            proxies={"http": None, "https": None},
        ) as response:
            headers_at = time.perf_counter()
            if response.status_code >= 400:
                print(
                    json.dumps(
                        {
                            "http_status": response.status_code,
                            "headers_seconds": round(headers_at - started, 3),
                            "error": response.text[:1000],
                        },
                        ensure_ascii=False,
                    )
                )
                return 1

            for lines in iter_sse_events(response):
                now = time.perf_counter()
                raw = event_data(lines)
                if not raw:
                    continue
                if first_event is None:
                    first_event = now
                if raw == b"[DONE]":
                    break
                event_count += 1
                data = json.loads(raw.decode("utf-8"))
                if isinstance(data.get("usage"), dict):
                    usage = data["usage"]
                choices = data.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                content = delta.get("content") or ""
                if reasoning:
                    if first_reasoning is None:
                        first_reasoning = now
                    reasoning_parts.append(str(reasoning))
                if content:
                    if first_content is None:
                        first_content = now
                    content_parts.append(str(content))
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                },
                ensure_ascii=False,
            )
        )
        return 1

    ended = time.perf_counter()
    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    completion_tokens = usage.get("completion_tokens")
    generation_seconds = ended - (first_event or started)
    tokens_per_second = None
    if isinstance(completion_tokens, (int, float)) and generation_seconds > 0:
        tokens_per_second = round(float(completion_tokens) / generation_seconds, 3)

    print(
        json.dumps(
            {
                "http_status": 200,
                "headers_seconds": round(headers_at - started, 3),
                "first_event_seconds": round(first_event - started, 3) if first_event else None,
                "first_reasoning_seconds": round(first_reasoning - started, 3) if first_reasoning else None,
                "first_content_seconds": round(first_content - started, 3) if first_content else None,
                "total_seconds": round(ended - started, 3),
                "event_count": event_count,
                "reasoning_chars": len(reasoning),
                "content_chars": len(content),
                "finish_reason": finish_reason,
                "usage": usage,
                "completion_tokens_per_second": tokens_per_second,
                "content_preview": content[:300],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
