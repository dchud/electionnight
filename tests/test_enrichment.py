from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from monitor import analytics
from monitor.ingest.sources import Source
from monitor.lake import BatchWriter, connect
from monitor.llm.citations import (
    split_sentences,
    validate_cited_text,
    validate_structured,
)
from monitor.llm.schemas import ItemA
from monitor.orchestrator import Database
from monitor.parsers.base import Item
from monitor.parsers.classify_a import (
    ClassifyParser,
    build_system_prompt,
    classify_by_keywords,
    is_opinion_url,
    load_vocabulary,
    normalize_llm_result,
    user_message,
)
from monitor.parsers.dedupe import (
    DedupeIndex,
    DedupeParser,
    hamming,
    normalize_title,
    simhash64,
)
from monitor.parsers.storylines import (
    StorylineEngine,
    ctfidf_keywords,
    ema_update,
)
from monitor.rollups import ROLLUPS, RollupEngine, diff_rows

CONFIG = Path("config")
NOW = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)


@pytest.fixture
def vocabulary():
    return load_vocabulary(CONFIG)


def unit(*values):
    vector = np.zeros(384, dtype=np.float32)
    vector[: len(values)] = values
    return vector / np.linalg.norm(vector)


# Classification


def test_system_prompt_lists_taxonomy_and_is_stable(vocabulary):
    prompt = build_system_prompt(vocabulary, CONFIG)
    assert "lines_wait_times" in prompt
    assert "ballot_measure" in prompt
    assert "{topics}" not in prompt
    assert prompt == build_system_prompt(vocabulary, CONFIG)
    assert len(prompt) > 4000


def test_keyword_path_marks_election_item(vocabulary):
    item = Item(
        "a",
        title="Early voting lines stretch around the block in Georgia",
        summary="County polling place officials expect record turnout.",
    )
    fields = classify_by_keywords(item, None, vocabulary)
    assert fields["election_relevant"] is True
    assert fields["method"] == "keywords"
    assert fields["states"] == ["GA"]
    assert "turnout" in fields["topics"]
    assert fields["primary_level"] != "none"


def test_keyword_path_rejects_unrelated_item(vocabulary):
    item = Item("b", title="Team wins title", summary="A great season.")
    fields = classify_by_keywords(item, None, vocabulary)
    assert fields["election_relevant"] is False
    assert fields["primary_level"] == "none"


def test_keyword_path_reads_non_english_lists(vocabulary):
    item = Item(
        "c",
        title="美国中期选举投票开始",
        summary="",
        language="zh",
    )
    assert classify_by_keywords(item, None, vocabulary)["election_relevant"]


def test_is_opinion_uses_url_and_categories():
    assert is_opinion_url("https://x.com/opinion/a", [])
    assert is_opinion_url("https://x.com/a", ["Editorial Board"])
    assert not is_opinion_url("https://x.com/news/a", ["Politics"])


def test_normalize_restricts_to_vocabularies(vocabulary):
    result = ItemA(
        item_ref="0",
        election_relevant=True,
        relevance_conf=1.4,
        us_politics=True,
        levels=["state"],
        primary_level="state",
        states=["ga", "XX"],
        race_types=["senate", "bogus"],
        topics=["turnout", "made_up"],
        free_tags=["tag"],
        is_results_report=False,
        is_opinion=False,
        headline_en="ignored for english",
        summary_en=None,
    )
    fields = normalize_llm_result(result, Item("a"), vocabulary)
    assert fields["relevance_conf"] == 1.0
    assert fields["states"] == ["GA"]
    assert fields["race_types"] == ["senate"]
    assert fields["topics"] == ["turnout"]
    assert fields["free_tags"] == ["tag", "made_up"]
    assert fields["headline_en"] is None


def test_user_message_carries_source_context():
    source = Source("s", "S", "https://s.org", scope="local", home_state="GA")
    text = user_message(
        [Item("a", source_id="s", title="Title", summary="Sum")], {"s": source}
    )
    assert '"item_ref": "0"' in text
    assert '"home_state": "GA"' in text


