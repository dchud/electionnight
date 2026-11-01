# Methods

This monitor reads news feeds during the 2026 US midterm elections and shows
how outlets cover the night. It reports what news outlets publish. It does not
report official results.

## Pipeline

One process, `pipeline`, writes to the lake. The dashboard process, `web`,
attaches the lake read-only and refreshes when a new snapshot appears.

```
config/sources.csv
       |
       v
  feed poller       tiers of 60, 180, and 600 s; conditional GET
       |
       v
  lang              detected language of title and summary
       |
       v
  classify_a        relevance, levels, states, topics, translation
       |
       v
  embed             384-dimension vector of title and summary
       |
       v
  dedupe            URL, title SimHash, and embedding near-duplicates
       |
       +----------------------+
       |                      |
       v                      v
  storylines            fetch_text       relevant items only
  online clusters             |
  and labels                  v
       |                 extract_b       entities, tone, places, race calls
       |                      |
       |                      v
       |                 entities, geo   resolve names and places
       |                      |
       +-----------+----------+
                   v
             DuckLake writer             commits a batch every 30 s
                   |
                   +--> rollups          gold.* tables
                   +--> briefs           cited summaries, on a schedule
                   |
                   v
             web (read-only)             in-memory facts, SSE, charts
```

Every row is append-only. A correction is a new row with a newer `created_at`,
and readers take the latest row. Each parser row records `parser` and
`parser_version`. To see them for one story, open its article page and expand
"Raw parser output".

## Models and versions

| Purpose | Model | Settings |
|---|---|---|
| Classification and translation | `claude-haiku-5-5` | Low effort, structured output, 10 items per call, cached system prompt |
| Deep extraction | `claude-haiku-5-5` | Structured output, 1 item per call, at most 6,000 input tokens |
| Storyline labels and summaries | `claude-haiku-5-5` | Structured output, on promotion and on growth |
| Entity resolution | `claude-haiku-5-5` | Structured output, batched |
| Briefs | `claude-sonnet-5-5` | Structured output, every sentence must cite supplied items |
| Embeddings | `intfloat/multilingual-e5-small` | Local ONNX on the CPU, 384 dimensions, `passage: ` and `query: ` prefixes |
| Language detection | `lingua` | Local |

The Methods page lists the configured models and the version of each parser in
service, below this document.

## Prompts, abridged

The models receive feed text as data and are told to ignore any instructions
inside it. All prompts ask for neutral wording with no party framing.

**Classification.** "You classify news items for a nonpartisan monitor of the
November 3, 2026 US midterm elections." An item is `election_relevant` when it
is about the 2026 US elections: campaigns, races, voting, results, election
administration, legal challenges, turnout, or the post-election transition of
power. Levels are `national`, `state`, and `local`, and an item may have
several. Topics and race types come from closed lists. Non-English items get
faithful, concise English translations of the headline and summary.

**Extraction.** "Extract only what the text states. Do not infer outcomes and
do not add outside knowledge." For each entity, tone is how the article
portrays it, from -2 to +2: "This is about the portrayal, not whether the news
is good for them." A tone needs a quote of 3 to 25 words copied verbatim from
the title, summary, or text, with the entity's name in the quote or within 200
characters of it. A tone whose quote fails this is discarded. A race call of
`projected`, `declared`, or `conceded` needs evidence that uses language for
that status and names the winner, and is not negated, conditional, about a
past election, or about a primary. A `leading` claim needs language about
counted votes, not polls. Every race call from an item published before polls
closed in that state is discarded, and before the first close nationally when
the state is unknown. A caller's name stays only if the article names that
caller. Rejecting a true claim costs less than publishing a false one, so these
rules also drop some true claims.

**Storyline labels.** The label is at most 8 words: "Name the event or subject,
not its significance." The summary is at most 2 sentences stating what the
headlines report, and each sentence ends with the references it rests on.

**Entity resolution.** "Null if the surface is too ambiguous to identify from
the context given. Do not guess."

