"""Golden-set contract tests. These call the Claude API and cost cents.

Run on prompt changes: `uv run pytest -m llm --force-enable-socket`
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from monitor.ingest.sources import Source
from monitor.llm.budget import Budget
from monitor.llm.client import LlmClient
from monitor.parsers.base import Item
from monitor.parsers.classify_a import ClassifyParser
from monitor.parsers.entities import EntityResolver
from monitor.parsers.extract_b import ExtractParser
from monitor.parsers.geo import Gazetteer
from monitor.settings import load_settings

pytestmark = pytest.mark.llm

GOLDEN = Path(__file__).parent / "fixtures" / "golden.jsonl"
MIN_RELEVANCE_ACCURACY = 0.90
MIN_LEVEL_AGREEMENT = 0.80
# Invented articles with invented candidates. After every poll close.
RACE_CLAIMS = Path(__file__).parent / "fixtures" / "race_claims.jsonl"
AFTER_THE_LAST_CLOSE = datetime(2026, 11, 4, 12, tzinfo=UTC)
FINAL = {"projected", "declared", "conceded"}
SHOWN = FINAL | {"leading", "too_close"}
MIN_TRUE_CLAIMS_FOUND = 0.90
MIN_FALSE_CLAIMS_AVOIDED = 0.90


def load_golden():
    lines = GOLDEN.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


async def test_stage_a_meets_golden_thresholds():
    settings = load_settings(".env")
    golden = load_golden()
    sources = {
        row["id"]: Source(
            row["id"],
            row["id"],
            "https://example.com",
            scope=row["scope"],
            home_state=row["home_state"],
            language=row["language"],
        )
        for row in golden
    }
    llm = LlmClient(settings, None, Budget(settings))
    parser = ClassifyParser(settings, llm, sources)
    items = [
        Item(
            row["id"],
            source_id=row["id"],
            title=row["title"],
            summary=row["summary"],
            language=row["language"],
        )
        for row in golden
    ]
    results = {}
    for start in range(0, len(items), 10):
        output = await parser.run(items[start : start + 10])
        for result in output.rows["nlp.classify_a"]:
            results[result["item_id"]] = result
    assert len(results) == len(golden)
    assert all(result["method"] == "llm" for result in results.values())
    relevant_right = sum(
        results[row["id"]]["election_relevant"] == row["relevant"]
        for row in golden
    )
    level_right = sum(
        results[row["id"]]["primary_level"] == row["primary_level"]
        for row in golden
    )
    for row in golden:
        result = results[row["id"]]
        if result["election_relevant"] != row["relevant"] or (
            result["primary_level"] != row["primary_level"]
        ):
            print(
                row["id"],
                row["title"][:60],
                result["election_relevant"],
                result["primary_level"],
            )
    print(
        f"relevance {relevant_right}/{len(golden)}, "
        f"level {level_right}/{len(golden)}, "
        f"${llm.budget.spent_total:.4f}"
    )
    assert relevant_right / len(golden) >= MIN_RELEVANCE_ACCURACY
    assert level_right / len(golden) >= MIN_LEVEL_AGREEMENT
    untranslated = [
        row["id"]
        for row in golden
        if row["language"] != "en" and not results[row["id"]]["headline_en"]
    ]
    assert untranslated == []


async def test_stage_b_holds_the_schema_and_claims_only_what_it_read():
    settings = load_settings(".env")
    golden = load_golden()
    sources = {
        row["id"]: Source(
            row["id"],
            row["id"],
            "https://example.com",
            scope=row["scope"],
            home_state=row["home_state"],
            language=row["language"],
        )
        for row in golden
    }
    llm = LlmClient(settings, None, Budget(settings))
    parser = ExtractParser(
        settings,
        llm,
        None,
        sources,
        EntityResolver(settings),
        Gazetteer.load(settings.paths.ref),
    )
    items = [
        Item(
            row["id"],
            source_id=row["id"],
            title=row["title"],
            summary=row["summary"],
            language=row["language"],
            text=f"{row['title']}\n\n{row['summary']}",
        )
        for row in golden
    ]
    leading = (
        "With 40% of the vote counted, Jane Smith leads John Jones in the "
        "Ohio Senate race, according to the Dayton Daily News. Neither "
        "candidate has declared victory."
    )
    items.append(
        Item(
            "leading",
            title="Smith leads Jones in Ohio Senate race",
            summary=leading,
            text=leading,
        )
    )
    output = await parser.run(items)
    assert output.statuses == {}
    rows = output.rows["nlp.extract_b"]
    assert len(rows) == len(items)
    calls = [
        row
        for row in output.rows.get("nlp.race_calls", [])
        if row["item_id"] == "leading"
    ]
    assert calls
    assert {row["status"] for row in calls} == {"leading"}
    for row in output.rows.get("nlp.mentions", []):
        evidence = row.get("tone_evidence")
        if evidence:
            item = next(i for i in items if i.item_id == row["item_id"])
            assert evidence in item.text


def claim_matches(case, row):
    want = case["status"]
    if want in FINAL and row["status"] not in FINAL:
        return False
    if want not in FINAL and row["status"] != want:
        return False
    winner = case.get("winner")
    return (
        not winner
        or winner.casefold() in (row["winner_name"] or "").casefold()
    )


async def test_race_claims_are_found_and_false_ones_are_not():
    settings = load_settings(".env")
    cases = [
        json.loads(line)
        for line in RACE_CLAIMS.read_text(encoding="utf-8").splitlines()
    ]
    llm = LlmClient(settings, None, Budget(settings))
    parser = ExtractParser(
        settings,
        llm,
        None,
        {},
        EntityResolver(settings),
        Gazetteer.load(settings.paths.ref),
    )
    output = await parser.run(
        [
            Item(
                case["id"],
                title=case["title"],
                summary="",
                text=case["text"],
                language=case["lang"],
                first_seen_at=AFTER_THE_LAST_CLOSE,
                published_at=AFTER_THE_LAST_CLOSE,
            )
            for case in cases
        ]
    )
    assert output.statuses == {}
    claims = {case["id"]: [] for case in cases}
    for row in output.rows.get("nlp.race_calls", []):
        if row["status"] in SHOWN:
            claims[row["item_id"]].append(row)
    true_cases = [case for case in cases if case["call"]]
    false_cases = [case for case in cases if not case["call"]]
    missed = [
        case["id"]
        for case in true_cases
        if not any(claim_matches(case, row) for row in claims[case["id"]])
    ]
    invented = {
        case["id"]: [row["evidence"] for row in claims[case["id"]]]
        for case in false_cases
        if claims[case["id"]]
    }
    found = 1 - len(missed) / len(true_cases)
    avoided = 1 - len(invented) / len(false_cases)
    assert found >= MIN_TRUE_CLAIMS_FOUND, missed
    assert avoided >= MIN_FALSE_CLAIMS_AVOIDED, invented
