"""MCP admin adapter — READ-ONLY, authenticated, owner-scoped (#46).

For a session that needs to confirm or remediate exact ledger rows (ids,
approval/quarantine status, review-queue membership, event/source dates) —
not just semantic recall — but must not be trusted to write. A sandboxed
coding-agent checkout is the motivating case: it needed to find precise fact
rows matching a keyword and could not, because `recall_memory`/`search_history`
(mcp_server.py) only do semantic retrieval over already-approved facts and
never surface status or the review queue.

Deliberately a SEPARATE server from mcp_server.py's four model-facing tools,
not an addition to it:
- Talks to the live service over its own HTTP API (`GET /v1/facts`,
  `GET /v1/review` — already part of the contract, docs/API.md) — never opens
  the sqlite file directly. A sandboxed process has no business holding the
  ledger's DB file open; the HTTP API is the one write/approval-gated path in,
  and the only path that works when the sandbox and the service are different
  machines/containers (raw file access wouldn't reach it at all).
- Exposes exactly two tools, both GET-only wrappers — no approve / dismiss /
  delete / save tool exists here, matching mcp_server.py's write-gate
  asymmetry: this surface can look, never touch.
- ALWAYS sends a bearer token (MEMORY_AUTH_TOKEN), even against loopback — and
  as of contract 1.1 (#46 v2) the service itself enforces this: `GET /v1/facts`
  and `GET /v1/review` require a matching owner admin token from ANY caller,
  loopback included, so this is no longer a client-side-only convention (v1
  of this module held itself to that bar voluntarily; a sandboxed process
  calling the raw HTTP endpoints directly, with no token, is now refused by
  the API regardless — see api.py's `_admin_auth`). An owner must still
  deliberately provision/share the token for a session to get in.
- Not part of the default registration: the owner opts a specific session in.

Register with (only for a session the owner is actively directing at ledger
remediation — MEMORY_API_URL points at wherever the live service is actually
reachable from that session, e.g. a loopback address, a tunnel, or a LAN host).
The variables MUST be passed with -e, so they reach this server when it later
runs; a shell env prefix in front of `claude mcp add` sets them only for the
registration command, and the registered server would start without them:
    claude mcp add -s user membro-admin \
        -e MEMORY_AUTH_TOKEN=<token> \
        -e MEMORY_API_URL=http://127.0.0.1:8901/v1 \
        -e PYTHONPATH=<repo> -- \
        <repo>/.venv/bin/python -m memory_service.mcp_admin_server
"""

import datetime
import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("membro-admin")


def _base_url() -> str:
    return os.environ.get("MEMORY_API_URL", "http://127.0.0.1:8901/v1").rstrip("/")


def _token() -> str:
    tok = os.environ.get("MEMORY_AUTH_TOKEN", "")
    if not tok:
        raise RuntimeError(
            "MEMORY_AUTH_TOKEN is not set. This admin capability always requires "
            "an owner-issued token, even on loopback, because it returns exact "
            "fact ids and review/moderation state — set MEMORY_AUTH_TOKEN before "
            "using search_facts/review_queue.")
    return tok


def _client() -> httpx.Client:
    """Own function so tests can substitute an in-process ASGI transport
    instead of a real socket — no network in the suite (project convention)."""
    return httpx.Client(base_url=_base_url(),
                        headers={"Authorization": f"Bearer {_token()}"},
                        timeout=10.0)


def _status_label(f: dict) -> str:
    if f.get("invalidated_at"):
        return "superseded"
    if f.get("quarantined_at") and f.get("review_dismissed_at"):
        return "dismissed"
    if f.get("quarantined_at"):
        return "pending review"
    return "approved"


def _day(ts) -> str:
    if not ts:
        return "?"
    return datetime.date.fromtimestamp(ts).isoformat()


def _fmt(facts: list[dict]) -> str:
    lines = []
    for f in facts:
        lines.append(
            f"[id={f['id']} · {_status_label(f)} · event {_day(f.get('event_date'))} "
            f"· origin {f.get('origin_agent', '')}] {f['content']}")
    return "\n".join(lines)


@mcp.tool()
def search_facts(query: str = "", status: str = "all") -> str:
    """Read-only: exact fact ROWS (id, status, text, event/source date, origin)
    matching a keyword, across every status — approved, pending review,
    dismissed, superseded (status: valid|superseded|quarantined|all, default
    all). For confirming and remediating specific rows, not general recall.
    Requires an owner MEMORY_AUTH_TOKEN — enforced by the service itself, even
    on loopback. Never approves, dismisses, edits, or deletes anything.

    Provenance matters when rows disagree: an explicit correction the OWNER
    stated directly always outranks older, even repeated or elaborate,
    model-mined history that contradicts it. Use the `origin_agent` and dates
    shown here to find the conflict and propose a supersession — never treat
    volume or detail of mined facts as outweighing the owner's own word."""
    try:
        with _client() as c:
            r = c.get("/facts", params={"status": status, "q": query, "limit": 200})
            r.raise_for_status()
    except RuntimeError as e:
        return f"Error: {e}"
    except httpx.HTTPError as e:
        return f"Error reaching the memory service at {_base_url()}: {e}"
    facts = r.json().get("facts", [])
    return _fmt(facts) if facts else "No matching facts."


@mcp.tool()
def review_queue(query: str = "") -> str:
    """Read-only: the held-for-review queue (quarantined, not yet dismissed) —
    exact ids, text, and dates, optionally filtered to rows containing a
    keyword. Requires an owner MEMORY_AUTH_TOKEN — enforced by the service
    itself, even on loopback. Never approves, dismisses, edits, or deletes
    anything.

    Same provenance rule as search_facts: an explicit owner correction always
    outranks conflicting model-mined history, however consistent or elaborate
    that history looks."""
    try:
        with _client() as c:
            r = c.get("/review")
            r.raise_for_status()
    except RuntimeError as e:
        return f"Error: {e}"
    except httpx.HTTPError as e:
        return f"Error reaching the memory service at {_base_url()}: {e}"
    facts = r.json().get("facts", [])
    if query.strip():
        q = query.strip().lower()
        facts = [f for f in facts if q in f["content"].lower()]
    return _fmt(facts) if facts else "No matching review-queue entries."


if __name__ == "__main__":
    mcp.run()
