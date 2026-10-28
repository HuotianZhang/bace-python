"""The assistant's model vendors, one class per wire format.

`assistant.chat` runs the same loop whoever answers: send the history, show
the text as it arrives, run the tools the answer asks for, send the results
back. What differs between vendors is only how a turn is sent, streamed and
written down, and that is all a class here does:

| | `Anthropic` | `OpenAICompatible` |
|---|---|---|
| speaks to | the Claude API | any `/chat/completions` endpoint: OpenAI, DeepSeek, Qwen (DashScope), Kimi (Moonshot), GLM (Zhipu), a local Ollama |
| SDK | `anthropic` 2.3 or later | `openai` 3.40 or later |
| tool results go back as | one user message of `tool_result` blocks | one `tool` message per call |

A turn is streamed as `{"type": "text", "text"}` frames and ends with one
`{"type": "turn", "turn": Turn}`. `Turn.message` is the assistant message as
the vendor wants it sent back, which is why histories are kept per vendor and
a conversation does not change vendor half-way.

Both SDKs are imported only when a provider is made, so a service without
either still runs, and `GET /assistant` says which is missing.
"""
from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

CLAUDE_MODEL = os.environ.get("BACE_ASSISTANT_MODEL", "claude-opus-5-5")
"""The Claude model when the asker names none; overridable, never chosen for them."""

MAX_TOKENS = 64000
EFFORT = "medium"

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
"""Models the API's server-side refusal fallback is sent with. Another Claude
model is asked without it rather than refused for it."""

SDK_MIN = {"anthropic": "2.3", "openai": "3.40"}
"""The releases the request shapes here were checked against."""


class Unavailable(RuntimeError):
    """The vendor's SDK is missing or too old, or the request cannot be made."""


class NoKey(Unavailable):
    """The provider needs a key and the request carried none."""


class BadSetting(Unavailable):
    """The address or the model cannot be used as given."""


def sdk(name: str):
    """The SDK module `name`, or `Unavailable` saying how to get it."""
    try:
        module = __import__(name)
    except ImportError as exc:
        raise Unavailable(f"the {name} package is not installed: pip install -e .[service]") from exc
    have = tuple(int(p) for p in module.__version__.split(".")[:2] if p.isdigit())
    if have < tuple(int(p) for p in SDK_MIN[name].split(".")):
        raise Unavailable(f"{name} {SDK_MIN[name]} or later is needed, and this Python has "
                          f"{module.__version__}: pip install -U {name}")
    return module


def status() -> dict:
    """Each vendor's SDK: there, with its version, or why not."""
    out = {}
    for kind, name in (("anthropic", "anthropic"), ("openai", "openai")):
        try:
            out[kind] = {"available": True, "sdk": sdk(name).__version__}
        except Unavailable as exc:
            out[kind] = {"available": False, "reason": str(exc)}
    out["anthropic"]["model"] = CLAUDE_MODEL
    return out


