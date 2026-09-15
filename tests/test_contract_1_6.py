"""Contract 1.6 (#72): every fact carries a scope, and a recall may name
the caller's conversation. A guest's fact stays in the conversation it came
from; the owner's facts, and everything stored before 1.6, are global.
Additive on 1.5, so the older contract tests keep passing beside these."""

import pytest
from fastapi.testclient import TestClient

from memory_service import db as db_mod
from memory_service import ledger, recall, weighting
from memory_service.api import create_app

APP = "multi-model-chat"


@pytest.fixture
def client(settings, fake_llm):
    app = create_app(settings)
    return TestClient(app, base_url="http://127.0.0.1",
                      headers={"Authorization": f"Bearer {app.state.admin_token}"})


def _ingest(client, conv, speaker, text, title="Kitchen chat"):
    r = client.post("/v1/ingest", json={
        "source_app": APP, "conversation_id": conv, "title": title,
        "messages": [{"external_id": f"{conv}-{speaker}-{len(text)}",
                      "speaker": speaker, "content": text,
                      "created_at": "2026-09-14T10:00:00+10:00"}]})
    assert r.status_code == 200, r.text


def _conv_and_message(settings, conv, speaker):
    c = db_mod.connect(settings.db_path)
    try:
        row = c.execute(
            "SELECT m.id AS mid, c.id AS cid FROM messages m "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.external_id=? AND m.speaker=? ORDER BY m.id DESC LIMIT 1",
            (conv, speaker)).fetchone()
        return row["cid"], row["mid"]
    finally:
        c.close()


def _recall(client, query="", **extra):
    body = {"query": query, "limit": 20}
    body.update(extra)
    return client.post("/v1/recall", json=body).json()["facts"]


# ---- handshake ----

def test_health_speaks_1_6(client):
    assert client.get("/v1/health").json()["contract_version"] == "1.6"


# ---- where a fact is bound at creation ----

def test_the_owners_facts_are_global(con, settings):
    f = ledger.add_fact(con, "Alex keeps the bandsaw blades in the top drawer.",
                        settings)
    assert f["scope"] == "global"
    assert ledger.get_fact(con, f["id"])["scope"] == "global"


def test_a_fact_from_a_guests_turn_is_bound_to_its_conversation(client, settings):
    _ingest(client, "room-1", "guest:Sam", "No coriander for me, ever.")
    cid, mid = _conv_and_message(settings, "room-1", "guest:Sam")
    c = db_mod.connect(settings.db_path)
    try:
        f = ledger.add_fact(c, "Sam never eats coriander.", settings,
                            source="chat", origin_agent=APP, source_app=APP,
                            conversation_id=cid, source_message_id=mid)
        unknown_cid, unknown_mid = cid, None
    finally:
        c.close()
    assert f["scope"] == "conversation"


def test_an_unidentified_guests_turn_binds_too(client, settings):
    _ingest(client, "room-2", "guest:unknown", "Someone here hates coriander.")
    cid, mid = _conv_and_message(settings, "room-2", "guest:unknown")
    c = db_mod.connect(settings.db_path)
    try:
        f = ledger.add_fact(c, "Someone in the room hates coriander.", settings,
                            source="chat", origin_agent=APP, source_app=APP,
                            conversation_id=cid, source_message_id=mid)
    finally:
        c.close()
    assert f["scope"] == "conversation"


def test_a_direct_save_with_guests_present_binds(client, settings):
    _ingest(client, "room-3", "user", "Let's plan dinner.")
    cid, _ = _conv_and_message(settings, "room-3", "user")
    c = db_mod.connect(settings.db_path)
    try:
        f = ledger.add_fact(c, "Dinner on Friday is at Sam's place.", settings,
                            origin_agent="claude-x", source_app=APP,
                            conversation_id=cid, guest_speakers=["guest:Sam"])
        alone = ledger.add_fact(c, "Alex prefers Friday dinners at home.",
                                settings, origin_agent="claude-x",
                                source_app=APP, conversation_id=cid)
    finally:
        c.close()
    assert f["scope"] == "conversation" and alone["scope"] == "global"


def test_a_guest_fact_with_no_conversation_cannot_bind(con, settings):
    f = ledger.add_fact(con, "Sam takes two sugars in tea.", settings,
                        origin_agent="claude-x", source_app=APP,
                        guest_speakers=["guest:Sam"])
    assert f["scope"] == "global"     # nothing to bind it to


