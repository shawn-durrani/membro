"""Privacy-safe data for the admin UI's Mathematics page — geometry, never
content. Every payload here is ids, ages, importances, scores, coordinates;
fact text never leaves the ledger through this module.

The embedding projection is classical PCA done without numpy: with the Gram
double-centering trick the eigenproblem lives in sample space (n×n, not
1536×1536), and Python 3.12's math.sumprod gives C-speed dot products — a
one-time ~10s compute for a thousand-plus-fact ledger, cached in settings and rebuilt as a
background job when the ledger grows.
"""

import json
import math
import random
import sqlite3

from . import db
from .weighting import DEFAULT_IMPORTANCE, HALF_LIFE_BASE_DAYS, score

SAMPLE_CAP = 2500  # Gram matrix is O(n²) memory — sample newest beyond this


def decay_data(con):
    """One point per non-superseded fact: where it sits on the decay surface."""
    rows = con.execute(
        "SELECT id, importance, event_date, created_at, quarantined_at "
        "FROM facts WHERE invalidated_at IS NULL").fetchall()
    now = db.now()
    facts = []
    for r in rows:
        imp = r["importance"] or DEFAULT_IMPORTANCE
        ts = r["event_date"] or r["created_at"] or now
        facts.append({
            "id": r["id"], "imp": imp,
            "age": round(max(0.0, now - ts) / 86400.0, 1),
            "score": round(score({"importance": imp, "event_date": ts}, now), 4),
            "q": 1 if r["quarantined_at"] else 0,
        })
    return {"half_life_base_days": HALF_LIFE_BASE_DAYS,
            "default_importance": DEFAULT_IMPORTANCE, "facts": facts}


def pca3(vectors, seed=11):
    """Top-3 principal-component projections of row vectors (no numpy).
    Gram trick: K = X·Xᵀ double-centered; power iteration with deflation;
    the projection of point i on component c is sqrt(λc)·yc[i]."""
    n = len(vectors)
    if n < 4:
        return [(0.0, 0.0, 0.0)] * n
    K = [[0.0] * n for _ in range(n)]
    for i in range(n):
        vi, Ki = vectors[i], K[i]
        for j in range(i, n):
            d = math.sumprod(vi, vectors[j])
            Ki[j] = d
            K[j][i] = d
    row_mean = [math.fsum(row) / n for row in K]
    total_mean = math.fsum(row_mean) / n
    for i in range(n):
        Ki, ri = K[i], row_mean[i]
        for j in range(n):
            Ki[j] += total_mean - ri - row_mean[j]

    rng = random.Random(seed)
    comps = []
    for _ in range(3):
        v = [rng.uniform(-1.0, 1.0) for _ in range(n)]
        lam = 0.0
        for _ in range(60):
            w = [math.sumprod(row, v) for row in K]
            for pv, _plam in comps:  # deflate: stay orthogonal to found comps
                c = math.sumprod(pv, w)
                w = [wi - c * pvi for wi, pvi in zip(w, pv)]
            lam = math.sqrt(math.sumprod(w, w)) or 1.0
            v = [wi / lam for wi in w]
        comps.append((v, lam))
    return [tuple(math.sqrt(max(lam, 0.0)) * v[i] for v, lam in comps)
            for i in range(n)]


# The live recall feed moved to access.py (the persistent access log) —
# lookups now survive restarts and include MCP client processes.


