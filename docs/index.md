# Election Night News Monitor

!!! warning "Agent-coded, for teaching"

    An AI coding agent, Claude Code, wrote this software for teaching purposes.
    It is not intended as production software, and it does not report official
    election results.

This monitor shows how news outlets cover an election as it happens. It reads a
few hundred news feeds, runs each story through a series of parsers, stores the
results in a [DuckLake](https://ducklake.select/), and shows them on a live
dashboard. It was built for a data science class on managing unstructured data,
held on the night of the US midterm elections, November 3, 2026.

The code is at
[github.com/dchud/electionnight](https://github.com/dchud/electionnight). These
pages describe a dashboard that runs on one laptop, and that dashboard is not
on this site. A "Live view" line names a page of the running dashboard.

## Teaching

- [Class walkthrough](walkthrough.md): the script for teaching from the "Under
  the hood" pages.
- [Methods](methods.md): how each number is produced, and its limits.

## Operating

- [Run of show](run_of_show.md): operating steps for election night, and what
  to do when something fails.
- [Verification log](verify_log.md): facts checked during the build, and where
  the specification differs.
