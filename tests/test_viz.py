"""Mathematics page data — geometry only, never content (privacy invariant)."""

import math
import time

from memory_service import viz
from memory_service.embeddings import pack


def test_pca3_separates_two_clusters(con):
    """Two blobs far apart in 8-D must land far apart on the first component."""
    import random
    rng = random.Random(3)
    a = [[5 + rng.gauss(0, 0.1) for _ in range(8)] for _ in range(12)]
    b = [[-5 + rng.gauss(0, 0.1) for _ in range(8)] for _ in range(12)]
    coords = viz.pca3(a + b)
    xs_a = [c[0] for c in coords[:12]]
    xs_b = [c[0] for c in coords[12:]]
    assert (max(xs_a) < min(xs_b)) or (max(xs_b) < min(xs_a))


def test_decay_payload_has_no_content(con):
    con.execute(
        "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
        "confidence, importance, content_hash) VALUES('a very private fact', "
        "'user', 'user', 1700000000, 1700000000, 'high', 7, 'h1')")
    con.commit()
    d = viz.decay_data(con)
    assert d["facts"] and d["facts"][0]["imp"] == 7
    assert "private" not in str(d)  # geometry only — content never leaves


def test_embeddings_cache_roundtrip(con):
    for i in range(6):
        vec = [0.0] * 8
        vec[i % 3] = 1.0
        con.execute(
            "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
            "confidence, content_hash, embedding) VALUES(?, 'user', 'user', "
            "1700000000, 1700000000, 'high', ?, ?)",
            (f"secret {i}", f"h{i}", pack(vec)))
    con.commit()
    assert viz.embeddings_cached(con) is None
    data = viz.compute_embeddings(con)
    assert len(data["points"]) == 6 and not data["sampled"]
    assert "secret" not in str(data)
    assert viz.embeddings_cached(con) == data  # stamp matches until ledger grows


def test_recall_trace_lockstep_with_recall(con, settings):
    """The trace is an instrumented twin of recall.recall — if the scoring or
    collapse logic drifts apart, this test fails."""
    from memory_service import recall, viz
    for i in range(8):
        con.execute(
            "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
            "confidence, content_hash) VALUES(?, 'user', 'user', ?, ?, 'high', ?)",
            (f"the quick brown fox number {i}", 1700000000 + i, 1700000000 + i, f"th{i}"))
    con.execute(  # quarantined: must appear in NEITHER
        "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
        "confidence, content_hash, quarantined_at) VALUES('quick secret', 'user', "
        "'user', 1700000000, 1700000000, 'high', 'qx', 1700000000)")
    con.commit()
    want = [f["id"] for f in recall.recall(con, settings, "quick fox", limit=5)]
    trace = viz.recall_trace(con, settings, "quick fox", limit=5)
    assert trace["results"] == want
    assert not trace["semantic"]  # keyless: keyword-only path
    states = {n["id"]: n["state"] for n in trace["nodes"]}
    assert all(states[i] == "kept" for i in want)
    assert "secret" not in str(trace)  # ids only, never content


def test_math_page_served(settings):
    from fastapi.testclient import TestClient
    from memory_service.api import create_app
    app = create_app(settings)
    # /math itself carries no ledger content (geometry only) and needs no
    # auth; the admin page it's linked FROM does (admin-gate v3), so authenticate
    # for that one request.
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.get("/math")
        assert r.status_code == 200
        assert "viz-cloud" in r.text and "viz-trace" in r.text
        # ONE always-current scene: no second canvas, and (consolidation) NO mode
        # toggle and NO replay/timeline controls remain.
        assert "viz-landscape" not in r.text
        for dead in ('id="cloud-landscape"', 'id="cloud-original"', 'id="cloud-play"',
                     'id="cloud-time"', 'id="cloud-date"', "▶ replay", "mode-toggle"):
            assert dead not in r.text, f"stale replay/mode control still present: {dead}"
        # kept: the biome ellipsoid renderer, click-to-sediment, zoom, veil
        assert "ellipseFromCov" in r.text        # covariance→ellipse biome volumes
        assert "sedimentForNode" in r.text and "sedimentForBiome" in r.text
        assert 'id="cloud-zoom"' in r.text and 'id="cloud-veil"' in r.text
        r = client.get("/", headers={"Authorization": f"Bearer {app.state.admin_token}"})
        assert 'href="/math"' in r.text  # admin links to it; section moved out
        assert "viz-cloud" not in r.text


def test_landscape_computing_until_projected(con):
    """Landscape reuses the cached projection; before it's built, it says so —
    the client polls, same contract as /v1/viz/embeddings."""
    assert viz.landscape_data(con) == {"status": "computing"}


