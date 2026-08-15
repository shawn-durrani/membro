"""Person records: the fleet's identity home (#33, design on the issue).

Apps capture voices and assert who spoke; membro records it durably. This
module holds the logic behind the /v1/persons routes: create-or-update,
alias rules, clip storage, the guest-fact link, and forget.

The five owner decisions (2026-08-14) this implements:
- every uploaded clip is kept (no server-side keep-best);
- persons are created only by capture apps (the admin surface renames,
  merges and forgets - it does not create);
- a guest fact links to a person always on introduced/owner-correction,
  and on voice-match only at confidence 0.8+ (the link itself arrives
  with the wire identity field; the alias-based backfill below covers
  existing facts);
- forget also moves that person's approved facts back into review as one
  person-forgotten group - nothing is silently deleted;
- relationship is owner-set free text.

Rules enforced here, not left to callers:
- an alias points at exactly one person; a collision is refused, never
  guessed;
- an owner-set display name survives client upserts;
- a name or alias membro has already seen as a MODEL speaker label in
  that app is refused outright - the crossband participant boundary
  (#65/#77), backstopped server-side;
- clip bytes live under data_dir/voice_anchors/, dir 0700, files 0600,
  content-addressed by sha256 so the same clip is never stored twice.
"""

import hashlib
import os
import time

from . import db, walls

DIR_NAME = "voice_anchors"

# Sources a human stood behind - mirrors crossband's vouch sources (#83).
HUMAN_METHODS = ("introduction", "owner-correction")


def clips_dir(settings):
    d = settings.data_dir / DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _row(con, slug):
    return con.execute("SELECT * FROM persons WHERE slug=?",
                       (slug,)).fetchone()


def out(con, person) -> dict:
    """One person as the API returns it: the row plus aliases and a clip
    count. No audio and no fact content."""
    aliases = [dict(r) for r in con.execute(
        "SELECT alias, kind FROM person_aliases WHERE person_id=? "
        "ORDER BY alias", (person["id"],))]
    clips = con.execute("SELECT COUNT(*) AS n FROM voice_anchors "
                        "WHERE person_id=?", (person["id"],)).fetchone()["n"]
    return {"slug": person["slug"], "display_name": person["display_name"],
            "name_owner_set": bool(person["name_owner_set"]),
            "relationship": person["relationship"],
            "created_at": person["created_at"],
            "origin_client": person["origin_client"],
            "merged_into": person["merged_into"],
            "forgotten_at": person["forgotten_at"],
            "aliases": aliases, "clip_count": clips}


def model_label_collision(con, name: str, source_app: str = "") -> bool:
    """The participant boundary's server-side backstop (#65/#77): has this
    name ever been seen as a MODEL speaker label? A person may never be
    created under a name the fleet uses for an AI seat."""
    like = (name or "").strip()
    if not like:
        return False
    rows = con.execute(
        "SELECT DISTINCT m.speaker FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id "
        "WHERE m.speaker = ? COLLATE NOCASE "
        + ("AND c.source_app = ?" if source_app else ""),
        ([like, source_app] if source_app else [like])).fetchall()
    return any(walls.speaker_class(r["speaker"]) == "model" for r in rows)


def upsert(con, settings, *, slug: str, display_name: str,
           aliases: list | None = None, relationship: str | None = None,
           origin_client: str = "") -> dict:
    """Create or update a person by slug (capture apps call this; the
    admin surface never creates). Owner-set names win: a client upsert
    updates the display name only while the owner has not renamed.
    Aliases are combined; one that already belongs to a DIFFERENT person
    raises ValueError rather than being reassigned."""
    display_name = " ".join((display_name or "").split())
    if not slug or not display_name:
        raise ValueError("slug and display_name are required")
    person = _row(con, slug)
    wanted = {display_name} | {a for a in (aliases or []) if a}
    for name in wanted:
        if model_label_collision(con, name, origin_client):
            raise ValueError(
                f"{name!r} is a model speaker label in this app - an AI "
                "participant can never become a person (#65)")
    if person is None:
        now = time.time()
        con.execute(
            "INSERT INTO persons(slug, display_name, relationship, "
            "created_at, updated_at, origin_client) VALUES(?,?,?,?,?,?)",
            (slug, display_name, relationship or "", now, now,
             origin_client))
        person = _row(con, slug)
    else:
        if not person["name_owner_set"]:
            con.execute("UPDATE persons SET display_name=? WHERE id=?",
                        (display_name, person["id"]))
        if relationship is not None and not person["relationship"]:
            con.execute("UPDATE persons SET relationship=? WHERE id=?",
                        (relationship, person["id"]))
    for alias in wanted:
        owner = con.execute(
            "SELECT person_id FROM person_aliases WHERE alias=? COLLATE NOCASE",
            (alias,)).fetchone()
        if owner and owner["person_id"] != person["id"]:
            other = con.execute("SELECT slug FROM persons WHERE id=?",
                                (owner["person_id"],)).fetchone()
            raise ValueError(
                f"alias {alias!r} already belongs to "
                f"{other['slug'] if other else 'another person'} - refusing "
                "to reassign a name")
        if not owner:
            con.execute(
                "INSERT INTO person_aliases(person_id, alias) VALUES(?,?)",
                (person["id"], alias))
    linked = link_guest_facts(con, _row(con, slug))
    con.execute("UPDATE persons SET updated_at=? WHERE id=?",
                (time.time(), person["id"]))
    con.commit()
    result = out(con, _row(con, slug))
    result["facts_linked"] = linked
    return result