**Briefs.** "Every sentence must rest on the supplied items." A sentence with
no citation is deleted. A citation is checked to be a supplied item, not
checked to support the claim. "Results are claims reported by news outlets, not
official results. Attribute them." Seats are never totaled. Each outlet
supplies at most 3 items to a brief.

## Metric definitions

**Volume.** Unique relevant items per bin. The Unique control switches to all
copies.

**Velocity and rising.** For a storyline or entity, *o* is the count in the
last 15 minutes and *e* is the count in the 120 minutes before that, times
15/120. The score is (*o* - *e*) / sqrt(*e* + 1). An entry is rising when the
score is at least 2 and *o* is at least 3. A storyline is new when its
`first_seen_at` is within 30 minutes. Rising Storylines lists only storylines
that meet the rule.

**State attention index.** The state's share of relevant items in the window,
divided by its share of relevant items over the baseline, November 1 12:00 to
November 3 12:00 ET. Both shares divide by all relevant items, including
stories that name no state, under the same Level, Language, and Unique
controls. Focus chooses which states are shown and does not change a state's
value. The ratio shrinks toward 1 with 5 pseudo-counts: (*n* + 5) / (*expected*
+ 5), where *expected* is the baseline share times the window's total. 1.0 is
normal for that state.

**States rising.** The attention index of the last 15 minutes, with the 120
minutes before them as the baseline. A state is listed when its value is above
1, largest first. A rise that every state shares leaves each state at 1.

**Tone aggregates.** The mean of mention-level or item-level tone, with a 95%
*t*-interval: mean plus or minus *t* times *s* / sqrt(*n*), where *t* is the
97.5th percentile with *n* - 1 degrees of freedom and *s* is the sample
standard deviation. The interval stops at the ends of the scale, -2 and +2. In
simulation with normally distributed tone, it covers the true mean 95% of the
time at *n* of 6, 14, 30, and 100.

- Hidden when *n* is under 5.
- Flagged "low n" when *n* is under 15. Charts draw the point hollow, and the
  map's tooltip says "low n".
- Always shown with *n*, the number of observations that have a tone.
- Opinion items are excluded.
- Figures that count all copies treat each copy as an observation. Copies of
  one story are not independent, so those intervals are narrower than the
  evidence supports.

**Coverage by lean.** For storyline *s* and lean bucket *b*: items(*s*, *b*) /
items(all relevant, *b*) in the window. This is the share of bucket *b*'s
election coverage devoted to *s*, so a bucket with more outlets does not
dominate. A bucket with no stories in the window has no share. It reads as no
data, not 0%, and does not count toward divergence.

**First to report.** The earliest `first_seen_at` per storyline among canonical
items. Poll timing makes this accurate to about 3 minutes at best.

**Race calls in the news.** Grouped by race and named candidate, with the
number of distinct outlets, their names, the earliest report, and who the
article says made the call. A caller's name comes from article text, so the
reporting outlets appear beside it. These are never added into a seat count.

**Copies.** Near-duplicates form a group with one canonical member. Metrics
count canonical members unless the Unique control says otherwise. Outlet-level
metrics, such as coverage by lean and the outlet lists, count all copies,
because republishing is that outlet's coverage choice.

**Evidence.** A count on the dashboard opens a drawer listing what it counted,
and the drawer's heading gives the same count. The heading also gives the
number of outlets, and the number of mentions when the count is of an entity's
mentions. The line under the heading names the filters and the time span, which
for some figures is not the global window: stories per minute covers the last
15 minutes, and Race Calls covers all time.

**Focus.** A story matches a race through race keys from extraction, or through
its classified state and race type. Without extraction, watching one House race
in a state matches all stories classified as that state's House coverage.

## Known limitations

**Paywalls.** Outlets with a hard paywall are not fetched, so their analysis
rests on the feed's title and summary. A metered outlet may return a partial
page, which is flagged as paywalled when it is short. Tone from a summary rests
on less text than tone from an article. The dashboard shows extracts and links
to the original, and never the full text.