def test_a_caller_may_set_the_scope_but_not_invent_one(con, settings):
    f = ledger.add_fact(con, "Alex bikes to work on Tuesdays.", settings,
                        conversation_id=None, scope="global")
    assert f["scope"] == "global"
    with pytest.raises(ValueError):
        ledger.add_fact(con, "Alex bikes to work on Wednesdays.", settings,
                        scope="everywhere")


# ---- what recall and the summary see ----

def _bound_and_global(client, settings, conv="room-r"):
    _ingest(client, conv, "guest:Sam", "I moved to Hobart last spring.")
    cid, mid = _conv_and_message(settings, conv, "guest:Sam")
    c = db_mod.connect(settings.db_path)
    try:
        bound = ledger.add_fact(c, "Sam moved to Hobart last spring.", settings,
                                source="chat", origin_agent=APP, source_app=APP,
                                conversation_id=cid, source_message_id=mid)
        ledger.approve(c, bound["id"])
        glob = ledger.add_fact(c, "Alex moved to Hobart in 2019.", settings)
    finally:
        c.close()
    return cid, bound["id"], glob["id"]


def test_recall_without_a_conversation_returns_global_facts_only(client, settings):
    _, bound, glob = _bound_and_global(client, settings)
    ids = {f["id"] for f in _recall(client, "Hobart")}
    assert glob in ids and bound not in ids
    ids = {f["id"] for f in _recall(client, "")}          # the empty-query path
    assert glob in ids and bound not in ids


def test_recall_from_the_conversation_sees_its_bound_facts(client, settings):
    _, bound, glob = _bound_and_global(client, settings)
    rows = _recall(client, "Hobart", source_app=APP, conversation_id="room-r")
    by_id = {f["id"]: f for f in rows}
    assert bound in by_id and glob in by_id
    assert by_id[bound]["scope"] == "conversation"
    assert by_id[glob]["scope"] == "global"
    rows = _recall(client, "", source_app=APP, conversation_id="room-r")
    assert bound in {f["id"] for f in rows}


def test_another_conversation_never_sees_them(client, settings):
    _, bound, glob = _bound_and_global(client, settings)
    _ingest(client, "room-other", "user", "A different evening.")
    ids = {f["id"] for f in _recall(client, "Hobart", source_app=APP,
                                    conversation_id="room-other")}
    assert glob in ids and bound not in ids
    # a conversation the service has never seen reads as no conversation
    ids = {f["id"] for f in _recall(client, "Hobart", source_app=APP,
                                    conversation_id="never-ingested")}
    assert glob in ids and bound not in ids


def test_bound_facts_never_join_the_summary(client, settings):
    _, bound, glob = _bound_and_global(client, settings)
    c = db_mod.connect(settings.db_path)
    try:
        durable, active = weighting.select_for_summary(c)
    finally:
        c.close()
    ids = {f["id"] for f in durable + active}
    assert glob in ids and bound not in ids


def test_recall_projection_carries_scope_and_nothing_new_besides(client, settings):
    _bound_and_global(client, settings)
    (row,) = [f for f in _recall(client, "Hobart 2019") if "2019" in f["content"]]
    assert set(row) <= {"id", "content", "event_date", "confidence",
                        "origin_agent", "score", "scope"}
    assert row["scope"] == "global"


# ---- the review queue and widening ----

def test_a_held_bound_fact_says_which_conversation(client, settings):
    _ingest(client, "room-q", "guest:Sam", "I'm allergic to shellfish.",
            title="Dinner planning")
    cid, mid = _conv_and_message(settings, "room-q", "guest:Sam")
    c = db_mod.connect(settings.db_path)
    try:
        f = ledger.add_fact(c, "Sam is allergic to shellfish.", settings,
                            source="chat", origin_agent=APP, source_app=APP,
                            conversation_id=cid, source_message_id=mid,
                            quarantine_reason="guest-attribution: stated by "
                                              "guest Sam, held for review")
    finally:
        c.close()
    (row,) = [r for r in client.get("/v1/review").json()["facts"]
              if r["id"] == f["id"]]
    assert row["scope"] == "conversation"
    assert row["conversation"] == {"id": cid, "source_app": APP,
                                   "external_id": "room-q",
                                   "title": "Dinner planning"}

    # approving keeps the binding; the scope route is the one way to widen
    assert client.post(f"/v1/facts/{f['id']}/approve").status_code == 200
    c = db_mod.connect(settings.db_path)
    try:
        assert ledger.get_fact(c, f["id"])["scope"] == "conversation"
    finally:
        c.close()
    r = client.post(f"/v1/facts/{f['id']}/scope", json={"scope": "global"})
    assert r.status_code == 200 and r.json()["scope"] == "global"
    assert f["id"] in {x["id"] for x in _recall(client, "shellfish")}
    assert client.post(f"/v1/facts/{f['id']}/scope",
                       json={"scope": "everywhere"}).status_code == 422
    assert client.post("/v1/facts/999999/scope",
                       json={"scope": "global"}).status_code == 404


