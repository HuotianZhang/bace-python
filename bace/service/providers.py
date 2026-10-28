"""The model providers adapt assistant turns to each vendor's wire format.

`assistant.chat` sends conversation messages, streams text, runs requested
tools and returns tool results. Each provider implements the sending,
streaming and representation of one turn. `Anthropic` uses Messages and
returns tool results in user-message blocks. `OpenAICompatible` uses
`/chat/completions` and one tool message per call.

A streamed turn yields `{"type": "text", "text"}` frames and ends with
`{"type": "turn", "turn": Turn}`. `Turn.message` preserves the assistant
message in the vendor's return format, so messages cannot be shared across
providers within a conversation (`docs/service-contract.md`, section 8a).

SDKs are imported when a provider is created. Missing SDKs leave the service
available; `GET /assistant` reports them."""
from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

CLAUDE_MODEL = os.environ.get("BACE_ASSISTANT_MODEL", "claude-opus-5-5")
"""The default Claude model applies when the request names none.

The environment can override this default."""

MAX_TOKENS = 64000
EFFORT = "medium"

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
"""These Claude models accept the API's server-side refusal fallback.

Other models are requested without that option rather than refused."""

SDK_MIN = {"anthropic": "2.3", "openai": "3.40"}
"""These SDK versions support the request shapes used here."""


class Unavailable(RuntimeError):
    """The unavailable-provider error identifies a missing SDK or unsupported request."""


class NoKey(Unavailable):
    """The missing-key error identifies a provider requiring a request key."""


class BadSetting(Unavailable):
    """The setting error identifies an unusable provider address or model."""


def sdk(name: str):
    """The SDK loader returns module `name` or raises `Unavailable` with a remedy."""
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
    """The provider status reports each SDK version or its unavailability reason."""
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
    """The endpoint check permits HTTPS or HTTP to the local machine.

    Other addresses are refused to prevent sending a key over remote plain HTTP
    (`docs/service-contract.md`, section 8a)."""
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
    """Parsed tool arguments are `None` when the input is invalid JSON.

    `raw` retains that input."""
    raw: str = ""


@dataclass
class Turn:
    message: Any
    calls: list[ToolCall]
    stop: str
    """The turn status normalizes vendor endings to `end`, `tools`, `max_tokens` or
    `refusal`."""
    usage: dict = field(default_factory=dict)


def error_text(module, exc: Exception) -> tuple[str, int | None]:
    """The provider error becomes the most specific matching console message.

    Both SDKs use the same error names, so one table handles them."""
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
    """The Anthropic provider streams Messages requests with tools."""

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
                # Unparseable tool input has no `tool_use` ID for a reply.
                # Retry the turn instead.
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
    """The compatible provider streams `/chat/completions` with function tools."""

    kind = "openai"

    def __init__(self, key: str, model: str | None, base_url: str, *, client=None) -> None:
        if not model:
            raise BadSetting("name the model, as the provider spells it")
        self.sdk = sdk("openai")
        self.model = model
        self.base_url = check_base_url(base_url)
        # The local server accepts any key; SDK construction requires one.
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
        # Tool calls take precedence when a server labels the turn `stop`.
        stop = {"length": "max_tokens", "content_filter": "refusal"}.get(finish) or \
            ("tools" if calls else "end")
        yield {"type": "turn", "turn": Turn(message, calls, stop, usage)}


def make(kind: str, *, key: str, model: str | None = None, base_url: str | None = None):
    """The provider factory builds the requested provider or raises `Unavailable`."""
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
