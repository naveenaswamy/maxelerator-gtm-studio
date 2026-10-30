"""`maxel` command line.

    maxel onboard examples/startups/qubit-sense.json [--credits 500]
    maxel cohort import examples/cohort.csv
    maxel credits qubit-sense
    maxel top-up qubit-sense 200
    maxel run qubit-sense                      # local runner (stub or Anthropic)
    maxel show qubit-sense                     # print the latest playbook
    maxel band run --role strategist           # one BAND agent (one process per role)
    maxel band run --all                       # all five, as child processes (dev)
    maxel band kickoff qubit-sense             # message to post in the BAND room
    maxel flexprice ping                       # check FlexPrice credentials
    maxel serve [--port 8080]                  # HTTP API
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import sys
from pathlib import Path

import click

from .models import StartupProfile
from .studio import build_studio


def _studio():
    return build_studio()


@click.group()
@click.option("-v", "--verbose", is_flag=True)
def main(verbose: bool) -> None:
    """Maxelerator GTM Studio: BAND agents write GTM playbooks; FlexPrice bills the credits."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@main.command()
@click.argument("profile_json", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--credits", type=float, default=None, help="Credits to load (default from config/studio.yaml)")
def onboard(profile_json: Path, credits: float | None) -> None:
    """Register a startup and create its FlexPrice customer + credit wallet."""
    from .provisioning import onboard_startup

    studio = _studio()
    profile = StartupProfile(**json.loads(profile_json.read_text()))
    credits = credits if credits is not None else studio.config.default_credits_per_startup
    profile = onboard_startup(profile, studio.store, studio.meter, credits, studio.settings.wallet_currency)
    click.echo(f"Onboarded {profile.name} as {profile.startup_id}: {studio.tools.check_credits(profile.startup_id)}")


@main.group()
def cohort() -> None:
    """Bulk operations for a whole cohort."""


@cohort.command("import")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--credits", type=float, default=None)
def cohort_import(csv_path: Path, credits: float | None) -> None:
    """Onboard every row of a cohort CSV (columns = StartupProfile fields)."""
    from .provisioning import onboard_startup

    studio = _studio()
    credits = credits if credits is not None else studio.config.default_credits_per_startup
    ok = failed = 0
    with csv_path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            row = {k: v for k, v in row.items() if v not in (None, "")}
            if "sells_to_aws_customers" in row:
                row["sells_to_aws_customers"] = row["sells_to_aws_customers"].strip().lower() in {"1", "true", "yes", "y"}
            try:
                profile = onboard_startup(StartupProfile(**row), studio.store, studio.meter, credits, studio.settings.wallet_currency)
                click.echo(f"  ok      {profile.startup_id}")
                ok += 1
            except Exception as exc:  # keep going; report at the end
                click.echo(f"  FAILED  {row.get('startup_id', '?')}: {exc}", err=True)
                failed += 1
    click.echo(f"Imported {ok} startups, {failed} failed.")
    sys.exit(1 if failed else 0)


@main.command()
@click.argument("startup_id")
def credits(startup_id: str) -> None:
    """Show remaining credits and the cost of a playbook."""
    click.echo(_studio().tools.check_credits(startup_id))


@main.command("top-up")
@click.argument("startup_id")
@click.argument("amount", type=float)
def top_up_cmd(startup_id: str, amount: float) -> None:
    """Add credits to a startup's wallet."""
    from .provisioning import top_up

    studio = _studio()
    top_up(startup_id, amount, studio.store, studio.meter)
    click.echo(studio.tools.check_credits(startup_id))


@main.command()
@click.argument("startup_id")
def run(startup_id: str) -> None:
    """Build a playbook with the local runner (no BAND needed)."""
    from .llm import llm_from_settings
    from .local_runner import LocalStudio

    studio = _studio()
    playbook_id = LocalStudio(studio, llm_from_settings(studio.settings)).run(startup_id)
    meta = studio.store.get_meta(startup_id, playbook_id)
    click.echo(f"Done: {playbook_id}, {meta.credits_charged:g} credits charged.")
    click.echo(f"Read it with: maxel show {startup_id}")


@main.command()
@click.argument("startup_id")
def show(startup_id: str) -> None:
    """Print the latest finished playbook for a startup."""
    studio = _studio()
    meta = studio.store.latest_playbook(startup_id)
    md = studio.store.get_playbook_markdown(startup_id, meta.playbook_id)
    if md is None:
        raise click.ClickException(f"latest playbook {meta.playbook_id} is {meta.status}")
    click.echo(md)


@main.group()
def band() -> None:
    """Run the studio as BAND agents."""


@band.command("run")
@click.option("--role", help="strategist | market_analyst | pricing_architect | messaging_writer | launch_planner")
@click.option("--all", "all_roles", is_flag=True, help="Start every role as a child process")
def band_run(role: str | None, all_roles: bool) -> None:
    from .band_runtime import run_all, run_role

    studio = _studio()
    if all_roles:
        run_all(studio)
    elif role:
        asyncio.run(run_role(studio, role))
    else:
        raise click.UsageError("pass --role <role> or --all")


@band.command("kickoff")
@click.argument("startup_id")
def band_kickoff(startup_id: str) -> None:
    """Print the message that starts a playbook in a BAND room."""
    from .band_runtime import kickoff_message

    studio = _studio()
    studio.store.get_startup(startup_id)
    click.echo(kickoff_message(studio, startup_id))


@main.group()
def flexprice() -> None:
    """FlexPrice helpers."""


@flexprice.command("ping")
def flexprice_ping() -> None:
    """Send one zero-credit test event to confirm the API key and region."""
    from .metering import FlexPriceClient

    s = _studio().settings
    client = FlexPriceClient(s.flexprice_api_key, s.flexprice_base_url)
    client.ingest(client.event(s.flexprice_event_name, "maxel-ping", {"credits": 0, "action": "ping"}))
    click.echo(f"FlexPrice accepted a test event at {s.flexprice_base_url}. Check the Event Debugger.")


@main.group()
def pricing() -> None:
    """Assign pricing models (local Qwen or rules) and create them in FlexPrice."""


@pricing.command("recommend")
@click.argument("startup_id")
def pricing_recommend(startup_id: str) -> None:
    """Assign a pricing model with MAXEL_PRICING_LLM (openai = local Qwen, rules, anthropic)."""
    click.echo(_studio().tools.recommend_pricing(startup_id))


@pricing.command("show")
@click.argument("startup_id")
@click.option("--json", "as_json", is_flag=True)
def pricing_show(startup_id: str, as_json: bool) -> None:
    from .pricing import setup_markdown

    _, result = _studio().store.latest_pricing(startup_id)
    if as_json:
        click.echo(result.model_dump_json(indent=2))
        return
    click.echo(f"source: {result.source} · approved: {result.approved} · {result.created_at}")
    if result.errors:
        click.echo("validation errors from the model:\n  " + "\n  ".join(result.errors))
    click.echo(setup_markdown(result.recommendation))


@pricing.command("approve")
@click.argument("startup_id")
@click.option("--from-file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Mentor-edited recommendation JSON; replaces the model's answer before approval")
def pricing_approve(startup_id: str, from_file: Path | None) -> None:
    """Mark the latest pricing model as mentor-approved (approved results become training data)."""
    from .pricing import PricingRecommendation

    studio = _studio()
    key, result = studio.store.latest_pricing(startup_id)
    if from_file:
        result.recommendation = PricingRecommendation.model_validate_json(from_file.read_text())
        result.source = "mentor"
    result.approved = True
    studio.store.put_pricing_at(key, result)
    click.echo(f"Approved {result.recommendation.model} for {startup_id} (source: {result.source}).")


@pricing.command("apply")
@click.argument("startup_id")
@click.option("--execute", is_flag=True, help="Actually call FlexPrice. Without it, print the calls (dry run).")
@click.option("--api-key", envvar="STARTUP_FLEXPRICE_API_KEY", help="The STARTUP's FlexPrice API key (not Maxelerator's)")
@click.option("--base-url", envvar="STARTUP_FLEXPRICE_BASE_URL", default="https://us.api.flexprice.io/v1")
def pricing_apply(startup_id: str, execute: bool, api_key: str | None, base_url: str) -> None:
    """Create the assigned pricing model in the startup's FlexPrice: metered feature, plan, prices."""
    from .metering import FlexPriceClient
    from .pricing import apply_steps, flexprice_steps

    studio = _studio()
    _, result = studio.store.latest_pricing(startup_id)
    profile = studio.store.get_startup(startup_id)
    steps = flexprice_steps(result.recommendation, profile.name, startup_id)
    if not execute:
        if not result.approved:
            click.echo("Note: this pricing model is not mentor-approved yet (maxel pricing approve).", err=True)
        click.echo(json.dumps(steps, indent=2))
        click.echo(f"\nDry run: {len(steps)} calls. Re-run with --execute and the startup's API key to create them.")
        return
    if not result.approved:
        raise click.ClickException("approve the pricing model first: maxel pricing approve " + startup_id)
    if not api_key:
        raise click.ClickException("set STARTUP_FLEXPRICE_API_KEY (the startup's own FlexPrice key)")
    results = apply_steps(FlexPriceClient(api_key, base_url), steps)
    for name, data in results.items():
        click.echo(f"  {name}: {data.get('id', 'ok')}")
    click.echo("Created in FlexPrice. Subscribe customers to the plan to start billing.")


@main.group()
def playbook() -> None:
    """Playbook review."""


@playbook.command("approve")
@click.argument("startup_id")
@click.argument("playbook_id", required=False)
def playbook_approve(startup_id: str, playbook_id: str | None) -> None:
    """Mark a finished playbook as mentor-approved (its sections become training data)."""
    studio = _studio()
    meta = studio.store.get_meta(startup_id, playbook_id) if playbook_id else studio.store.latest_playbook(startup_id)
    if meta.status != "complete":
        raise click.ClickException(f"{meta.playbook_id} is {meta.status}")
    meta.approved = True
    studio.store.put_meta(meta)
    click.echo(f"Approved {meta.playbook_id} (written by {meta.llm or 'BAND agents'}).")


@main.group()
def training() -> None:
    """Build JSONL training data for the local Qwen model (Qwen Studio / mlx_lm.lora)."""


@training.command("pricing-seed")
@click.option("--count", default=120, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), default=Path("training/pricing_seed.jsonl"), show_default=True)
@click.option("--split", is_flag=True, help="Write train.jsonl/valid.jsonl into --out (a folder) for mlx_lm.lora")
def training_pricing_seed(count: int, out: Path, split: bool) -> None:
    """Synthetic profiles labelled by the rules: teaches Qwen the pricing JSON format."""
    from .training import pricing_seed, write_jsonl

    paths = write_jsonl(pricing_seed(count), out, split=split)
    click.echo(f"Wrote {count} examples: " + ", ".join(map(str, paths)))


