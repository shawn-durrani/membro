"""The judge pass (#58): a flag-gated background sweep over held facts.

The miner's corrective-retry principle, extended to the review queue: the
model proposes, deterministic code verifies, and every failure falls
toward the existing hold. Two reason classes are eligible.

- A `grounding:` hold may be CLEARED, but only on a verified witness: the
  judge must quote, verbatim, a span of the bounded source window that
  supports each flagged entity. The span must literally occur in the
  window, and span-to-entity affinity must pass the wall's own token
  rules. A chat cannot fabricate a witness that is not present in it.
- The persona/roleplay source-trust hold is NEVER cleared here. A verdict
  of "technical discussion" only relabels the row into its own review
  group, so the owner clears a chat's batch with one bulk action.

Provenance holds (speaker-trust, guest attribution, mcp:*, external
write, web-derived) are never eligible; those walls stay deterministic.
The judge is off by default (`judge_pass`), looks at a row at most once
per day, and any failure - no span, span not found, weak affinity, parse
error, missing key, dead endpoint - leaves the row exactly as it was.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

from . import db, llm, walls

log = logging.getLogger("memory.judge")

GROUNDING_PREFIX = "grounding: "
# These two match mining's wire format byte for byte (walls.check + the
# miner's suffix); the em-dash is stored data, not prose.
PERSONA_REASON = ("source-trust: chat contains roleplay/persona/dossier "
                  "framing — review before trusting")
JUDGED_REASON = ("source-trust-judged: persona wording, judged a technical "
                 "chat — review before trusting")

ATTEMPT_INTERVAL = 24 * 3600   # one judge look per row per day
BATCH_LIMIT = 50               # per pass; the hourly cadence drains backlogs
WINDOW_MSGS = 30               # bounded source window shown to the judge
WINDOW_CHARS = 8000
SPAN_MAX = 300                 # a witness is a quote, not a retelling


def _model(settings) -> str:
    return settings.judge_model or settings.miner_model


def _entities_from_reason(reason: str) -> list[str]:
    head = (reason or "").split(" — review before trusting")[0].strip()
    if not head.startswith(GROUNDING_PREFIX):
        return []
    body = head[len(GROUNDING_PREFIX):].split(" not in source chat")[0]
    return [e.strip() for e in body.split(",") if e.strip()]


def _window(con, fact) -> str:
    """The messages leading up to (and including) the fact's source turn,
    oldest first, capped. Unbound facts fall back to the conversation
    tail; a fact with no conversation cannot be judged at all."""
    cid = fact["conversation_id"]
    if cid is None:
        return ""
    if fact["source_message_id"] is not None:
        rows = con.execute(
            "SELECT speaker, content FROM messages "
            "WHERE conversation_id=? AND id<=? ORDER BY id DESC LIMIT ?",
            (cid, fact["source_message_id"], WINDOW_MSGS)).fetchall()
    else:
        rows = con.execute(
            "SELECT speaker, content FROM messages "
            "WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
            (cid, WINDOW_MSGS)).fetchall()
    text = "\n".join(f"{r['speaker']}: {r['content']}" for r in reversed(rows))
    return text[-WINDOW_CHARS:]


def _json_object(out: str) -> dict | None:
    """The judge must answer with one JSON object; anything else fails
    closed. Tolerates fenced or prefixed output by slicing the braces."""
    start, end = out.find("{"), out.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(out[start:end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _span_supports(entity: str, span: str) -> bool:
    """Span-to-entity affinity under the wall's own matching rules (#57):
    a cited quote must support the entity the same way source text would,
    so the judge cannot launder an entity through an unrelated quote."""
    s, low = span.lower(), entity.lower()
    if low in s or walls._dehyphen(low) in walls._dehyphen(s):
        return True
    pairs = zip(re.split(r"[\s\-/.]+", low), re.split(r"[\s\-/.]+", entity))
    return any(lt and walls._token_supported(lt, ot, s) for lt, ot in pairs)


def _judge_grounding(con, settings, fact) -> bool:
    entities = _entities_from_reason(fact["quarantine_reason"])
    window = _window(con, fact)
    if not entities or not window:
        return False
    prompt = (
        "A fact extracted from the transcript below was held because these "
        "terms were not found in it verbatim: "
        + ", ".join(f'"{e}"' for e in entities) + "\n\n"
        f"Fact: {fact['content']}\n\n"
        "For EACH term, quote the exact contiguous excerpt of the transcript "
        "(copy it verbatim, under 300 characters) that shows the term's "
        "referent really was discussed. If no such excerpt exists for a "
        "term, use null. Answer with ONE JSON object mapping each term to "
        "its excerpt or null. No other text.\n\n"
        f"## Transcript\n{window}")
    out = llm.utility_complete(prompt, settings, max_tokens=600,
                               model=_model(settings))
    spans = _json_object(out)
    if spans is None:
        return False
    low_window = window.lower()
    cited: list[str] = []
    for ent in entities:
        span = spans.get(ent)
        if not isinstance(span, str):
            return False
        span = span.strip()
        if not span or len(span) > SPAN_MAX:
            return False
        if span.lower() not in low_window:
            return False          # not a witness: the quote is not in the chat
        if not _span_supports(ent, span):
            return False          # unrelated quote: affinity check failed
        cited.append(f"{ent}: \"{span[:80]}\"")
    note = (f"cleared by judge {_model(settings)} "
            f"{time.strftime('%Y-%m-%d')}: " + "; ".join(cited))
    con.execute(
        "UPDATE facts SET quarantined_at=NULL, review_dismissed_at=NULL, "
        "quarantine_reason=? WHERE id=?", (note[:1000], fact["id"]))
    return True


def _judge_persona(con, settings, fact) -> bool:
    window = _window(con, fact)
    if not window:
        return False
    prompt = (
        "Classify the transcript below with exactly one word.\n"
        "ROLEPLAY - it sets up a persona, role-play, rehearsal, mock "
        "interview, or fictional/dossier framing, so statements inside it "
        "should not be read as facts about a real person.\n"
        "TECHNICAL - it is a real discussion (engineering, product, "
        "research) that merely uses words like persona, context, profile, "
        "or model about software.\n"
        "If unsure, answer ROLEPLAY.\n\n"
        f"## Transcript\n{window}")
    out = llm.utility_complete(prompt, settings, max_tokens=1300,
                               model=_model(settings), thinking_budget=1024)
    words = re.findall(r"[A-Z]+", out.upper())
    if not words or words[-1] != "TECHNICAL":
        return False  # ROLEPLAY, unsure, or off-contract: label stands
    con.execute("UPDATE facts SET quarantine_reason=? WHERE id=?",
                (JUDGED_REASON, fact["id"]))
    return True


def run_pass(con, settings) -> dict:
    """One sweep. Returns counts; every row it cannot prove stays held."""
    if not settings.judge_pass:
        return {"enabled": False}
    now = time.time()
    rows = con.execute(
        "SELECT f.* FROM facts f "
        "LEFT JOIN judge_attempts a ON a.fact_id = f.id "
        "WHERE f.quarantined_at IS NOT NULL AND f.review_dismissed_at IS NULL "
        "AND f.invalidated_at IS NULL "
        "AND (a.attempted_at IS NULL OR a.attempted_at < ?) "
        "AND (f.quarantine_reason LIKE ? OR f.quarantine_reason = ?) "
        "AND instr(f.quarantine_reason, '; ') = 0 "   # single-flag rows only
        "ORDER BY f.id LIMIT ?",
        (now - ATTEMPT_INTERVAL, GROUNDING_PREFIX + "%", PERSONA_REASON,
         BATCH_LIMIT)).fetchall()
    cleared = relabelled = 0
    for fact in rows:
        con.execute(
            "INSERT INTO judge_attempts(fact_id, attempted_at) VALUES(?,?) "
            "ON CONFLICT(fact_id) DO UPDATE SET attempted_at=excluded.attempted_at",
            (fact["id"], now))
        try:
            if fact["quarantine_reason"].startswith(GROUNDING_PREFIX):
                cleared += bool(_judge_grounding(con, settings, fact))
            else:
                relabelled += bool(_judge_persona(con, settings, fact))
        except Exception as exc:  # fail closed: key, endpoint, anything
            log.warning("judge: fact %s left held (%s)", fact["id"], exc)
    con.commit()
    if rows:
        log.info("judge pass: %d examined, %d cleared, %d relabelled, %d left held",
                 len(rows), cleared, relabelled,
                 len(rows) - cleared - relabelled)
    return {"enabled": True, "examined": len(rows),
            "cleared": cleared, "relabelled": relabelled}


def start_scheduler(settings) -> threading.Event:
    """Startup plus hourly, the FTS auto-repair cadence. Returns a stop
    Event; disabled means the Event is returned and nothing runs."""
    stop = threading.Event()
    if not settings.judge_pass:
        return stop

    def _loop():
        while True:
            try:
                con = db.connect(settings.db_path)
                try:
                    run_pass(con, settings)
                finally:
                    con.close()
            except Exception:
                log.exception("judge pass failed; will retry next interval")
            if stop.wait(3600):
                return

    threading.Thread(target=_loop, daemon=True, name="judge-pass").start()
    return stop
