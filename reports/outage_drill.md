# Outage drill

Run 2026-11-02 00:34 UTC on a scratch lake.
200 feeds on a local server, 20 s offline, poll cadence shortened to 3 s.
20 of 20 checks passed.

The local server stopped and refused connections for the outage.
Sleep was simulated with a clock difference and has not been
exercised here by closing the lid.

| Check | Seen | Result |
|---|---|---|
| all 200 feeds read before the outage | 3 s | pass |
| outage is noticed | network | pass |
| no feed is penalized | 0 of 200 with failures | pass |
| feed health is unchanged | {'healthy': 200, 'total': 200} | pass |
| no failed fetches are recorded | 0 | pass |
| every feed catches up after 20 s offline, with 25 stories published against a feed window of 10 | 7 s, 5000 of 5000 stories | pass |
| stories past the feed window come from sitemaps | 3000 | pass |
| the gap is recorded | [('network',)] | pass |
| catch-up stories move to their published time | 5000 moved | pass |
| normal polling resumes | 3 s | pass |
| a clock difference reads as sleep | sleep | pass |
| every feed catches up after sleep | 6 s | pass |
| the sleep gap has its true length | ('sleep', 45.0) | pass |
| a slow failing probe leaves the outage's start at the sleep | 2026-11-01 23:48:50.886088+00:00 | pass |
| catch-up is still owed during a short drop | 2026-11-01 23:48:50.886088+00:00 | pass |
| catch-up finishes after a drop under the catch-up minimum | 12 s | pass |
| one run lasted through the drop | same task | pass |
| every story from the sleep is read | 5000 of 5000 | pass |
| the gap row starts at the sleep | [('sleep', 45.0)] | pass |
| no feed is penalized by the sequence | {'healthy': 200, 'total': 200} | pass |
