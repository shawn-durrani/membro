"""Utility-model completions, lab-configurable — routed by model name.

Fails loudly when the needed key is missing (no silent no-op mining runs)."""

import os
import threading

import anthropic
from openai import OpenAI


class MissingKeyError(RuntimeError):
    pass


# One client per process per provider (#78, mirroring embeddings.py): a fresh
# SDK client per call paid a new TLS handshake for every mining/summary call.
# Both SDK clients are thread-safe; jobs threads share them. The key checks
# below still run FIRST, so keyless behavior (loud MissingKeyError, no
# client ever built) is unchanged.
_lock = threading.Lock()
_clients: dict = {}


def _client(provider: str):
    if provider not in _clients:
        with _lock:
            if provider not in _clients:
                _clients[provider] = (anthropic.Anthropic() if provider == "anthropic"
                                      else OpenAI())
    return _clients[provider]


def utility_complete(prompt: str, settings, max_tokens: int = 1000,
                     model: str | None = None) -> str:
    model = model or settings.miner_model
    if model.startswith("claude"):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise MissingKeyError(f"utility model {model} needs ANTHROPIC_API_KEY")
        resp = _client("anthropic").messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    if not os.environ.get("OPENAI_API_KEY"):
        raise MissingKeyError(f"utility model {model} needs OPENAI_API_KEY")
    resp = _client("openai").chat.completions.create(
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
    if not os.environ.get("OPENAI_API_KEY"):
        raise MissingKeyError(f"utility model {model} needs OPENAI_API_KEY")
    content = [{"type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}}
               for mime, b64 in images]
    content.append({"type": "text", "text": prompt})
    resp = _client("openai").chat.completions.create(
        model=model, max_completion_tokens=max_tokens,
        messages=[{"role": "user", "content": content}])
    return (resp.choices[0].message.content or "").strip()
