"""Utility-model completions, lab-configurable — routed by model name.

`claude*` models go to Anthropic; every other name goes to an
OpenAI-compatible endpoint — OpenAI itself by default, or the server named
by `llm_base_url` (#59): Ollama, MLX, LM Studio. With a base URL set no
API key is required; local servers ignore the placeholder the SDK insists
on. Fails loudly when a needed key is missing (no silent no-op mining
runs); a dead local endpoint fails just as loudly, with a connection
error at call time."""

import os
import threading

import anthropic
from openai import OpenAI


class MissingKeyError(RuntimeError):
    pass


# One client per process per provider (mirroring embeddings.py): a fresh
# SDK client per call paid a new TLS handshake for every mining/summary call.
# Both SDK clients are thread-safe; jobs threads share them. The key checks
# below still run FIRST, so keyless behavior (loud MissingKeyError, no
# client ever built) is unchanged.
_lock = threading.Lock()
_clients: dict = {}


def _base_url(settings) -> str:
    return (getattr(settings, "llm_base_url", "") or "").strip()


def _client(provider: str, settings=None):
    if provider not in _clients:
        with _lock:
            if provider not in _clients:
                if provider == "anthropic":
                    _clients[provider] = anthropic.Anthropic()
                else:
                    kwargs = {}
                    base = _base_url(settings)
                    if base:
                        kwargs["base_url"] = base
                        if not os.environ.get("OPENAI_API_KEY"):
                            # the SDK requires a key string; a local
                            # server never reads it (#59)
                            kwargs["api_key"] = "local"
                    _clients[provider] = OpenAI(**kwargs)
    return _clients[provider]


def _check_openai_key(model: str, settings) -> None:
    if _base_url(settings):
        return  # local/self-hosted endpoint: no key required (#59)
    if not os.environ.get("OPENAI_API_KEY"):
        raise MissingKeyError(
            f"utility model {model} needs OPENAI_API_KEY "
            "(or llm_base_url for a local server)")


def utility_complete(prompt: str, settings, max_tokens: int = 1000,
                     model: str | None = None,
                     thinking_budget: int | None = None) -> str:
    """One text completion. `thinking_budget` (#58) buys the claude branch
    extended thinking (minimum 1024, and it must stay under max_tokens);
    the compatible branch ignores it — local servers have no such knob."""
    model = model or settings.miner_model
    if model.startswith("claude"):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise MissingKeyError(f"utility model {model} needs ANTHROPIC_API_KEY")
        extra = {}
        if thinking_budget:
            budget = max(1024, min(thinking_budget, max_tokens - 1))
            extra["thinking"] = {"type": "enabled", "budget_tokens": budget}
        resp = _client("anthropic").messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}], **extra)
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    _check_openai_key(model, settings)
    resp = _client("openai", settings).chat.completions.create(
        model=model, max_completion_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}])
    return (resp.choices[0].message.content or "").strip()


def utility_vision(prompt: str, images: list[tuple[str, str]], settings,
                   max_tokens: int = 400, model: str | None = None) -> str:
    """One completion over a prompt plus images [(mime, base64), ...].

    Same routing, key checks, and loud keyless failure as utility_complete —
    a caption run must never silently no-op and pretend images were seen.
    """
    model = model or settings.miner_model
    if model.startswith("claude"):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise MissingKeyError(f"utility model {model} needs ANTHROPIC_API_KEY")
        content = [{"type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64}}
                   for mime, b64 in images]
        content.append({"type": "text", "text": prompt})
        resp = _client("anthropic").messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    _check_openai_key(model, settings)
    content = [{"type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}}
               for mime, b64 in images]
    content.append({"type": "text", "text": prompt})
    resp = _client("openai", settings).chat.completions.create(
        model=model, max_completion_tokens=max_tokens,
        messages=[{"role": "user", "content": content}])
    return (resp.choices[0].message.content or "").strip()
