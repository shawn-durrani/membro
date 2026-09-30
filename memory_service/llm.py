"""Utility-model completions, lab-configurable — routed by model name.

`claude*` models go to Anthropic; every other name goes to an
OpenAI-compatible endpoint — OpenAI itself by default, or the server named
by `llm_base_url` (#59): Ollama, MLX, LM Studio. With a base URL set no
API key is required; local servers ignore the placeholder the SDK insists
on. Fails loudly when a needed key is missing (no silent no-op mining
runs); a dead local endpoint fails just as loudly, with a connection
error at call time."""

import logging
import os
import sys
import threading

import anthropic
from openai import OpenAI

log = logging.getLogger("memory_service.llm")


class MissingKeyError(RuntimeError):
    pass


# How a reply ends when the model finished it: "end_turn" and
# "stop_sequence" from Anthropic, "stop" from an OpenAI-compatible server.
# Anything else is a reply cut short: out of room ("max_tokens", "length"),
# refused, or past the context window.
FINISHED = frozenset({"end_turn", "stop_sequence", "stop"})


class CutOffError(RuntimeError):
    """A reply the model didn't finish. Carries the model, the stop reason
    and the cap, never the text."""

    def __init__(self, model: str, stop_reason: str, max_tokens: int):
        self.model = model
        self.stop_reason = stop_reason
        self.max_tokens = max_tokens
        super().__init__(f"{model} stopped before it finished (stop reason "
                         f"{stop_reason}, cap {max_tokens} tokens)")


def _check_finished(model: str, stop_reason, max_tokens: int) -> None:
    # A server that reports no reason at all can't be checked, so its reply
    # stands. Anthropic always reports one.
    if stop_reason is not None and stop_reason not in FINISHED:
        raise CutOffError(model, stop_reason, max_tokens)


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


# A prompt is a plain string, or a list of parts when it has a prefix worth
# caching. A part is {"text": str}; one with "cache": "5m" or "1h" puts an
# Anthropic cache breakpoint at its end, so the next call that starts with
# the same bytes reads everything up to there at a tenth of the price. The
# compatible branch joins the parts into one string: OpenAI caches a repeated
# prefix on its own, and local servers ignore the question.
TTLS = ("5m", "1h")


def _parts(prompt) -> list[dict]:
    return [{"text": prompt}] if isinstance(prompt, str) else list(prompt)


def prompt_text(prompt, system=None) -> str:
    """The prompt as one string: the system text, a blank line, then the
    parts in order. What the compatible branch sends, and what tests read."""
    body = "".join(p["text"] for p in _parts(prompt))
    head = "".join(p["text"] for p in _parts(system)) if system else ""
    return f"{head}\n\n{body}" if head else body


def _blocks(prompt) -> list[dict]:
    blocks = []
    for p in _parts(prompt):
        if not p["text"]:
            continue  # the API refuses an empty text block
        block = {"type": "text", "text": p["text"]}
        ttl = p.get("cache")
        if ttl:
            if ttl not in TTLS:
                raise ValueError(f"cache ttl must be one of {TTLS}, not {ttl!r}")
            block["cache_control"] = ({"type": "ephemeral"} if ttl == "5m"
                                      else {"type": "ephemeral", "ttl": "1h"})
        blocks.append(block)
    return blocks


# Token counts per call site since the process started, for the usage log
# line: [calls, input, cache write, cache read, output]. Counts only, never
# text.
_tally: dict[str, list[int]] = {}


def _n(usage, *names) -> int:
    for name in names:
        value = getattr(usage, name, None) if usage is not None else None
        if isinstance(value, int):
            return value
    return 0


