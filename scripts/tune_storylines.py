#!/usr/bin/env python3
"""Replay storyline clustering over baseline items at several thresholds.

Reads the lake read-only, writes nothing to it, and prints cluster statistics
and sample headlines, so the join threshold can be chosen from real data.

    uv run python scripts/tune_storylines.py --join 0.86 0.88 0.90 --samples 6
"""

from __future__ import annotations

import argparse

import numpy as np

from monitor.lake import connect
from monitor.parsers.storylines import StorylineEngine
from monitor.settings import get_settings

QUERY = """
    SELECT i.item_id, i.source_id, i.first_seen_at,
           coalesce(c.headline_en, i.title), c.states, c.primary_level, e.vec
    FROM lake.nlp.classify_a c
    JOIN lake.raw.items i USING (item_id)
    JOIN lake.nlp.embeddings e USING (item_id)
    JOIN lake.nlp.dup_groups d USING (item_id)
    WHERE c.election_relevant AND c.relevance_conf >= 0.5 AND d.is_canonical
    ORDER BY i.first_seen_at, i.item_id
"""


def replay(settings, rows, join: float, merge: float) -> StorylineEngine:
    settings.nlp.storyline_join = join
    settings.nlp.storyline_merge = merge
    engine = StorylineEngine(settings)
    for item_id, source, seen_at, title, states, level, vec in rows:
        engine.assign(
            item_id,
            np.array(vec, dtype=np.float32),
            title or "",
            source,
            seen_at,
            list(states or []),
            level,
        )
    engine.merge_pass()
    return engine


def report(engine: StorylineEngine, total: int, samples: int) -> None:
    lines = [s for s in engine.storylines.values() if s.status != "merged"]
    active = [s for s in lines if s.status == "active"]
    sizes = sorted((s.n_items for s in lines), reverse=True)
    assigned_active = sum(s.n_items for s in active)
    print(
        f"  storylines {len(lines)}, active {len(active)}, "
        f"largest {sizes[:6]}, in active "
        f"{assigned_active / max(total, 1):.0%}, singletons "
        f"{sum(1 for size in sizes if size == 1)}"
    )
    for storyline in sorted(active, key=lambda s: -s.n_items)[:samples]:
        print(f"  [{storyline.n_items} items, {len(storyline.sources)} src]")
        members = sorted(storyline.members, key=lambda m: -m.similarity)
        for member in members[:4]:
            print(f"     {member.similarity:.2f} {member.title[:90]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--join", type=float, nargs="+", required=True)
    parser.add_argument("--merge-gap", type=float, default=0.03)
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    settings = get_settings()
    conn = connect(settings, read_only=True)
    rows = conn.execute(QUERY).fetchall()
    conn.close()
    print(f"{len(rows)} relevant canonical items")
    vectors = np.array([row[6] for row in rows[:1500]], dtype=np.float32)
    if len(vectors) > 1:
        pairs = vectors @ vectors.T
        upper = pairs[np.triu_indices_from(pairs, k=1)]
        quantiles = np.quantile(upper, [0.5, 0.9, 0.99, 0.999])
        print(
            "pairwise similarity p50/p90/p99/p99.9: "
            + " ".join(f"{q:.3f}" for q in quantiles)
        )
    for join in args.join:
        print(f"join {join}, merge {join + args.merge_gap}")
        engine = replay(settings, rows, join, join + args.merge_gap)
        report(engine, len(rows), args.samples)


if __name__ == "__main__":
    main()
