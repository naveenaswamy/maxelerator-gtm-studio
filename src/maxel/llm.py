"""LLM backends for the local runner (BAND mode uses BAND's own Anthropic adapter)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import requests


@dataclass
class Completion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLM:
    def complete(self, system: str, user: str, max_tokens: int = 4000) -> Completion:  # pragma: no cover
        raise NotImplementedError


class AnthropicLLM(LLM):
    """Calls the Anthropic Messages API. Uses the official SDK when installed, HTTPS otherwise."""

    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str = "claude-sonnet-5-5", timeout: float = 300):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when MAXEL_LLM=anthropic")
        self.api_key, self.model, self.timeout = api_key, model, timeout
        try:
            import anthropic  # optional

            self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        except ImportError:
            self._client = None

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> Completion:
        messages = [{"role": "user", "content": user}]
        if self._client is not None:
            msg = self._client.messages.create(model=self.model, max_tokens=max_tokens, system=system, messages=messages)
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            return Completion(text, msg.usage.input_tokens, msg.usage.output_tokens)

        resp = requests.post(
            self.URL,
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        return Completion(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0))


class StubLLM(LLM):
    """Deterministic writer for tests, demos and dry runs. Costs nothing, needs no key."""

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> Completion:
        marker = "SECTION:"
        section = next((ln.split(marker, 1)[1].strip() for ln in user.splitlines() if marker in ln), "section")
        profile = {}
        if "```json" in user:
            try:
                profile = json.loads(user.split("```json", 1)[1].split("```", 1)[0])
            except ValueError:
                profile = {}
        name = profile.get("name", "the startup")
        buyers = profile.get("target_customers", "early adopters")
        text = (
            f"**Draft ({section}) for {name}.** Stub output: set MAXEL_LLM=anthropic for real content.\n\n"
            f"- First buyers: {buyers}\n"
            f"- Product: {profile.get('product', 'n/a')}\n"
            f"- Stage: {profile.get('stage', 'n/a')}\n\n"
            "| Item | Hypothesis | How to validate |\n| --- | --- | --- |\n"
            "| Buyer pain | To be confirmed | 10 discovery calls |\n"
        )
        return Completion(text, input_tokens=len(user) // 4, output_tokens=len(text) // 4)


class OpenAICompatLLM(LLM):
    """Any OpenAI-compatible chat endpoint. Built for a local Qwen served by `mlx_lm.server`:

        mlx_lm.server --model mlx-community/Qwen2.5-0.5B-Instruct-4bit \\
                      --adapter-path <qwen_studio run>/adapter --port 8081

    then MAXEL_OPENAI_BASE_URL=http://127.0.0.1:8081/v1. Also works with vLLM, Ollama or LM Studio.
    """

    def __init__(self, base_url: str, model: str | None = None, api_key: str | None = None,
                 temperature: float = 0.0, timeout: float = 300):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.temperature, self.timeout = model, temperature, timeout
        self.headers = {"content-type": "application/json"}
        if api_key:
            self.headers["authorization"] = f"Bearer {api_key}"

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> Completion:
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": self.temperature,
        }
        if self.model:  # mlx_lm.server uses the model it was started with when this is omitted
            body["model"] = self.model
        resp = requests.post(self.url, headers=self.headers, json=body, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
        return Completion(text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


def llm_by_name(name: str, settings) -> LLM:
    if name == "anthropic":
        return AnthropicLLM(settings.anthropic_api_key, settings.model)
    if name in ("openai", "qwen", "local"):
        return OpenAICompatLLM(settings.openai_base_url, settings.openai_model, settings.openai_api_key)
    if name == "stub":
        return StubLLM()
    raise ValueError(f"unknown LLM '{name}' (use stub | anthropic | openai)")


def llm_from_settings(settings) -> LLM:
    """The model that writes playbook sections in local mode (MAXEL_LLM)."""
    return llm_by_name(settings.llm, settings)
