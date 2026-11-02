"""Data for the article, search, storyline, entity, and state pages.

Every count on a drill-down page opens an evidence drawer, so each one is
computed with the same global filters the drawer applies.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape as html_escape
from typing import Any

from markupsafe import Markup

from monitor import analytics
from monitor.settings import Settings
from monitor.web.filters import WINDOWS, Filters
from monitor.web.panels import (
    LEAN_BUCKETS,
    Panels,
    decorate_item,
    lean_glyph,
    lean_text,
    load_sql,
    safe_url,
)
from monitor.web.readmodel import ReadModel
from monitor.web.vectors import SearchUnavailable, Vectors

BACKLOG_AFTER = timedelta(hours=2)
EXTRACT_CHARS = 1200
NEIGHBORS = 8
RELATED_STORYLINES = 8
STORY_ARTICLES = 150
QUOTES_PER_SIDE = 5
LATEST_PER_OUTLET = 3
SEARCH_RESULTS = 50
SEARCH_POOL = 300
LEVEL_LABELS = {
    "national": "National",
    "state": "State",
    "local": "Local",
    "none": "Unclassified",
}
LEVEL_POSITION = {level: n for n, level in enumerate(LEVEL_LABELS)}
LEAN_FACETS = (*LEAN_BUCKETS, "NA", "Unrated")
ENTITY_TYPES = {"PERSON", "ORG", "PARTY", "OFFICE", "MEASURE", "GPE", "EVENT"}
OFFICE_ORDER = {
    "senate": 0,
    "governor": 1,
    "lt_governor": 2,
    "attorney_general": 3,
    "secretary_of_state": 4,
    "house": 5,
    "ballot_measure": 6,
}
OFFICE_LABELS = {
    "senate": "Senate",
    "governor": "Governor",
    "lt_governor": "Lt. Governor",
    "attorney_general": "Attorney General",
    "secretary_of_state": "Secretary of State",
    "house": "House",
    "ballot_measure": "Ballot measure",
}
Params = Mapping[str, str]


def bin_minutes(filters: Filters) -> int:
    """Bin size for a time chart, chosen from the window's length."""
    minutes = (filters.now - filters.window_start).total_seconds() / 60
    if minutes <= 180:
        return 5
    if minutes <= 720:
        return 15
    return 30


def tone_bin_minutes(filters: Filters) -> int:
    """Tone needs more observations per point than a count does."""
    return max(bin_minutes(filters), 15) * 2


def parse_iso(text: Any) -> datetime | None:
    """An ISO timestamp from reference data or the status file."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None


def race_label(race: dict[str, Any]) -> str:
    """A race's name: `House 07`, or a ballot measure's readable slug."""
    office = race.get("office") or ""
    if office == "ballot_measure":
        key = race.get("race_key") or ""
        slug = key.split("-M-", 1)[1] if "-M-" in key else key
        return slug.replace("-", " ").capitalize()
    label = OFFICE_LABELS.get(office, office.replace("_", " ").title())
    district = race.get("district")
    return f"{label} {district}" if district else label


def placement_reason(item: dict[str, Any]) -> str | None:
    """Why an item sits at its published time: `backlog`, `gap`, or None."""
    placed, seen = item["first_seen_at"], item["seen_raw_at"]
    if placed == seen:
        return None
    return "backlog" if seen - placed > BACKLOG_AFTER else "gap"


def tone_cell(row: dict[str, Any]) -> dict[str, Any]:
    """Tone for one row, under the Methods page's hide and low-n rules."""
    aggregate = analytics.tone_aggregate(
        row.get("tone_sum") or 0,
        row.get("tone_sq") or 0,
        row.get("tone_n") or 0,
    )
    return {
        "mean": aggregate.mean,
        "ci_low": aggregate.ci_low,
        "ci_high": aggregate.ci_high,
        "n": aggregate.n,
        "low_n": aggregate.low_n,
        "hidden": aggregate.hidden,
    }


# Article text


def cut_extract(text: str, limit: int = EXTRACT_CHARS) -> tuple[str, bool]:
    """The opening of a text, ending on a boundary when one is close."""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    for mark in ("\n", ". ", " "):
        cut = text.rfind(mark, 0, limit)
        if cut > limit // 2:
            return text[: cut + (1 if mark == ". " else 0)].rstrip(), True
    return text[:limit], True


@dataclass
class Span:
    start: int
    end: int
    css: str
    href: str = ""
    title: str = ""


def _occurrences(text: str, surface: str) -> list[tuple[int, int]]:
    if not surface:
        return []
    pattern = rf"(?<!\w){re.escape(surface)}(?!\w)"
    return [match.span() for match in re.finditer(pattern, text)]