def recall_trace(con, settings, query, limit=10):
    """The recall pipeline, instrumented: every fact's score components and
    what happened to it (kept / collapsed / over-budget / dim). Same math as
    recall.recall — test_viz asserts the two stay in lockstep, so if the
    scoring ever changes there, the trace test fails here. ids only, as ever."""
    from . import embeddings as emb
    from . import recall as rc
    query = (query or "").strip()
    entries = [dict(r) for r in con.execute(
        "SELECT * FROM facts WHERE quarantined_at IS NULL AND invalidated_at IS NULL")]
    qvec = None
    try:
        qvec = emb.embed_query(query, settings)  # LRU: reuses the chat recall's
    except Exception:
        qvec = None
    words = [w.lower() for w in query.split() if len(w) >= 3]
    now = db.now()
    nodes = []
    for e in entries:
        sem = (emb.cosine(qvec, emb.unpack(e["embedding"]))
               if qvec is not None and e.get("embedding") else 0.0)
        kw = sum(1 for w in words if w in e["content"].lower())
        rec = rc._recency(e, now)
        score = sem + 0.12 * min(kw, 3) + 0.05 + rc.RECENCY_WEIGHT * rec
        nodes.append({"id": e["id"], "imp": e.get("importance") or DEFAULT_IMPORTANCE,
                      "sem": round(sem, 4), "kw": min(kw, 3), "rec": round(rec, 3),
                      "score": score,  # unrounded until AFTER the sort — recall
                      "cand": bool(sem > rc.SEMANTIC_FLOOR or kw), "_e": e})
    nodes.sort(key=lambda n: -n["score"])  # ...sorts unrounded; ties must match it
    kept, kept_vecs, seen_hash = [], [], set()
    for n in nodes:
        if not n["cand"]:
            n["state"] = "dim"
            continue
        if len(kept) >= limit:
            n["state"] = "over"
            continue
        e = n["_e"]
        h = e.get("content_hash")
        if h and h in seen_hash:
            n["state"] = "dup"
            continue
        ev = emb.unpack(e["embedding"]) if e.get("embedding") else None
        if ev is not None and any(emb.cosine(ev, kv) > rc.PARAPHRASE_CUTOFF
                                  for kv in kept_vecs):
            n["state"] = "collapsed"
            continue
        n["state"] = "kept"
        kept.append(n["id"])
        if h:
            seen_hash.add(h)
        if ev is not None:
            kept_vecs.append(ev)
    # Evolution support: lifecycle timestamps per node, plus the near-duplicate
    # edges among candidates. Cosine similarity is time-independent, so the
    # client can faithfully replay ranking + greedy collapse AS OF any past
    # moment: alive(T) from the timestamps, recency(T) recomputed, dup edges
    # reused. ("What would this recall have answered last March?")
    cands = [n for n in nodes if n["cand"]]
    # Rigorous prune before the O(n²) edge pass: recency contributes at most
    # RECENCY_WEIGHT to a score, so a candidate more than that below the
    # limit-th best static score can never reach the top-k at ANY moment —
    # its dup edges could only ever relabel far-tail nodes cosmetically.
    static = lambda n: n["sem"] + 0.12 * n["kw"] + 0.05  # noqa: E731
    cands.sort(key=static, reverse=True)
    if len(cands) > limit:
        floor = static(cands[limit - 1]) - rc.RECENCY_WEIGHT
        cands = [n for n in cands if static(n) >= floor]
    cands = cands[:200]
    unit = {}
    for n in cands:
        e = n["_e"]
        if e.get("embedding"):
            v = emb.unpack(e["embedding"])
            norm = math.sqrt(math.sumprod(v, v)) or 1.0
            unit[n["id"]] = [x / norm for x in v]  # cosine = one sumprod now
    dup_edges = []
    ids = list(unit)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if math.sumprod(unit[ids[i]], unit[ids[j]]) > rc.PARAPHRASE_CUTOFF:
                dup_edges.append([ids[i], ids[j]])
    rnd = lambda v: round(v, 1) if v else None  # noqa: E731
    for n in nodes:
        e = n.pop("_e")
        n["score"] = round(n["score"], 4)
        n["c"] = rnd(e.get("created_at"))
        n["iv"] = rnd(e.get("invalidated_at"))
        n["qt"] = rnd(e.get("quarantined_at"))
        n["ed"] = rnd(e.get("event_date") or e.get("created_at"))
    return {"semantic": qvec is not None, "results": kept, "nodes": nodes,
            "dup_edges": dup_edges, "now": now,
            "recency_weight": rc.RECENCY_WEIGHT}


def _stamp(con):
    r = con.execute(
        "SELECT COUNT(*), COALESCE(MAX(id),0) FROM facts "
        "WHERE embedding IS NOT NULL").fetchone()
    return f"v2:{r[0]}:{r[1]}"  # v2: lifecycle fields + superseded facts included


def embeddings_cached(con):
    """Return the cached projection if it still matches the ledger, else None."""
    raw = db.get_setting(con, "viz_embeddings")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if data.get("stamp") == _stamp(con) else None


def compute_embeddings(con):
    """Project every embedded fact (superseded included — they're history) to
    3D, carrying the full lifecycle so the page can REPLAY the ledger's life:
    birth, quarantine, dismissal, supersession (with the edge)."""
    from . import embeddings as emb
    rows = con.execute(
        "SELECT id, importance, created_at, event_date, quarantined_at, "
        "invalidated_at, superseded_by, review_dismissed_at, embedding "
        "FROM facts WHERE embedding IS NOT NULL ORDER BY id DESC").fetchall()
    sampled = len(rows) > SAMPLE_CAP
    rows = rows[:SAMPLE_CAP]
    vectors = [emb.unpack(r["embedding"]) for r in rows]
    coords = pca3(vectors)
    rnd = lambda v: round(v, 1) if v else None  # noqa: E731
    pts = []
    for r, (x, y, z) in zip(rows, coords):
        pts.append({"id": r["id"], "x": round(x, 3), "y": round(y, 3),
                    "z": round(z, 3), "imp": r["importance"] or DEFAULT_IMPORTANCE,
                    "c": rnd(r["created_at"]), "qt": rnd(r["quarantined_at"]),
                    "iv": rnd(r["invalidated_at"]), "sb": r["superseded_by"],
                    "dm": rnd(r["review_dismissed_at"])})
    data = {"stamp": _stamp(con), "sampled": sampled, "points": pts}
    db.set_setting(con, "viz_embeddings", json.dumps(data))
    con.commit()
    return data


