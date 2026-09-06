"""The summary — a fast cache over the ledger, never the source of truth.

Rebuilt from currently-valid, non-quarantined facts; the fact ids that fed each
build are recorded for claim→fact traceability (contract invariant 6).

Every generated profile is also appended to summary_versions — regeneration is
never destructive, and any prior version can be restored (the restore itself
appends; history is never rewritten). The settings keys hold the CURRENT
profile, as ever; versions are the memory of the summary itself.
"""

import datetime
import json
import logging

from . import db, embeddings, llm, weighting

log = logging.getLogger("memory_service.summary")

BUDGET_TOLERANCE = 1.2  # accept up to 20% over budget; beyond that, one rewrite
# A draft under its floor is expanded only when the selected entries carry at
# least this many times the floor in words of their own. A thin ledger has
# nothing to expand into, and a model asked to fill a range the entries
# cannot fill reaches for filler; a short profile is the better outcome.
FILL_MATERIAL = 2.0


def provenance_tag(fact: dict) -> str:
    """Coarse, MECHANICALLY grounded provenance — never a guess at who
    specifically said something.

    - 'direct': origin_agent IS the literal 'user' sentinel (ledger.is_trusted
      treats it identically) — the owner saved this themselves.
    - 'mined': source IS 'chat' — mining.py sets this, and only this, when a
      fact is distilled from a multi-turn conversation chunk. No single turn
      or speaker is recorded for a mined fact, so it must never be written up
      as "the user said" or attributed to any named participant.
    - anything else: the raw origin_agent verbatim (a participant's own slug,
      an approved mcp:<client> write, etc). We do NOT flatten this into an
      invented bucket like "curated" — that would claim more certainty about
      who authored it than the record actually supports.
    """
    if fact.get("origin_agent") == "user":
        return "direct"
    if fact.get("source") == "chat":
        return "mined"
    return fact.get("origin_agent") or "unknown"


def _provenance_entry(fact: dict) -> dict:
    return {"id": fact["id"], "origin_agent": fact.get("origin_agent"),
            "source": fact.get("source"), "tag": provenance_tag(fact)}