def test_landscape_payload_is_geometry_only(con):
    """Clouds, freshness, co-occurrence edges, sediment — all ids/coords/scores,
    never a word of content (the module-wide privacy invariant)."""
    from memory_service.embeddings import pack
    v = [1.0] + [0.0] * 7
    w = [0.0, 1.0] + [0.0] * 6
    # two separable blobs so PCA finds structure and clusters form
    for i in range(8):
        vec = v if i < 4 else w
        con.execute(
            "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
            "confidence, importance, content_hash, embedding) VALUES(?, 'user', 'user', "
            "1700000000, 1700000000, 'high', ?, ?, ?)",
            (f"top secret memory {i}", 5, f"lh{i}", pack(vec)))
    con.commit()
    viz.compute_embeddings(con)          # build the projection the landscape rides on
    d = viz.landscape_data(con)
    assert "top secret" not in str(d)    # geometry only
    assert d["counts"]["alive"] == 8
    # 3D biomes: centroid + covariance, NOT a flat 2D hull
    assert d["clouds"] and all(
        {"c", "colour", "size", "cx", "cy", "cz", "cov", "spread", "freshness"}
        <= set(c) for c in d["clouds"])
    assert all("hull" not in c for c in d["clouds"])   # the 2D hull is gone
    assert all(len(c["cov"]) == 6 for c in d["clouds"])  # xx,yy,zz,xy,xz,yz
    assert all({"id", "x", "y", "z", "fresh", "cl"} <= set(n) for n in d["nodes"])
    # the one scene needs summary membership for the dots' "in summary" ring
    assert "summary_ids" in d and isinstance(d["summary_ids"], list)
    # no recalls happened → no co-occurrence web, and the payload SAYS so
    assert d["edges"] == [] and "edges" in d["notes"]


def test_landscape_endpoint_triggers_projection(settings):
    """The single scene is self-sufficient: hitting /v1/viz/landscape cold kicks
    off the projection build and returns {"status": "computing"} (no separate
    /embeddings call needed) — then serves the biome data once ready."""
    from fastapi.testclient import TestClient
    from memory_service.api import create_app
    from memory_service.embeddings import pack
    from memory_service import db
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        c = db.connect(settings.db_path)
        try:
            for i in range(6):
                vec = [0.0] * 8
                vec[i % 3] = 1.0
                c.execute(
                    "INSERT INTO facts(content, source, origin_agent, created_at, "
                    "event_date, confidence, importance, content_hash, embedding) "
                    "VALUES(?, 'user', 'user', 1700000000, 1700000000, 'high', 5, ?, ?)",
                    (f"memory {i}", f"pe{i}", pack(vec)))
            c.commit()
        finally:
            c.close()
        # cold: triggers the compute job, returns computing (not a 500/404)
        first = client.get("/v1/viz/landscape").json()
        assert first == {"status": "computing"}
        for _ in range(50):
            got = client.get("/v1/viz/landscape").json()
            if "clouds" in got:
                break
            time.sleep(0.1)
        assert "clouds" in got and got["counts"]["alive"] == 6


def test_landscape_clustering_does_not_collapse_dense_data(con):
    """Dense CONNECTED data must partition into a useful, bounded number of 3D
    biomes — never 1–2 — and every biome must carry real 3D volume (a non-zero
    covariance), not a degenerate point."""
    import random
    from memory_service.embeddings import pack
    rng = random.Random(7)
    # 300 points from a SINGLE gaussian blob in 8-D: fully connected, no gaps.
    for i in range(300):
        vec = [rng.gauss(0, 1) for _ in range(8)]
        con.execute(
            "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
            "confidence, importance, content_hash, embedding) VALUES(?, 'user', 'user', "
            "1700000000, 1700000000, 'high', 5, ?, ?)",
            (f"memory {i}", f"dh{i}", pack(vec)))
    con.commit()
    viz.compute_embeddings(con)
    d = viz.landscape_data(con)
    assert d["counts"]["alive"] == 300
    # bounded and USEFUL: neither collapsed to 1–2 nor exploded past the cap
    assert viz.LANDSCAPE_MIN_CLUSTERS <= len(d["clouds"]) <= viz.LANDSCAPE_MAX_CLUSTERS
    assert all(c["size"] > 0 for c in d["clouds"])          # no empty regions
    assert sum(c["size"] for c in d["clouds"]) == 300       # every point placed
    assert {n["cl"] for n in d["nodes"]} == set(range(len(d["clouds"])))  # contiguous
    # each biome has 3D extent: diagonal covariance entries non-negative and,
    # summed, strictly positive (a real volume, not a collapsed point)
    for c in d["clouds"]:
        xx, yy, zz = c["cov"][0], c["cov"][1], c["cov"][2]
        assert xx >= 0 and yy >= 0 and zz >= 0
        assert (xx + yy + zz) > 0 and c["spread"] > 0
    # nodes carry the third dimension (this is a 3D space, not a flat map)
    assert all("z" in n for n in d["nodes"])
    assert any(abs(n["z"]) > 1e-6 for n in d["nodes"])


