"""One question through a throwaway pair: fill membro, then ask crossband.

Filling membro has two steps, and the second is the one that matters. Ingest
writes the conversation record. Mining (``/v1/distill``) turns it into ledger
facts, and recall and the profile read the ledger. Without mining the seat
finds nothing, abstains on every question, and scores well on exactly the
questions where abstaining is right.

Asking uses a new chat with one seat in it from the start, with web research
and the code guest switched off. A reused chat would replay other seats'
turns, and web research would let a seat answer without memory.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .launcher import SOURCE_APP, LaunchError, Pair, http_json

# Added after every question. LongMemEval marks 30 of its 500 questions as
# unanswerable, and the right answer to those is to say so.
ABSTENTION_HINT = (
    "If the conversation history does not contain the answer, say plainly that "
    "you do not have that information rather than guessing.")


def wait_job(pair: Pair, started, *, timeout: float = 900.0) -> dict | None:
    """Wait on a membro background job. Job polls need the owner token."""
    job_id = (started or {}).get("job_id")
    if not job_id:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = http_json(f"{pair.membro.base_url}/v1/jobs/{job_id}",
                          token=pair.membro_token) or {}
        if state.get("status") != "running":
            return state
        time.sleep(0.5)
    raise LaunchError(f"membro job {job_id} didn't finish within {timeout}s")


def ingest(pair: Pair, question) -> list[str]:
    """Write each haystack session as its own conversation. Returns their ids."""
    pair.reverify()
    ids = []
    for session in question.sessions:
        cid = f"{question.question_id}-{session['session_id']}"
        messages = [{"external_id": f"{cid}-{i}", "speaker": turn["speaker"],
                     "content": turn["content"],
                     "created_at": session["created_at"], "attachments": []}
                    for i, turn in enumerate(session["turns"])]
        http_json(f"{pair.membro.base_url}/v1/ingest", payload={
            "source_app": SOURCE_APP, "conversation_id": cid,
            "title": f"bench {cid}", "messages": messages})
        ids.append(cid)
    return ids


def mine(pair: Pair, conversation_ids, *, timeout: float = 900.0) -> dict:
    """Mine every conversation, then rebuild the profile once.

    ``regenerate_summary=False`` on each distill and one rebuild at the end
    is what membro's API offers bulk loads for.
    """
    pair.reverify()
    failures = []
    for cid in conversation_ids:
        state = wait_job(pair, http_json(
            f"{pair.membro.base_url}/v1/distill",
            payload={"source_app": SOURCE_APP, "conversation_id": cid,
                     "regenerate_summary": False}), timeout=timeout)
        if state and state.get("status") == "failed":
            failures.append(state.get("error") or "failed")
    summary = wait_job(pair, http_json(
        f"{pair.membro.base_url}/v1/summary/regenerate", payload={}),
        timeout=timeout) or {}
    if summary.get("status") == "failed":
        failures.append("summary: " + str(summary.get("error")))
    return {"failures": failures,
            "summary_chars": (summary.get("result") or {}).get("summary_chars")}


def ledger_counts(pair: Pair) -> dict:
    """Fact totals from membro's health route: every fact, the ones recall
    can see, and the ones held for review."""
    detail = (http_json(f"{pair.membro.base_url}/v1/health") or {}).get("detail") or {}
    facts = detail.get("facts") or {}
    return {"facts": facts.get("total"), "current": facts.get("current"),
            "quarantined": facts.get("quarantined")}


def pick_reply(payload):
    """The seat's answer from a crossband messages reply, or ``None``.

    The route answers ``{"messages": [...]}`` and each row names its
    ``speaker``, the seat's slug or ``"user"``. Filtering on anything else
    would hand back the question as the answer.
    """
    messages = payload.get("messages") if isinstance(payload, dict) else payload
    replies = [m for m in (messages or [])
               if isinstance(m, dict) and m.get("speaker") != "user"
               and (m.get("content") or "").strip()]
    return replies[-1] if replies else None


def round_errors(stream: str) -> list[str]:
    """The error events in a round's stream. A seat that fails part way
    can leave half a reply behind, and that isn't an answer."""
    out = []
    for line in stream.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "error":
            out.append(str(event.get("message") or "error"))
    return out


def _seat(pair: Pair, prefer: str = "claude") -> dict:
    state = http_json(f"{pair.crossband.base_url}/api/state")
    seats = state.get("participants") or []
    if not seats:
        raise LaunchError("the throwaway crossband has no seats")
    return next((p for p in seats if p.get("slug") == prefer), seats[0])


@dataclass
class Answer:
    text: str
    seat: str
    model: str
    seconds: float


def ask(pair: Pair, question, *, hint: bool = True,
        timeout: float = 300.0) -> Answer:
    pair.reverify()
    seat = _seat(pair)
    base = pair.crossband.base_url
    chat = http_json(f"{base}/api/chats", payload={
        "title": f"bench {question.question_id}",
        "participant_ids": [seat["id"]]})
    http_json(f"{base}/api/chats/{chat['id']}", method="PATCH", payload={
        "web_enabled": False, "code_enabled": False, "memory_enabled": True})
    prompt = f"{question.question}\n\n{ABSTENTION_HINT}" if hint else question.question
    started = time.monotonic()
    # The reply is the round's event stream. Reading it to the end waits for
    # the round to finish.
    stream = http_json(f"{base}/api/chats/{chat['id']}/send",
                       payload={"text": prompt}, timeout=timeout,
                       expect_json=False)
    failed = round_errors(stream)
    if failed:
        raise LaunchError(f"the seat failed: {failed[0]}"[:300])
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        reply = pick_reply(http_json(f"{base}/api/chats/{chat['id']}/messages"))
        if reply is not None:
            return Answer(text=reply["content"].strip(),
                          seat=seat.get("slug", ""), model=seat.get("model", ""),
                          seconds=round(time.monotonic() - started, 1))
        time.sleep(1.0)
    raise LaunchError(f"no reply in chat {chat['id']} after the round ended")