def locate_spans(
    text: str,
    mentions: list[dict[str, Any]],
    places: list[dict[str, Any]],
    query: str = "",
) -> list[Span]:
    """Entity and place spans in a text, without overlaps.

    Offsets count only when they still match the surface, because the text
    shown is not always the text the parser read. Every other occurrence of
    the surface is found by search. Longer spans win an overlap, and entities
    win over places.
    """
    suffix = f"?{query}" if query else ""
    candidates: list[Span] = []
    for mention in mentions:
        surface = mention.get("surface") or ""
        kind = mention.get("ent_type") or ""
        css = f"ent-{kind}" if kind in ENTITY_TYPES else "ent-EVENT"
        entity_id = mention.get("entity_id")
        href = f"/entity/{entity_id}{suffix}" if entity_id else ""
        parts = [
            mention.get("canonical_name") or surface,
            kind,
            mention.get("role"),
        ]
        if mention.get("tone") is not None:
            parts.append(f"tone {mention['tone']:+.1f}")
        title = " · ".join(str(part) for part in parts if part)
        spots = set(_occurrences(text, surface))
        start, end = mention.get("char_start"), mention.get("char_end")
        if (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(text)
            and text[start:end] == surface
        ):
            spots.add((start, end))
        candidates.extend(
            Span(a, b, f"entity-span {css}", href, title) for a, b in spots
        )
    candidates.sort(key=lambda s: (s.start, -(s.end - s.start)))
    chosen: list[Span] = []
    taken_to = 0
    for span in candidates:
        if span.start >= taken_to:
            chosen.append(span)
            taken_to = span.end
    for place in places:
        usps = place.get("state_usps")
        if not usps or place.get("method") == "source_home":
            continue
        href = f"/state/{usps}{suffix}"
        title = " · ".join(
            str(part)
            for part in (place.get("place_type"), usps, place.get("cd_code"))
            if part
        )
        for a, b in _occurrences(text, place.get("surface") or ""):
            same = next(
                (s for s in chosen if (s.start, s.end) == (a, b)), None
            )
            if same is not None:
                same.href = same.href or href
            elif all(b <= s.start or a >= s.end for s in chosen):
                chosen.append(Span(a, b, "entity-span ent-GPE", href, title))
    return sorted(chosen, key=lambda s: s.start)


def highlight(text: str, spans: list[Span]) -> Markup:
    """Escape a text and wrap its spans. All feed text passes through here."""
    pieces: list[str] = []
    position = 0
    for span in spans:
        pieces.append(html_escape(text[position : span.start]))
        inner = html_escape(text[span.start : span.end])
        attrs = (
            f'class="{html_escape(span.css)}" '
            f'title="{html_escape(span.title)}"'
        )
        if span.href:
            pieces.append(f'<a {attrs} href="{html_escape(span.href)}">')
            pieces.append(f"{inner}</a>")
        else:
            pieces.append(f"<span {attrs}>{inner}</span>")
        position = span.end
    pieces.append(html_escape(text[position:]))
    return Markup("".join(pieces))