async def test_classify_without_llm_uses_keywords(settings):
    settings.paths.config = CONFIG
    parser = ClassifyParser(settings, None, {})
    item = Item("a", title="Midterm election day arrives in Ohio", summary="")
    output = await parser.run([item])
    row = output.rows["nlp.classify_a"][0]
    assert row["method"] == "keywords"
    assert item.relevant is True
    assert item.states == ["OH"]


# Dedupe


def test_normalize_title_removes_outlet_suffix():
    assert normalize_title("Senate race tightens - AP News") == (
        "senate race tightens"
    )


def test_simhash_is_close_for_near_duplicates():
    a = simhash64(
        normalize_title("Senate race in Georgia tightens as polls close")
    )
    b = simhash64(
        normalize_title("Senate race in Georgia tightens as polls close - WSB")
    )
    c = simhash64(
        normalize_title("Stocks fall after central bank decision today")
    )
    assert hamming(a, b) == 0
    assert hamming(a, c) > 3


def test_index_finds_title_match_within_window(settings):
    index = DedupeIndex(settings)
    title_hash = simhash64("a b c d e f")
    index.add(title_hash, unit(1, 0), NOW, "2026-11-03", "g1")
    match = index.find(title_hash, unit(0, 1), NOW + timedelta(hours=1), "x")
    assert match.group_id == "g1"
    assert match.method == "title_hash"
    assert (
        index.find(title_hash, unit(0, 1), NOW + timedelta(hours=40), "x")
        is None
    )


def test_index_embedding_match_needs_same_published_day(settings):
    index = DedupeIndex(settings)
    index.add(simhash64("aaa bbb"), unit(1, 0.01), NOW, "2026-11-03", "g1")
    other = simhash64("zzz yyy xxx www")
    match = index.find(other, unit(1, 0.01), NOW, "2026-11-03")
    assert match.method == "embedding"
    assert index.find(other, unit(1, 0.01), NOW, "2026-11-02") is None


async def test_syndicated_copies_collapse_into_one_group(settings):
    conn = connect(settings)
    parser = DedupeParser(
        settings, Database(conn), index=DedupeIndex(settings)
    )
    title = "Georgia Senate race tightens as voters head to polls"
    items = [
        Item("a", title=title, first_seen_at=NOW, vec=unit(1, 0)),
        Item(
            "b",
            title=f"{title} - Local News",
            first_seen_at=NOW + timedelta(minutes=1),
            vec=unit(1, 0.02),
        ),
        Item(
            "c",
            title="Unrelated market story on interest rates",
            first_seen_at=NOW + timedelta(minutes=2),
            vec=unit(0, 1),
        ),
    ]
    output = await parser.run(items)
    rows = {r["item_id"]: r for r in output.rows["nlp.dup_groups"]}
    assert rows["a"]["dup_group_id"] == rows["b"]["dup_group_id"]
    assert rows["a"]["is_canonical"] is True
    assert rows["b"]["is_canonical"] is False
    assert rows["c"]["is_canonical"] is True
    assert items[1].is_canonical is False
    conn.close()


# Storylines


def engine(settings, join=0.82):
    settings.nlp.storyline_join = join
    return StorylineEngine(settings)


def test_item_joins_nearest_storyline_above_threshold(settings):
    lines = engine(settings)
    first, _ = lines.assign("a", unit(1, 0), "t", "s1", NOW, ["GA"], "state")
    second, score = lines.assign("b", unit(1, 0.1), "t", "s2", NOW, [], None)
    third, _ = lines.assign("c", unit(0, 1), "t", "s3", NOW, [], None)
    assert second is first
    assert score > 0.82
    assert third is not first


def test_promotion_needs_items_and_sources_within_window(settings):
    lines = engine(settings)
    story, _ = lines.assign("a", unit(1, 0), "t", "s1", NOW, [], None)
    lines.assign("b", unit(1, 0), "t", "s1", NOW, [], None)
    lines.assign("c", unit(1, 0), "t", "s1", NOW, [], None)
    assert story.status == "candidate"
    lines.assign("d", unit(1, 0), "t", "s2", NOW, [], None)
    assert story.status == "active"


