# Degradation drill

Run 2026-11-01 21:02 UTC on a scratch lake.
20 of 20 checks passed.

The LLM rung used a real invalid key against the API, then the real key.
Its failure window was 5 s instead of 120 s, and the probe wait was skipped.
Disk levels used an injected free-space function. Web-slow used injected query times.

| Rung | Check | Seen | Result |
|---|---|---|---|
| LLM | invalid key moves Stage A to keywords (window 5 s, configured 120 s) | method=keywords, after 5 s | pass |
| LLM | last error names the failure | HTTP 401 | pass |
| LLM | strip input reports degraded | True | pass |
| LLM | Stage B holds items while degraded | True | pass |
| LLM | valid key: a probe recovers Stage A | method=llm | pass |
| LLM | degraded state clears | False | pass |
| LLM | Stage B resumes | ItemB | pass |
| Lag | lag over 120 s sheds load | True | pass |
| Lag | embed rate halves | 0.5 | pass |
| Lag | backfill pauses | True | pass |
| Lag | recovers when lag clears | False | pass |
| Lag | embed rate returns | 1.0 | pass |
| Disk | 9 GB free | WARN | pass |
| Disk | 4 GB free | DEGRADE | pass |
| Disk | 2 GB free | AUTOPRUNE | pass |
| Disk | 30 GB free | OK | pass |
| Web | p95 1000 ms is slow | True | pass |
| Web | refresh waits under 20 s | True | pass |
| Web | refresh goes out at 20 s | True | pass |
| Web | recovers when queries are fast | False | pass |
