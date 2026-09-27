"""bench_memory: runs published memory benchmarks against crossband and membro.

Each question gets its own throwaway membro and crossband, started from git
worktrees with their own data folders and ports, and deleted afterwards.
See bench_memory/README.md.
"""

__all__ = ["__version__"]

# Recorded in every run's manifest, so a resumed run can prove it's the same
# harness. Independent of membro's own version and its API contract version.
__version__ = "0.1.0"
