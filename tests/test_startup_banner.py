"""The startup banner never leaves a live secret in a log.

Under launchd, stdout is `data/service.log`, so whatever the service prints
at startup is kept in plain text for good. The recovery secret is printed
only when there's no other way to learn it: a first run with a token minted
for this start. A configured token already lives in `.env`.
"""

import uvicorn

from memory_service import api, auth, db
from memory_service.config import Settings

TOKEN = "configured-token-" + "x" * 24


def _run_main(monkeypatch, capsys, settings):
    monkeypatch.setattr(api, "load_settings", lambda: settings)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    api.main()
    return capsys.readouterr().out


def _enrol(settings):
    db.init(settings)
    con = db.connect(settings.db_path)
    try:
        auth.set_owner_password(con, "correct horse battery")
    finally:
        con.close()


def test_configured_token_never_printed(tmp_path, monkeypatch, capsys):
    s = Settings(data_dir=tmp_path / "data", auth_token=TOKEN)
    first = _run_main(monkeypatch, capsys, s)
    assert TOKEN not in first
    assert "MEMORY_AUTH_TOKEN" in first
    _enrol(s)
    later = _run_main(monkeypatch, capsys, s)
    assert TOKEN not in later
    assert "log in with your password" in later


def test_generated_token_printed_only_before_enrolment(tmp_path, monkeypatch,
                                                       capsys):
    s = Settings(data_dir=tmp_path / "data")
    seen = []
    real = api.create_app

    def _spy(settings):
        app = real(settings)
        seen.append(app.state.admin_token)
        return app

    monkeypatch.setattr(api, "create_app", _spy)
    first = _run_main(monkeypatch, capsys, s)
    assert seen[-1] in first                   # first-run setup needs it
    _enrol(s)
    later = _run_main(monkeypatch, capsys, s)
    assert seen[-1] not in later
    assert "set MEMORY_AUTH_TOKEN" in later    # how to get one when needed
