"""Summary versions — the profile's own history, append-only.

Regenerating never destroys the previous profile; any version can be restored;
the restore itself appends (history is never rewritten); nothing in the
service deletes a version.
"""

from fastapi.testclient import TestClient

from memory_service import ledger, summary
from memory_service.api import create_app


def _regen(con, settings, fake_llm, text):
    fake_llm["queue"] = [text]
    summary.regenerate(con, settings)


def test_every_rebuild_is_kept(con, settings, fake_llm):
    ledger.add_fact(con, "Alex is learning woodworking.", settings)
    _regen(con, settings, fake_llm, "## Identity\n- first profile")
    _regen(con, settings, fake_llm, "## Identity\n- second profile")
    vs = summary.versions(con)
    assert len(vs) == 2
    assert summary.get(con)["summary"] == "## Identity\n- second profile"
    assert summary.get_version(con, vs[-1]["id"])["content"] == "## Identity\n- first profile"
    assert "content" not in vs[0]  # the list stays light; fetch one for text


def test_restore_brings_a_version_back_by_appending(con, settings, fake_llm):
    ledger.add_fact(con, "Alex is learning woodworking.", settings)
    _regen(con, settings, fake_llm, "## Identity\n- the good one")
    first_id = summary.versions(con)[0]["id"]
    _regen(con, settings, fake_llm, "## Identity\n- the worse rewrite")
    s = summary.restore(con, first_id, settings)
    assert s["summary"] == "## Identity\n- the good one"  # current again
    vs = summary.versions(con)
    assert len(vs) == 3  # restore appended; nothing was rewritten or removed
    assert vs[0]["restored_from"] == first_id
    assert summary.get_version(con, vs[1]["id"])["content"] == "## Identity\n- the worse rewrite"


def test_restore_unknown_version(con, settings):
    assert summary.restore(con, 999, settings) is None


def test_versions_endpoints(settings, con, fake_llm):
    ledger.add_fact(con, "Alex is learning woodworking.", settings)
    _regen(con, settings, fake_llm, "## Identity\n- v1")
    _regen(con, settings, fake_llm, "## Identity\n- v2")
    con.close()
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        vs = client.get("/v1/summary/versions").json()["versions"]
        assert len(vs) == 2 and vs[0]["word_count"] == len("## Identity\n- v2".split())
        old = client.get(f"/v1/summary/versions/{vs[-1]['id']}").json()
        assert old["content"] == "## Identity\n- v1"
        r = client.post(f"/v1/summary/versions/{vs[-1]['id']}/restore")
        assert r.json()["summary"] == "## Identity\n- v1"
        assert client.get("/v1/summary").json()["summary"] == "## Identity\n- v1"
        assert client.get("/v1/summary/versions/999").status_code == 404
        assert client.post("/v1/summary/versions/999/restore").status_code == 404


def test_no_automated_delete_or_update_of_versions():
    """Same standard as facts/messages/access_log: append-only in source."""
    import pathlib

    from memory_service import summary as mod
    for p in pathlib.Path(mod.__file__).parent.glob("*.py"):
        src = p.read_text().lower()
        assert "delete from summary_versions" not in src, p.name
        assert "update summary_versions" not in src, p.name
