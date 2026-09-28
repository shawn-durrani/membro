"""Text fields at 16px on touch screens (#140).

iOS Safari zooms the page in when you tap a field whose text is under 16px,
and it doesn't zoom back out, so the owner kept resetting the zoom on his
phone. Pinned here: the admin page, the maths page and the locked page each
hold every text field at 16px under (pointer: coarse), and none of them fixes
the zoom by turning pinch zoom off in the viewport tag.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memory_service.api import create_app

STATIC = Path(__file__).resolve().parent.parent / "memory_service" / "static"
TEXT_TYPES = ("text", "password", "search", "email", "url", "number", "tel", "date")


def _coarse_rules(css: str) -> list[tuple[list[str], str]]:
    """(selectors, declarations) for each rule inside @media (pointer: coarse)."""
    at = re.search(r"@media\s*\(pointer:\s*coarse\)\s*\{", css)
    assert at, "no @media (pointer: coarse) block"
    depth, i = 1, at.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        i += 1
    body = re.sub(r"/\*.*?\*/", "", css[at.end():i - 1], flags=re.S)
    return [([s.strip() for s in sel.split(",")], decl)
            for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", body)]


def _style(html: str) -> str:
    return "\n".join(re.findall(r"<style>(.*?)</style>", html, flags=re.S))


def _assert_viewport_zoomable(html: str) -> None:
    meta = re.search(r'<meta[^>]+name="viewport"[^>]*>', html)
    assert meta, "no viewport tag"
    assert not re.search(r"maximum-scale|user-scalable\s*=\s*(no|0)", meta.group(0))


@pytest.mark.parametrize("page", ["index.html", "math.html"])
def test_static_page_holds_every_text_field_at_16px_on_touch(page):
    html = (STATIC / page).read_text()
    rules = _coarse_rules(_style(html))
    sels, decl = next((s, d) for s, d in rules if "16px" in d)
    assert re.search(r"font-size:\s*16px\s*!important", decl), \
        "the 16px floor needs !important to beat class rules like .theme-pick"
    assert "textarea" in sels and "select" in sels
    assert any(s.startswith("[contenteditable]") for s in sels)
    inp = next(s for s in sels if s.startswith("input"))
    for t in TEXT_TYPES:
        assert f"[type={t}]" not in inp, f"input[type={t}] is left out"
    _assert_viewport_zoomable(html)


def test_no_field_in_the_admin_page_sets_its_own_inline_font_size():
    html = (STATIC / "index.html").read_text()
    for tag in re.findall(r"<(?:input|textarea|select)\b[^>]*>", html):
        assert "font-size" not in tag and "font:" not in tag, tag


@pytest.mark.parametrize("enrolled", [False, True])
def test_locked_page_fields_are_16px_on_touch(tmp_path, settings, enrolled):
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1")
    if enrolled:
        from memory_service import auth, db
        c = db.connect(app.state.settings.db_path)
        try:
            auth.set_owner_password(c, "a-durable-owner-passphrase")
        finally:
            c.close()
    html = client.get("/").text
    assert 'type="password"' in html
    rules = _coarse_rules(_style(html))
    assert any("input" in s and re.search(r"font-size:\s*16px", d) for s, d in rules)
    _assert_viewport_zoomable(html)
