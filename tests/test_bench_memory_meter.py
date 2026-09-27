"""The benchmark meter: token counts, prices, the budget cap and the mock.

The meter is where a benchmark run's cost comes from, so its parsing and
pricing are pinned here against replies in each provider's real shape. The
SDK tests point the real Anthropic and OpenAI clients at a mock meter, which
checks the mock speaks each wire format well enough for the apps' own SDKs.
"""

import base64
import json
import struct

import anthropic
import httpx
import openai
import pytest

from bench_memory.live import meter as m


def test_dated_models_price_as_their_base_model():
    assert m.price_for("claude-haiku-4-5-20251001") == ((1.0, 5.0), True)
    assert m.price_for("gpt-4o-2024-08-06") == ((2.5, 10.0), True)
    price, known = m.price_for("some-new-model")
    assert price == m.FALLBACK_PRICE and not known


def test_anthropic_cache_tokens_are_priced_at_their_own_rates():
    cost, _ = m.cost_of("anthropic", {"model": "claude-opus-4-8", "input": 1000,
                                      "output": 100, "cache_write": 1000,
                                      "cache_read": 10_000})
    # 1000 + 1250 + 1000 input-equivalents at $5, 100 out at $25
    assert cost == pytest.approx((3250 * 5 + 100 * 25) / 1e6)


def test_openai_cached_tokens_sit_inside_the_input_total():
    cost, _ = m.cost_of("openai", {"model": "gpt-4o", "input": 1000,
                                   "cache_read": 400, "output": 10})
    assert cost == pytest.approx((600 * 2.5 + 400 * 1.25 + 10 * 10) / 1e6)


def test_usage_from_an_anthropic_reply():
    body = json.dumps({"model": "claude-haiku-4-5", "usage": {
        "input_tokens": 12, "output_tokens": 3,
        "cache_creation_input_tokens": 5, "cache_read_input_tokens": 7}}).encode()
    assert m.parse_usage("anthropic", body) == {
        "model": "claude-haiku-4-5", "input": 12, "output": 3,
        "cache_read": 7, "cache_write": 5}


def test_usage_from_an_anthropic_stream():
    events = [
        {"type": "message_start", "message": {"model": "claude-opus-4-8", "usage": {
            "input_tokens": 40, "output_tokens": 1, "cache_read_input_tokens": 9}}},
        {"type": "content_block_delta", "delta": {"text": "hi"}},
        {"type": "message_delta", "usage": {"output_tokens": 17}},
    ]
    body = "".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in events).encode()
    u = m.parse_usage("anthropic", body)
    assert (u["model"], u["input"], u["output"], u["cache_read"]) == (
        "claude-opus-4-8", 40, 17, 9)


def test_usage_from_openai_replies():
    chat = json.dumps({"model": "gpt-4o-2024-08-06", "usage": {
        "prompt_tokens": 150, "completion_tokens": 1,
        "prompt_tokens_details": {"cached_tokens": 0}}}).encode()
    assert m.parse_usage("openai", chat)["input"] == 150
    emb = json.dumps({"model": "text-embedding-3-small",
                      "usage": {"prompt_tokens": 8, "total_tokens": 8}}).encode()
    assert m.parse_usage("openai", emb)["input"] == 8


def test_the_mock_miner_returns_a_grounded_fact():
    prompt = ("You maintain a permanent memory ledger about User.\n"
              "## Conversation excerpt\n[msg 1] User: I started learning the "
              "cello this week and love it\n\n[msg 2] assistant: nice")
    text = m._mock_text(prompt)
    assert text.startswith("NEW src=1 importance=5: User said I started")
    assert m._mock_text("Is the model response correct? Answer yes or no only.") == "no"
    assert m._mock_text("anything else") == m.MOCK_ANSWER


@pytest.fixture
def mock_meter(tmp_path):
    meter = m.Meter(tmp_path / "usage.jsonl", mock=True)
    meter.start()
    meter.questions["q1"] = "qid-1"
    meter.phases["q1"] = "build"
    yield meter
    meter.stop()


def _rows(meter):
    return [json.loads(line) for line in meter.usage_path.read_text().splitlines()]


def test_the_anthropic_sdk_works_against_the_mock(mock_meter):
    client = anthropic.Anthropic(api_key="x",
                                 base_url=f"{mock_meter.url}/q1.membro/anthropic")
    reply = client.messages.create(model="claude-haiku-4-5", max_tokens=50,
                                   messages=[{"role": "user", "content": "hello"}])
    assert reply.content[0].text == m.MOCK_ANSWER
    with client.messages.stream(model="claude-opus-4-8", max_tokens=50,
                                messages=[{"role": "user", "content": "hi"}]) as s:
        assert s.get_final_text() == m.MOCK_ANSWER
    rows = _rows(mock_meter)
    assert [r["model"] for r in rows] == ["claude-haiku-4-5", "claude-opus-4-8"]
    assert all(r["question_id"] == "qid-1" and r["phase"] == "build"
               and r["service"] == "membro" and r["cost_usd"] == 0.0
               for r in rows)
    assert rows[1]["output"] > 0


