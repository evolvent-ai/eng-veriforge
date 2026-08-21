#!/usr/bin/env python3
"""Trusted direct adapter for OpenAI Chat Completions provider runs."""
from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any


OUTPUT_FILES = [
    "garden_workflow_report.json",
    "stage_progress.md",
    "service_consistency_matrix.md",
    "risk_log.md",
    "calendar_change_log.md",
    "auth_log.md",
    "venue_weather_log.md",
    "data_quality_log.md",
    "final_review.md",
]
TRACE_VERSION = "veriforge-trace/v1"
MAX_TOOL_ROUNDS = 32
SECRET_PATTERN = re.compile(r"(?i)(api[_ -]?key|password|secret|cookie|access[_ -]?token)\s*[:=]\s*[^\s,;]+")
MAX_PROVIDER_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2, 5)


class ProviderRequestError(RuntimeError):
    """Provider failure annotated with whether retrying can help."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


def redact(text: str, credential: str) -> str:
    text = text.replace(credential, "<redacted>")
    return SECRET_PATTERN.sub(lambda match: match.group(1) + "=<redacted>", text).strip()[-2000:]


def trace_paths() -> tuple[Path, Path]:
    root = Path(os.environ.get("VERIFORGE_TRACE_DIR", Path(os.environ.get("VERIFORGE_ROLL_DIR", ".")) / "trace"))
    root.mkdir(parents=True, exist_ok=True)
    return root, root / "events.jsonl"


def trace_event(event_path: Path, event: dict[str, Any], credential: str) -> None:
    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(credential, "<redacted>")
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value
    safe = scrub(event)
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False) + "\n")


def write_trace_index(*, mode: str, streaming: bool, tool_calls: bool, raw_response: bool) -> Path:
    root, event_path = trace_paths()
    index = root / "index.json"
    index.write_text(
        json.dumps(
            {
                "trace_version": TRACE_VERSION,
                "mode": mode,
                "streaming": streaming,
                "tool_calls_available": tool_calls,
                "normalized_events": True,
                "events": str(event_path),
                "raw_response_available": raw_response,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return index


def output_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_fixtures",
                "description": "List the authoritative fixture files available to this benchmark.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_fixture",
                "description": "Read one fixture file by its relative path under the fixture root.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_output",
                "description": "Write one required benchmark output file under outputs. Never write elsewhere.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": OUTPUT_FILES},
                        "content": {"type": "string"},
                    },
                    "required": ["name", "content"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def execute_tool(name: str, arguments: dict[str, Any], fixtures: Path, output_dir: Path) -> str:
    if name == "list_fixtures":
        return json.dumps(sorted(path.relative_to(fixtures).as_posix() for path in fixtures.rglob("*") if path.is_file()), ensure_ascii=False)
    if name == "read_fixture":
        relative = Path(str(arguments.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("fixture path must stay under the fixture root")
        path = (fixtures / relative).resolve()
        if fixtures.resolve() not in path.parents or not path.is_file():
            raise ValueError("fixture path is not available")
        return path.read_text(encoding="utf-8")
    if name == "write_output":
        filename = str(arguments.get("name", ""))
        if filename not in OUTPUT_FILES:
            raise ValueError("output filename is not allowlisted")
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("output content must be a string")
        path = output_dir / filename
        if filename.endswith(".json"):
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("JSON output must be an object")
            path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            path.write_text(content.rstrip() + "\n", encoding="utf-8")
        return json.dumps({"written": filename}, ensure_ascii=False)
    raise ValueError(f"unknown tool: {name}")


def fixture_bundle(root: Path) -> str:
    sections = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        sections.append(f"\n===== fixtures/{relative} =====\n{path.read_text(encoding='utf-8')}")
    return "".join(sections)


def build_prompt(task_spec: Path, fixtures: Path) -> str:
    contract = task_spec.read_text(encoding="utf-8")
    files = ", ".join(OUTPUT_FILES)
    tool_loop = os.environ.get("VERIFORGE_TRACE_MODE") == "chat_tool_loop"
    if tool_loop:
        output_instruction = (
            "Use the provided list_fixtures, read_fixture, and write_output tools. "
            "Read the staged task contract and authoritative fixtures through those tools, "
            "then write every required output file under outputs. Do not return a files envelope; "
            "after all files are written, return a short completion message."
        )
        fixture_section = "===== fixture bundle =====\nFixtures are available only through the read_fixture tool."
    else:
        output_instruction = (
            f"Return only one valid JSON object with a top-level key named \\\"files\\\". The value of \\\"files\\\" must contain exactly these keys: {files}."
        )
        fixture_section = f"===== fixture bundle =====\n{fixture_bundle(fixtures)}"
    return f"""You are the Agent under evaluation. Treat the task contract and fixtures below as authoritative.
