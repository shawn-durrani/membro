"""What the harness pins upstream: the scorer's commit, the dataset revision,
and the file hashes a download is checked against.

A benchmark number means something only while the data and the grader stay
fixed, so every run records these values in its manifest. Nothing upstream is
vendored: the dataset is downloaded into a cache outside the repo, and the
judge's prompt templates in `judge.py` are reproduced under the upstream MIT
licence with its notice.
"""

from __future__ import annotations

UPSTREAM_REPO = "xiaowu0162/LongMemEval"
UPSTREAM_URL = "https://github.com/xiaowu0162/LongMemEval"
# The commit whose src/evaluation/evaluate_qa.py the judge reproduces.
PINNED_COMMIT = "9e0b455f4ef0e2ab8f2e582289761153549043fc"
SCORER_RELPATH = "src/evaluation/evaluate_qa.py"
# Code and data are both MIT: the repo's LICENSE file, and the `license: mit`
# field on both Hugging Face dataset cards.
LICENCE = "MIT"
LICENCE_HOLDER = "Copyright (c) 2024 Di Wu"

# The cleaned release replaced the original dataset in September 2025: it
# drops history sessions that interfered with the answers.
DATASET_REPO = "xiaowu0162/longmemeval-cleaned"
DATASET_REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"

# variant -> (file in the dataset repo, sha256 of that file at the revision)
VARIANTS = {
    "longmemeval_s": (
        "longmemeval_s_cleaned.json",
        "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442",
    ),
}

# The judge the paper and the scorer's own `gpt-4o` alias use.
JUDGE_MODEL = "gpt-4o-2024-08-06"


def dataset_url(variant: str) -> str:
    filename, _ = VARIANTS[variant]
    return (f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/"
            f"{DATASET_REVISION}/{filename}")


def pins() -> dict:
    """Everything a manifest records about upstream."""
    return {
        "scorer": f"{UPSTREAM_REPO}@{PINNED_COMMIT}:{SCORER_RELPATH}",
        "dataset": f"{DATASET_REPO}@{DATASET_REVISION}",
        "judge_model": JUDGE_MODEL,
        "licence": LICENCE,
    }