def entity_tones(mentions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per entity in an article: its tone and evidence quotes."""
    grouped: dict[str, dict[str, Any]] = {}
    for mention in mentions:
        key = mention.get("entity_id") or mention.get("surface") or ""
        entry = grouped.setdefault(
            key,
            {
                "entity_id": mention.get("entity_id"),
                "name": mention.get("canonical_name")
                or mention.get("surface"),
                "ent_type": mention.get("ent_type"),
                "role": mention.get("role"),
                "party": mention.get("entity_party") or mention.get("party"),
                "tones": [],
                "quotes": [],
            },
        )
        if mention.get("tone") is not None:
            entry["tones"].append(float(mention["tone"]))
        quote = mention.get("tone_evidence")
        if quote and quote not in entry["quotes"]:
            entry["quotes"].append(quote)
    rows = []
    for entry in grouped.values():
        tones = entry.pop("tones")
        entry["tone"] = sum(tones) / len(tones) if tones else None
        rows.append(entry)
    return sorted(
        rows,
        key=lambda r: (r["tone"] is None, -abs(r["tone"] or 0), r["name"]),
    )


@dataclass
class SearchRequest:
    """One search, from the URL. `level` is the global Level control."""

    q: str = ""
    kw: str = ""
    state: str = ""
    source: str = ""
    lean: str = ""
    time: str = "any"
    include_irrelevant: bool = False
    page: int = 0

    @classmethod
    def parse(cls, params: Params) -> SearchRequest:
        state = (params.get("state") or "").strip().upper()
        lean = params.get("lean") or ""
        time = params.get("time") or "any"
        try:
            page = max(int(params.get("page") or 0), 0)
        except ValueError:
            page = 0
        return cls(
            q=(params.get("q") or "").strip(),
            kw=(params.get("kw") or "").strip(),
            state=state if re.fullmatch(r"[A-Z]{2}", state) else "",
            source=(params.get("source") or "").strip(),
            lean=lean if lean in LEAN_FACETS else "",
            time=time if time in WINDOWS else "any",
            include_irrelevant=params.get("all") == "1",
            page=page,
        )

    def query_pairs(self) -> dict[str, str]:
        """The facets as URL params, without the global controls."""
        pairs = {
            "q": self.q,
            "kw": self.kw,
            "state": self.state,
            "source": self.source,
            "lean": self.lean,
            "time": "" if self.time == "any" else self.time,
            "all": "1" if self.include_irrelevant else "",
        }
        return {key: value for key, value in pairs.items() if value}


@dataclass
class SearchResult:
    request: SearchRequest
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    facets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    error: str = ""
    n_vectors: int = 0
    mode: str = "empty"


class Drill:
    def __init__(
        self,
        settings: Settings,
        model: ReadModel,
        panels: Panels,
        vectors: Vectors,
    ) -> None:
        self.settings = settings
        self.model = model
        self.panels = panels
        self.vectors = vectors
        Section = Callable[[Filters, str, Params], dict[str, Any]]
        self.sections: dict[str, dict[str, Section]] = {
            "story": {
                "head": self.story_head,
                "outlets": self.story_outlets,
                "entities": self.story_entities,
                "states": self.story_states,
                "coverage": self.story_coverage,
                "articles": self.story_articles,
                "related": self.story_related,
            },
            "entity": {
                "head": self.entity_head,
                "lean": self.entity_lean,
                "comentions": self.entity_comentions,
                "storylines": self.entity_storylines,
                "quotes": self.entity_quotes,
                "calls": self.entity_calls,
            },
            "state": {
                "head": self.state_head,
                "races": self.state_races,
                "outlets": self.state_outlets,
                "storylines": self.state_storylines,
                "calls": self.state_calls,
            },
        }

    def _where(
        self, filters: Filters, **options: Any
    ) -> tuple[str, dict[str, Any]]:
        return filters.where(self.panels.focus_sets(), **options)

    def _sql(self, name: str, where: str, **parts: Any) -> str:
        sql = load_sql(name).replace("{where}", where)
        for key, value in parts.items():
            sql = sql.replace("{" + key + "}", str(value))
        return sql

    def page_context(
        self, page: str, key: str, filters: Filters, params: Params
    ) -> dict[str, Any]:
        """Every section of a page, for its first render."""
        context: dict[str, Any] = {"page_key": key}
        for section in self.sections[page].values():
            context.update(section(filters, key, params))
        return context

    def section(
        self,
        page: str,
        key: str,
        name: str,
        filters: Filters,
        params: Params,
    ) -> dict[str, Any] | None:
        """One section's context, or None when there is no such section."""
        section = self.sections.get(page, {}).get(name)
        if section is None:
            return None
        return {"page_key": key, **section(filters, key, params)}

    def exists(self, page: str, key: str) -> bool:
        if page == "story":
            return self.resolve_storyline(key) is not None
        if page == "entity":
            return self.entity(key) is not None
        if page == "state":
            return self.state(key) is not None
        return False

    # Article

    def article(
        self, item_id: str, filters: Filters, query: str = ""
    ) -> dict[str, Any] | None:
        """Everything the article view shows. Feed text is untrusted."""
        item = self.panels.article(item_id)
        if item is None:
            return None
        mentions = self.model.query(
            "article_mentions",
            """
            SELECT m.*, e.canonical_name, e.party AS entity_party
            FROM mentions m LEFT JOIN entities e USING (entity_id)
            WHERE m.item_id = $item_id
            ORDER BY m.char_start NULLS LAST, m.surface
            """,
            {"item_id": item_id},
        )
        places = self.model.query(
            "article_places",
            "SELECT * FROM places WHERE item_id = $item_id "
            "ORDER BY county_fips NULLS LAST, surface",
            {"item_id": item_id},
        )
        fetched = self.model.article_text(item_id) or {}
        text = fetched.get("text") or ""
        basis = "full" if text else "summary"
        extract, truncated = cut_extract(text or (item.get("summary") or ""))
        spans = locate_spans(extract, mentions, places, query)
        story = None
        if item.get("storyline_id"):
            rows = self.model.query(
                "article_story",
                "SELECT storyline_id, label, status, n_items FROM storylines "
                "WHERE storyline_id = $id",
                {"id": item["storyline_id"]},
            )
            story = rows[0] if rows else None
        place_rows = {
            (row.get("surface"), row.get("state_usps")): row
            for row in places
            if row.get("state_usps") and row.get("method") != "source_home"
        }
        return {
            **item,
            "lean_source_url": safe_url(item.get("lean_source_url")),
            "has_extract": bool(extract),
            "extract_html": highlight(extract, spans),
            "extract_basis": basis,
            "extract_truncated": truncated,
            "paywalled": bool(fetched.get("paywalled")),
            "fetch_status": fetched.get("http_status"),
            "entity_rows": entity_tones(mentions),
            "place_rows": list(place_rows.values()),
            "storyline": story,
            "neighbors": self.neighbors(item, filters),
            "group": self.dup_group(item, filters),
            "placed_because": placement_reason(item),
            "after_as_of": filters.is_replay
            and item["seen_raw_at"] > filters.now,
        }

    def neighbors(
        self, item: dict[str, Any], filters: Filters
    ) -> list[dict[str, Any]]:
        """Nearest items by embedding, leaving out the item's own copies."""
        self.vectors.refresh()
        vector = self.vectors.items.vector(item["item_id"])
        if vector is None:
            return []
        where, params = self._where(
            filters, windowed=False, relevant=bool(item.get("relevant"))
        )
        clause = "f.item_id <> $self"
        extra: dict[str, Any] = {"self": item["item_id"]}
        if item.get("dup_group_id"):
            clause += " AND f.dup_group_id IS DISTINCT FROM $group"
            extra["group"] = item["dup_group_id"]
        eligible = self.model.column(
            f"SELECT f.item_id FROM facts f WHERE {where} AND {clause}",
            {**params, **extra},
        )
        ranked = self.vectors.items.rank(vector, eligible, NEIGHBORS)
        return self._items_in_order(ranked)

    def _items_in_order(
        self, ranked: list[tuple[str, float]]
    ) -> list[dict[str, Any]]:
        if not ranked:
            return []
        rows = self.model.rows(
            load_sql("search_results"), {"ids": [key for key, _ in ranked]}
        )
        by_id = {row["item_id"]: row for row in rows}
        return [
            {**decorate_item(by_id[key]), "score": score}
            for key, score in ranked
            if key in by_id
        ]

    def dup_group(
        self, item: dict[str, Any], filters: Filters
    ) -> list[dict[str, Any]]:
        group = item.get("dup_group_id")
        if not group:
            return []
        rows = self.model.query(
            "dup_group",
            """
            SELECT f.item_id, f.headline, f.title, f.url, f.language,
                   f.first_seen_at, f.published_at, f.primary_level, f.states,
                   f.tone, f.topics, f.canonical,
                   coalesce(s.name, f.source_id) AS outlet, f.source_id,
                   s.lean_label, s.country
            FROM facts f LEFT JOIN sources s USING (source_id)
            WHERE f.dup_group_id = $group AND f.seen_raw_at <= $as_of
            ORDER BY f.canonical DESC, f.first_seen_at, f.item_id
            """,
            {"group": group, "as_of": filters.now},
        )
        return [decorate_item(row) for row in rows]

    def parser_json(self, item_id: str) -> str:
        """Every parser's rows for an item, pretty-printed for the raw view."""
        outputs = self.model.parser_outputs(item_id)

        def readable(value: Any) -> Any:
            if isinstance(value, str) and value[:1] in {"{", "["}:
                try:
                    return json.loads(value)
                except ValueError:
                    return value
            return value

        tidy = {
            table: [
                {key: readable(value) for key, value in row.items()}
                for row in rows
            ]
            for table, rows in outputs.items()
            if rows
        }
        return json.dumps(tidy, indent=2, ensure_ascii=False, default=str)

    # Search

    def search(self, request: SearchRequest, filters: Filters) -> SearchResult:
        """Rank eligible items by cosine, or by time for a keyword alone.

        Eligibility comes first, so a narrow facet still fills the page: the
        ranking runs over every eligible vector, not a top slice.
        """
        result = SearchResult(request)
        if not request.q and not request.kw:
            self.vectors.refresh()
            result.n_vectors = len(self.vectors.items)
            return result
        where, params = self._search_where(request, filters)
        start = request.page * SEARCH_RESULTS
        if not request.q:
            return self._keyword_search(result, where, params, start)
        try:
            query = self.vectors.embed_query(request.q)
        except SearchUnavailable as error:
            result.error = str(error)[:200]
            result.mode = "unavailable"
            return result
        self.vectors.refresh()
        result.n_vectors = len(self.vectors.items)
        eligible = self.model.column(
            f"SELECT f.item_id FROM facts f WHERE {where}", params
        )
        ranked = self.vectors.items.rank(
            query, eligible, max(SEARCH_POOL, start + SEARCH_RESULTS)
        )
        result.mode = "semantic"
        result.total = len(ranked)
        result.items = self._items_in_order(
            ranked[start : start + SEARCH_RESULTS]
        )
        result.facets = self._facets([key for key, _ in ranked])
        return result

    def _keyword_search(
        self,
        result: SearchResult,
        where: str,
        params: dict[str, Any],
        start: int,
    ) -> SearchResult:
        ids = self.model.column(
            f"SELECT f.item_id FROM facts f WHERE {where} "
            "ORDER BY f.first_seen_at DESC, f.item_id LIMIT $pool",
            {**params, "pool": max(SEARCH_POOL, start + SEARCH_RESULTS)},
        )
        result.n_vectors = len(self.vectors.items)
        result.mode = "keyword"
        result.total = len(ids)
        result.items = self._items_in_order(
            [(key, 0.0) for key in ids[start : start + SEARCH_RESULTS]]
        )
        result.facets = self._facets(ids)
        return result

    def _search_where(
        self, request: SearchRequest, filters: Filters
    ) -> tuple[str, dict[str, Any]]:
        windowed = request.time != "any"
        scoped = filters.with_(window=request.time) if windowed else filters
        where, params = self._where(
            scoped,
            windowed=windowed,
            relevant=not request.include_irrelevant,
        )
        clauses = [where]
        if request.state:
            clauses.append("list_contains(f.states, $facet_state)")
            params["facet_state"] = request.state
        if request.source:
            clauses.append("f.source_id = $facet_source")
            params["facet_source"] = request.source
        if request.lean:
            clauses.append(
                "coalesce((SELECT lean_label FROM sources s "
                "WHERE s.source_id = f.source_id), 'Unrated') = $facet_lean"
            )
            params["facet_lean"] = request.lean
        if request.kw:
            clauses.append(
                "(contains(lower(f.title), lower($kw)) "
                "OR contains(lower(f.headline), lower($kw)) "
                "OR contains(lower(coalesce(f.summary, '')), lower($kw)))"
            )
            params["kw"] = request.kw
        return " AND ".join(clauses), params

    def _facets(self, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Counts over the result pool, for each facet's choices."""
        if not ids:
            return {}
        rows = self.model.rows(
            """
            WITH pool AS (
                SELECT f.*, coalesce(s.name, f.source_id) AS outlet,
                       coalesce(s.lean_label, 'Unrated') AS lean_label
                FROM facts f LEFT JOIN sources s USING (source_id)
                WHERE f.item_id IN (SELECT unnest($ids))
            )
            SELECT 'level' AS facet, value, value AS label, count(*) AS n
            FROM (SELECT unnest(levels) AS value FROM pool) GROUP BY 1, 2, 3
            UNION ALL
            SELECT 'state', value, value, count(*)
            FROM (SELECT unnest(states) AS value FROM pool) GROUP BY 1, 2, 3
            UNION ALL
            SELECT 'source', source_id, any_value(outlet), count(*)
            FROM pool GROUP BY source_id
            UNION ALL
            SELECT 'lean', lean_label, lean_label, count(*)
            FROM pool GROUP BY lean_label
            ORDER BY n DESC, label
            """,
            {"ids": ids},
        )
        facets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            facets.setdefault(row["facet"], []).append(row)
        return facets

    def search_choices(self) -> dict[str, Any]:
        """Facet choices that do not depend on a search."""
        outlets = self.model.query(
            "search_outlets",
            "SELECT source_id, name FROM sources ORDER BY name",
        )
        states = self.model.query(
            "search_states", "SELECT usps FROM states ORDER BY usps"
        )
        return {
            "outlets": outlets,
            "states": [row["usps"] for row in states],
            "leans": [
                {"value": label, "label": lean_text(label)}
                for label in LEAN_FACETS
            ],
        }

    # Storyline

    def resolve_storyline(self, storyline_id: str) -> str | None:
        """The storyline's id, or the one it merged into. None when unknown."""
        current = storyline_id
        for _ in range(8):
            rows = self.model.query(
                "story_merge",
                "SELECT merged_into FROM storylines WHERE storyline_id = $id",
                {"id": current},
            )
            if not rows:
                return None
            target = rows[0]["merged_into"]
            if not target or target == current:
                return current
            current = target
        return current

    def story_head(
        self, filters: Filters, storyline_id: str, params: Params
    ) -> dict[str, Any]:
        row = self.model.query(
            "story_row",
            "SELECT * FROM storylines WHERE storyline_id = $id",
            {"id": storyline_id},
        )[0]

        def totals(**options: Any) -> dict[str, Any]:
            where, extra = self._where(filters, **options)
            return self.model.query(
                "story_totals",
                self._sql("story_totals", where),
                {**extra, "storyline_id": storyline_id},
            )[0]

        where, extra = self._where(filters, windowed=False, unique=False)
        first = self.model.query(
            "story_first",
            self._sql("story_first", where),
            {**extra, "storyline_id": storyline_id},
        )
        return {
            "story": {
                **row,
                "citation_map": json.loads(row.get("citations") or "{}"),
            },
            "totals": totals(),
            "totals_copies": totals(unique=False),
            "totals_all_time": totals(windowed=False),
            "first_report": first[0] if first else None,
        }

    def story_volume(
        self, filters: Filters, storyline_id: str
    ) -> dict[str, Any]:
        where, params = self._where(filters)
        minutes = bin_minutes(filters)
        rows = self.model.query(
            "story_volume",
            self._sql("story_volume", where, bin=minutes),
            {**params, "storyline_id": storyline_id},
        )
        table = [
            {
                **row,
                "level_name": LEVEL_LABELS[row["level"]],
                "order": LEVEL_POSITION[row["level"]],
                "bin_minutes": minutes,
            }
            for row in rows
        ]
        return {
            "table": table if any(row["n"] for row in rows) else [],
            "rules": self.panels.poll_rules(filters),
        }

    def story_outlets(
        self, filters: Filters, storyline_id: str, params: Params
    ) -> dict[str, Any]:
        where, extra = self._where(filters, unique=False)
        rows = self.model.query(
            "story_outlets",
            self._sql("story_outlets", where),
            {**extra, "storyline_id": storyline_id},
        )
        return {"outlets": [decorate_item(row) for row in rows]}

    def story_entities(
        self, filters: Filters, storyline_id: str, params: Params
    ) -> dict[str, Any]:
        where, extra = self._where(filters)
        rows = self.model.query(
            "story_entities",
            self._sql("story_entities", where),
            {**extra, "storyline_id": storyline_id},
        )
        peak = max((row["n"] for row in rows), default=1)
        return {
            "entities": [
                {
                    **row,
                    "mentions": row["n"],
                    "width": row["n"] / peak * 100,
                    **tone_cell(row),
                }
                for row in rows
            ]
        }

    def story_states(
        self, filters: Filters, storyline_id: str, params: Params
    ) -> dict[str, Any]:
        where, extra = self._where(filters)
        return {
            "states": self.model.query(
                "story_states",
                self._sql("story_states", where),
                {**extra, "storyline_id": storyline_id},
            )
        }

    def story_coverage(
        self, filters: Filters, storyline_id: str, params: Params
    ) -> dict[str, Any]:
        """Each lean bucket's share of its election coverage. Counts copies."""
        where, extra = self._where(filters, unique=False)
        rows = self.model.query(
            "lean_share",
            load_sql("lean_share").replace("{where}", where),
            {**extra, "ids": [storyline_id]},
        )
        totals: dict[str, int] = {}
        counts: dict[str, int] = {}
        for row in rows:
            totals[row["lean_label"]] = row["bucket_total"]
            if row["storyline_id"] is not None:
                counts[row["lean_label"]] = row["n"]
        bars: list[dict[str, Any]] = [
            {
                "bucket": bucket,
                "label": lean_text(bucket),
                "glyph": lean_glyph(bucket),
                "n": counts.get(bucket, 0),
                "total": totals.get(bucket, 0),
                "share": analytics.coverage_share(
                    counts.get(bucket, 0), totals.get(bucket, 0)
                ),
            }
            for bucket in LEAN_FACETS
        ]
        peak = max((bar["share"] or 0 for bar in bars), default=0) or 1
        for bar in bars:
            bar["width"] = (bar["share"] or 0) / peak * 100
        return {"coverage": bars if any(bar["n"] for bar in bars) else []}

    def story_articles(
        self, filters: Filters, storyline_id: str, params: Params
    ) -> dict[str, Any]:
        """Items in the storyline. Unique collapses each duplicate group."""
        collapse = filters.copies == "unique"
        where, extra = self._where(filters)
        copies_where, _ = self._where(filters, windowed=False, unique=False)
        clauses = ["TRUE"]
        keyword = (params.get("kw") or "").strip()
        lean = params.get("lean") or ""
        if keyword:
            clauses.append(
                "(contains(lower(f.title), lower($kw)) "
                "OR contains(lower(f.headline), lower($kw)))"
            )
            extra["kw"] = keyword
        if lean in LEAN_FACETS:
            clauses.append("coalesce(s.lean_label, 'Unrated') = $lean")
            extra["lean"] = lean
        rows = self.model.query(
            "story_articles",
            self._sql(
                "story_articles",
                where,
                copies_where=copies_where,
                filter=" AND ".join(clauses),
                group_key="coalesce(dup_group_id, item_id)"
                if collapse
                else "item_id",
            ),
            {
                **extra,
                "storyline_id": storyline_id,
                "limit": STORY_ARTICLES,
            },
        )
        return {
            "articles": [decorate_item(row) for row in rows],
            "articles_total": rows[0]["total"] if rows else 0,
            "articles_kw": keyword,
            "articles_lean": lean if lean in LEAN_FACETS else "",
            "lean_choices": [
                {"value": label, "label": lean_text(label)}
                for label in LEAN_FACETS
            ],
        }

    def story_related(
        self, filters: Filters, storyline_id: str, params: Params
    ) -> dict[str, Any]:
        """Labeled storylines nearest by centroid."""
        self.vectors.refresh()
        vector = self.vectors.storylines.vector(storyline_id)
        if vector is None:
            return {"related": []}
        rows = self.model.query(
            "related_pool",
            """
            SELECT storyline_id, label, status, n_items, n_sources
            FROM storylines
            WHERE merged_into IS NULL AND coalesce(label, '') <> ''
              AND first_seen_at <= $as_of AND storyline_id <> $id
            """,
            {"as_of": filters.now, "id": storyline_id},
        )
        by_id = {row["storyline_id"]: row for row in rows}
        ranked = self.vectors.storylines.rank(
            vector, list(by_id), RELATED_STORYLINES
        )
        where, extra = self._where(filters, windowed=False)
        counts = self.model.query(
            "related_counts",
            f"""
            SELECT f.storyline_id, count(*) AS n FROM facts f
            WHERE {where} AND f.storyline_id IN (SELECT unnest($ids))
            GROUP BY 1
            """,
            {**extra, "ids": [key for key, _ in ranked]},
        )
        n_by_id = {row["storyline_id"]: row["n"] for row in counts}
        return {
            "related": [
                {**by_id[key], "score": score, "n": n_by_id.get(key, 0)}
                for key, score in ranked
            ]
        }

    # Entity

    def entity(self, entity_id: str) -> dict[str, Any] | None:
        rows = self.model.query(
            "entity_row",
            "SELECT * FROM entities WHERE entity_id = $id",
            {"id": entity_id},
        )
        return rows[0] if rows else None

    def entity_head(
        self, filters: Filters, entity_id: str, params: Params
    ) -> dict[str, Any]:
        entity = self.entity(entity_id) or {}
        where, extra = self._where(filters)
        totals = self.model.query(
            "entity_totals",
            f"""
            SELECT count(*) AS mentions, count(DISTINCT m.item_id) AS stories
            FROM mentions m JOIN facts f USING (item_id)
            WHERE {where} AND m.entity_id = $entity_id
            """,
            {**extra, "entity_id": entity_id},
        )[0]
        where, extra = self._where(filters, windowed=False)
        all_time = self.model.query(
            "entity_totals_all",
            f"""
            SELECT count(DISTINCT m.item_id) AS stories
            FROM mentions m JOIN facts f USING (item_id)
            WHERE {where} AND m.entity_id = $entity_id
            """,
            {**extra, "entity_id": entity_id},
        )[0]
        race = None
        if entity.get("race_key"):
            rows = self.model.query(
                "entity_race",
                "SELECT * FROM races WHERE race_key = $key",
                {"key": entity["race_key"]},
            )
            if rows:
                race = {**rows[0], "label": race_label(rows[0])}
        return {
            "entity": entity,
            "race": race,
            "entity_totals": totals,
            "entity_all_time": all_time["stories"],
        }

    def entity_mentions(
        self, filters: Filters, entity_id: str
    ) -> dict[str, Any]:
        where, params = self._where(filters)
        minutes = bin_minutes(filters)
        rows = self.model.query(
            "entity_bins",
            self._sql("entity_bins", where, bin=minutes),
            {**params, "entity_id": entity_id},
        )
        table = [{**row, "bin_minutes": minutes} for row in rows]
        return {
            "table": table if any(row["n"] for row in rows) else [],
            "rules": self.panels.poll_rules(filters),
        }

    def entity_tone(self, filters: Filters, entity_id: str) -> dict[str, Any]:
        """Mean tone per bin with its 95% interval. Small bins are hidden."""
        where, params = self._where(filters)
        minutes = tone_bin_minutes(filters)
        rows = self.model.query(
            "entity_tone_bins",
            self._sql("entity_tone_bins", where, bin=minutes),
            {**params, "entity_id": entity_id},
        )
        table = []
        for row in rows:
            cell = tone_cell(row)
            if cell.pop("hidden"):
                continue
            table.append(
                {
                    **cell,
                    "bin_start": row["bin_start"],
                    "bin_minutes": minutes,
                }
            )
        return {
            "table": table,
            "rules": self.panels.poll_rules(filters),
            "span": [
                {"bin_start": filters.window_start},
                {"bin_start": filters.now},
            ],
        }

    def entity_lean(
        self, filters: Filters, entity_id: str, params: Params
    ) -> dict[str, Any]:
        return {"lean_rows": self.entity_tone_lean(filters, entity_id)}

    def entity_tone_lean(
        self, filters: Filters, entity_id: str
    ) -> list[dict[str, Any]]:
        where, extra = self._where(filters, unique=False)
        rows = self.model.query(
            "entity_tone_lean",
            self._sql("entity_tone_lean", where),
            {**extra, "entity_id": entity_id},
        )
        by_bucket = {row["bucket"]: row for row in rows}
        result = []
        for bucket in (*LEAN_BUCKETS, "All"):
            row = by_bucket.get(bucket)
            if row is None:
                continue
            result.append(
                {
                    "bucket": bucket,
                    "glyph": lean_glyph(bucket),
                    "mentions": row["n"],
                    **tone_cell(row),
                }
            )
        return result

    def entity_lean_chart(
        self, filters: Filters, entity_id: str
    ) -> dict[str, Any]:
        rows = self.entity_tone_lean(filters, entity_id)
        return {"table": [row for row in rows if not row["hidden"]]}

    def entity_comentions(
        self, filters: Filters, entity_id: str, params: Params
    ) -> dict[str, Any]:
        where, extra = self._where(filters)
        rows = self.model.query(
            "entity_comentions",
            self._sql("entity_comentions", where),
            {**extra, "entity_id": entity_id},
        )
        peak = max((row["n"] for row in rows), default=1)
        return {
            "comentions": [
                {**row, "width": row["n"] / peak * 100} for row in rows
            ]
        }

    def entity_storylines(
        self, filters: Filters, entity_id: str, params: Params
    ) -> dict[str, Any]:
        where, extra = self._where(filters)
        return {
            "entity_stories": self.model.query(
                "entity_storylines",
                self._sql("entity_storylines", where),
                {**extra, "entity_id": entity_id},
            )
        }

    def entity_quotes(
        self, filters: Filters, entity_id: str, params: Params
    ) -> dict[str, Any]:
        """The strongest evidence quotes in each direction."""
        where, extra = self._where(filters, unique=False)

        def side(compare: str, order: str) -> list[dict[str, Any]]:
            rows = self.model.query(
                f"entity_quotes_{order}",
                self._sql(
                    "entity_quotes", where, compare=compare, order=order
                ),
                {
                    **extra,
                    "entity_id": entity_id,
                    "limit": QUOTES_PER_SIDE,
                },
            )
            return [decorate_item(row) for row in rows]

        return {
            "quotes_negative": side("<", "ASC"),
            "quotes_positive": side(">", "DESC"),
        }

    def entity_calls(
        self, filters: Filters, entity_id: str, params: Params
    ) -> dict[str, Any]:
        return {
            "rows": self.panels.race_calls(
                filters,
                clause="rc.winner_entity_id = $entity_id",
                extra={"entity_id": entity_id},
            )
        }

    # State

    def state(self, usps: str) -> dict[str, Any] | None:
        rows = self.model.query(
            "state_row",
            "SELECT * FROM states WHERE usps = $usps",
            {"usps": usps},
        )
        return rows[0] if rows else None

    def state_head(
        self, filters: Filters, usps: str, params: Params
    ) -> dict[str, Any]:
        state = self.state(usps) or {}
        sets = self.panels.focus_sets()
        where, extra = filters.where(sets)
        totals_sql = """
            SELECT count(*) FILTER (WHERE list_contains(f.states, $usps)) AS n,
                   count(*) AS total
            FROM facts f WHERE {where}
        """
        totals = self.model.query(
            "state_totals",
            totals_sql.format(where=where),
            {**extra, "usps": usps},
        )[0]
        where, extra = filters.with_(focus="all").where(sets)
        unfocused = self.model.query(
            "state_totals",
            totals_sql.format(where=where),
            {**extra, "usps": usps},
        )[0]
        baseline = self.panels.attention_baseline(filters)
        close_at = parse_iso(state.get("poll_close_et"))
        all_close_at = parse_iso(state.get("poll_close_all_et"))
        return {
            "state": state,
            "is_battleground": usps in sets.battleground_states,
            "state_watched": usps in sets.watch_states,
            "state_n": totals["n"],
            "attention": analytics.attention_index(
                unfocused["n"],
                unfocused["total"],
                baseline.by_state.get(usps, 0),
                baseline.total,
            ),
            "close_at": close_at,
            "all_close_at": all_close_at if all_close_at != close_at else None,
            "polls_closed": close_at is not None and close_at <= filters.now,
        }

    def state_attention(self, filters: Filters, usps: str) -> dict[str, Any]:
        """The index per bin. As on the map, Focus does not change a value."""
        unfocused = filters.with_(focus="all")
        where, params = self._where(unfocused)
        baseline = self.panels.attention_baseline(filters)
        minutes = max(bin_minutes(filters), 15)
        rows = self.model.query(
            "state_bins",
            self._sql("state_bins", where, bin=minutes),
            {**params, "usps": usps},
        )
        rules = self.panels.poll_rules(filters)
        close_at = parse_iso((self.state(usps) or {}).get("poll_close_et"))
        if close_at and filters.window_start <= close_at <= filters.now:
            label = f"Polls close {usps}"
            rules = [*rules, {"time": close_at.isoformat(), "label": label}]
        table = [
            {
                "bin_start": row["bin_start"],
                "bin_minutes": minutes,
                "n": row["n"],
                "total": row["total"],
                "attention": round(
                    analytics.attention_index(
                        row["n"],
                        row["total"],
                        baseline.by_state.get(usps, 0),
                        baseline.total,
                    ),
                    3,
                ),
            }
            for row in rows
        ]
        return {
            "table": table if any(row["n"] for row in rows) else [],
            "rules": rules,
        }

    def state_counties(self, filters: Filters, usps: str) -> dict[str, Any]:
        state = self.state(usps) or {}
        where, params = self._where(filters)
        rows = self.model.query(
            "state_counties",
            self._sql("state_counties", where),
            {**params, "fips": state.get("fips") or "??"},
        )
        return {"table": rows, "fips": state.get("fips")}

    def state_races(
        self, filters: Filters, usps: str, params: Params
    ) -> dict[str, Any]:
        rows = self.model.query(
            "state_races",
            "SELECT * FROM races WHERE state = $usps",
            {"usps": usps},
        )
        ids = sorted({cid for row in rows for cid in row["candidates"] or []})
        names = {
            row["entity_id"]: row
            for row in self.model.query(
                "race_candidates",
                "SELECT entity_id, canonical_name, party FROM entities "
                "WHERE entity_id IN (SELECT unnest($ids))",
                {"ids": ids},
            )
        }
        sets = self.panels.focus_sets()
        watched = set(sets.watch_race_keys)
        races = [
            {
                **row,
                "label": race_label(row),
                "rating_url": safe_url(row.get("rating_url")),
                "watched": row["race_key"] in watched,
                "candidate_rows": [
                    names[cid]
                    for cid in row["candidates"] or []
                    if cid in names
                ],
            }
            for row in rows
        ]
        races.sort(
            key=lambda r: (
                OFFICE_ORDER.get(r["office"], 9),
                r.get("district") or "",
                r["race_key"],
            )
        )
        return {"races": races}

    def state_outlets(
        self, filters: Filters, usps: str, params: Params
    ) -> dict[str, Any]:
        where, extra = self._where(filters)
        outlets = self.model.query(
            "state_outlets",
            self._sql("state_outlets", where),
            {**extra, "usps": usps},
        )
        where, extra = self._where(filters, windowed=False)
        latest = self.model.query(
            "state_latest",
            self._sql("state_latest", where),
            {**extra, "usps": usps, "per_outlet": LATEST_PER_OUTLET},
        )
        by_outlet: dict[str, list[dict[str, Any]]] = {}
        for row in latest:
            by_outlet.setdefault(row["source_id"], []).append(
                decorate_item(row)
            )
        return {
            "local_outlets": [
                {
                    **decorate_item(row),
                    "latest": by_outlet.get(row["source_id"], []),
                }
                for row in outlets
            ]
        }

    def state_storylines(
        self, filters: Filters, usps: str, params: Params
    ) -> dict[str, Any]:
        where, extra = self._where(filters)
        return {
            "state_stories": self.model.query(
                "state_storylines",
                self._sql("state_storylines", where),
                {**extra, "usps": usps},
            )
        }

    def state_calls(
        self, filters: Filters, usps: str, params: Params
    ) -> dict[str, Any]:
        return {
            "rows": self.panels.race_calls(
                filters,
                clause="rc.state = $usps",
                extra={"usps": usps},
            )
        }

    # Evidence

    def evidence_label(self, kind: str, key: str) -> str | None:
        """The name of what a drawer lists, when an id alone would show."""
        lookups = {
            "storyline": (
                "SELECT label AS name FROM storylines "
                "WHERE storyline_id = $key"
            ),
            "entity": (
                "SELECT canonical_name AS name FROM entities "
                "WHERE entity_id = $key"
            ),
            "tone": (
                "SELECT canonical_name AS name FROM entities "
                "WHERE entity_id = $key"
            ),
            "source": "SELECT name FROM sources WHERE source_id = $key",
        }
        if kind not in lookups:
            return None
        rows = self.model.query(
            f"evidence_label_{kind}", lookups[kind], {"key": key}
        )
        return (rows[0]["name"] or None) if rows else None

    # Chart data for the pages, keyed by chart name.

    def chart_data(
        self, chart: str, filters: Filters, key: str
    ) -> dict[str, Any] | None:
        builders: dict[str, Callable[[Filters, str], dict[str, Any]]] = {
            "story_volume": self.story_volume,
            "entity_mentions": self.entity_mentions,
            "entity_tone": self.entity_tone,
            "entity_tone_lean": self.entity_lean_chart,
            "state_attention": self.state_attention,
            "state_counties": self.state_counties,
            "story_map": lambda f, k: {
                "table": self.story_states(f, k, {})["states"]
            },
        }
        builder = builders.get(chart)
        if builder is None:
            return None
        if chart.startswith("story_"):
            key = self.resolve_storyline(key) or key
        return builder(filters, key)