**Polling latency.** Each outlet is polled every 60, 180, or 600 seconds by
tier, so `first_seen_at` trails publication by up to one polling interval. Rank
outlets by first report only with that margin in mind. Items wait in stages
before they reach the dashboard, and `/status` shows the lag per stage. Under
load the pipeline pauses backfill and throttles embedding; `/status` shows when
it is shedding.

**LLM error rates.** Classification, translation, extraction, and labels are
model output and contain errors. No error rate was measured against
hand-labeled items from this event. A set of golden examples tests the prompts'
contract, not their accuracy at scale. The ledger's "Not ok" column counts
calls that failed, not answers that were wrong. To check any number, open its
evidence and read the stories.

**Tone caveats.** Tone measures how the article portrays an entity, not
whether the news is good for it, and not the outlet's view. Treat a difference
between lean buckets as real only when their intervals do not overlap. The
intervals describe sampling only and do not include model error. Lean labels
rate an outlet as a whole, not each story.

**Redistricting.** Several states drew new congressional maps for 2026. Census
district boundary files may not match them, so there is no district map. House
races are listed by their `XX-NN` code, and county maps show local geography.
County data comes from place names matched against the Census gazetteer, so a
county named in a story is not proof the story is about all of that county. A
name shared by several counties, with no state to choose among them, gets no
county.

**Search.** Results rank by cosine similarity, which measures closeness of
meaning across languages, not truth or importance. Vectors cover the title and
summary, not the full text.

## Differences from the build specification

`docs/verify_log.md` records every checked fact and difference. These change
what the dashboard shows.

**Time axis.** The specification places every item at `first_seen_at`. On the
dashboard, an item published more than 2 hours before it was first seen sits at
its published time, so a feed's first fetch does not appear as one spike of
days-old stories. An item published during a collection gap, or up to 20
minutes before it, and first seen within 15 minutes after it also sits at its
published time, so a gap does not appear as one spike on return. The article
view shows the true first-seen time and says when and why an item was placed
this way. `gold.*` bins use `first_seen_at` unchanged, so they can differ from
dashboard counts for those items.

**Tone.** The specification shrinks a mean toward 0 with 5 pseudo-observations
and gives it a 95% interval from the standard error. An interval around the
shrunk mean does not cover the true mean 95% of the time: in simulation with a
true mean of -1.0 and a standard deviation of 0.5, it covered 32% at *n* of 6
and 83% at *n* of 100. The dashboard draws the unshrunk mean with a
*t*-interval, and the low-n flag marks small counts.

**Replay.** Replay shows what had been first seen by its time, wherever an item
sits on the time axis. The attention baseline, the active storyline count, and
the status that admits a storyline to Rising are as of that time. Storyline
labels, summaries, and assignments, and each item's classification and tone,
are current, and the replay header says so. Feed health is not shown for a past
moment.

**Catching up after a gap.** Collection reads RSS and Atom feeds. After a gap
of 5 minutes or more, it also reads each listed outlet's Google News sitemap
once, because a sitemap lists about two days of stories and a feed lists fewer.
Only stories published since shortly before the gap are added. They have no
feed summary and may come from sections the outlet's feed omits, so that outlet
can show more stories for that period than it would otherwise.

**Storyline thresholds.** The specification sets a join threshold of 0.82 and a
merge threshold of 0.88. Baseline pairwise similarity over relevant items has a
median of 0.794 and a 99th percentile of 0.860, so 0.82 places almost
everything in one cluster. The system uses a join threshold of 0.92 and a merge
threshold of 0.95. `scripts/tune_storylines.py` repeats the sweep.

## Cost

The ledger below this document sums `nlp.llm_calls`. Dollar figures are
estimates from token counts and the configured prices. The budget guard moves
low-signal sources to keyword classification at 80% of the daily cap, and stops
all LLM calls except briefs at the hard cap.
