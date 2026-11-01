"""The parser interface, and the item that moves between stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class Item:
    """One story moving through the stages, enriched along the way."""

    item_id: str
    source_id: str = ""
    title: str = ""
    summary: str = ""
    url: str = ""
    canonical_url: str = ""
    first_seen_at: datetime | None = None
    published_at: datetime | None = None
    language: str = "en"
    feed_categories: list[str] = field(default_factory=list)
    headline_en: str | None = None
    summary_en: str | None = None
    relevant: bool = False
    relevance_conf: float = 0.0
    states: list[str] = field(default_factory=list)
    primary_level: str | None = None
    vec: Any = None
    is_canonical: bool = True
    text: str | None = None
    paywalled: bool = False
    backfill: bool = False

    @property
    def english_title(self) -> str:
        return self.headline_en or self.title

    @property
    def english_summary(self) -> str:
        return self.summary_en or self.summary


@dataclass
class ParserOutput:
    """Rows per target table, plus how each item finished.

    An item absent from `statuses` finished `ok`. Only items that finished
    `ok` or `skipped` move to the next stage.
    """

    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    statuses: dict[str, tuple[str, str | None]] = field(default_factory=dict)

    def add(self, table: str, row: dict[str, Any]) -> None:
        self.rows.setdefault(table, []).append(row)

    def mark(
        self, item_id: str, status: str, error: str | None = None
    ) -> None:
        self.statuses[item_id] = (status, error)


class Parser(Protocol):
    """One step of the pipeline.

    `input_query` selects `item_id`s still needing this parser at its current
    version, and is used at startup and for backfill. Live items arrive from
    the previous stage through an in-process queue.
    """

    name: str
    version: str
    input_query: str
    batch_size: int
    max_concurrency: int

    async def run(self, batch: list[Item]) -> ParserOutput: ...


def needs_parser_sql(name: str, version: str, where: str = "TRUE") -> str:
    """SQL for items lacking a finished run of this parser version."""
    return f"""
        SELECT i.item_id FROM lake.raw.items i
        WHERE {where}
          AND i.item_id NOT IN (
            SELECT item_id FROM lake.nlp.parser_runs
            WHERE parser = '{name}' AND parser_version = '{version}'
              AND status IN ('ok', 'skipped'))
        ORDER BY i.first_seen_at DESC
        LIMIT $limit
    """


RELEVANT_SQL = """
    i.item_id IN (
        SELECT item_id FROM (
            SELECT item_id, election_relevant, relevance_conf
            FROM lake.nlp.classify_a
            QUALIFY row_number() OVER (
                PARTITION BY item_id ORDER BY created_at DESC) = 1
        )
        WHERE election_relevant AND relevance_conf >= {threshold}
    )
"""

HYDRATE_SQL = """
    SELECT i.item_id, i.source_id, i.title, i.summary, i.url, i.canonical_url,
           i.first_seen_at, i.published_at,
           coalesce(l.language, i.language, 'en') AS language,
           i.feed_categories, c.headline_en, c.summary_en,
           coalesce(c.election_relevant, FALSE) AS relevant,
           coalesce(c.relevance_conf, 0) AS relevance_conf,
           coalesce(c.states, []) AS states, c.primary_level,
           coalesce(d.is_canonical, TRUE) AS is_canonical
    FROM (SELECT * FROM lake.raw.items WHERE item_id IN (SELECT unnest($ids)))
        i
    LEFT JOIN (
        SELECT item_id, language FROM lake.nlp.lang
        WHERE item_id IN (SELECT unnest($ids))
        QUALIFY row_number() OVER (
            PARTITION BY item_id ORDER BY created_at DESC) = 1
    ) l USING (item_id)
    LEFT JOIN (
        SELECT * FROM lake.nlp.classify_a
        WHERE item_id IN (SELECT unnest($ids))
        QUALIFY row_number() OVER (
            PARTITION BY item_id ORDER BY created_at DESC) = 1
    ) c USING (item_id)
    LEFT JOIN (
        SELECT item_id, is_canonical FROM lake.nlp.dup_groups
        WHERE item_id IN (SELECT unnest($ids))
        QUALIFY row_number() OVER (
            PARTITION BY item_id ORDER BY created_at DESC) = 1
    ) d USING (item_id)
"""


def item_from_row(row: tuple[Any, ...]) -> Item:
    return Item(
        item_id=row[0],
        source_id=row[1] or "",
        title=row[2] or "",
        summary=row[3] or "",
        url=row[4] or "",
        canonical_url=row[5] or "",
        first_seen_at=row[6],
        published_at=row[7],
        language=row[8] or "en",
        feed_categories=list(row[9] or []),
        headline_en=row[10],
        summary_en=row[11],
        relevant=bool(row[12]),
        relevance_conf=float(row[13] or 0),
        states=list(row[14] or []),
        primary_level=row[15],
        is_canonical=bool(row[16]),
    )


def item_from_poller(row: dict[str, Any]) -> Item:
    return Item(
        item_id=row["item_id"],
        source_id=row["source_id"],
        title=row["title"],
        summary=row["summary"],
        url=row["url"],
        canonical_url=row["canonical_url"],
        first_seen_at=row["first_seen_at"],
        published_at=row["published_at"],
        language=row["language"] or "en",
        feed_categories=list(row["feed_categories"] or []),
    )