def test_needs_label_on_promotion_and_growth(settings):
    lines = engine(settings)
    story, _ = lines.assign("a", unit(1, 0), "t", "s1", NOW, [], None)
    assert lines.needs_label(story) is False
    story.status = "active"
    assert lines.needs_label(story) is True
    story.label = "x"
    story.labeled_at_items = 10
    story.n_items = 14
    assert lines.needs_label(story) is False
    story.n_items = 15
    assert lines.needs_label(story) is True


def test_merge_pass_merges_into_older(settings):
    lines = engine(settings, join=0.99)
    older, _ = lines.assign("a", unit(1, 0), "t", "s", NOW, ["GA"], None)
    newer, _ = lines.assign(
        "b", unit(1, 0.2), "t", "s", NOW + timedelta(minutes=1), ["MI"], None
    )
    assert newer is not older
    older.status = newer.status = "active"
    assert lines.merge_pass() == [(newer.storyline_id, older.storyline_id)]
    assert newer.status == "merged"
    assert newer.merged_into == older.storyline_id
    assert older.n_items == 2
    assert set(older.states) == {"GA", "MI"}


def test_dormancy_and_revival(settings):
    lines = engine(settings)
    story, _ = lines.assign("a", unit(1, 0), "t", "s", NOW, [], None)
    story.status = "active"
    assert lines.dormancy_pass(NOW + timedelta(hours=5)) == 1
    assert story.status == "dormant"
    revived, _ = lines.assign(
        "b", unit(1, 0), "t", "s", NOW + timedelta(hours=5), [], None
    )
    assert revived is story
    assert story.status == "active"


def test_storyline_outside_pool_window_starts_fresh(settings):
    lines = engine(settings)
    story, _ = lines.assign("a", unit(1, 0), "t", "s", NOW, [], None)
    later, _ = lines.assign(
        "b", unit(1, 0), "t", "s", NOW + timedelta(hours=9), [], None
    )
    assert later is not story


def test_ema_keeps_unit_length():
    updated = ema_update(unit(1, 0), unit(0, 1))
    assert np.linalg.norm(updated) == pytest.approx(1.0)
    assert updated[0] > updated[1]


def test_ctfidf_prefers_distinctive_terms():
    from collections import Counter

    background = Counter({"senate": 500, "fulton": 1})
    words = ctfidf_keywords(
        ["Fulton senate delays", "Fulton senate lines"], background, 1000
    )
    assert words.index("fulton") < words.index("senate")


# Citations


def test_validator_trims_uncited_sentences_and_unknown_citations():
    text = (
        "Polls closed in Georgia.[1] The race is close. "
        "Officials reported delays.[9] Turnout was high.[2][9]"
    )
    result = validate_cited_text(text, {"1": "item-a", "2": "item-b"})
    assert result.markdown == (
        "Polls closed in Georgia.[^1] Turnout was high.[^2]"
    )
    assert result.citations == {"1": "item-a", "2": "item-b"}
    assert result.sentences_dropped == 2
    assert result.citations_dropped == 2


def test_validator_renumbers_in_order_of_use():
    result = validate_cited_text(
        "First.[7] Second.[3][7]", {"7": "a", "3": "b"}
    )
    assert result.markdown == "First.[^1] Second.[^2][^1]"
    assert result.citations == {"1": "a", "2": "b"}


def test_split_sentences_keeps_citations_attached():
    assert split_sentences("One.[1] Two.[2]") == ["One.[1]", "Two.[2]"]


# Metrics


def test_velocity_score_matches_definition():
    assert analytics.velocity_score(6, 16) == pytest.approx((6 - 2) / 3**0.5)
    assert analytics.is_rising(6, 16) is True
    assert analytics.is_rising(2, 0) is False
    assert analytics.is_rising(3, 80) is False


