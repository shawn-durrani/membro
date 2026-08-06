<!-- Thanks! Small, complete changes land fastest. -->

## What & why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] Tests green **keyless**: `env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest tests/ -q`
- [ ] No real personal data anywhere in the diff. Synthetic roster only ; Alex, wife Sam, daughter Maya, Fairhaven as the stock place name, stock fictional companies (Initech, Globex); a name outside the roster is a review question by definition
- [ ] The invariants still hold (append-only; episodic record immutable; quarantine honored ; `tests/test_invariants.py`)
- [ ] CHANGELOG.md entry under Unreleased in this same PR if this is user-visible; docs updated if they now lie
- [ ] New UI surfaces have a plain-English "what is this & why it matters" explainer
