"""Grade answers the way LongMemEval's own scorer does.

The prompt templates, the judge model, the request settings and the verdict
rule are reproduced from the pinned upstream ``evaluate_qa.py`` (see
``upstream.py``), so a score from here is comparable with a score from the
scorer. The templates are copied word for word under the upstream licence:

    MIT License

    Copyright (c) 2024 Di Wu

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to permit
    persons to whom the Software is furnished to do so, subject to the
    following conditions:

    The above copyright notice and this permission notice shall be included
    in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
    OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
    MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
    NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
    DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
    OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
    USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import upstream

# ---- reproduced from evaluate_qa.py at upstream.PINNED_COMMIT, unchanged ----

_FACTUAL = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."  # noqa: E501
_TEMPORAL = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."  # noqa: E501
_UPDATE = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."  # noqa: E501
_PREFERENCE = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."  # noqa: E501
_ABSTENTION = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."  # noqa: E501


def anscheck_prompt(task: str, question: str, answer: str, response: str,
                    abstention: bool = False) -> str:
    if abstention:
        return _ABSTENTION.format(question, answer, response)
    if task in ("single-session-user", "single-session-assistant",
                "multi-session"):
        return _FACTUAL.format(question, answer, response)
    if task == "temporal-reasoning":
        return _TEMPORAL.format(question, answer, response)
    if task == "knowledge-update":
        return _UPDATE.format(question, answer, response)
    if task == "single-session-preference":
        return _PREFERENCE.format(question, answer, response)
    raise NotImplementedError(task)


def request(prompt: str) -> dict:
    """The chat completion the scorer sends for one question."""
    return {"model": upstream.JUDGE_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "n": 1, "temperature": 0, "max_tokens": 10}


def verdict(judge_reply: str) -> bool:
    """The scorer's rule: any "yes" in the reply is a pass."""
    return "yes" in judge_reply.strip().lower()

# ---- end of the reproduced part ----


RESULTS_NAME = "judged.jsonl"


def judged(path: Path | str) -> dict[str, dict]:
    path = Path(path)
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        out[row["question_id"]] = row
    return out


def grade(predictions: list[dict], references: list[dict], out_path: Path,
          complete) -> dict[str, dict]:
    """Judge every prediction not already judged, appending one row each.

    ``complete`` takes the request dict and returns the judge's reply text.
    Rows hold the id, type, verdict and judge model, and no answer text.
    """
    refs = {r["question_id"]: r for r in references}
    done = judged(out_path)
    with open(out_path, "a", encoding="utf-8") as fh:
        for pred in predictions:
            qid = pred["question_id"]
            if qid in done or qid not in refs:
                continue
            ref = refs[qid]
            prompt = anscheck_prompt(ref["question_type"], ref["question"],
                                     ref["answer"], pred["hypothesis"],
                                     abstention="_abs" in qid)
            label = verdict(complete(request(prompt)))
            row = {"question_id": qid, "question_type": ref["question_type"],
                   "abstention": "_abs" in qid, "label": label,
                   "judge_model": upstream.JUDGE_MODEL}
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            fh.flush()
            done[qid] = row
    return done
