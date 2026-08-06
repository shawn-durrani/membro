"""The written process must say what we actually do (#114).

CONTRIBUTING.md and CLAUDE.md disagreed for months: one said work lands as
"small complete commits", the other said it goes through a PR. The stale one
was the guide a contributor is pointed to, so a session following it would have
committed straight to main and left no issue→PR trail at all.

That trail is the point. The issue holds *why*, the PR holds *what changed and
what it cost*, and the link between them outlives the conversation that produced
them — a future session reconstructs the reasoning from it rather than
re-deriving or re-litigating a settled decision.

These are prose assertions, which is unusual and deliberate: the failure mode is
documentation drift, and drift is exactly what nobody notices. Cheap to keep
honest, and the alternative is finding out months later.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONTRIBUTING = REPO / "CONTRIBUTING.md"
CLAUDE = REPO / "CLAUDE.md"


def test_contributing_documents_the_pr_pipeline():
    """The specific regression: the guide described commit-to-main only."""
    text = CONTRIBUTING.read_text()
    for required in ("PR linked to its issue", "squash-merge", "Fixes #N",
                     "CI green"):
        assert required in text, f"CONTRIBUTING.md no longer states {required!r}"


def test_contributing_explains_who_the_trail_is_for():
    """Without the reason, the pipeline reads as bureaucracy and gets skipped
    the first time someone is in a hurry."""
    text = CONTRIBUTING.read_text().lower()
    assert "next" in text and "reasoning" in text, \
        "CONTRIBUTING.md must say the trail exists for whoever comes next"


def test_contributing_warns_about_stacked_prs():
    """Squash-merging orphans a PR branched off another open PR — silently,
    with a green MERGED badge and a closed issue. A sibling project learned this the
    hard way; the warning must not be lost in the port."""
    text = CONTRIBUTING.read_text()
    assert "never off another open PR" in text
    assert "orphan" in text


def test_claude_md_defers_rather_than_duplicating_the_process():
    """One source, so the two can't disagree again. CLAUDE.md keeps only what
    is specific to an AI session."""
    text = CLAUDE.read_text()
    assert "CONTRIBUTING.md" in text, "CLAUDE.md must point at the single source"
    # the old duplicated pipeline wording must not come back
    assert "branch → PR linked to its issue → CI green → squash-merge" not in text, \
        "the pipeline is documented in CONTRIBUTING.md — don't re-copy it here"


def test_both_docs_agree_the_supervisor_owns_restarts():
    """#111: a hand-start fails loudly while the old code keeps running, so
    'restart the service' has to name the supported command in both places."""
    for path in (CONTRIBUTING, CLAUDE):
        assert "launchctl kickstart" in path.read_text(), \
            f"{path.name} must name the supported restart"


def test_sessions_are_told_to_merge_their_own_prs():
    """A session that waits for human approval stalls; the human comments
    async. The PR plus green CI is the gate."""
    text = CLAUDE.read_text()
    assert "Merge your own PRs" in text
    assert "don't wait for human approval" in text.lower()