def link_guest_facts(con, person) -> int:
    """The migration-step-3 link, applied whenever a person gains aliases:
    an existing guest fact whose source message was spoken by
    guest:<alias> links to this person. Only unlinked facts; a name that
    is ambiguous across persons cannot happen (aliases are unique)."""
    aliases = [r["alias"] for r in con.execute(
        "SELECT alias FROM person_aliases WHERE person_id=?",
        (person["id"],))]
    if not aliases:
        return 0
    labels = [f"guest:{a}" for a in aliases]
    q = ",".join("?" for _ in labels)
    cur = con.execute(
        f"UPDATE facts SET person_id=? WHERE person_id IS NULL AND "
        f"source_message_id IN (SELECT id FROM messages WHERE speaker "
        f"COLLATE NOCASE IN ({q}))", [person["id"]] + labels)
    return cur.rowcount


def add_clip(con, settings, person, *, data: bytes, seconds: float = 0,
             score: float = 0, source: str = "", captured_at: float = 0,
             client: str = "") -> dict:
    """Store one clip, content-addressed. The same bytes for the same
    person is a no-op ({'deduped': True})."""
    sha = hashlib.sha256(data).hexdigest()
    dup = con.execute(
        "SELECT id FROM voice_anchors WHERE person_id=? AND sha256=?",
        (person["id"], sha)).fetchone()
    if dup:
        return {"deduped": True, "anchor_id": dup["id"]}
    stored = f"{sha}.wav"
    path = clips_dir(settings) / stored
    if not path.exists():
        path.write_bytes(data)
        os.chmod(path, 0o600)
    cur = con.execute(
        "INSERT INTO voice_anchors(person_id, sha256, stored_name, seconds, "
        "score, source, captured_at, client) VALUES(?,?,?,?,?,?,?,?)",
        (person["id"], sha, stored, seconds, score, source,
         captured_at or time.time(), client))
    con.commit()
    return {"deduped": False, "anchor_id": cur.lastrowid}


def forget(con, settings, person) -> dict:
    """The one-press forget, exactly the numbered steps on the issue:
    delete the audio (journalled, content-free), mark the person
    forgotten, move their approved facts back into review as one
    person-forgotten group (owner decision 4 - nothing silently
    deleted), and report what happened. Clip files are shared only by
    sha collision within the same store, so each stored file whose last
    row is gone is unlinked."""
    rows = con.execute("SELECT id, stored_name FROM voice_anchors "
                       "WHERE person_id=?", (person["id"],)).fetchall()
    con.execute("DELETE FROM voice_anchors WHERE person_id=?",
                (person["id"],))
    removed = 0
    for r in rows:
        shared = con.execute(
            "SELECT 1 FROM voice_anchors WHERE stored_name=? LIMIT 1",
            (r["stored_name"],)).fetchone()
        if not shared:
            (clips_dir(settings) / r["stored_name"]).unlink(missing_ok=True)
            removed += 1
    now = time.time()
    con.execute("UPDATE persons SET forgotten_at=?, updated_at=? WHERE id=?",
                (now, now, person["id"]))
    held = con.execute(
        "UPDATE facts SET quarantined_at=?, quarantine_reason=?, "
        "review_dismissed_at=NULL WHERE person_id=? "
        "AND invalidated_at IS NULL AND quarantined_at IS NULL",
        (now, f"person-forgotten: {person['display_name']} was forgotten "
              "by the owner - re-review each fact", person["id"])).rowcount
    db.journal_erasure(
        con, "voice",
        f"person:{person['slug']} clips:{len(rows)} files_removed:{removed}")
    con.commit()
    return {"forgotten": person["slug"], "clips_deleted": len(rows),
            "files_removed": removed, "facts_held": held}