def test_landscape_clustering_is_deterministic(con):
    """Same ledger → same biomes AND same covariance every call (fixed seed +
    fixed iterations), so the map doesn't reshuffle between refreshes."""
    import random
    from memory_service.embeddings import pack
    rng = random.Random(11)
    for i in range(120):
        vec = [rng.gauss(0, 1) for _ in range(8)]
        con.execute(
            "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
            "confidence, importance, content_hash, embedding) VALUES(?, 'user', 'user', "
            "1700000000, 1700000000, 'high', 5, ?, ?)",
            (f"memory {i}", f"km{i}", pack(vec)))
    con.commit()
    viz.compute_embeddings(con)
    a = viz.landscape_data(con)
    b = viz.landscape_data(con)
    assert [n["cl"] for n in a["nodes"]] == [n["cl"] for n in b["nodes"]]
    assert [c["colour"] for c in a["clouds"]] == [c["colour"] for c in b["clouds"]]
    assert [c["cov"] for c in a["clouds"]] == [c["cov"] for c in b["clouds"]]
    assert [(c["cx"], c["cy"], c["cz"]) for c in a["clouds"]] \
        == [(c["cx"], c["cy"], c["cz"]) for c in b["clouds"]]


def test_landscape_edges_from_cooccurrence(con):
    """Facts returned together by a recall become a weighted edge — the honest
    reading of 'memories that keep surfacing together'."""
    from memory_service import access
    from memory_service.embeddings import pack
    for i in range(6):
        vec = [0.0] * 8
        vec[i % 3] = 1.0
        con.execute(
            "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
            "confidence, importance, content_hash, embedding) VALUES(?, 'user', 'user', "
            "1700000000, 1700000000, 'high', 5, ?, ?)",
            (f"memory {i}", f"eh{i}", pack(vec)))
    con.commit()
    viz.compute_embeddings(con)
    access.record(con, "recall", "q", facts=[{"id": 1, "score": 0.9},
                                             {"id": 2, "score": 0.8}])
    access.record(con, "recall", "q", facts=[{"id": 1, "score": 0.9},
                                             {"id": 2, "score": 0.8}])
    d = viz.landscape_data(con)
    edge = next(e for e in d["edges"] if {e["a"], e["b"]} == {1, 2})
    assert edge["w"] == 2  # recalled together twice


def test_landscape_sediment_from_supersession(con):
    """A superseded fact becomes a buried stratum under the fact that replaced
    it — past-tense provenance made inspectable, geometry only."""
    from memory_service.embeddings import pack
    vec = [1.0] + [0.0] * 7
    # fact 1 (old, retired) superseded_by fact 2 (current)
    con.execute(
        "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
        "confidence, importance, content_hash, embedding, invalidated_at, superseded_by) "
        "VALUES('lived in one place', 'user', 'user', 1700000000, 1700000000, 'high', 5, "
        "'s1', ?, 1700100000, 2)", (pack(vec),))
    con.execute(
        "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
        "confidence, importance, content_hash, embedding) VALUES('lives elsewhere now', "
        "'user', 'user', 1700100000, 1700100000, 'high', 5, 's2', ?)", (pack(vec),))
    con.commit()
    viz.compute_embeddings(con)
    d = viz.landscape_data(con)
    strat = next(s for s in d["sediment"] if s["head"] == 2)
    assert strat["layers"][0]["id"] == 1 and strat["layers"][0]["d"] == 1
    assert "lived in" not in str(d)  # geometry only


def test_recall_trace_evolution_fields(con, settings):
    """Evolution replay data: lifecycle per node, static dup edges, no content."""
    from memory_service import viz
    from memory_service.embeddings import pack
    v = [1.0] + [0.0] * 7
    for i in range(3):
        con.execute(
            "INSERT INTO facts(content, source, origin_agent, created_at, event_date, "
            "confidence, content_hash, embedding) VALUES(?, 'user', 'user', ?, ?, "
            "'high', ?, ?)",
            (f"alpha topic {i}", 1700000000 + i * 86400, 1700000000 + i * 86400,
             f"e{i}", pack(v)))  # identical vectors → cosine 1.0 → dup edges
    con.commit()
    trace = viz.recall_trace(con, settings, "alpha", limit=5)
    n = trace["nodes"][0]
    assert {"c", "iv", "qt", "ed"} <= set(n)
    # 3 identical candidate vectors → all pairs are dup edges (3 choose 2)
    assert len(trace["dup_edges"]) == 3
    assert "now" in trace and "recency_weight" in trace
    assert "alpha" not in str(trace["nodes"]) and "topic" not in str(trace)


# The live-event feed is the persistent access log now — tests moved to
# test_access_log.py.
