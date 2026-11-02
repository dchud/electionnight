# Lean ratings: owner review

Proposed media-bias labels for the enabled US outlets in config/sources.csv
whose lean_label is not NA. Source file: config/lean_ratings.csv. Every row has
confirmed=false; nothing here is final until you confirm it. Retrieved
2026-11-01.

Method: one web search per outlet, reading AllSides pages as returned by
search. Direct fetches of allsides.com returned HTTP 403 (WebFetch, plus one
curl retry), so every AllSides label and confidence level below comes from
search-result text, not from a page I loaded myself. I stopped there rather
than work around the block. Ad Fontes values were fetched directly from each
adfontesmedia.com page listed in the CSV. The seed lean_hint was used only for
the comparison column, never to fill a label.

AllSides confidence is low or initial for most state and local outlets (see
notes in the CSV). Several AllSides pages say they rate online written content
only, not broadcast.

## Counts

| Result | Count |
|---|---|
| Rated Left (L) | 8 |
| Rated Lean Left (LL) | 33 |
| Rated Center (C) | 56 |
| Rated Lean Right (LR) | 11 |
| Rated Right (R) | 3 |
| **Rated total** | **111** |
| Unrated: no AllSides page found | 41 |
| Unrated: related page, Not Rated, or conflicting evidence (see Flag) | 12 |
| Not researched | 0 |
| **Total outlets** | **164** |

Comparison with the seed lean_hint:

| Comparison | Count |
|---|---|
| DISAGREE | 12 |
| agree | 28 |
| new rating (hint was Unrated) | 71 |
| both Unrated | 46 |
| NO RATING FOUND where the hint had a label | 7 |

DISAGREE means both sides carry a label and the labels differ. NO RATING FOUND
means the seed expected a label but I found no usable AllSides page. Rows are
ordered: disagreements, no rating found, agreements, new ratings, both Unrated.

## Review table