def test_is_new_window():
    assert analytics.is_new(NOW - timedelta(minutes=29), NOW)
    assert not analytics.is_new(NOW - timedelta(minutes=31), NOW)


def test_attention_index_is_one_at_baseline_and_shrinks_small_counts():
    assert analytics.attention_index(10, 100, 100, 1000) == pytest.approx(1.0)
    assert analytics.attention_index(40, 100, 100, 1000) == pytest.approx(3.0)
    assert analytics.attention_index(2, 100, 0, 1000) == pytest.approx(1.4)
    assert analytics.attention_index(1, 10, 0, 0) == 1.0


def test_tone_aggregate_hides_small_counts_and_flags_low_n():
    assert analytics.tone_aggregate(2.0, 2.0, 4).hidden is True
    low = analytics.tone_aggregate(5.0, 5.0, 5)
    assert low.hidden is False
    assert low.low_n is True
    assert low.mean == pytest.approx(1.0)
    high = analytics.tone_aggregate(15.0, 25.0, 15)
    assert high.low_n is False
    assert high.mean == pytest.approx(1.0)


def test_t_975_matches_published_values():
    published = {4: 2.776, 5: 2.571, 13: 2.160, 29: 2.045, 99: 1.984}
    for df, value in published.items():
        assert analytics.t_975(df) == pytest.approx(value, abs=0.001)


@pytest.mark.parametrize("n", [6, 14, 30, 100])
@pytest.mark.parametrize(("mean", "sd"), [(-1.0, 0.5), (1.5, 0.3), (0.3, 0.8)])
def test_tone_interval_covers_the_true_mean_95_percent_of_the_time(
    n, mean, sd
):
    trials = 4000
    samples = np.random.default_rng(n).normal(mean, sd, (trials, n))
    covered = 0
    for sum_, square in zip(
        samples.sum(axis=1), (samples**2).sum(axis=1), strict=True
    ):
        tone = analytics.tone_aggregate(float(sum_), float(square), n)
        covered += tone.ci_low <= mean <= tone.ci_high
    assert 0.93 <= covered / trials <= 0.97


def test_tone_interval_of_two_counts_with_one_mean_overlap():
    small = analytics.tone_aggregate(-6.0, 6.45, 6)
    large = analytics.tone_aggregate(-100.0, 108.91, 100)
    assert small.mean == pytest.approx(large.mean)
    assert small.ci_low <= large.ci_high and large.ci_low <= small.ci_high


def test_tone_interval_stops_at_the_ends_of_the_scale():
    tone = analytics.tone_aggregate(9.0, 17.0, 5)
    assert tone.ci_high == analytics.TONE_MAX


def test_coverage_share_and_divergence():
    assert analytics.coverage_share(5, 50) == 0.1
    assert analytics.coverage_share(5, 0) is None
    assert analytics.divergence([0.1, 0.4, 0.2]) == pytest.approx(0.3)


def test_a_bucket_without_coverage_does_not_drive_divergence():
    assert analytics.divergence([0.5, None, 0.2]) == pytest.approx(0.3)
    assert analytics.divergence([0.5, None]) == 0.0
    assert analytics.divergence([None, None]) == 0.0


# Rollups


def rollup(table):
    return next(r for r in ROLLUPS if r.table == table)


def test_diff_appends_only_changes_and_zeroes_vanished_keys():
    topic = rollup("gold.topic_bins")
    stored = {(NOW, "turnout"): (3,), (NOW, "gone"): (2,), (NOW, "same"): (1,)}
    computed = [(NOW, "turnout", 4), (NOW, "same", 1), (NOW, "new", 1)]
    rows, merged = diff_rows(topic, computed, stored)
    changed = {row["topic"]: row["n_items"] for row in rows}
    assert changed == {"turnout": 4, "new": 1, "gone": 0}
    assert merged[(NOW, "gone")] == (0,)
    assert diff_rows(topic, computed, merged)[0] == []