def _record_usage(site: str, model: str, usage) -> None:
    """One content-free log line per model call: where it came from, the
    model, and its token counts, with that site's running cache share."""
    if usage is None:
        return
    fresh = _n(usage, "input_tokens", "prompt_tokens")
    written = _n(usage, "cache_creation_input_tokens")
    read = _n(usage, "cache_read_input_tokens")
    if not read:
        # OpenAI counts cached tokens inside prompt_tokens
        details = getattr(usage, "prompt_tokens_details", None)
        read = _n(details, "cached_tokens")
        fresh -= read
    out = _n(usage, "output_tokens", "completion_tokens")
    with _lock:
        t = _tally.setdefault(site, [0, 0, 0, 0, 0])
        for i, v in enumerate((1, fresh, written, read, out)):
            t[i] += v
        calls, total_in, total_read = t[0], t[1] + t[2] + t[3], t[3]
    share = round(100 * total_read / total_in) if total_in else 0
    log.info("model call %s on %s: input %d, cache write %d, cache read %d, "
             "output %d. This site since start: %d calls, %d%% of input read "
             "from cache", site, model, fresh, written, read, out, calls, share)


def usage_totals() -> dict:
    """The running per-site counts behind the log line."""
    with _lock:
        return {site: dict(zip(("calls", "input", "cache_write", "cache_read",
                                "output"), t)) for site, t in _tally.items()}


def enable_usage_log() -> None:
    """Send the usage lines to the service's own output. The service sets no
    logging up, so an info line from this module would go nowhere, and a
    root-level setup would also print every HTTP request the SDKs make."""
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def utility_complete(prompt, settings, max_tokens: int = 1000,
                     model: str | None = None,
                     thinking_budget: int | None = None, *,
                     system=None, site: str = "utility",
                     must_finish: bool = False) -> str:
    """One text completion. `thinking_budget` (#58) buys the claude branch
    extended thinking (minimum 1024, and it must stay under max_tokens);
    the compatible branch ignores it — local servers have no such knob.

    `prompt` and `system` are strings or lists of parts (see TTLS above).
    `site` names the caller in the usage log line. With `must_finish`, a
    reply the model didn't finish raises CutOffError instead of coming back
    as text; without it, a caller gets whatever text there is, as before."""
    model = model or settings.miner_model
    if model.startswith("claude"):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise MissingKeyError(f"utility model {model} needs ANTHROPIC_API_KEY")
        extra = {}
        if thinking_budget:
            budget = max(1024, min(thinking_budget, max_tokens - 1))
            extra["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if system:
            extra["system"] = _blocks(system)
        # A plain string goes as it always has, so an uncached call's
        # request is unchanged.
        content = prompt if isinstance(prompt, str) else _blocks(prompt)
        resp = _client("anthropic").messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}], **extra)
        _record_usage(site, model, getattr(resp, "usage", None))
        if must_finish:
            _check_finished(model, getattr(resp, "stop_reason", None),
                            max_tokens)
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    _check_openai_key(model, settings)
    messages = [{"role": "user", "content": prompt_text(prompt)}]
    if system:
        messages.insert(0, {"role": "system", "content": prompt_text(system)})
    resp = _client("openai", settings).chat.completions.create(
        model=model, max_completion_tokens=max_tokens, messages=messages)
    _record_usage(site, model, getattr(resp, "usage", None))
    if must_finish:
        _check_finished(model, getattr(resp.choices[0], "finish_reason", None),
                        max_tokens)
    return (resp.choices[0].message.content or "").strip()


def utility_vision(prompt: str, images: list[tuple[str, str]], settings,
                   max_tokens: int = 400, model: str | None = None, *,
                   site: str = "caption") -> str:
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
        _record_usage(site, model, getattr(resp, "usage", None))
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    _check_openai_key(model, settings)
    content = [{"type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}}
               for mime, b64 in images]
    content.append({"type": "text", "text": prompt})
    resp = _client("openai", settings).chat.completions.create(
        model=model, max_completion_tokens=max_tokens,
        messages=[{"role": "user", "content": content}])
    _record_usage(site, model, getattr(resp, "usage", None))
    return (resp.choices[0].message.content or "").strip()