| Outlet | Proposed | Seed hint | Comparison | Flag |
|---|---|---|---|---|
| Bloomberg | C | LL | DISAGREE | AllSides moved Lean Left to Center in 2026; low confidence |
| PBS News | LL | C | DISAGREE | AllSides page is PBS NewsHour |
| CNBC | LL | C | DISAGREE |  |
| Semafor | LL | C | DISAGREE |  |
| National Review | LR | R | DISAGREE | opinion section rated Right separately |
| Washington Examiner | LR | R | DISAGREE |  |
| The Free Press | LR | C | DISAGREE | URL slug inferred from page title; reviewers split Center/Lean Right |
| Reason | C | LR | DISAGREE |  |
| The Bulwark | L | LL | DISAGREE |  |
| Bolts | L | LL | DISAGREE |  |
| La Opinión | L | LL | DISAGREE |  |
| The Boston Globe | L | LL | DISAGREE |  |
| Punchbowl News | Unrated | C | NO RATING FOUND (hint C) |  |
| NOTUS | Unrated | C | NO RATING FOUND (hint C) |  |
| The 19th | Unrated | LL | NO RATING FOUND (hint LL) |  |
| Inside Elections | Unrated | C | NO RATING FOUND (hint C) |  |
| Silver Bulletin | Unrated | C | NO RATING FOUND (hint C) | AllSides rates Nate Silver personally: Lean Left (low confidence) |
| Election Law Blog | Unrated | C | NO RATING FOUND (hint C) |  |
| Stateline | Unrated | LL | NO RATING FOUND (hint LL) |  |
| UPI | C | C | agree |  |
| The New York Times | LL | LL | agree | opinion section rated Left separately |
| The Wall Street Journal | C | C | agree | opinion section rated Lean Right separately |
| Fox News | R | R | agree |  |
| NBC News | LL | LL | agree |  |
| CBS News | LL | LL | agree |  |
| ABC News | LL | LL | agree |  |
| NPR | LL | LL | agree | opinion rated separately |
| MS NOW (formerly MSNBC) | L | L | agree | AllSides page is titled MSNBC |
| Politico | LL | LL | agree |  |
| The Hill | C | C | agree | near Lean Left border; 2026 reviews split |
| Axios | LL | LL | agree |  |
| Roll Call | C | C | agree |  |
| Newsweek | C | C | agree |  |
| Time | LL | LL | agree | two AllSides pages; canonical unclear |
| The Atlantic | L | L | agree |  |
| Vox | L | L | agree |  |
| Christian Science Monitor | C | C | agree |  |
| Business Insider | LL | LL | agree | two AllSides pages; canonical unclear |
| The Dispatch | LR | LR | agree |  |
| New York Post | R | R | agree | opinion rated separately |
| The Washington Times | LR | LR | agree |  |
| Washington Free Beacon | R | R | agree | two AllSides pages; canonical unclear |
| Deseret News (national politics) | C | C | agree | rating is for Deseret News overall |
| RealClearPolitics | LR | LR | agree | original content Lean Right; RCP aggregator rated Center separately |
| Votebeat | C | C | agree |  |
| Ballotpedia News | C | C | agree | rating is for Ballotpedia overall |
| Los Angeles Times | LL | LL | agree |  |
| Alaska Beacon | C | Unrated | new rating (hint was Unrated) |  |
| Arizona Mirror | C | Unrated | new rating (hint was Unrated) |  |
| CalMatters | C | Unrated | new rating (hint was Unrated) |  |
| Colorado Newsline | C | Unrated | new rating (hint was Unrated) |  |
| The Colorado Sun | LL | Unrated | new rating (hint was Unrated) |  |
| CT Mirror | C | Unrated | new rating (hint was Unrated) |  |
| Georgia Recorder | C | Unrated | new rating (hint was Unrated) |  |
| Honolulu Civil Beat | C | Unrated | new rating (hint was Unrated) |  |
| Iowa Capital Dispatch | C | Unrated | new rating (hint was Unrated) |  |
| Iowa Public Radio | C | Unrated | new rating (hint was Unrated) |  |
| Louisiana Illuminator | LL | Unrated | new rating (hint was Unrated) |  |
| Maine Public | LL | Unrated | new rating (hint was Unrated) | AllSides page is titled Maine Public Radio |
| Maryland Matters | LL | Unrated | new rating (hint was Unrated) |  |
| Michigan Advance | C | Unrated | new rating (hint was Unrated) |  |
| Bridge Michigan | C | Unrated | new rating (hint was Unrated) | AllSides page is titled Bridgemi.com |
| Michigan Public | C | Unrated | new rating (hint was Unrated) | AllSides page is titled NPR Michigan Radio |
| Minnesota Reformer | L | Unrated | new rating (hint was Unrated) |  |
| MPR News | LL | Unrated | new rating (hint was Unrated) | second AllSides page (Minnesota Public Radio) says Center |
| Mississippi Today | C | Unrated | new rating (hint was Unrated) | second AllSides page (Mississippi Today News) says Lean Left |
| Missouri Independent | LL | Unrated | new rating (hint was Unrated) |  |
| Daily Montanan | C | Unrated | new rating (hint was Unrated) |  |
| Montana Free Press | LL | Unrated | new rating (hint was Unrated) | second AllSides page says Not Rated |
| Nevada Current | C | Unrated | new rating (hint was Unrated) |  |
| The Nevada Independent | C | Unrated | new rating (hint was Unrated) |  |
| New Hampshire Bulletin | C | Unrated | new rating (hint was Unrated) |  |
| NHPR | C | Unrated | new rating (hint was Unrated) | AllSides page is titled New Hampshire Public Radio |
| New Jersey Monitor | LL | Unrated | new rating (hint was Unrated) | confidence level not captured |
| NJ Spotlight News | LL | Unrated | new rating (hint was Unrated) |  |
| New York Focus | C | Unrated | new rating (hint was Unrated) |  |
| Ohio Capital Journal | LL | Unrated | new rating (hint was Unrated) |  |
| Oklahoma Watch | C | Unrated | new rating (hint was Unrated) |  |
| OPB | C | Unrated | new rating (hint was Unrated) | AllSides page is titled Oregon Public Broadcasting |
| Pennsylvania Capital-Star | LR | Unrated | new rating (hint was Unrated) |  |
| Spotlight PA | C | Unrated | new rating (hint was Unrated) |  |
| Tennessee Lookout | LL | Unrated | new rating (hint was Unrated) |  |
| The Texas Tribune | LL | Unrated | new rating (hint was Unrated) |  |
| VTDigger | C | Unrated | new rating (hint was Unrated) |  |
| Vermont Public | C | Unrated | new rating (hint was Unrated) |  |
| Virginia Mercury | C | Unrated | new rating (hint was Unrated) |  |
| Wisconsin Examiner | LL | Unrated | new rating (hint was Unrated) |  |
| Wisconsin Public Radio | C | Unrated | new rating (hint was Unrated) |  |
| Wisconsin Watch | LL | Unrated | new rating (hint was Unrated) |  |
| WyoFile | LR | Unrated | new rating (hint was Unrated) |  |
| Cowboy State Daily | LR | Unrated | new rating (hint was Unrated) |  |
| AL.com | C | Unrated | new rating (hint was Unrated) |  |
| KQED | C | Unrated | new rating (hint was Unrated) |  |
| Tampa Bay Times | C | Unrated | new rating (hint was Unrated) |  |
| Honolulu Star-Advertiser | C | Unrated | new rating (hint was Unrated) |  |
| Chicago Sun-Times | LL | Unrated | new rating (hint was Unrated) |  |
| The Advocate (Baton Rouge) | C | Unrated | new rating (hint was Unrated) | several similar AllSides pages; canonical unclear |
| The Times-Picayune / NOLA.com | C | Unrated | new rating (hint was Unrated) | AllSides page is titled The Times-Picayune |
| Portland Press Herald | C | Unrated | new rating (hint was Unrated) |  |
| Bangor Daily News | LL | Unrated | new rating (hint was Unrated) |  |
| The Baltimore Banner | C | Unrated | new rating (hint was Unrated) |  |
| WBUR | C | Unrated | new rating (hint was Unrated) |  |
| Star Tribune | LL | Unrated | new rating (hint was Unrated) |  |
| St. Louis Post-Dispatch | C | Unrated | new rating (hint was Unrated) |  |
| Omaha World-Herald | C | Unrated | new rating (hint was Unrated) |  |
| New Hampshire Union Leader | C | Unrated | new rating (hint was Unrated) |  |
| NJ.com | LL | Unrated | new rating (hint was Unrated) |  |
| Santa Fe New Mexican | C | Unrated | new rating (hint was Unrated) |  |
| Gothamist | C | Unrated | new rating (hint was Unrated) |  |
| OregonLive | C | Unrated | new rating (hint was Unrated) | AllSides page is titled The Oregonian |
| The Philadelphia Inquirer | LL | Unrated | new rating (hint was Unrated) | two AllSides pages |
| Pittsburgh Post-Gazette | C | Unrated | new rating (hint was Unrated) |  |
| The Post and Courier | LR | Unrated | new rating (hint was Unrated) |  |
| The Salt Lake Tribune | LL | Unrated | new rating (hint was Unrated) |  |
| Richmond Times-Dispatch | LR | Unrated | new rating (hint was Unrated) | two AllSides pages |
| Cardinal News | C | Unrated | new rating (hint was Unrated) |  |
| The Seattle Times | C | Unrated | new rating (hint was Unrated) |  |
| KUOW | C | Unrated | new rating (hint was Unrated) |  |
| Alabama Reflector | Unrated | Unrated | both Unrated |  |
| Alabama Daily News | Unrated | Unrated | both Unrated |  |
| Alaska Public Media | Unrated | Unrated | both Unrated | story-page badges say Center; rating page URL not found |
| Arkansas Advocate | Unrated | Unrated | both Unrated |  |
| CPR News | Unrated | Unrated | both Unrated |  |
| Connecticut Public | Unrated | Unrated | both Unrated | AllSides page says Not Rated (may be stale) |
| Spotlight Delaware | Unrated | Unrated | both Unrated |  |
| Florida Phoenix | Unrated | Unrated | both Unrated |  |
| GPB News | Unrated | Unrated | both Unrated |  |
| Idaho Capital Sun | Unrated | Unrated | both Unrated |  |
| Capitol News Illinois | Unrated | Unrated | both Unrated |  |
| Indiana Capital Chronicle | Unrated | Unrated | both Unrated |  |
| Kansas Reflector | Unrated | Unrated | both Unrated | page says Not Rated; story badges say Lean Left |
| Kentucky Lantern | Unrated | Unrated | both Unrated |  |
| Maine Morning Star | Unrated | Unrated | both Unrated |  |
| CommonWealth Beacon | Unrated | Unrated | both Unrated |  |
| Nebraska Examiner | Unrated | Unrated | both Unrated |  |
| Flatwater Free Press | Unrated | Unrated | both Unrated |  |
| Source NM | Unrated | Unrated | both Unrated |  |
| City & State New York | Unrated | Unrated | both Unrated |  |
| NC Newsline | Unrated | Unrated | both Unrated |  |
| WUNC | Unrated | Unrated | both Unrated |  |
| North Dakota Monitor | Unrated | Unrated | both Unrated |  |
| Statehouse News Bureau (Ohio) | Unrated | Unrated | both Unrated |  |
| Oklahoma Voice | Unrated | Unrated | both Unrated |  |
| Oregon Capital Chronicle | Unrated | Unrated | both Unrated | page says Not Rated (may be stale); story badges say Center |
| Rhode Island Current | Unrated | Unrated | both Unrated |  |
| SC Daily Gazette | Unrated | Unrated | both Unrated |  |
| South Dakota Searchlight | Unrated | Unrated | both Unrated |  |
| South Dakota Public Broadcasting | Unrated | Unrated | both Unrated |  |
| Utah News Dispatch | Unrated | Unrated | both Unrated |  |
| VPM News | Unrated | Unrated | both Unrated |  |
| Washington State Standard | Unrated | Unrated | both Unrated |  |
| West Virginia Watch | Unrated | Unrated | both Unrated |  |
| Mountain State Spotlight | Unrated | Unrated | both Unrated | AllSides page says Not Rated (may be stale) |
| Anchorage Daily News | Unrated | Unrated | both Unrated | story-page badges say Center; rating page URL not found |
| KJZZ | Unrated | Unrated | both Unrated | AllSides page found is KJZZ Fronteras, Not Rated |
| WBEZ | Unrated | Unrated | both Unrated |  |
| KCUR | Unrated | Unrated | both Unrated |  |
| St. Louis Public Radio | Unrated | Unrated | both Unrated |  |
| Las Vegas Review-Journal | Unrated | Unrated | both Unrated | AllSides page says Not Rated (may be stale) |
| Bismarck Tribune | Unrated | Unrated | both Unrated | story-page badges say Center; rating page URL not found |
| Cleveland.com | Unrated | Unrated | both Unrated | AllSides rates Cleveland Plain Dealer: Center (not applied) |
| WHYY | Unrated | Unrated | both Unrated | story-page badges say Center; rating page URL not found |
| WPLN | Unrated | Unrated | both Unrated |  |
| KUT | Unrated | Unrated | both Unrated |  |