Process all 28 events chronologically and obey every authorization, safety, evidence, and output constraint.

{output_instruction}
The value for garden_workflow_report.json must be a JSON object matching the contract. Each Markdown value must be a string. Do not use Markdown fences around the response and do not add explanatory text outside the JSON object.

===== task.yaml =====
{contract}

{fixture_section}
"""


def api_url(adapter: str, base_url: str, endpoint: str) -> str:
    base = base_url.rstrip("/")
    if adapter == "anthropic_messages":
        if endpoint != "messages":
            raise ValueError(f"adapter/endpoint mismatch: {adapter} vs {endpoint}")
        return base + "/v1/messages"
    if adapter == "openai_chat":
        if endpoint != "chat_completions":
            raise ValueError(f"adapter/endpoint mismatch: {adapter} vs {endpoint}")
        return base + "/chat/completions"
    if adapter == "openai_responses":
        if endpoint != "responses":
            raise ValueError(f"adapter/endpoint mismatch: {adapter} vs {endpoint}")
        return base + "/responses"
    raise ValueError(f"unsupported adapter: {adapter}")


def request_payload(
    adapter: str,
    model: str,
    prompt: str,
    native_parameters: dict[str, Any],
    *,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if adapter == "anthropic_messages":
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        payload.update(native_parameters)
        return payload
    if adapter == "openai_responses":
        payload: dict[str, Any] = {"model": model, "input": prompt}
        payload.update(native_parameters)
        return payload
    payload = {
        "model": model,
        "messages": messages or [{"role": "user", "content": prompt}],
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    payload.update(native_parameters)
    return payload


def response_text(adapter: str, response: dict[str, Any]) -> str:
    if adapter == "anthropic_messages":
        text = "".join(item.get("text", "") for item in response.get("content", []) if isinstance(item, dict))
        if text:
            return text
        raise ProviderRequestError("provider response has no text content", retryable=True)
    if adapter == "openai_chat":
        try:
            message = response["choices"][0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderRequestError(
                "provider response is missing choices[0].message.content",
                retryable=True,
            ) from exc
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = [item.get("text", "") for item in content if isinstance(item, dict)]
            if all(isinstance(chunk, str) for chunk in chunks):
                return "".join(chunks)
        raise ProviderRequestError("provider response content is not text", retryable=True)
    if isinstance(response.get("output_text"), str) and response["output_text"]:
        return response["output_text"]
    chunks = []
    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    text = "".join(chunks)
    if text:
        return text
    raise ProviderRequestError("provider response has no output text", retryable=True)


def chat_message_content(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


def run_chat_tool_loop(
    prompt: str,
    credential: str,
    model: str,
    url: str,
    native_parameters: dict[str, Any],
    fixtures: Path,
    output_dir: Path,
) -> str:
    """Run a bounded OpenAI-compatible tool loop with filesystem-safe tools."""
    _trace_root, trace_file = trace_paths()
    write_trace_index(mode="chat_tool_loop", streaming=False, tool_calls=True, raw_response=False)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt + "\nUse the supplied tools to read fixtures and write every required output file. When all files are written, return a short completion message."}]
    tools = output_tool_definitions()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {credential}"}
    for round_index in range(1, MAX_TOOL_ROUNDS + 1):
        payload = request_payload("openai_chat", model, "", native_parameters, messages=messages, tools=tools)
        payload.pop("response_format", None)
        trace_event(trace_file, {"type": "provider_request", "round": round_index, "mode": "chat_tool_loop", "model": model, "url": url, "prompt_sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()}, credential)
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=1800) as response:
                raw = response.read().decode("utf-8")
                response_payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            diagnostic = exc.read().decode("utf-8", errors="replace")
            trace_event(trace_file, {"type": "provider_error", "round": round_index, "status": exc.code, "diagnostic": redact(diagnostic, credential)}, credential)
            raise ProviderRequestError(f"provider HTTP {exc.code}: {redact(diagnostic, credential)}", retryable=exc.code in {408, 409, 425, 429, 500, 502, 503, 504}) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as exc:
            trace_event(trace_file, {"type": "provider_error", "round": round_index, "diagnostic": redact(str(exc), credential)}, credential)
            raise ProviderRequestError(f"provider tool-loop request failed: {redact(str(exc), credential)}", retryable=True) from exc
        if not isinstance(response_payload, dict):
            raise ProviderRequestError("provider tool-loop response must be a JSON object", retryable=True)
        choices = response_payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderRequestError("provider tool-loop response is missing choices", retryable=True)
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ProviderRequestError("provider tool-loop response is missing message", retryable=True)
        tool_calls = message.get("tool_calls")
        trace_event(trace_file, {"type": "provider_response", "round": round_index, "finish_reason": choices[0].get("finish_reason"), "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0, "content_sha256": hashlib.sha256(chat_message_content(message).encode()).hexdigest()}, credential)
        if not isinstance(tool_calls, list) or not tool_calls:
            return chat_message_content(message)
        messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": tool_calls})
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            name = str(function.get("name", ""))
            trace_event(trace_file, {"type": "tool_call", "round": round_index, "tool": name, "call_id": tool_call.get("id")}, credential)
            try:
                arguments = json.loads(function.get("arguments", "{}"))
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                result = execute_tool(name, arguments, fixtures, output_dir)
                trace_event(trace_file, {"type": "tool_result", "round": round_index, "tool": name, "ok": True, "result_sha256": hashlib.sha256(result.encode()).hexdigest()}, credential)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                result = json.dumps({"error": redact(str(exc), credential)}, ensure_ascii=False)
                trace_event(trace_file, {"type": "tool_result", "round": round_index, "tool": name, "ok": False, "error": redact(str(exc), credential)}, credential)
            messages.append({"role": "tool", "tool_call_id": tool_call.get("id", ""), "content": result})
    raise ProviderRequestError(f"provider tool loop exceeded {MAX_TOOL_ROUNDS} rounds", retryable=False)


def call_provider(prompt: str, credential: str, fixtures: Path, output_dir: Path) -> str:
    adapter = required_env("VERIFORGE_ADAPTER")
    provider = required_env("VERIFORGE_PROVIDER")
    model = required_env("VERIFORGE_MODEL_NAME")
    base_url = required_env("VERIFORGE_BASE_URL")
    endpoint = required_env("VERIFORGE_ENDPOINT")
    native_parameters = json.loads(required_env("VERIFORGE_NATIVE_PARAMETERS_JSON"))
    headers = {"Content-Type": "application/json"}
    if adapter == "anthropic_messages":
        headers.update({"x-api-key": credential, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {credential}"
    url = api_url(adapter, base_url, endpoint)
    trace_mode = os.environ.get("VERIFORGE_TRACE_MODE", "chat_single_turn")
    if adapter == "openai_chat" and trace_mode == "chat_tool_loop":
        return run_chat_tool_loop(prompt, credential, model, url, native_parameters, fixtures, output_dir)
    _trace_root, trace_file = trace_paths()
    def request_once(parameters: dict[str, Any]) -> dict[str, Any]:
        trace_event(
            trace_file,
            {
                "type": "provider_request",
                "mode": trace_mode,
                "model": model,
                "url": url,
                "parameters": {key: value for key, value in parameters.items() if key != "response_format"},
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            },
            credential,
        )
        body = json.dumps(request_payload(adapter, model, prompt, parameters)).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=1800) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            diagnostic = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in {408, 409, 425, 429, 500, 502, 503, 504}
            raise ProviderRequestError(
                f"provider HTTP {exc.code}: {redact(diagnostic, credential)}",
                retryable=retryable,
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise ProviderRequestError(
                f"provider request failed: {redact(str(exc), credential)}",
                retryable=True,
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderRequestError(
                f"provider returned invalid JSON: {redact(raw, credential)}",
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError("provider response must be a JSON object", retryable=True)
        trace_event(
            trace_file,
            {
                "type": "provider_response",
                "mode": trace_mode,
                "model": model,
                "response_id": payload.get("id"),
                "finish_reason": (payload.get("choices") or [{}])[0].get("finish_reason") if isinstance(payload.get("choices"), list) and payload.get("choices") else payload.get("status"),
                "usage": payload.get("usage"),
                "content_sha256": hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest(),
            },
            credential,
        )
        return payload

    print(f"[veriforge] provider request: POST {url} (model={model})", file=sys.stderr, flush=True)
    try:
        payload = request_once(native_parameters)
    except ProviderRequestError as exc:
        # Some OpenAI-compatible gateways reject JSON mode even though they
        # implement the rest of Chat Completions. The prompt and parser still
        # enforce the envelope, so retry once without the optional parameter.
        message = str(exc).casefold()
        json_mode_rejected = (
            adapter == "openai_chat"
            and "http 400" in message
            and any(term in message for term in ("response_format", "json_object", "json mode", "structured"))
        )
        if not json_mode_rejected or "response_format" not in native_parameters:
            raise
        fallback = dict(native_parameters)
        fallback.pop("response_format", None)
        print(
            "[veriforge] provider rejected response_format=json_object; retrying without JSON mode",
            file=sys.stderr,
            flush=True,
        )
        payload = request_once(fallback)
    text = response_text(adapter, payload)
    if not text.strip():
        raise RuntimeError("provider returned no text output")
    print("[veriforge] provider response received; validating output files", file=sys.stderr, flush=True)
    return text


def parse_output(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model output does not contain a JSON object")
        payload = json.loads(candidate[start:end + 1])
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        raise ValueError("model output must contain a files object")
    if set(files) != set(OUTPUT_FILES):
        missing = sorted(set(OUTPUT_FILES) - set(files))
        extra = sorted(set(files) - set(OUTPUT_FILES))
        raise ValueError(f"model output file set mismatch; missing={missing}, extra={extra}")
    return files


def write_outputs(files: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in OUTPUT_FILES:
        value = files[name]
        path = output_dir / name
        if name.endswith(".json"):
            if isinstance(value, str):
                value = json.loads(value)
            if not isinstance(value, dict):
                raise ValueError(f"{name} must be a JSON object")
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            path.write_text(value.rstrip() + "\n", encoding="utf-8")
        if os.environ.get("VERIFORGE_TRACE_MODE") == "chat_tool_loop":
            _trace_root, event_path = trace_paths()
            trace_event(event_path, {"type": "file_write", "name": name, "path": str(path)}, os.environ.get("VERIFORGE_API_KEY", ""))


def main() -> int:
    if "--self-test" in sys.argv:
        sample = {"files": {name: ({"ok": True} if name.endswith(".json") else "# test") for name in OUTPUT_FILES}}
        with Path(os.environ.get("TMPDIR", "/tmp")).joinpath("veriforge-provider-self-test.json").open("w", encoding="utf-8") as handle:
            json.dump(sample, handle)
        parse_output(json.dumps(sample))
        print("provider adapter self-test: ready")
        return 0
    task_spec = Path(required_env("VERIFORGE_TASK_SPEC"))
    fixtures = Path(required_env("VERIFORGE_FIXTURES_DIR"))
    output_dir = Path(required_env("VERIFORGE_OUTPUT_DIR"))
    credential = required_env("VERIFORGE_API_KEY")
    trace_root, trace_file = trace_paths()
    write_trace_index(
        mode=os.environ.get("VERIFORGE_TRACE_MODE", "chat_single_turn"),
        streaming=False,
        tool_calls=os.environ.get("VERIFORGE_TRACE_MODE") == "chat_tool_loop",
        raw_response=False,
    )
    prompt = build_prompt(task_spec, fixtures)
    files = None
    last_error: Exception | None = None
    for attempt in range(1, MAX_PROVIDER_ATTEMPTS + 1):
        try:
            response = call_provider(prompt, credential, fixtures, output_dir)
            if os.environ.get("VERIFORGE_TRACE_MODE") == "chat_tool_loop":
                if not all((output_dir / name).is_file() for name in OUTPUT_FILES):
                    raise ValueError("chat tool loop finished without writing every required output")
                files = {name: True for name in OUTPUT_FILES}
            else:
                files = parse_output(response)
            # Validate and stage the complete set before declaring the attempt
            # successful. This makes malformed JSON-valued file fields retryable
            # instead of failing after the provider loop has already ended.
            if os.environ.get("VERIFORGE_TRACE_MODE") != "chat_tool_loop" and files and all(value is not None for value in files.values()):
                write_outputs(files, output_dir)
            break
        except ProviderRequestError as exc:
            last_error = exc
            if not exc.retryable or attempt == MAX_PROVIDER_ATTEMPTS:
                raise
            print(
                f"[veriforge] transient provider failure; retry {attempt + 1}/{MAX_PROVIDER_ATTEMPTS} "
                f"after {RETRY_BACKOFF_SECONDS[attempt - 1]}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == MAX_PROVIDER_ATTEMPTS:
                raise
            print(
                f"[veriforge] provider returned invalid output; retry {attempt + 1}/{MAX_PROVIDER_ATTEMPTS} "
                f"after {RETRY_BACKOFF_SECONDS[attempt - 1]}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
    if files is None:
        raise last_error or RuntimeError("provider returned no output")
    if files is None:
        raise last_error or RuntimeError("provider returned no output")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"provider adapter error: {exc}", file=sys.stderr)
        raise SystemExit(2)