def attach_live(con, data):
    """Request-time overlay on the cached projection: what 'now' looks like —
    the current summary's members and the clock."""
    raw = db.get_setting(con, "summary_sources")
    ids = []
    if raw:
        try:
            ids = json.loads(raw).get("fact_ids", [])
        except json.JSONDecodeError:
            pass
    return {**data, "summary_ids": ids, "now": db.now()}


# ---- Memory landscape: the playful "shape of me" lens ----------
# Same discipline as the rest of this module: geometry only — ids, coordinates,
# cluster indices, freshness scores, edge weights, timestamps. No fact text ever
# leaves the ledger here. Every layer below is built from data that actually
# exists; where a layer would need data the ledger doesn't hold, we say so in
# the payload (`notes`) rather than inventing it.

LANDSCAPE_MAX_EDGES = 120  # keep the co-occurrence web readable, never a hairball
# Palette is INDICES only — the client picks hues. The contract: a
# colour distinguishes a cloud from its NEIGHBOURS; it is never a stable
# topic→colour mapping. Greedy graph-colouring guarantees adjacent clouds
# differ while reusing a small set.
LANDSCAPE_PALETTE_N = 8
# Clustering: k-means on the actual 3D projected coordinates (the 3D redesign
# — the landscape is a rotatable 3D "biome", not a flattened 2D map). Grid
# connected-components collapsed dense data (thousands of points → 2 blobs); flat
# 2D k-means then bait-and-switched the 3D Replay space into a flat map. k-means
# in 3D PARTITIONS the embedding cloud into a bounded number of contiguous
# regions no matter how dense/connected it is. Each region also carries its 3D
# centroid + covariance so the client can render a translucent volumetric
# ellipsoid (a Gaussian "splat"), not a fake hull. Deterministic: fixed seed +
# k-means++ init + fixed iterations.
LANDSCAPE_MIN_CLUSTERS = 6
LANDSCAPE_MAX_CLUSTERS = 14
LANDSCAPE_KMEANS_ITERS = 30
LANDSCAPE_KMEANS_SEED = 17


def _target_k(n):
    """A useful, bounded region count that grows gently with the ledger:
    ~sqrt(n/2), clamped to [MIN, MAX] and never more than there are points."""
    if n <= LANDSCAPE_MIN_CLUSTERS:
        return max(1, n)
    return min(LANDSCAPE_MAX_CLUSTERS, max(LANDSCAPE_MIN_CLUSTERS,
                                          round(math.sqrt(n / 2.0))))


def _kmeans(points, k, seed=LANDSCAPE_KMEANS_SEED):
    """Deterministic k-means (no numpy). k-means++ seeding for spread starts,
    then fixed-iteration Lloyd's; empty clusters are re-seeded to the farthest
    point so k regions always survive. Returns (assignment, centroids) with
    contiguous indices over the NON-EMPTY clusters only."""
    n = len(points)
    if k <= 1 or n == 0:
        return [0] * n, [_mean(points)] if points else []
    rng = random.Random(seed)
    d2 = lambda a, b: sum((a[i] - b[i]) ** 2 for i in range(len(a)))  # noqa: E731
    # k-means++ init
    centroids = [points[rng.randrange(n)]]
    while len(centroids) < k:
        dists = [min(d2(p, c) for c in centroids) for p in points]
        total = math.fsum(dists)
        if total <= 0:  # all points identical to a centroid — pad deterministically
            centroids.append(points[len(centroids) % n])
            continue
        target, acc = rng.random() * total, 0.0
        for i, dv in enumerate(dists):
            acc += dv
            if acc >= target:
                centroids.append(points[i])
                break
    assign = [0] * n
    for _ in range(LANDSCAPE_KMEANS_ITERS):
        changed = False
        for i, p in enumerate(points):
            best, bd = 0, None
            for c, cen in enumerate(centroids):
                dv = d2(p, cen)
                if bd is None or dv < bd:
                    bd, best = dv, c
            if assign[i] != best:
                assign[i] = best
                changed = True
        members = [[] for _ in range(len(centroids))]
        for i, a in enumerate(assign):
            members[a].append(points[i])
        for c in range(len(centroids)):
            if members[c]:
                centroids[c] = _mean(members[c])
            else:  # re-seed the empty cluster to the worst-served point
                far_i = max(range(n), key=lambda i: min(d2(points[i], cen)
                                                        for cen in centroids))
                centroids[c] = points[far_i]
                changed = True
        if not changed:
            break
    # drop any still-empty clusters and re-index contiguously
    used = sorted(set(assign))
    remap = {old: new for new, old in enumerate(used)}
    return [remap[a] for a in assign], [centroids[o] for o in used]


