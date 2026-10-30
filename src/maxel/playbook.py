"""Assemble saved sections into the finished playbook Markdown."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .models import PlaybookMeta, StartupProfile, StudioConfig, utcnow


def strip_leading_heading(markdown: str) -> str:
    """Agents sometimes repeat the section title as a heading; the template adds its own."""
    lines = markdown.strip().splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def render_playbook(
    templates_dir: Path,
    config: StudioConfig,
    profile: StartupProfile,
    meta: PlaybookMeta,
    sections: dict[str, str],
) -> str:
    env = Environment(loader=FileSystemLoader(str(templates_dir)), undefined=StrictUndefined, keep_trailing_newline=True)
    template = env.get_template("playbook.md.j2")
    # Executive summary first, then the rest in configured order.
    ordered = sorted(config.sections, key=lambda s: (not s.final, config.sections.index(s)))
    rendered = [
        {"title": s.title, "body": strip_leading_heading(sections[s.id]), "owner": config.roles[s.owner].display_name}
        for s in ordered
        if s.id in sections
    ]
    return template.render(
        studio=config.name,
        profile=profile,
        meta=meta,
        sections=rendered,
        generated_at=utcnow(),
    )
