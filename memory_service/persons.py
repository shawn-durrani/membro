"""Person records: the fleet's identity home (#33, design on the issue).

Apps capture voices and assert who spoke; membro records it durably. This
module holds the logic behind the /v1/persons routes: create-or-update,
alias rules, clip storage, the guest-fact link, and forget.

The five owner decisions (2026-08-14) this implements:
- membro never prunes clips by itself (no server-side keep-best). Since
  the owner's decision of 2026-09-27 ("only keep the best"), a capture
  app sends the clips it drops as ordinary clip deletes, and the clips
  it no longer uses are counted against its kept-set manifest (contract
  1.8, #127) and deleted only on the owner's press;
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
import json
import os
import re
import time

from . import db, walls

DIR_NAME = "voice_anchors"

# Methods a human stood behind - mirrors crossband's vouch sources (#83).
HUMAN_METHODS = ("introduced", "owner-correction")
VOICE_MATCH_MIN_CONFIDENCE = 0.8   # owner decision 3 (membro#33)
# Why a capture app dropped a clip (contract 1.8, #127): journalled on the
# clip delete it sends. Anything else is ignored, never stored.
DROP_REASONS = ("rotation", "settled", "set-aside")
MANIFEST_MAX = 1000                # sha256s in one kept-set manifest
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# The journal refs _erase_anchor and forget write, read back by the
# snapshot restore's replay (#129). A slug runs up to the next field, so
# one with a space in it still parses.
CLIP_REF = re.compile(
    r"^clip:([0-9a-f]{12}) person:(.+?) file_removed:(True|False)(?: |$)")
FORGET_REF = re.compile(r"^person:(.+?) clips:\d+ files_removed:\d+$")


def binding(con, identity) -> int | None:
    """The person a fact should link to, per owner decision 3: a
    human-confirmed identity (introduced / owner-correction) always binds;
    a voice-match binds at confidence 0.8 or above; by-elimination and
    anything unknown never auto-binds - those stay for the review queue's
    human. A merged-away slug resolves to its winner; a forgotten or
    unknown slug binds nothing."""
    if not identity or not identity.get("person"):
        return None
    method = identity.get("method") or ""
    if method not in HUMAN_METHODS and not (
            method == "voice-match"
            and (identity.get("confidence") or 0) >= VOICE_MATCH_MIN_CONFIDENCE):
        return None
    person = _row(con, identity["person"])
    for _ in range(8):                     # follow merges, bounded
        if person is None or person["forgotten_at"]:
            return None
        if not person["merged_into"]:
            return person["id"]
        person = con.execute("SELECT * FROM persons WHERE id=?",
                             (person["merged_into"],)).fetchone()
    return None


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
            # The change stamp the delta pull filters on. GET /v1/persons
            # accepts ?since= against this column, but the projection never
            # carried it - so a syncing app could not learn the newest stamp
            # it saw, its watermark sat at zero forever, and every pass
            # re-read everyone. Additive; absent meant exactly that bug.
            "updated_at": person["updated_at"],
            "origin_client": person["origin_client"],
            "merged_into": person["merged_into"],
            "forgotten_at": person["forgotten_at"],
            "aliases": aliases, "clip_count": clips,
            # 1.8 (#127): stored clips the capture app's kept-set manifest
            # leaves out. Counted only, never deleted from here.
            "unused_clips": len(unused_clips(con, person))}


def grounding_names(con) -> set[str]:
    """Lowercased names, aliases, and their word tokens for every active
    person — the personal half of the grounding allowlist (#57). The miner
    may call a person by any name membro itself holds for them ("Alex"
    spoken, "Alexandra" written), and the wall must not read that as an
    invented entity. Personal nouns never live in code or committed config;
    this reads them from the same table the People page manages."""
    rows = con.execute(
        "SELECT p.display_name AS n FROM persons p "
        "WHERE p.merged_into IS NULL AND p.forgotten_at IS NULL "
        "UNION "
        "SELECT a.alias AS n FROM person_aliases a "
        "JOIN persons p ON p.id = a.person_id "
        "WHERE p.merged_into IS NULL AND p.forgotten_at IS NULL").fetchall()
    names: set[str] = set()
    for r in rows:
        name = (r["n"] or "").strip().lower()
        if not name:
            continue
        names.add(name)
        names.update(t for t in name.split() if len(t) >= 2)
    return names


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


def store_manifest(con, person, *, client: str, shas: list) -> dict:
    """Keep a capture app's kept-set manifest for one person (contract 1.8,
    #127): the content addresses of the clips its bank holds. Replaces the
    app's last one. Deletes nothing: the manifest only feeds the count the
    People page shows and the owner's delete. Raises ValueError on a value
    that isn't a sha256 or a list over MANIFEST_MAX."""
    client = (client or "").strip()
    if not client:
        raise ValueError("a manifest names the client that sent it")
    if len(shas) > MANIFEST_MAX:
        raise ValueError(f"a manifest holds at most {MANIFEST_MAX} clips")
    keep = sorted({str(s).lower() for s in shas})
    if not all(SHA256.match(s) for s in keep):
        raise ValueError("every manifest entry must be a sha256 hex digest")
    upto = con.execute("SELECT COALESCE(MAX(id), 0) AS n "
                       "FROM voice_anchors").fetchone()["n"]
    con.execute(
        "INSERT INTO clip_manifests(person_id, client, shas, anchor_upto, "
        "received_at) VALUES(?,?,?,?,?) ON CONFLICT(person_id, client) DO "
        "UPDATE SET shas=excluded.shas, anchor_upto=excluded.anchor_upto, "
        "received_at=excluded.received_at",
        (person["id"], client, json.dumps(keep), upto, time.time()))
    con.commit()
    return {"stored": len(keep), "unused_clips": len(unused_clips(con, person))}


def unused_clips(con, person) -> list:
    """The stored clips a capture app no longer uses, by its manifest: rows
    that app uploaded, stored before the manifest arrived, and missing from
    it. A manifest older than the person's last change (a clip moved in or
    deleted, a merge, a rename) judges nothing until the app sends a fresh
    one, and a person with no manifest has no unused clips. Reads only."""
    person = con.execute("SELECT * FROM persons WHERE id=?",
                         (person["id"],)).fetchone()
    if person is None or person["forgotten_at"]:
        return []
    out = []
    for m in con.execute("SELECT * FROM clip_manifests WHERE person_id=?",
                         (person["id"],)).fetchall():
        if (person["updated_at"] or 0) > m["received_at"]:
            continue
        keep = set(json.loads(m["shas"]))
        out += [r for r in con.execute(
            "SELECT id, sha256, stored_name FROM voice_anchors "
            "WHERE person_id=? AND client=? AND id<=? ORDER BY id",
            (person["id"], m["client"], m["anchor_upto"]))
            if r["sha256"] not in keep]
    return out


def delete_unused(con, settings, person) -> dict:
    """The owner's press on the People page: delete one person's clips the
    capture app no longer uses. The set is worked out again here, never
    taken from the caller. Each clip goes through the clip eraser's own
    steps and journals its own content-free row, marked reason:unused."""
    rows = unused_clips(con, person)
    unlink = [_erase_anchor(con, person, r, "unused") for r in rows]
    if rows:
        con.execute("UPDATE persons SET updated_at=? WHERE id=?",
                    (time.time(), person["id"]))
    con.commit()
    removed = _unlink(settings, unlink)
    return {"slug": person["slug"], "deleted": len(rows),
            "files_removed": removed}


def delete_all_unused(con, settings) -> dict:
    """The owner's one press for everyone: delete_unused for each person
    membro hasn't forgotten."""
    deleted = removed = people = 0
    for person in con.execute("SELECT * FROM persons WHERE forgotten_at IS "
                              "NULL ORDER BY id").fetchall():
        r = delete_unused(con, settings, person)
        if r["deleted"]:
            people += 1
            deleted += r["deleted"]
            removed += r["files_removed"]
    return {"deleted": deleted, "files_removed": removed, "persons": people}


def _erase_anchor(con, person, row, reason: str = ""):
    """Delete one clip row and journal it, content-free, in the caller's
    transaction. Returns the stored file name when no other row shares
    those bytes, for the caller to unlink once it has committed."""
    con.execute("DELETE FROM voice_anchors WHERE id=?", (row["id"],))
    shared = con.execute(
        "SELECT 1 FROM voice_anchors WHERE stored_name=? LIMIT 1",
        (row["stored_name"],)).fetchone()
    ref = (f"clip:{row['sha256'][:12]} person:{person['slug']} "
           f"file_removed:{not shared}")
    if reason:
        ref += f" reason:{reason}"
    db.journal_erasure(con, "voice", ref)
    return None if shared else row["stored_name"]


def _unlink(settings, names) -> int:
    removed = 0
    for name in names:
        if name:
            (clips_dir(settings) / name).unlink(missing_ok=True)
            removed += 1
    return removed


def forget(con, settings, person,
           replay: tuple[float, str] | None = None) -> dict:
    """The one-press forget, exactly the numbered steps on the issue:
    delete the audio (journalled, content-free), mark the person
    forgotten, move their approved facts back into review as one
    person-forgotten group (owner decision 4 - nothing silently
    deleted), and report what happened. Clip files are shared only by
    sha collision within the same store, so each stored file whose last
    row is gone is unlinked.

    `replay` is only passed by the snapshot restore (#129): the time and
    ref of the journal row it is replaying. The person is marked
    forgotten at that time and the same row is journalled again, so a
    later restore finds the journal whole. The files are left alone: the
    owner's forget already removed each one no other clip shared."""
    rows = con.execute("SELECT id, stored_name FROM voice_anchors "
                       "WHERE person_id=?", (person["id"],)).fetchall()
    con.execute("DELETE FROM voice_anchors WHERE person_id=?",
                (person["id"],))
    removed = 0
    if replay is None:
        for r in rows:
            shared = con.execute(
                "SELECT 1 FROM voice_anchors WHERE stored_name=? LIMIT 1",
                (r["stored_name"],)).fetchone()
            if not shared:
                (clips_dir(settings) / r["stored_name"]).unlink(missing_ok=True)
                removed += 1
    now = time.time()
    con.execute("UPDATE persons SET forgotten_at=?, updated_at=? WHERE id=?",
                (replay[0] if replay else now, now, person["id"]))
    # The kept-set manifests go too: hashes of audio that no longer exists.
    con.execute("DELETE FROM clip_manifests WHERE person_id=?",
                (person["id"],))
    held = con.execute(
        "UPDATE facts SET quarantined_at=?, quarantine_reason=?, "
        "review_dismissed_at=NULL WHERE person_id=? "
        "AND invalidated_at IS NULL AND quarantined_at IS NULL",
        (now, f"person-forgotten: {person['display_name']} was forgotten "
              "by the owner - re-review each fact", person["id"])).rowcount
    if replay:
        db.journal_erasure(con, "voice", replay[1], ts=replay[0])
    else:
        db.journal_erasure(
            con, "voice",
            f"person:{person['slug']} clips:{len(rows)} files_removed:{removed}")
    con.commit()
    return {"forgotten": person["slug"], "clips_deleted": len(rows),
            "files_removed": removed, "facts_held": held}


def replay_erasure(con, settings, ref: str, ts: float) -> str | None:
    """Replay one journalled voice erasure onto a restored snapshot (#129):
    a clip delete, whatever its reason, or a forget. Returns "clip" or
    "person" when the restored copy still held what was erased, and None
    when it didn't, which the restore journals as already absent. A
    replayed row is journalled under its own time and ref.

    The journal names a clip by its person and the first 12 characters of
    its sha256. When the delete removed the file, no row anywhere held
    those bytes afterwards, so every row carrying them goes. That also
    catches a clip moved or merged to another person after the snapshot,
    which the restored copy still files under its old owner. When the
    file stayed, another clip still used it, so only the named person's
    row goes. A prefix that matches two different sha256s is left alone:
    a 1 in 2^48 chance, and the restore's missing-file check still
    names either row if its audio is gone.

    Rows only. The owner's delete already removed the file, or left it
    for a clip that shares it."""
    m = CLIP_REF.match(ref)
    if m:
        prefix, slug, file_removed = m.groups()
        rows = con.execute(
            "SELECT a.id, a.person_id, a.sha256, p.slug FROM voice_anchors a "
            "JOIN persons p ON p.id = a.person_id "
            "WHERE substr(a.sha256, 1, 12)=?", (prefix,)).fetchall()
        if len({r["sha256"] for r in rows}) != 1:
            return None
        if file_removed == "False":
            rows = [r for r in rows if r["slug"] == slug]
            if not rows:
                return None
        now = time.time()
        for r in rows:
            con.execute("DELETE FROM voice_anchors WHERE id=?", (r["id"],))
            # a manifest older than the change judges nothing (#127)
            con.execute("UPDATE persons SET updated_at=? WHERE id=?",
                        (now, r["person_id"]))
        db.journal_erasure(con, "voice", ref, ts=ts)
        con.commit()
        return "clip"
    m = FORGET_REF.match(ref)
    if m:
        person = _row(con, m.group(1))
        if person is None or person["forgotten_at"]:
            return None
        forget(con, settings, person, replay=(ts, ref))
        return "person"
    return None


def clips_missing_files(con, settings) -> list[dict]:
    """Clip rows whose audio file isn't on disk, oldest first, as the
    person's slug and the clip's id: content-free. Reads only."""
    d = settings.data_dir / DIR_NAME
    return [{"person": r["slug"], "clip": r["id"]} for r in con.execute(
        "SELECT a.id, a.stored_name, p.slug FROM voice_anchors a "
        "JOIN persons p ON p.id = a.person_id ORDER BY a.id")
        if not (d / r["stored_name"]).is_file()]


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


def delete_clip(con, settings, person, anchor_id: int,
                reason: str = "") -> dict:
    """Delete one clip - the owner's judgement that this audio should not
    exist under this person (or crossband replaying that judgement).
    Journalled like every erasure; bytes unlinked when no other row
    shares them. `reason` (contract 1.8, #127) is why a capture app
    dropped the clip, one of DROP_REASONS, journalled on the same row;
    any other value is ignored."""
    row = con.execute(
        "SELECT id, sha256, stored_name FROM voice_anchors "
        "WHERE id=? AND person_id=?", (anchor_id, person["id"])).fetchone()
    if not row:
        return {"deleted": False, "reason": "no such clip"}
    name = _erase_anchor(con, person, row,
                         reason if reason in DROP_REASONS else "")
    con.execute("UPDATE persons SET updated_at=? WHERE id=?",
                (time.time(), person["id"]))
    con.commit()
    return {"deleted": True, "file_removed": bool(_unlink(settings, [name]))}


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
