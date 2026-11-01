"""The `monitor` command line.

Commands import their implementation on call, so `monitor --help` stays fast
and does not load model or database libraries.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from monitor.settings import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
sources_app = typer.Typer(no_args_is_help=True, help="Manage news sources.")
ref_app = typer.Typer(no_args_is_help=True, help="Build reference data.")
app.add_typer(sources_app, name="sources")
app.add_typer(ref_app, name="ref")


@app.command()
def init() -> None:
    """Create directories and lake tables, and set lake options."""
    from monitor.lake import init_lake

    init_lake(get_settings())
    typer.echo("lake initialized")


@app.command()
def verify() -> None:
    """Run scriptable environment checks and update the verification log."""
    from monitor.verify import run_verify

    raise typer.Exit(run_verify(get_settings()))


@sources_app.command("discover")
def sources_discover(
    limit: int = typer.Option(0, help="Only process the first N outlets."),
) -> None:
    """Find and validate feeds, then write sources.csv and the report."""
    from monitor.ingest.discover import run_discover

    run_discover(get_settings(), limit=limit or None)


@sources_app.command("check")
def sources_check() -> None:
    """Fetch-test all enabled feeds once."""
    from monitor.ingest.discover import run_check

    raise typer.Exit(run_check(get_settings()))


@sources_app.command("export-opml")
def sources_export_opml(path: Path) -> None:
    """Write enabled feeds to an OPML file."""
    from monitor.ingest.opml import export_opml

    typer.echo(f"{export_opml(get_settings(), path)} feeds written")


@sources_app.command("import-opml")
def sources_import_opml(path: Path) -> None:
    """Apply an OPML file's feed set to sources.csv."""
    from monitor.ingest.opml import import_opml

    typer.echo(import_opml(get_settings(), path))


@ref_app.command("build-geo")
def ref_build_geo() -> None:
    """Build states, counties, places, and map files."""
    from monitor.ref.gazetteer import build_geo

    build_geo(get_settings())


@ref_app.command("build-candidates")
def ref_build_candidates() -> None:
    """Build the candidate and entity roster."""
    from monitor.ref.candidates import build_candidates

    build_candidates(get_settings())


@ref_app.command("build-races")
def ref_build_races(
    dry_run: bool = typer.Option(False, help="Report without writing."),
) -> None:
    """Fetch race ratings and build the watchlist."""
    from monitor.ref.races import build_races

    build_races(get_settings(), dry_run=dry_run)


@ref_app.command("build-aliases")
def ref_build_aliases() -> None:
    """Generate entity aliases with Haiku."""
    from monitor.ref.candidates import build_aliases

    build_aliases(get_settings())


@ref_app.command("all")
def ref_all() -> None:
    """Run every reference build in order."""
    from monitor.ref.candidates import build_aliases, build_candidates
    from monitor.ref.gazetteer import build_geo
    from monitor.ref.races import build_races

    settings = get_settings()
    build_geo(settings)
    build_races(settings, dry_run=False)
    build_candidates(settings)
    build_aliases(settings)


@app.command()
def pipeline(
    no_llm: bool = typer.Option(False, "--no-llm", help="Keyword path only."),
    stages: str = typer.Option("", help="Comma-separated stage names."),
) -> None:
    """Run the writer process: poller, parsers, rollups, maintenance."""
    from monitor.pipeline import run_pipeline

    names = [s.strip() for s in stages.split(",") if s.strip()]
    run_pipeline(get_settings(), no_llm=no_llm, stages=names or None)


@app.command()
def web(
    host: str | None = typer.Option(None),
    port: int | None = typer.Option(None),
    lan: bool = typer.Option(False, help="Listen on all interfaces."),
    reload: bool = typer.Option(
        False,
        "--reload",
        help="Restart when web code changes, for live edits.",
    ),
) -> None:
    """Run the read-only dashboard."""
    from monitor.web.app import serve

    settings = get_settings()
    bind = "0.0.0.0" if lan else (host or settings.web.host)
    serve(settings, host=bind, port=port or settings.web.port, reload=reload)


@app.command()
def rollups(
    rebuild: bool = typer.Option(False, help="Rebuild every bin."),
) -> None:
    """Recompute gold tables."""
    from monitor.control import run_or_request

    typer.echo(run_or_request(get_settings(), "rollups", rebuild=rebuild))


@app.command()
def brief(
    now: bool = typer.Option(False, "--now", help="Write a brief right now."),
    focus: str = typer.Option("all", help="all, battleground, or state:XX."),
) -> None:
    """Generate a cited brief."""
    from monitor.control import run_or_request

    if not now:
        raise typer.BadParameter("pass --now to write a brief")
    typer.echo(run_or_request(get_settings(), "brief", focus=focus))


@app.command()
def disk() -> None:
    """Show disk use by category and free space."""
    from monitor.disk import report_disk

    typer.echo(report_disk(get_settings()))


@app.command()
def prune(
    thin: str | None = typer.Option(None, help="Keep one snapshot per span."),
    older_than: str | None = typer.Option(None, help="Used with --thin."),
    before: datetime | None = typer.Option(
        None, help="Expire all snapshots before this time."
    ),
    all_but_latest: bool = typer.Option(False, "--all-but-latest"),
    backups: int | None = typer.Option(None, help="Keep newest N backups."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Expire snapshots and old files. Current data rows are never deleted."""
    from monitor.control import run_or_request

    typer.echo(
        run_or_request(
            get_settings(),
            "prune",
            thin=thin,
            older_than=older_than,
            before=before.isoformat() if before else None,
            all_but_latest=all_but_latest,
            backups=backups,
            dry_run=dry_run,
        )
    )


@app.command()
def backup(
    now: bool = typer.Option(False, "--now", help="Back up the catalog now."),
) -> None:
    """Back up the lake catalog."""
    from monitor.maintenance import backup_catalog

    if not now:
        raise typer.BadParameter("pass --now to run a backup")
    typer.echo(backup_catalog(get_settings()))


@app.command()
def offload(
    lake: bool = typer.Option(
        False, "--lake", help="Also upload the current Parquet files."
    ),
) -> None:
    """Copy archives and catalog backups to the configured S3 bucket."""
    from monitor.offload import Offloader, is_configured

    settings = get_settings()
    if not is_configured(settings):
        raise typer.BadParameter("no S3 bucket is configured in .env")
    offloader = Offloader(settings)
    typer.echo(offloader.upload_lake() if lake else offloader.run())


@app.command("replay-check")
def replay_check(
    from_: datetime = typer.Option(..., "--from"),
    to: datetime = typer.Option(..., "--to"),
) -> None:
    """Report item counts by bin for a time range."""
    from monitor.rollups import replay_report

    typer.echo(replay_report(get_settings(), from_, to))


@app.command()
def cost() -> None:
    """Report LLM spend by purpose, model, and day."""
    from monitor.llm.budget import cost_report

    typer.echo(cost_report(get_settings()))
