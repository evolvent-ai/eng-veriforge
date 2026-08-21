#!/usr/bin/env python3
"""Trusted direct adapter for OpenAI Chat Completions provider runs."""
from __future__ import annotations

import json
import os
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


def fixture_bundle(root: Path) -> str:
    sections = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        sections.append(f"\n===== fixtures/{relative} =====\n{path.read_text(encoding='utf-8')}")
    return "".join(sections)


def build_prompt(task_spec: Path, fixtures: Path) -> str:
    contract = task_spec.read_text(encoding="utf-8")
    files = ", ".join(OUTPUT_FILES)
    return f"""You are the Agent under evaluation. Treat the task contract and fixtures below as authoritative.
Process all 28 events chronologically and obey every authorization, safety, evidence, and output constraint.

Return only one valid JSON object with a top-level key named \"files\". The value of \"files\" must contain exactly these keys: {files}.
The value for garden_workflow_report.json must be a JSON object matching the contract. Each Markdown value must be a string. Do not use Markdown fences around the response and do not add explanatory text outside the JSON object.

===== task.yaml =====
{contract}

===== fixture bundle =====
{fixture_bundle(fixtures)}
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


def request_payload(adapter: str, model: str, prompt: str, native_parameters: dict[str, Any]) -> dict[str, Any]:
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
        "messages": [{"role": "user", "content": prompt}],
    }
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


def call_provider(prompt: str, credential: str) -> str:
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
    def request_once(parameters: dict[str, Any]) -> dict[str, Any]:
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
    prompt = build_prompt(task_spec, fixtures)
    files = None
    last_error: Exception | None = None
    for attempt in range(1, MAX_PROVIDER_ATTEMPTS + 1):
        try:
            files = parse_output(call_provider(prompt, credential))
            # Validate and stage the complete set before declaring the attempt
            # successful. This makes malformed JSON-valued file fields retryable
            # instead of failing after the provider loop has already ended.
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