def seed_items(writer, count=6):
    bin_start = NOW.replace(minute=0)
    writer.add(
        "raw.items",
        [
            {
                "item_id": f"i{n}",
                "source_id": "s1" if n % 2 else "s2",
                "title": "t",
                "first_seen_at": bin_start + timedelta(minutes=n),
                "feed_categories": [],
            }
            for n in range(count)
        ],
    )
    writer.add(
        "nlp.classify_a",
        [
            {
                "item_id": f"i{n}",
                "election_relevant": n < 4,
                "relevance_conf": 0.9,
                "primary_level": "state" if n < 4 else "none",
                "states": ["GA"] if n < 4 else [],
                "topics": ["turnout"] if n < 4 else [],
                "levels": [],
                "race_types": [],
                "free_tags": [],
            }
            for n in range(count)
        ],
    )
    writer.add(
        "nlp.dup_groups",
        [
            {
                "item_id": "i1",
                "dup_group_id": "g",
                "is_canonical": False,
            }
        ],
    )
    writer.flush()


def test_rollup_counts_unique_and_appends_nothing_when_unchanged(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    seed_items(writer)
    engine_ = RollupEngine(settings, writer)
    first = engine_.run(conn, now=NOW)
    writer.flush()
    assert first["gold.bin_counts"] > 0
    rows = conn.execute(
        "SELECT level, n_items, n_relevant, n_unique FROM lake.gold.bin_counts"
        " WHERE level = 'state'"
    ).fetchall()
    assert rows == [("state", 4, 4, 3)]
    states = conn.execute(
        "SELECT state_usps, n_items, n_unique FROM lake.gold.state_bins"
    ).fetchall()
    assert states == [("GA", 4, 3)]
    assert engine_.run(conn, now=NOW) == {}
    conn.close()


def test_rollup_rebuild_agrees_with_incremental(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    seed_items(writer)
    engine_ = RollupEngine(settings, writer)
    # Cycles run inside a bin until the seeded bin has left the window.
    for minutes in (0, 60, 118, 121, 123, 126, 130):
        engine_.run(conn, now=NOW + timedelta(minutes=minutes, seconds=17))
        writer.flush()
    assert RollupEngine(settings, writer).run(conn, rebuild=True) == {}
    conn.close()


def test_rollup_restart_loads_stored_state(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    seed_items(writer)
    RollupEngine(settings, writer).run(conn, now=NOW)
    writer.flush()
    assert RollupEngine(settings, writer).run(conn, now=NOW) == {}
    conn.close()


@pytest.mark.parametrize(
    "text",
    [
        "Sen. Warnock spoke in Atlanta.[1]",
        "Polls closed on Oct. 26, a record for early voting.[1]",
        "U.S. Rep. Kaptur and J. D. Vance spoke.[1]",
        "The race in Ga. is close, officials said.[1]",
        'Trump said he would be "Mr. Veto" if Democrats win.[1]',
    ],
)
def test_abbreviations_do_not_split_a_sentence(text):
    assert split_sentences(text) == [text]
    result = validate_cited_text(text, {"1": "a"})
    assert result.sentences_dropped == 0
    assert result.markdown == text.replace("[1]", "[^1]")


def test_uncited_sentence_is_still_trimmed_after_an_abbreviation():
    text = "Sen. Warnock spoke. The crowd was large.[1] It rained."
    result = validate_cited_text(text, {"1": "a"})
    assert result.markdown == "The crowd was large.[^1]"
    assert result.sentences_dropped == 2


def test_structured_sentences_stay_whole():
    result = validate_structured(
        [
            [
                ("Voting ended on Oct. 26, a record.", ["1"]),
                ("No source here.", []),
                ("Sen. Warnock spoke.", ["2", "9"]),
            ]
        ],
        {"1": "a", "2": "b"},
    )
    assert result.markdown == (
        "Voting ended on Oct. 26, a record.[^1] Sen. Warnock spoke.[^2]"
    )
    assert result.citations == {"1": "a", "2": "b"}
    assert result.sentences_dropped == 1
    assert result.citations_dropped == 1