def is_local(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_base_url(url: str) -> str:
    """`url` if it is somewhere a key may be sent: https, or http to this machine."""
    parsed = urlparse(url or "")
    if parsed.scheme == "https" and parsed.hostname:
        return url.rstrip("/")
    if parsed.scheme == "http" and is_local(url):
        return url.rstrip("/")
    raise BadSetting("the API address must be https://, or http:// to this machine "
                     "(a local model)")


@dataclass
class ToolCall:
    id: str
    name: str
    input: Any
    """The parsed arguments, or None when they were not JSON (`raw` then holds them)."""
    raw: str = ""


@dataclass
class Turn:
    message: Any
    calls: list[ToolCall]
    stop: str
    """`end`, `tools`, `max_tokens` or `refusal`, whatever the vendor called it."""
    usage: dict = field(default_factory=dict)


def error_text(module, exc: Exception) -> tuple[str, int | None]:
    """A sentence for the console, most specific first. Both SDKs name their
    errors alike, so one table serves."""
    if module is None:
        return f"{type(exc).__name__}: {exc}", None
    if isinstance(exc, module.AuthenticationError):
        return "the API key was refused: check it in the assistant's settings", 401
    if isinstance(exc, module.PermissionDeniedError):
        return "this API key may not use the model", 403
    if isinstance(exc, module.RateLimitError):
        return "rate limited by the API; wait a minute and ask again", 429
    if isinstance(exc, module.BadRequestError):
        return f"the API refused the request: {exc.message}", 400
    if isinstance(exc, module.APIStatusError):
        return f"the API answered {exc.status_code}: {exc.message}", exc.status_code
    if isinstance(exc, module.APIConnectionError):
        return "could not reach the API: is this PC online, and is the address right?", None
    return f"{type(exc).__name__}: {exc}", None


class Anthropic:
    """The Claude API: a streamed Messages request with the tools."""

    kind = "anthropic"

    def __init__(self, key: str, model: str | None = None, *, client=None) -> None:
        self.sdk = sdk("anthropic")
        self.model = model or CLAUDE_MODEL
        self.client = client or self.sdk.AsyncAnthropic(api_key=key)

    @property
    def signature(self) -> tuple:
        return (self.kind, self.model)

    def user(self, text: str) -> dict:
        return {"role": "user", "content": text}

    def results(self, done: Sequence[tuple[ToolCall, str, bool]]) -> list[dict]:
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": content,
             **({} if ok else {"is_error": True})} for call, content, ok in done]}]

    async def turn(self, messages: list, system: str, tools: list[dict]) -> AsyncIterator[dict]:
        extra = ({"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
                 if self.model in FALLBACK_MODELS else {})
        retries = 0
        while True:
            try:
                async with self.client.beta.messages.stream(
                    model=self.model, max_tokens=MAX_TOKENS, system=system,
                    tools=[{**t, "eager_input_streaming": True} for t in tools],
                    messages=messages, output_config={"effort": EFFORT},
                    cache_control={"type": "ephemeral"}, **extra,
                ) as stream:
                    async for event in stream:
                        if event.type == "text":
                            yield {"type": "text", "text": event.text}
                    response = await stream.get_final_message()
                break
            except ValueError:
                # Tool input the SDK could not parse at all: there is no
                # tool_use id to answer, so the turn is asked again.
                retries += 1
                if retries > 2:
                    raise
        u = response.usage
        calls = [ToolCall(b.id, b.name, b.input) for b in response.content if b.type == "tool_use"]
        stop = {"tool_use": "tools", "end_turn": "end"}.get(response.stop_reason, response.stop_reason)
        yield {"type": "turn", "turn": Turn(
            {"role": "assistant", "content": response.content}, calls, stop,
            {"input": u.input_tokens or 0, "output": u.output_tokens or 0,
             "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0})}


class OpenAICompatible:
    """Any `/chat/completions` endpoint with function tools, streamed."""

    kind = "openai"

    def __init__(self, key: str, model: str | None, base_url: str, *, client=None) -> None:
        if not model:
            raise BadSetting("name the model, as the provider spells it")
        self.sdk = sdk("openai")
        self.model = model
        self.base_url = check_base_url(base_url)
        # A local server takes any key; the SDK refuses to be made without one.
        self.client = client or self.sdk.AsyncOpenAI(api_key=key or "local",
                                                     base_url=self.base_url)

    @property
    def signature(self) -> tuple:
        return (self.kind, self.base_url, self.model)

    def user(self, text: str) -> dict:
        return {"role": "user", "content": text}

    def results(self, done: Sequence[tuple[ToolCall, str, bool]]) -> list[dict]:
        return [{"role": "tool", "tool_call_id": call.id, "content": content}
                for call, content, _ok in done]

    async def turn(self, messages: list, system: str, tools: list[dict]) -> AsyncIterator[dict]:
        stream = await self.client.chat.completions.create(
            model=self.model, stream=True,
            messages=[{"role": "system", "content": system}, *messages],
            tools=[{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in tools])
        text: list[str] = []
        parts: dict[int, dict] = {}
        finish = None
        usage = {"input": 0, "output": 0, "cache_read": 0}
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage["input"] = chunk.usage.prompt_tokens or 0
                usage["output"] = chunk.usage.completion_tokens or 0
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is not None:
                if delta.content:
                    text.append(delta.content)
                    yield {"type": "text", "text": delta.content}
                for tc in delta.tool_calls or ():
                    part = parts.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    part["id"] = tc.id or part["id"]
                    if tc.function is not None:
                        part["name"] += tc.function.name or ""
                        part["arguments"] += tc.function.arguments or ""
            finish = choice.finish_reason or finish
        calls = []
        for i in sorted(parts):
            part = parts[i]
            try:
                args = json.loads(part["arguments"] or "{}")
            except json.JSONDecodeError:
                args = None
            calls.append(ToolCall(part["id"] or f"call_{i}", part["name"], args, part["arguments"]))
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
        if calls:
            message["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": c.raw or "{}"}}
                                     for c in calls]
        # Some servers answer "stop" with tool calls in hand; the calls decide.
        stop = {"length": "max_tokens", "content_filter": "refusal"}.get(finish) or \
            ("tools" if calls else "end")
        yield {"type": "turn", "turn": Turn(message, calls, stop, usage)}


def make(kind: str, *, key: str, model: str | None = None, base_url: str | None = None):
    """The provider a request names. `Unavailable` says why it cannot be made."""
    if kind == "anthropic":
        if not key:
            raise NoKey("the assistant needs your Anthropic API key")
        return Anthropic(key, model)
    if kind == "openai":
        url = check_base_url(base_url or "")
        if not model:
            raise BadSetting("name the model, as the provider spells it")
        if not key and not is_local(url):
            raise NoKey("this provider needs your API key")
        return OpenAICompatible(key, model, url)
    raise BadSetting(f"no provider called {kind!r}")


__all__ = ["Anthropic", "BadSetting", "CLAUDE_MODEL", "NoKey", "OpenAICompatible", "ToolCall", "Turn",
           "Unavailable",
           "check_base_url", "error_text", "is_local", "make", "sdk", "status"]
