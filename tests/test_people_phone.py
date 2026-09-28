"""The People page at phone width (#143).

Each person's details sat under a 40px indent the browser adds to `dd`, the
rename box and merge picker wrapped wherever they landed, and a clip's
300px player ran off the card. Pinned here: the phone rules that fix it
live inside the 560px block, so the desktop layout is untouched, and the
rename box's width is a class rule a phone rule can override, never an
inline style.
"""

import re
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "memory_service" / "static" / "index.html"


def _css() -> str:
    html = INDEX.read_text()
    css = "\n".join(re.findall(r"<style>(.*?)</style>", html, flags=re.S))
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _split_phone_block(css: str) -> tuple[str, str]:
    """(inside, outside) the @media (max-width: 560px) block."""
    at = re.search(r"@media\s*\(max-width:\s*560px\)\s*\{", css)
    assert at, "no @media (max-width: 560px) block"
    depth, i = 1, at.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        i += 1
    return css[at.end():i - 1], css[:at.start()] + css[i:]


def _decls(css: str, selector: str) -> str:
    return ";".join(decl for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                    if selector in [s.strip() for s in sel.split(",")])


def test_a_persons_details_use_the_full_width_on_a_phone():
    phone, _ = _split_phone_block(_css())
    assert re.search(r"margin:\s*\S+\s+0\s+0\s*(;|$)", _decls(phone, "#people-body dd")), \
        "dd must drop the browser's 40px indent"
    fields = _decls(phone, "#people-body .fields")
    assert re.search(r"display:\s*flex", fields) and re.search(r"flex-wrap:\s*wrap", fields)


def test_each_box_grows_to_share_a_line_with_its_button():
    phone, _ = _split_phone_block(_css())
    for sel in ("#people-body .fields > input", "#people-body .fields > select"):
        decl = _decls(phone, sel)
        assert re.search(r"flex:\s*1\s+1\s+\d+%", decl), sel
        assert re.search(r"min-width:\s*0", decl), sel


def test_a_clip_player_fits_the_card():
    phone, _ = _split_phone_block(_css())
    assert re.search(r"min-width:\s*0", _decls(phone, "#people-body .clip-row > audio"))


def test_the_desktop_layout_is_left_alone():
    _, desktop = _split_phone_block(_css())
    assert "#people-body" not in desktop, "People rules outside the phone block change the desktop"
    assert re.search(r"width:\s*120px", _decls(desktop, ".person-rename"))


def test_the_rename_box_has_no_inline_width():
    html = INDEX.read_text()
    tag = re.search(r'<input[^>]*id="rn-[^>]*>', html)
    assert tag and 'class="person-rename"' in tag.group(0)
    assert "width" not in tag.group(0), "an inline width beats the phone rule"
