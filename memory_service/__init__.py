"""Membro: a local-first memory service for AI assistants.

Two version numbers, deliberately independent:

- `__version__` is the application's own release, moving with every
  tagged release of this repository.
- `Settings.contract_version` is the HTTP wire contract clients code
  against (see docs/API.md). It moves only when that contract changes,
  so a client is not asked to care about a release that did not touch
  the surface it speaks to.
"""

__version__ = "0.1.1"