def rename(con, person, display_name: str, relationship=None) -> dict:
    """The owner renames (admin surface): sets the name AND the owner-set
    flag, so no client upsert can change it again. Relationship is
    owner-set free text (decision 5)."""
    display_name = " ".join((display_name or "").split())
    if not display_name:
        raise ValueError("a name is required")
    now = time.time()
    con.execute("UPDATE persons SET display_name=?, name_owner_set=1, "
                "updated_at=? WHERE id=?",
                (display_name, now, person["id"]))
    if relationship is not None:
        con.execute("UPDATE persons SET relationship=? WHERE id=?",
                    (relationship, person["id"]))
    con.commit()
    return out(con, con.execute("SELECT * FROM persons WHERE id=?",
                                (person["id"],)).fetchone())


def move_clip(con, settings, person, anchor_id: int, to_person) -> dict:
    """Re-point one clip to the right person - the owner's correction
    (or crossband replaying one made there). The bytes stay; only the
    attribution changes. Refused onto a forgotten person."""
    if to_person["forgotten_at"]:
        raise ValueError("cannot move a clip to a forgotten person")
    row = con.execute(
        "SELECT id, sha256 FROM voice_anchors WHERE id=? AND person_id=?",
        (anchor_id, person["id"])).fetchone()
    if not row:
        return {"moved": False, "reason": "no such clip"}
    dup = con.execute(
        "SELECT id FROM voice_anchors WHERE person_id=? AND sha256=?",
        (to_person["id"], row["sha256"])).fetchone()
    now = time.time()
    if dup:
        # the target already holds these bytes: the move collapses to a
        # delete of the mis-attributed row
        con.execute("DELETE FROM voice_anchors WHERE id=?", (row["id"],))
    else:
        con.execute("UPDATE voice_anchors SET person_id=? WHERE id=?",
                    (to_person["id"], row["id"]))
    con.execute("UPDATE persons SET updated_at=? WHERE id IN (?, ?)",
                (now, person["id"], to_person["id"]))
    con.commit()
    return {"moved": True, "to": to_person["slug"]}


def delete_clip(con, settings, person, anchor_id: int) -> dict:
    """Delete one clip - the owner's judgement that this audio should not
    exist under this person (or crossband replaying that judgement).
    Journalled like every erasure; bytes unlinked when no other row
    shares them."""
    row = con.execute(
        "SELECT id, sha256, stored_name FROM voice_anchors "
        "WHERE id=? AND person_id=?", (anchor_id, person["id"])).fetchone()
    if not row:
        return {"deleted": False, "reason": "no such clip"}
    con.execute("DELETE FROM voice_anchors WHERE id=?", (row["id"],))
    shared = con.execute(
        "SELECT 1 FROM voice_anchors WHERE stored_name=? LIMIT 1",
        (row["stored_name"],)).fetchone()
    removed = False
    if not shared:
        (clips_dir(settings) / row["stored_name"]).unlink(missing_ok=True)
        removed = True
    con.execute("UPDATE persons SET updated_at=? WHERE id=?",
                (time.time(), person["id"]))
    db.journal_erasure(con, "voice",
                       f"clip:{row['sha256'][:12]} person:{person['slug']} "
                       f"file_removed:{removed}")
    con.commit()
    return {"deleted": True, "file_removed": removed}


def merge(con, settings, loser, winner) -> dict:
    """Fold LOSER into WINNER - aliases, clips and fact links re-point,
    the loser row stays with merged_into set (supersede, never rewrite).
    Refused when either is forgotten. Crossband merges replay through
    this too, so a merged-away membro record can never resurrect stale
    clips into a rebuild."""
    if loser["id"] == winner["id"]:
        raise ValueError("cannot merge a person into themselves")
    if loser["forgotten_at"] or winner["forgotten_at"]:
        raise ValueError("cannot merge a forgotten person")
    now = time.time()
    con.execute("UPDATE person_aliases SET person_id=? WHERE person_id=?",
                (winner["id"], loser["id"]))
    # clips the winner already holds (same bytes) would violate the
    # per-person sha uniqueness - drop the loser's duplicate rows first
    con.execute(
        "DELETE FROM voice_anchors WHERE person_id=? AND sha256 IN "
        "(SELECT sha256 FROM voice_anchors WHERE person_id=?)",
        (loser["id"], winner["id"]))
    con.execute("UPDATE voice_anchors SET person_id=? WHERE person_id=?",
                (winner["id"], loser["id"]))
    con.execute("UPDATE facts SET person_id=? WHERE person_id=?",
                (winner["id"], loser["id"]))
    con.execute("UPDATE persons SET merged_into=?, updated_at=? WHERE id=?",
                (winner["id"], now, loser["id"]))
    con.execute("UPDATE persons SET updated_at=? WHERE id=?",
                (now, winner["id"]))
    con.commit()
    return {"merged": loser["slug"], "into": winner["slug"]}