def test_a_global_facts_review_row_names_no_conversation(client, settings):
    c = db_mod.connect(settings.db_path)
    try:
        f = ledger.add_fact(c, "Alex is allergic to nothing at all.", settings,
                            quarantine_reason="importance: unscored")
    finally:
        c.close()
    (row,) = [r for r in client.get("/v1/review").json()["facts"]
              if r["id"] == f["id"]]
    assert row["scope"] == "global" and row["conversation"] is None


def test_the_scope_route_is_owner_gated(client, settings):
    c = db_mod.connect(settings.db_path)
    try:
        f = ledger.add_fact(c, "Alex is left handed.", settings)
    finally:
        c.close()
    stranger = TestClient(client.app, base_url="http://127.0.0.1")
    assert stranger.post(f"/v1/facts/{f['id']}/scope",
                         json={"scope": "conversation"}).status_code == 401


# ---- the one-time backfill ----

def test_existing_guest_facts_are_bound_once_on_upgrade(settings, fake_llm):
    """A v1 database: rows already stored from a guest's turn, a held
    direct save with guests present, and the owner's own facts. Opening it
    with this code binds the first two and leaves the third alone, once."""
    db_mod.init(settings)
    c = db_mod.connect(settings.db_path)
    try:
        c.execute("PRAGMA user_version = 1")
        c.execute("INSERT INTO conversations(source_app, external_id, title, "
                  "created_at) VALUES(?,?,?,?)",
                  (APP, "old-room", "Old room", 1.0))
        cid = c.execute("SELECT id FROM conversations").fetchone()[0]
        c.execute("INSERT INTO messages(conversation_id, external_id, speaker, "
                  "content, created_at) VALUES(?,?,?,?,?)",
                  (cid, "m1", "guest:Sam", "I hate coriander.", 1.0))
        mid = c.execute("SELECT id FROM messages").fetchone()[0]

        def row(content, **kw):
            fields = {"content": content, "created_at": 1.0, "event_date": 1.0,
                      "content_hash": db_mod.content_hash(content),
                      "scope": "global"}
            fields.update(kw)
            cols = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            c.execute(f"INSERT INTO facts({cols}) VALUES({marks})",
                      tuple(fields.values()))
            return c.execute("SELECT id FROM facts ORDER BY id DESC LIMIT 1"
                             ).fetchone()[0]

        approved_guest = row("Sam hates coriander.", conversation_id=cid,
                             source_message_id=mid)
        held_guest = row("Sam is vegetarian.", conversation_id=cid,
                         quarantined_at=1.0,
                         quarantine_reason="guest-attribution: stated by guest Sam")
        held_present = row("Dinner is at Sam's.", conversation_id=cid,
                           quarantined_at=1.0,
                           quarantine_reason="web-derived: x.org; guest-present: "
                                             "Sam was in the room")
        owner = row("Alex likes coriander.", conversation_id=cid)
        c.commit()
    finally:
        c.close()

    db_mod.init(settings)      # the upgrade
    c = db_mod.connect(settings.db_path)
    try:
        scopes = {r["id"]: r["scope"] for r in c.execute("SELECT id, scope FROM facts")}
        assert scopes[approved_guest] == "conversation"
        assert scopes[held_guest] == "conversation"
        assert scopes[held_present] == "conversation"
        assert scopes[owner] == "global"
        assert c.execute("PRAGMA user_version").fetchone()[0] == db_mod.SCHEMA_VERSION
        # widened by hand, then reopened: the backfill does not run twice
        ledger.set_scope(c, approved_guest, "global")
    finally:
        c.close()
    db_mod.init(settings)
    c = db_mod.connect(settings.db_path)
    try:
        assert ledger.get_fact(c, approved_guest)["scope"] == "global"
    finally:
        c.close()
