# Outage drill

Run 2026-11-01 21:24 UTC on a scratch lake.
200 feeds on a local server, 20 s offline, poll cadence shortened to 3 s.
12 of 12 checks passed.

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
| every feed catches up after 20 s offline | 6 s, 200 of 200 stories | pass |
| the gap is recorded | [('network',)] | pass |
| catch-up stories move to their published time | 200 moved | pass |
| normal polling resumes | 3 s | pass |
| a clock difference reads as sleep | sleep | pass |
| every feed catches up after sleep | 7 s | pass |
| the sleep gap has its true length | ('sleep', 45.0) | pass |
