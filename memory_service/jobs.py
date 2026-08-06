"""Background job registry — slow LLM work never blocks a request.

Contract: async endpoints return 202 + job_id; poll GET /jobs/{id}. Failures
are recorded and surfaced, never silent.
"""

import threading
import uuid

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def run(kind: str, fn) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {"kind": kind, "status": "running", "error": None, "result": None}

    def _work():
        try:
            result = fn()
            with _lock:
                _jobs[job_id].update(status="ok", result=result)
        except Exception as e:
            with _lock:
                _jobs[job_id].update(status="failed", error=str(e))

    threading.Thread(target=_work, daemon=True).start()
    return job_id


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None
