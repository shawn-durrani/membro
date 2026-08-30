<!-- Thanks! Small, complete changes land fastest. -->

## What & why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] Tests green **keyless**: `env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest tests/ -q`
- [ ] No real personal data anywhere in the diff. Fleet synthetic roster only: people Alex, Sam, Dave, Mateo; place Fairhaven; companies AcmeCo, Initech, Globex. A name outside the roster is a review question by definition
- [ ] The invariants still hold (append-only; episodic record immutable; quarantine honoured; `tests/test_invariants.py`)
- [ ] `changelog.d/` fragment in this same PR if this is user-visible; docs updated if they now lie
- [ ] New UI surfaces have a plain-English "what is this & why it matters" explainer
