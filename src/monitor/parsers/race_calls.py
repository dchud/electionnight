"""Rules for race-call claims reported in the news.

These are claims in articles, never official results. Rejecting a true claim
costs less than publishing a false one. A claim counts only if its evidence
appears verbatim in the article. A win or concession claim must use language
for its status, name the winner, and not be negated, conditional, about a past
cycle, or about a primary. A lead must name the leader and rest on counted
votes, not on a poll. A caller stays only if the article names that caller.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from monitor.lake import hash_id
from monitor.llm.schemas import RaceCallB

ELECTION_YEAR = "2026"
FINAL_STATUSES = {"projected", "declared", "conceded"}
UNSPECIFIED = {"unspecified", "unknown", "none"}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

# Each pattern reads article text as written, where an acronym is in capitals,
# and reads a model's caller value with case ignored.
CALLERS = (
    (
        "AP",
        r"\bAP\b|(?i:associated press|ассошиэйтед|أسوشيتد برس|美联社|美聯社)",
    ),
    ("NBC", r"\bNBC\b"),
    ("CNN", r"\bCNN\b"),
    ("Fox", r"\bFox\b|\bFOX\b"),
    ("DDHQ", r"\bDDHQ\b|(?i:decision ?desk)"),
    ("ABC", r"\bABC\b"),
    ("CBS", r"\bCBS\b"),
    ("NYT", r"\bNYT\b|(?i:new york times)"),
)

# Status language. Patterns run on casefolded text.
WIN_RE = re.compile(
    r"\bwon\b|\bwins\b|\bwinner\b|\bdefeat(?:s|ed)\b|\bbeat(?:s)?\b|"
    r"\b(?:re-?)?elected\b|\bvictory over\b|\bvictorious\b|"
    r"\bgan(?:a|ó|an|aron|ado|adora?)\b|\bvenc(?:e|ió|edora?)\b|\btriunf\w+|"
    r"\b(?:re)?elegid[oa]\b|\b(?:re)?elect[oa]\b|\bse impuso\b|"
    r"\bgagn(?:e|é|ée|ent)\b|\b(?:r|l')?emport\w+|\bvainqueur\b|"
    r"\b(?:ré)?élue?\b|\bvictoire de\b|"
    r"\bпобед\w+|\bвыиграл\w*|\b(?:пере)?избран\w*|"
    r"فاز|يفوز|تفوز|بفوز|الفوز|هزم|"
    r"赢得|贏得|获胜|獲勝|胜选|勝選|当选|當選|胜出|勝出|击败|擊敗"
)
PROJECT_RE = re.compile(
    r"\bproject(?:s|ed|ing|ion|ions)\b|"
    r"\bcall(?:s|ed|ing)? (?:the |this )?(?:race|contest|seat|election)\b|"
    r"\bcalled (?:it )?for\b|\brace call\b|"
    r"\bproyect(?:a|ó|an|aron|ado|ada)\b|"
    r"\bprojet(?:é|ée|te|tent|tait)\b"
)
DECLARE_RE = re.compile(
    r"\bdeclared? (?:\w+ ){0,5}winner\b|"
    r"\bwinner (?:was |has been )?declared\b|"
    r"\bcertifi(?:es|ed)\b|\bproclam\w+|"
    r"\bdeclarad[oa] (?:\w+ ){0,2}(?:ganador|vencedor)\w*|"
    r"\bdéclaré\w* (?:\w+ ){0,2}(?:vainqueur|élu)\w*|"
    r"\b(?:объявлен|признан)\w* победител\w*|"
    r"(?:أعلن\w*|إعلان) فوز|宣布"
)
CONCEDE_RE = re.compile(
    r"\bconced(?:e|es|ed|ing|ió|ieron|ido)\b|\bconcession\b|\bconcéd\w+|"
    r"\b(?:reconoc|admit)\w+ (?:su |la )?derrota\b|"
    r"\b(?:reconn|admi)\w+ (?:sa |la )?défaite\b|"
    r"\bпризна\w+ (?:сво[её] )?поражени\w+|"
    r"(?:أقر|اعترف|يقر|يعترف|تقر|تعترف)\w* ب\w*هزيم\w*|"
    r"承认败选|承認敗選|承认失败|承認失敗|认输|認輸"
)
# A candidate claiming victory is not an outlet's or an official's call.
SELF_CLAIM_RE = re.compile(
    r"\b(?:declar|claim|proclaim)\w* (?:\w+ ){0,2}victor\w*|"
    r"\brevendiqu\w+ la victoire\b|"
    r"\bse (?:declar|proclam)\w+ (?:ganador|vencedor)\w*"
)
VOTE_COUNT_RE = re.compile(
    r"\bvotes?\b.{0,20}\bcounted\b|\bballots?\b.{0,20}\bcounted\b|"
    r"\bof (?:the )?(?:votes?|ballots?)\b|\bvote count\b|\breturns\b|"
    r"\bprecincts?\b|\breporting\b|\btall(?:y|ied)\b|\bcounted\b|"
    r"\b(?:early|partial) results\b|\bresults so far\b|"
    r"\bescrutinio\b|\bescrutad\w+|\bde los votos\b|\bvotos contados\b|"
    r"\bresultados parciales\b|"
    r"\bdépouillé\w*|\bdépouillement\b|\bdes voix\b|\bdes suffrages\b|"
    r"\brésultats partiels\b|"
    r"\bподсч\w+|\bбюллетен\w+|\bголосов\b|"
    r"فرز|الأصوات|计票|計票|开票|開票|选票|選票"
)

# What disqualifies a claim. Patterns run on casefolded text.
NEGATED_RE = re.compile(
    r"\b(?:not|no|never|neither|nor|without|yet|unable|cannot|too early|"
    r"too close|premature\w*|refus\w+|declin\w+|fail\w*|reject\w+|den(?:y|ies|"
    r"ied)|disput\w+|\w+n't)\b|"
    r"\bnunca\b|\bsin\b|\bne\b|\bn'|\bpas\b|\bsans\b|\bне\b|\bнет\b|"
    r"\bلم\b|\bلن\b|\bلا\b|未|没有|沒有|尚未"
)
CONDITIONAL_RE = re.compile(
    r"\b(?:could|would|might|may|should|if|unless|whether|likely|poised|"
    r"expected|hop(?:es?|ing)|aim(?:s|ing)?|seek(?:s|ing)?|tr(?:ies|ying)|"
    r"plans?|wants?|needs?|chances?|odds|favou?red|on track|set to|bid to|"
    r"next (?:week|month|year|tuesday))\b|"
    r"\bsi\b|\bpodría\b|\bpourrait\b|\bесли\b|\bможет\b|إذا|يأمل|如果|可能|或将|或將"
)
# "Will" and "to win" look ahead, unless a named caller is projecting a result.
FUTURE_RE = re.compile(r"\bwill\b|\bto win\b|\bgoing to\b")
CALLER_PROJECTS_RE = re.compile(
    r"(?:\bap\b|associated press|\bnbc\b|\bcnn\b|\bfox\b|\babc\b|\bcbs\b|"
    r"\bddhq\b|decision desk|\bnyt\b|new york times)(?: news)?"
    r"(?: decision desk)? (?:has |have )?(?:project|call)"
)
YEAR_RE = re.compile(r"\b(?:19|20)\d\d\b")
PAST_RE = re.compile(
    r"\b(?:first elected|previous(?:ly)?|prior|last (?:year|time|cycle|"
    r"election|november|month)|years? ago|ago|former(?:ly)?|in the past|since|"
    r"once|(?:won|carried) (?:the|this) state|presidential)\b"
)
PRIMARY_RE = re.compile(
    r"\bprimar(?:y|ies)\b|\bnominat\w+|\bcaucus\w*|\brunoff\b|"
    r"\bprimarias?\b|\bprimaires?\b|\bпраймериз\b|التمهيدية|初选|初選"
)
POLL_RE = re.compile(
    r"\bpolls?\b|\bpolling\b|\bpollsters?\b|\bsurveys?\b|\bforecast\w*|"
    r"\bpredict\w*|\bencuestas?\b|\bsondeos?\b|\bsondages?\b|\bопрос\w*|"
    r"استطلاع|民调|民調"
)
# The act of voting is not a poll result.
POLL_LOGISTICS_RE = re.compile(
    r"\bpolls? (?:close|closed|closes|closing|open|opened|opens|opening)\b|"
    r"\bpolling (?:place|station|location|site)s?\b|\bpoll workers?\b"
)
SPACE_RE = re.compile(r"\s+")
WORD_RE = re.compile(r"\w+", re.UNICODE)
CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]")
QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def normalize_text(text: str | None) -> str:
    return SPACE_RE.sub(" ", (text or "").translate(QUOTES)).strip().casefold()


def appears_in(quote: str | None, text: str) -> bool:
    """True when a quote is verbatim, ignoring spacing, quotes, and case."""
    needle = normalize_text(quote)
    return bool(needle) and needle in normalize_text(text)


def fold(text: str | None) -> str:
    """Normalized text with accents removed, for comparing names."""
    decomposed = unicodedata.normalize("NFKD", normalize_text(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _surname(name: str) -> str | None:
    words = [w for w in WORD_RE.findall(fold(name)) if w not in SUFFIXES]
    return words[-1] if words else None


def _has_word(text: str, word: str) -> bool:
    if CJK_RE.search(word):
        return word in text
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def names_person(names: Iterable[str | None], text: str) -> bool:
    """True when the text has the surname from any of these names."""
    folded = fold(text)
    surnames = (_surname(name) for name in names if name)
    return any(_has_word(folded, word) for word in surnames if word)


def normalize_caller(value: str | None) -> str:
    if not value or value.strip().casefold() in UNSPECIFIED:
        return "unspecified"
    for caller, pattern in CALLERS:
        if re.search(pattern, value, re.I):
            return caller
    return "other"


def checked_caller(value: str | None, text: str) -> str:
    """The caller, if the article names that caller."""
    caller = normalize_caller(value)
    if caller == "unspecified":
        return caller
    if caller == "other":
        return caller if appears_in(value, text) else "unspecified"
    pattern = dict(CALLERS)[caller]
    return caller if re.search(pattern, text) else "unspecified"


def _disqualified(evidence: str) -> str | None:
    """Why this language does not report a settled result, if it does not."""
    if NEGATED_RE.search(evidence):
        return "negated"
    if CONDITIONAL_RE.search(evidence):
        return "conditional"
    if FUTURE_RE.search(evidence) and not CALLER_PROJECTS_RE.search(evidence):
        return "conditional"
    if any(year != ELECTION_YEAR for year in YEAR_RE.findall(evidence)):
        return "past cycle"
    if PAST_RE.search(evidence):
        return "past cycle"
    if PRIMARY_RE.search(evidence):
        return "primary"
    if POLL_RE.search(POLL_LOGISTICS_RE.sub(" ", evidence)):
        return "poll"
    return None


def _winner_conceded(winner: str, evidence: str) -> bool:
    """True when the named winner is the one conceding."""
    surname = _surname(winner)
    if not surname:
        return False
    pattern = rf"(?<!\w){re.escape(surname)} (?:\w+ ){{0,2}}conced"
    return re.search(pattern, fold(evidence)) is not None


def _status_language(call: RaceCallB, evidence: str) -> bool:
    if call.status == "conceded":
        return bool(CONCEDE_RE.search(evidence)) and not _winner_conceded(
            call.winner_name or "", evidence
        )
    if SELF_CLAIM_RE.search(evidence):
        return False
    if WIN_RE.search(evidence):
        return True
    pattern = PROJECT_RE if call.status == "projected" else DECLARE_RE
    return bool(pattern.search(evidence))


def call_problem(
    call: RaceCallB, text: str, names: Iterable[str | None] = ()
) -> str | None:
    """Why a claim fails the evidence rules, or None if it passes.

    `names` adds other known names for the winner, such as renderings in
    other scripts, to the name the model gave.
    """
    if not appears_in(call.evidence, text):
        return "not verbatim"
    if call.status not in FINAL_STATUSES and call.status != "leading":
        return None
    evidence = normalize_text(call.evidence)
    if not call.winner_name:
        return "no winner named"
    if not names_person([call.winner_name, *names], call.evidence):
        return "winner not in evidence"
    if reason := _disqualified(evidence):
        return reason
    if call.status == "leading":
        return None if VOTE_COUNT_RE.search(evidence) else "no counted votes"
    return None if _status_language(call, evidence) else "no explicit language"


def accept_call(
    call: RaceCallB, text: str, names: Iterable[str | None] = ()
) -> bool:
    """Apply the evidence rules."""
    return call_problem(call, text, names) is None


def call_confidence(status: str, caller: str) -> float:
    if status in FINAL_STATUSES:
        return 0.9 if caller not in {"unspecified", "other"} else 0.7
    return 0.5


def call_row(
    item_id: str,
    call: RaceCallB,
    race_key: str | None,
    winner_entity_id: str | None,
    first_seen_at: Any,
    parser: str,
    version: str,
    text: str,
) -> dict[str, Any]:
    caller = checked_caller(call.called_by, text)
    return {
        "call_id": hash_id(
            item_id, race_key or call.race_desc, call.winner_name, call.status
        ),
        "item_id": item_id,
        "race_key": race_key,
        "race_desc": call.race_desc,
        "winner_name": call.winner_name,
        "winner_entity_id": winner_entity_id,
        "party": call.party,
        "called_by": caller,
        "status": call.status,
        "claimed_at_text": call.claimed_at_text,
        "confidence": call_confidence(call.status, caller),
        "evidence": call.evidence,
        "state": call.state,
        "office": call.office,
        "district": call.district,
        "first_seen_at": first_seen_at,
        "parser": parser,
        "parser_version": version,
    }