def _mean(points):
    n = len(points)
    d = len(points[0])
    return tuple(math.fsum(p[i] for p in points) / n for i in range(d))


def _covariance(points, mean):
    """3×3 covariance of a cluster (flattened to the 6 unique upper-triangle
    entries xx,yy,zz,xy,xz,yz). The client projects this to a screen-space
    ellipse (Σ₂ = JΣJᵀ) to draw a translucent volumetric splat that reshapes
    correctly as the scene rotates — the honest 'this cluster has volume' cue."""
    n = len(points)
    xx = yy = zz = xy = xz = yz = 0.0
    mx, my, mz = mean[0], mean[1], mean[2]
    for p in points:
        dx, dy, dz = p[0] - mx, p[1] - my, p[2] - mz
        xx += dx * dx; yy += dy * dy; zz += dz * dz
        xy += dx * dy; xz += dx * dz; yz += dy * dz
    return [xx / n, yy / n, zz / n, xy / n, xz / n, yz / n]


def _spread(points, mean):
    """Mean distance of members to the centroid — a single radius the client
    uses for level-of-detail glow sizing and for the privacy veil's equal orbs."""
    n = len(points)
    return math.fsum(math.sqrt(sum((p[i] - mean[i]) ** 2 for i in range(len(mean))))
                     for p in points) / n


def _neighbour_colours(centroids):
    """Colour so adjacent regions differ. Adjacency = each cluster linked to its
    nearest few centroids (a cheap proximity graph over Voronoi cells); then
    greedy graph-colouring by descending degree over a small palette."""
    m = len(centroids)
    if m == 0:
        return {}
    d2 = lambda a, b: sum((a[i] - b[i]) ** 2 for i in range(len(a)))  # noqa: E731
    near = min(3, m - 1)
    adj = {c: set() for c in range(m)}
    for c in range(m):
        order = sorted((x for x in range(m) if x != c), key=lambda x: d2(centroids[c], centroids[x]))
        for x in order[:near]:
            adj[c].add(x)
            adj[x].add(c)
    colour = {}
    for c in sorted(range(m), key=lambda c: -len(adj[c])):
        used = {colour[nb] for nb in adj[c] if nb in colour}
        k = 0
        while k in used:
            k += 1
        colour[c] = k % LANDSCAPE_PALETTE_N
    return colour


def _cooccurrence_edges(con, alive_ids):
    """Weighted edges from the access log: facts RECALLED TOGETHER. This is the
    honest reading of the landscape brief's 'memories that keep surfacing together in the
    same conversations' — two facts co-occur each time one recall returned both.
    Sparse until recall history accrues; absent entirely on a fresh ledger, and
    the client says so rather than drawing a fake web."""
    pair_w = {}
    try:
        rows = con.execute("SELECT result_ids FROM access_log").fetchall()
    except sqlite3.OperationalError:  # pre-migration DB: no access log yet
        return []
    for r in rows:
        try:
            ids = [p[0] for p in json.loads(r["result_ids"] or "[]")]
        except (json.JSONDecodeError, TypeError, IndexError):
            continue
        ids = sorted({i for i in ids if i in alive_ids})
        for a_i in range(len(ids)):
            for b_i in range(a_i + 1, len(ids)):
                key = (ids[a_i], ids[b_i])
                pair_w[key] = pair_w.get(key, 0) + 1
    edges = sorted(pair_w.items(), key=lambda kv: -kv[1])[:LANDSCAPE_MAX_EDGES]
    return [{"a": a, "b": b, "w": w} for (a, b), w in edges]


