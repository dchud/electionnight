"""Golden-set contract tests. These call the Claude API and cost cents.

Run on prompt changes: `uv run pytest -m llm --force-enable-socket`
"""

import json
from pathlib import Path

import pytest

from monitor.ingest.sources import Source
from monitor.llm.budget import Budget
from monitor.llm.client import LlmClient
from monitor.parsers.base import Item
from monitor.parsers.classify_a import ClassifyParser
from monitor.settings import load_settings

pytestmark = pytest.mark.llm

GOLDEN = Path(__file__).parent / "fixtures" / "golden.jsonl"
MIN_RELEVANCE_ACCURACY = 0.90
MIN_LEVEL_AGREEMENT = 0.80


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