def _load_provenance(con, ids: list[int]) -> list[dict]:
    """Re-derive structured provenance for a known list of fact ids straight
    from the ledger — origin_agent/source are set once at write time and never
    edited (PATCH covers content/event_date/confidence/importance only), so
    this is exact, not a guess. Used by restore(), which only has ids to work
    from, not full fact rows. An id since hard-deleted by the owner (the one
    destructive path, human-only) is simply absent rather than an error."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT id, origin_agent, source FROM facts WHERE id IN ({placeholders})",
        ids).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [_provenance_entry(by_id[i]) for i in ids if i in by_id]


def get(con) -> dict:
    raw = db.get_setting(con, "summary_sources")
    sources = json.loads(raw) if raw else {}
    return {
        "summary": db.get_setting(con, "summary"),
        "generated_at": sources.get("generated_at"),
        "source_fact_ids": sources.get("fact_ids", []),
        # Structured, per-fact provenance — a downstream consumer can
        # check who/what a claim traces back to WITHOUT trusting unlabelled
        # prose for attribution. Additive field: older summary_sources rows
        # (generated before this shipped) simply have none, so default empty
        # rather than error.
        "provenance": sources.get("provenance", []),
    }


def regenerate(con, settings) -> str:
    try:
        embeddings.ensure_fact_embeddings(con, settings)
    except Exception:
        pass
    durable, active = weighting.select_for_summary(con)
    facts = durable + active
    if not facts:
        db.set_setting(con, "summary", "")
        db.set_setting(con, "summary_sources", json.dumps(
            {"generated_at": db.now(), "fact_ids": [], "provenance": []}))
        con.commit()
        return ""
    words = settings.memory_summary_words
    # Models follow local caps far better than one global number — a
    # per-section ceiling distributes the trimming so no section balloons
    # while unequal spend between sections stays possible.
    per_section = max(150, words // 4)
    # The floor (#96). A ceiling alone reads as "stop early" to a model also
    # told not to pad, and the profile settled at about half its budget with
    # a ledger that could fill it. The prompt now names a range, floor to
    # ceiling, and a short draft gets one expansion from the same entries.
    # 0 turns the floor off; above 1 reads as 1 (a floor cannot sit above
    # the ceiling).
    fill = max(0.0, min(1.0, settings.memory_summary_fill))
    floor = int(words * fill)

    def block(fs):
        # Each entry carries its event_date (the date it is ABOUT) date-only,
        # so the model can resolve temporal conflicts: when two entries disagree
        # about the same current-state or active-thread status, the later date
        # is the current one (see the latest-wins rule in the prompt).
        # ...and its PROVENANCE tag — mechanically derived (see
        # provenance_tag), never a guessed speaker: 'direct' the owner said it
        # themselves, 'mined' it was distilled from conversation with no
        # recorded speaker, anything else is the raw origin_agent as written.
        def line(f):
            day = datetime.date.fromtimestamp(
                f.get("event_date") or f.get("created_at") or 0).isoformat()
            return f"- [{f['id']} · {day} · {provenance_tag(f)}] {f['content']}"
        return "\n".join(line(f) for f in fs) or "(none)"

    # The spine encodes the load-bearing structure (stable → volatile, per
    # MemGPT-style separation); the MIDDLE of the profile self-organizes around
    # what the facts actually cluster into (A-MEM / Generative-Agents-reflection
    # style) — see docs/REFERENCES.md.
    #
    # This shipped 2026-07-09 behind `summary_emergent_topics` as a reversible
    # experiment and GRADUATED 2026-07-26: the flag is gone and this is
    # simply how the profile is written. To revert, restore the fixed layout the
    # flag used to select:
    #     "under these markdown headings, including only sections that have
    #      content: Identity, Work & Projects, Preferences, Relationships &
    #      People, Goals & Active Threads, Recent Changes. "
    headings = (
        "under markdown headings in this order: Identity, Preferences, "
        "Relationships & People — then 2 to 5 TOPIC sections of your own — "
        "then Goals & Active Threads, Recent Changes. You name the topic "
        "sections from what the entries genuinely cluster around (a "
        "project, a hobby, a career thread, a place); create only topics "
        "that earn their space, and let a topic vanish when its facts "
        "fade. Include only sections that have content. "
    )
    # How to read the entries. This travels with them on EVERY request that
    # carries them: the expansion pass is a fresh call with no memory of the
    # first, so it needs the same latest-wins and provenance rules, verbatim.
    reading = (
        f"Below are selected entries from the memory ledger about "
        f"{settings.user_name}, in two groups (each oldest first; later entries "
        "reflect more recent information). Each entry is prefixed with "
        "[id · date], where date is the day the entry is ABOUT. DURABLE entries "
        "are long-standing, identity-level facts; ACTIVE entries are recent "
        "context and live threads — give current threads their space rather "
        "than compressing them under old history. "
        "LATEST WINS: when two or more entries conflict about the same "
        "current-state or active-thread status (e.g. a job, a location, or "
        "whether a thread is warm/live/waiting), the entry with the later date "
        "is the current one; state only that as current, and never assert an "
        "older, superseded status as if it still holds — mention it, if at all, "
        "only as past history. "
        "PROVENANCE (read carefully, this is not optional): each entry's "
        "final bracket segment is its provenance tag. 'direct' means the owner "
        "stated it themselves. 'mined' means it was distilled from a "
        "multi-turn conversation — NO single speaker or turn is recorded for "
        "it, so you must NEVER write it up as \"the user said\", \"they told "
        "me\", or attribute it to any named person or AI participant; state it "
        "as a plain fact instead (e.g. write \"trains for a triathlon\", never "
        "\"said they train for a triathlon\"). Any other tag is the literal "
        "origin recorded at write time (a participant's own name, or a tool) — "
        "treat it only as that raw label, never dress it up into a fuller "
        "claim about who specifically said something. Mention provenance in "
        "your prose at all only when it is materially relevant (e.g. flagging "
        "something as told to you directly vs. inferred); most entries need no "
        "provenance language whatsoever. "
    )
    # What the room is for. A ceiling alone leaves a model free to stop at
    # generalities; the range is filled with what the entries already hold.
    specifics = ("dates, the names of projects and places, numbers, and the "
                 "current state of each thread")
    entries = (f"## Durable entries\n{block(durable)}\n\n"
               f"## Active entries\n{block(active)}")
    prompt = (
        reading +
        "Write a structured profile summary organized "
        + headings +
        "Be faithful — merge "
        f"and organize, within a HARD budget of {words} words total and no "
        f"more than {per_section} words in any one section. "
        + (f"Aim for between {floor} and {words} words. " if floor else "")
        + f"Spend the room on the specifics the entries carry: {specifics}. "
        "Every section must be present and finished: when trimming is needed, "
        "drop per-fact detail, never a whole section (the later sections are "
        "the most current). The full ledger remains available to readers via "
        "a recall tool: leave out what does not fit, and invent nothing. "
        "Reply with ONLY the profile.\n\n" + entries
    )
    # generous token ceiling: the WORD budget is enforced below by rewriting,
    # never by truncation — a truncated profile silently loses its LAST
    # sections (Goals, Recent Changes — the most current ones), which is far
    # worse than a long one
    max_tokens = max(8000, words * 4)
    text = llm.utility_complete(prompt, settings, max_tokens=max_tokens,
                                model=settings.summary_model)
    passes = []  # the rewrite passes that shaped this version, in order
    drafted = len(text.split())
    # The floor is a softer promise than the ceiling: it is asked for only
    # when the entries can support it, and a draft still short after one
    # expansion is kept. Fail short, never invented.
    if floor and drafted < floor:
        material = sum(len(f["content"].split()) for f in facts)
        if material >= floor * FILL_MATERIAL:
            expand = (
                reading + entries + "\n\n"
                f"The profile below is {drafted} words, under its target of "
                f"at least {floor} words. Expand it to between {floor} and "
                f"{words} words using ONLY the entries above: add the "
                f"specifics you left out ({specifics}), keep every heading "
                f"and section, keep no more than {per_section} words in any "
                "one section, keep the latest-wins and provenance rules, "
                "invent nothing. Reply with ONLY the profile.\n\n" + text
            )
            try:
                expanded = llm.utility_complete(expand, settings,
                                                max_tokens=max_tokens,
                                                model=settings.summary_model)
            except Exception:
                expanded = ""  # the short-but-faithful draft is still valid
            # A reply shorter than the draft it was asked to expand dropped
            # something, or is not a profile at all; the draft stays.
            if len(expanded.split()) > drafted:
                text = expanded
                passes.append("expand")
            if len(text.split()) < floor:
                log.info("summary: %d words after the expansion pass, under "
                         "the %d-word floor of a %d-word budget; kept as is",
                         len(text.split()), floor, words)
        else:
            log.info("summary: the draft is %d words, under the %d-word "
                     "floor, and its %d entries carry %d words, too few to "
                     "expand from; kept as is",
                     drafted, floor, len(facts), material)
    # The budget is a promise to the user: the setting must mean what it says.
    # One rewrite pass when the draft overshoots; if that fails or is still
    # long, keep the draft — fail long, never short. An expansion that
    # overshoots takes this same pass, so a build never sends more than two
    # rewrites.
    drafted = len(text.split())
    if drafted > words * BUDGET_TOLERANCE:
        squeeze = (
            f"The profile below is {drafted} words — over its {words}-word "
            f"budget. Rewrite it to at most {words} words. Keep every section "
            "present and finished; tighten wording and drop per-fact detail "
            "rather than dropping sections (the later sections are the most "
            "current). Keep the same headings; change no facts. Reply with "
            "ONLY the profile.\n\n" + text
        )
        try:
            squeezed = llm.utility_complete(squeeze, settings,
                                            max_tokens=max_tokens,
                                            model=settings.summary_model)
        except Exception:
            squeezed = ""  # the verbose-but-complete draft is still a valid summary
        if squeezed:
            text = squeezed
            passes.append("squeeze")
    fact_ids = sorted(f["id"] for f in facts)
    # Structured provenance, persisted alongside fact_ids so a
    # downstream consumer can trace any claim back to what it actually traces
    # to — origin_agent and source kept RAW, never flattened into an invented
    # category — rather than trusting the prose tag alone.
    by_id = {f["id"]: f for f in facts}
    provenance = [_provenance_entry(by_id[i]) for i in fact_ids]
    db.set_setting(con, "summary", text)
    db.set_setting(con, "summary_sources", json.dumps({
        "generated_at": db.now(),
        "fact_ids": fact_ids,
        "provenance": provenance,
    }))
    if text:
        _append_version(con, text, fact_ids, settings, passes=passes)
    con.commit()
    return text


def _append_version(con, text, fact_ids, settings, restored_from=None,
                    generated_at=None, model=None, passes=None):
    """`passes` names the rewrite passes that shaped a fresh generation, in
    order ("expand", "squeeze"); None on a restore, which generated nothing,
    and on rows stored before the column existed."""
    con.execute(
        "INSERT INTO summary_versions(generated_at, content, source_fact_ids, "
        "word_count, word_budget, model, restored_from, passes) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (generated_at or db.now(), text, json.dumps(fact_ids), len(text.split()),
         settings.memory_summary_words, model or settings.summary_model,
         restored_from, None if passes is None else json.dumps(passes)))


def _decode_passes(d: dict) -> dict:
    d["passes"] = json.loads(d["passes"]) if d.get("passes") else None
    return d


def versions(con) -> list[dict]:
    """Version metadata, newest first — content deliberately absent (the list
    stays light; fetch one version for its text)."""
    return [_decode_passes(dict(r)) for r in con.execute(
        "SELECT id, generated_at, word_count, word_budget, model, restored_from, "
        "passes FROM summary_versions ORDER BY id DESC")]


def get_version(con, version_id: int) -> dict | None:
    row = con.execute("SELECT * FROM summary_versions WHERE id=?",
                      (version_id,)).fetchone()
    if not row:
        return None
    d = _decode_passes(dict(row))
    d["source_fact_ids"] = json.loads(d["source_fact_ids"] or "[]")
    return d


def restore(con, version_id: int, settings) -> dict | None:
    """Make a prior version current again. Appends a new version row pointing
    at its source — the history itself is never rewritten or deleted."""
    v = get_version(con, version_id)
    if not v:
        return None
    db.set_setting(con, "summary", v["content"])
    db.set_setting(con, "summary_sources", json.dumps({
        "generated_at": db.now(),
        "fact_ids": v["source_fact_ids"],
        "provenance": _load_provenance(con, v["source_fact_ids"]),
    }))
    _append_version(con, v["content"], v["source_fact_ids"], settings,
                    restored_from=version_id, model=v["model"])
    con.commit()
    return get(con)