def _sediment(con, coord_ids):
    """Supersession strata (the landscape brief, past tense): for each current fact, the
    chain of older facts it buried. Geometry only — id, when it was retired,
    importance, depth. Reading the buried TEXT stays a deliberate opt-in in the
    admin ledger; the append-only invariant means every layer is still there."""
    rows = con.execute(
        "SELECT id, importance, invalidated_at, superseded_by FROM facts").fetchall()
    predecessors = {}   # newer_id -> [older rows]
    by_id = {}
    for r in rows:
        by_id[r["id"]] = r
        if r["superseded_by"] is not None:
            predecessors.setdefault(r["superseded_by"], []).append(r)
    strata = []
    for head in coord_ids:
        layers, frontier, depth, seen = [], [head], 0, {head}
        while frontier and depth < 20:
            depth += 1
            nxt = []
            for cur in frontier:
                for old in predecessors.get(cur, []):
                    if old["id"] in seen:
                        continue
                    seen.add(old["id"])
                    layers.append({
                        "id": old["id"], "d": depth,
                        "imp": old["importance"] or DEFAULT_IMPORTANCE,
                        "at": round(old["invalidated_at"], 1) if old["invalidated_at"] else None})
                    nxt.append(old["id"])
            frontier = nxt
        if layers:
            strata.append({"head": head, "layers": layers})
    return strata


def landscape_data(con):
    """The memory landscape: clouds + freshness + co-occurrence web +
    sediment, all from the CACHED projection so it's cheap to re-serve. Returns
    {"status": "computing"} when the projection isn't built yet — the client
    polls, same as /v1/viz/embeddings."""
    cached = embeddings_cached(con)
    if not cached:
        return {"status": "computing"}
    coords = {p["id"]: p for p in cached["points"]}
    now = db.now()
    rows = con.execute(
        "SELECT id, importance, event_date, created_at, quarantined_at, "
        "invalidated_at FROM facts WHERE invalidated_at IS NULL "
        "AND quarantined_at IS NULL").fetchall()
    nodes = []
    for r in rows:
        p = coords.get(r["id"])
        if not p:  # not embedded / not in the sampled projection
            continue
        imp = r["importance"] or DEFAULT_IMPORTANCE
        ed = r["event_date"] or r["created_at"] or now
        fresh = round(score({"importance": imp, "event_date": ed}, now), 4)
        nodes.append({"id": r["id"], "x": p["x"], "y": p["y"], "z": p["z"],
                      "imp": imp, "fresh": fresh,
                      "age": round(max(0.0, now - ed) / 86400.0, 1)})
    alive_ids = {n["id"] for n in nodes}
    # k-means the real 3D projection into a bounded number of contiguous biomes
    pts3d = [(n["x"], n["y"], n["z"]) for n in nodes]
    k = _target_k(len(nodes))
    assign, centroids = _kmeans(pts3d, k)
    colour = _neighbour_colours(centroids)
    for n, a in zip(nodes, assign):
        n["cl"] = a
    clouds = []
    for c in range(len(centroids)):
        idx = [i for i in range(len(nodes)) if assign[i] == c]
        if not idx:
            continue
        member_pts = [pts3d[i] for i in idx]
        cen = centroids[c]
        cov = _covariance(member_pts, cen)
        fresh_mean = math.fsum(nodes[i]["fresh"] for i in idx) / len(idx)
        clouds.append({
            "c": c, "colour": colour.get(c, 0), "size": len(idx),
            "cx": round(cen[0], 3), "cy": round(cen[1], 3), "cz": round(cen[2], 3),
            "cov": [round(v, 4) for v in cov],
            "spread": round(_spread(member_pts, cen), 3),
            "freshness": round(fresh_mean, 4)})
    edges = _cooccurrence_edges(con, alive_ids)
    strata = _sediment(con, alive_ids)
    notes = {}
    if not edges:
        notes["edges"] = ("No co-occurrence web yet: edges are drawn only once "
                          "recalls have returned the same facts together. Ask "
                          "your memory a few questions and they appear.")
    if not strata:
        notes["sediment"] = ("No sediment yet: nothing in the ledger has been "
                             "superseded. Corrections over time build the strata.")
    # current summary membership — for the dots' "in today's summary" ring
    summary_ids = []
    raw = db.get_setting(con, "summary_sources")
    if raw:
        try:
            summary_ids = json.loads(raw).get("fact_ids", [])
        except json.JSONDecodeError:
            pass
    return {"now": now, "sampled": cached.get("sampled", False),
            "nodes": nodes, "clouds": clouds, "edges": edges,
            "sediment": strata, "palette_n": LANDSCAPE_PALETTE_N,
            "summary_ids": summary_ids,
            "counts": {"alive": len(nodes), "clouds": len(clouds),
                       "edges": len(edges), "strata": len(strata)},
            "notes": notes}
