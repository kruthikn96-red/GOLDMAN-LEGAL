"""Provider-agnostic LLM client for structured rule extraction.

Four backends:
  - Anthropic Claude via tool-use (forced tool_choice on a single tool whose
    input_schema is the extraction payload schema).
  - OpenAI via Structured Outputs (response_format with a Pydantic model).
  - Google Gemini via structured JSON output.
  - Ollama for local models via JSON-mode chat completion.

All expose `extract(chunk, schema_dict)` returning a parsed dict.

The abstraction is intentionally thin — all providers natively support
"return JSON matching this schema". Going beyond two providers, or adding
streaming/batching/async, would warrant a heavier framework like LiteLLM.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from prompts import render_few_shot_messages, render_system_prompt, render_user_message

PLACEHOLDER_API_KEYS = {
    "",
    "your_google_ai_studio_key_here",
    "your_actual_google_ai_studio_key",
    "your_anthropic_key_here",
    "your_openai_key_here",
}


@dataclass
class LLMResponse:
    payload: dict       # parsed JSON the model returned (matches extraction_payload_schema)
    model: str          # model id used
    provider: str       # "anthropic" | "openai" | "google" | "ollama"


class LLMClient(Protocol):
    def extract(
        self, chunk: dict, schema: dict, allowed_attrs: list[str] | None = None
    ) -> LLMResponse: ...


def _require_api_key(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value in PLACEHOLDER_API_KEYS:
        raise RuntimeError(
            f"{name} is not set. Add your real key to .env as {name}=..."
        )
    return value


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class AnthropicClient:
    def __init__(self, model: str = "claude-sonnet-4-5", max_tokens: int = 2048):
        # Lazy import so OpenAI-only users don't need anthropic installed.
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens

    def extract(
        self, chunk: dict, schema: dict, allowed_attrs: list[str] | None = None
    ) -> LLMResponse:
        schema = constrain_attrs(schema, allowed_attrs or [])
        system_prompt = render_system_prompt(allowed_attrs or [])
        tool = {
            "name": "emit_rules",
            "description": (
                "Emit the structured rules extracted from the given subsection. "
                "Always call this tool exactly once with the full payload."
            ),
            "input_schema": schema,
        }
        messages = render_few_shot_messages() + [
            {"role": "user", "content": render_user_message(chunk)}
        ]
        # Prompt cache the system prompt + tool schema (largest stable prefix).
        system = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_rules"},
            messages=messages,
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "emit_rules":
                return LLMResponse(payload=block.input, model=self.model, provider="anthropic")
        raise RuntimeError(f"Anthropic response missing tool_use block: {resp}")


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


class OpenAIClient:
    def __init__(self, model: str = "gpt-4o-2024-08-06", max_tokens: int = 2048):
        import openai

        self._openai = openai
        self._client = openai.OpenAI()
        self.model = model
        self.max_tokens = max_tokens

    def extract(
        self, chunk: dict, schema: dict, allowed_attrs: list[str] | None = None
    ) -> LLMResponse:
        system_prompt = render_system_prompt(allowed_attrs or [])
        messages = (
            [{"role": "system", "content": system_prompt}]
            + render_few_shot_messages()
            + [{"role": "user", "content": render_user_message(chunk)}]
        )
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=messages,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        return LLMResponse(payload=json.loads(content), model=self.model, provider="openai")


def _make_openai_strict(schema: dict) -> dict:
    """Walk a JSON Schema and adapt it for OpenAI strict mode.

    OpenAI strict mode requires:
      - additionalProperties: false on every object,
      - every property listed in `required`,
      - no `default` values.
    Pydantic's Optional[X] = None already produces `anyOf: [..., {"type":"null"}]`,
    so making everything required works as long as we strip defaults.
    """
    import copy

    schema = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if "properties" in node and isinstance(node["properties"], dict):
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


def constrain_attrs(schema: dict, allowed_attrs: list[str]) -> dict:
    """Walk schema; on any property named attr, set its enum."""
    import copy

    schema = copy.deepcopy(schema)
    if not allowed_attrs:
        return schema

    def walk(node):
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict) and "attr" in properties:
                attr_schema = properties["attr"]
                if isinstance(attr_schema, dict):
                    attr_schema["enum"] = allowed_attrs
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


# ---------------------------------------------------------------------------
# Ollama / local
# ---------------------------------------------------------------------------


class OllamaClient:
    def __init__(
        self,
        model: str = "gemma3:4b",
        max_tokens: int = 2048,
        base_url: str | None = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")

    def extract(
        self, chunk: dict, schema: dict, allowed_attrs: list[str] | None = None
    ) -> LLMResponse:
        system_prompt = render_system_prompt(allowed_attrs or [])
        messages = (
            [{"role": "system", "content": system_prompt}]
            + render_few_shot_messages()
            + [{"role": "user", "content": render_user_message(chunk)}]
        )
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_predict": self.max_tokens,
            },
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Could not reach Ollama at {self.base_url}. Start it with `ollama serve`."
            ) from e

        content = data.get("message", {}).get("content", "")
        return LLMResponse(
            payload=json.loads(content),
            model=self.model,
            provider="ollama",
        )


# ---------------------------------------------------------------------------
# Gemini / Google
# ---------------------------------------------------------------------------


class GeminiClient:
    def __init__(self, model: str = "gemini-2.0-flash", max_tokens: int = 2048):
        # Lazy import so non-Gemini users don't need google-genai installed.
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self.model = model
        self.max_tokens = max_tokens

    def extract(
        self, chunk: dict, schema: dict, allowed_attrs: list[str] | None = None
    ) -> LLMResponse:
        schema = constrain_attrs(schema, allowed_attrs or [])
        system_prompt = render_system_prompt(allowed_attrs or [])
        messages = render_few_shot_messages() + [
            {"role": "user", "content": render_user_message(chunk)}
        ]
        contents = []
        for message in messages:
            role = "model" if message["role"] == "assistant" else message["role"]
            contents.append(
                self._types.Content(
                    role=role,
                    parts=[self._types.Part.from_text(text=message["content"])],
                )
            )

        resp = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=self._types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=self.max_tokens,
                response_mime_type="application/json",
            ),
        )
        return LLMResponse(payload=json.loads(resp.text), model=self.model, provider="google")


def _make_gemini_schema(schema: dict) -> dict:
    """Return a Gemini-compatible envelope schema.

    The executable v2 Pydantic schema is recursive because expressions can nest
    through logical `args`. Gemini's response_schema endpoint rejects the full
    Pydantic dialect and recursive refs, so Gemini gets a loose JSON envelope;
    strict validation still happens immediately afterward with `Rule`.
    """
    return {
        "type": "object",
        "properties": {
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rule_id": {"type": "string"},
                        "section": {"type": "string"},
                        "subsection": {"type": "string"},
                        "source_text": {"type": "string"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "unmapped_attributes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "body": {"type": "object"},
                    },
                    "required": [
                        "rule_id",
                        "section",
                        "subsection",
                        "source_text",
                        "confidence",
                        "unmapped_attributes",
                        "body",
                    ],
                },
            }
        },
        "required": ["rules"],
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_client(provider: str | None = None, model: str | None = None) -> LLMClient:
    provider = provider or os.environ.get("LLM_PROVIDER", "anthropic")
    provider = provider.lower()
    if provider == "anthropic":
        _require_api_key("ANTHROPIC_API_KEY")
        return AnthropicClient(model=model or "claude-sonnet-4-5")
    if provider == "openai":
        _require_api_key("OPENAI_API_KEY")
        return OpenAIClient(model=model or "gpt-4o-2024-08-06")
    if provider in {"google", "gemini"}:
        _require_api_key("GEMINI_API_KEY")
        return GeminiClient(model=model or "gemini-2.0-flash")
    if provider in {"ollama", "local"}:
        return OllamaClient(model=model or os.environ.get("OLLAMA_MODEL", "gemma3:4b"))
    raise ValueError(
        f"Unknown provider: {provider!r}. Use 'anthropic', 'openai', 'google', or 'ollama'."
    )