def test_the_openai_sdk_works_against_the_mock(mock_meter):
    client = openai.OpenAI(api_key="x",
                           base_url=f"{mock_meter.url}/q1.membro/openai/v1")
    emb = client.embeddings.create(model="text-embedding-3-small",
                                   input=["one", "two"])
    assert len(emb.data) == 2 and len(emb.data[0].embedding) == 1536
    chat = client.chat.completions.create(
        model="gpt-4o-2024-08-06", max_tokens=10,
        messages=[{"role": "user", "content": "Answer yes or no only."}])
    assert chat.choices[0].message.content == "no"


def test_mock_embeddings_decode_as_float32():
    status, _, body = m.mock_reply("openai", "/v1/embeddings", {
        "model": "text-embedding-3-small", "input": "x", "dimensions": 8,
        "encoding_format": "base64"})
    raw = base64.b64decode(json.loads(body)["data"][0]["embedding"])
    assert status == 200 and len(struct.unpack("<8f", raw)) == 8


def test_an_unknown_route_is_refused(mock_meter):
    r = httpx.post(f"{mock_meter.url}/q1.membro/elsewhere/v1/x", json={})
    assert r.status_code == 404


def _upstream(handler):
    """A paid meter whose 'provider' is a function in this test."""
    return httpx.MockTransport(handler)


def test_a_paid_call_is_forwarded_and_priced(tmp_path):
    seen = {}

    def provider(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("x-api-key")
        seen["encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(200, json={
            "model": "claude-haiku-4-5-20251001", "content": [
                {"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1_000_000, "output_tokens": 0}})

    meter = m.Meter(tmp_path / "usage.jsonl", mock=False, budget_usd=5.0,
                    transport=_upstream(provider))
    meter.start()
    try:
        r = httpx.post(f"{meter.url}/q2.crossband/anthropic/v1/messages",
                       headers={"x-api-key": "real-key"},
                       json={"model": "claude-haiku-4-5", "messages": []})
        assert r.status_code == 200 and r.json()["content"][0]["text"] == "ok"
    finally:
        meter.stop()
    assert seen == {"url": "https://api.anthropic.com/v1/messages",
                    "auth": "real-key", "encoding": "identity"}
    assert meter.spent == pytest.approx(1.0)
    row = _rows(meter)[0]
    assert row["cost_usd"] == pytest.approx(1.0) and row["service"] == "crossband"
    assert "real-key" not in meter.usage_path.read_text()


def test_the_budget_is_a_hard_cap(tmp_path):
    calls = []

    def provider(request):
        calls.append(1)
        return httpx.Response(200, json={"model": "claude-opus-4-8", "usage": {
            "input_tokens": 1_000_000, "output_tokens": 0}})

    meter = m.Meter(tmp_path / "usage.jsonl", mock=False, budget_usd=4.0,
                    transport=_upstream(provider))
    meter.start()
    try:
        url = f"{meter.url}/q1.membro/anthropic/v1/messages"
        assert httpx.post(url, json={}).status_code == 200   # $5, over the cap
        refused = httpx.post(url, json={})
    finally:
        meter.stop()
    assert refused.status_code == 400 and "budget" in refused.text
    assert len(calls) == 1 and meter.refused == 1


def test_a_resumed_meter_counts_earlier_spend(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text(json.dumps({"cost_usd": 1.25}) + "\n" + json.dumps({"cost_usd": 0.5}) + "\n")
    assert m.Meter(path, mock=False, budget_usd=10).spent == pytest.approx(1.75)


def test_summary_groups_spend(tmp_path):
    path = tmp_path / "usage.jsonl"
    rows = [
        {"phase": "build", "service": "membro", "model": "claude-haiku-4-5",
         "question_id": "a", "cost_usd": 0.2, "input": 100, "output": 5, "status": 200},
        {"phase": "answer", "service": "crossband", "model": "claude-opus-4-8",
         "question_id": "a", "cost_usd": 0.1, "input": 50, "output": 5, "status": 200},
        {"phase": "judge", "service": "harness", "model": "mystery",
         "question_id": "", "cost_usd": 0.0, "status": 500, "priced": False},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    s = m.summarise(path)
    assert s["cost_usd"] == pytest.approx(0.3)
    assert s["by_question"] == {"a": pytest.approx(0.3)}
    assert s["by_phase"]["build"]["calls"] == 1
    assert s["errors"] == 1 and s["unpriced"] == ["mystery"]