@training.command("export")
@click.option("--task", type=click.Choice(["pricing", "sections", "all"]), default="all")
@click.option("--out", type=click.Path(path_type=Path), default=Path("training/approved.jsonl"), show_default=True)
@click.option("--include-unapproved", is_flag=True, help="Also export results no mentor approved (not recommended)")
@click.option("--with-seed", default=0, help="Mix in N synthetic pricing examples")
@click.option("--split", is_flag=True)
def training_export(task: str, out: Path, include_unapproved: bool, with_seed: int, split: bool) -> None:
    """Export mentor-approved pricing models and playbook sections as training data."""
    from .training import export_pricing, export_sections, pricing_seed, write_jsonl

    studio = _studio()
    rows = []
    if task in ("pricing", "all"):
        rows += export_pricing(studio.store, approved_only=not include_unapproved)
    if task in ("sections", "all"):
        rows += export_sections(studio.store, studio.config, approved_only=not include_unapproved)
    if with_seed:
        rows += pricing_seed(with_seed)
    if not rows:
        raise click.ClickException("nothing to export yet: approve pricing models or playbooks first")
    paths = write_jsonl(rows, out, split=split)
    click.echo(f"Wrote {len(rows)} examples: " + ", ".join(map(str, paths)))
    if len(rows) < 10:
        click.echo("Qwen Studio needs at least 10 distinct examples; add --with-seed 100.", err=True)


@main.command()
@click.option("--host", default="0.0.0.0")
@click.option("--port", default=8080, type=int)
def serve(host: str, port: int) -> None:
    """Run the HTTP API."""
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
