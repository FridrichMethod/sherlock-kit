"""`shk rules` and `shk install-skill`: the packaged agent rules and skill of a site.

Both commands take the site from --site alone (Context.flag_site, default Sherlock) and never
read a configuration. SKILL below is the site's skill name: sherlock-kit on Sherlock,
sherlock-kit-marlowe on Marlowe, so the copies of both sites live side by side in one project.

`shk rules KIND` prints the site's packaged rules file of that kind verbatim.

`shk install-skill --project DIR` copies the site's packaged skill to
DIR/.claude/skills/SKILL/ (Claude Code) and DIR/.agents/skills/SKILL/ (Codex), with a
marker line after the front matter that names the shk version. `--rules KIND` also
copies the site's rules to DIR/.claude/rules/SKILL.md, unless ~/.claude/rules/SKILL.md
already loads in Claude Code (`--force` copies anyway). This compatibility check does
not require a global shk executable. Codex has no rules import, so it gets a hint
instead of a file: append the output of the site's `shk rules KIND` command.

Copies are idempotent. `--check` writes nothing and exits 1 when a copy is missing
or stale. It never writes into HOME itself or through a link that leaves DIR.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from sherlock_kit import __version__, sites
from sherlock_kit.errors import EXIT_LOCAL, EXIT_OK, Outcome, ShkError

if TYPE_CHECKING:
    from sherlock_kit.cli import Context

KINDS = ("workstation", "cluster")  # the rules kinds of every site
CLIENTS = ("claude", "codex")
SKILL_DIRS = {"claude": ".claude/skills", "codex": ".agents/skills"}
MARKER = f"<!-- installed by sherlock-kit {__version__} via shk install-skill; edits are overwritten -->\n"
FRONT_MATTER_END = "\n---\n"


@dataclass
class Wanted:
    """What install-skill would write: project-relative path -> text, plus notices for the reply."""

    files: dict[str, str] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)


def packaged(*parts: str) -> str:
    """The text of a data file shipped in sherlock_kit/data, given its path parts."""
    return (resources.files("sherlock_kit") / "data").joinpath(*parts).read_text(encoding="utf-8")


def rules_copy(site: sites.Site) -> str:
    """The project-relative rules copy of the site, also the path under HOME that skips it."""
    return f".claude/rules/{site.skill}.md"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the rules and install-skill commands."""
    rules = subparsers.add_parser("rules", help="print the packaged Sherlock rules for agents")
    rules.add_argument("kind", choices=KINDS)
    rules.set_defaults(handler=print_rules)

    skill = subparsers.add_parser(
        "install-skill", help="copy the sherlock-kit skill (and rules) into a project"
    )
    sherlock_rules = rules_copy(sites.SHERLOCK)
    skill.add_argument("--project", required=True, type=Path, metavar="DIR")
    skill.add_argument("--client", choices=(*CLIENTS, "all"), default="all")
    skill.add_argument(
        "--rules", choices=KINDS, help=f"also copy these rules to DIR/{sherlock_rules}"
    )
    skill.add_argument(
        "--check", action="store_true", help="write nothing; exit 1 if a copy is missing or stale"
    )
    skill.add_argument(
        "--force", action="store_true", help=f"copy --rules even when ~/{sherlock_rules} exists"
    )
    skill.set_defaults(handler=install)


def print_rules(args: argparse.Namespace, ctx: Context) -> Outcome:
    """`shk rules KIND`: the site's packaged rules file, verbatim."""
    return Outcome.text(packaged("rules", ctx.flag_site.rules[args.kind]))


def marked_skill(site: sites.Site) -> str:
    """The site's SKILL.md with the marker right after its front matter, which must stay first."""
    text = packaged("skills", site.skill, "SKILL.md")
    end = text.index(FRONT_MATTER_END, 3) + len(FRONT_MATTER_END)
    return text[:end] + MARKER + text[end:]


def wanted_files(args: argparse.Namespace, home: Path, site: sites.Site) -> Wanted:
    """The copies and notices for these arguments and site, given the user's home directory."""
    clients = CLIENTS if args.client == "all" else (args.client,)
    wanted = Wanted()
    skill = marked_skill(site)
    for client in clients:
        wanted.files[f"{SKILL_DIRS[client]}/{site.skill}/SKILL.md"] = skill
    personal = f"{SKILL_DIRS['claude']}/{site.skill}"
    if "claude" in clients and (home / personal).exists():
        wanted.notices.append(
            f"~/{personal} exists: Claude Code uses that personal skill, not this copy"
        )
    rules = rules_copy(site)
    if args.rules and "claude" in clients:
        if (home / rules).exists() and not args.force:
            wanted.notices.append(
                f"~/{rules} already loads in Claude Code: --rules skipped (--force copies anyway)"
            )
        else:
            wanted.files[rules] = packaged("rules", site.rules[args.rules])
    if args.rules and "codex" in clients:
        wanted.notices.append(
            f"Codex has no rules import: append the output of `{sites.command(site)} rules "
            f"{args.rules}` to the project's AGENTS.md if Codex should load it"
        )
    return wanted


def refuse_links_out_of(project: Path, paths: list[str]) -> None:
    """A link inside the project is fine; one that leads elsewhere (HOME, another checkout) is not."""
    outside = []
    for rel in paths:
        resolved = (project / rel).resolve()
        if not resolved.is_relative_to(project):
            outside.append(f"{rel} -> {resolved}")
    if outside:
        raise ShkError(
            EXIT_LOCAL,
            "target_outside_project",
            f"{'; '.join(outside)}: a link leads out of {project}; nothing written",
        )


def copy_state(path: Path, text: str) -> str:
    """missing, current (exactly the text install-skill would write now) or stale."""
    if not path.exists():
        return "missing"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return "current"
    return "stale"


def install(args: argparse.Namespace, ctx: Context) -> Outcome:
    """`shk install-skill`: write (or with --check only compare) the site's project copies."""
    home = Path.home().resolve()
    project = args.project.expanduser().resolve()
    if not project.is_dir() or project == home:
        raise ShkError(
            EXIT_LOCAL,
            "project_invalid",
            f"--project {args.project}: not a directory or HOME itself",
        )
    wanted = wanted_files(args, home, ctx.flag_site)
    refuse_links_out_of(project, list(wanted.files))
    states = {rel: copy_state(project / rel, text) for rel, text in wanted.files.items()}
    if not args.check:
        for rel, text in wanted.files.items():
            if states[rel] != "current":
                (project / rel).parent.mkdir(parents=True, exist_ok=True)
                (project / rel).write_text(text, encoding="utf-8")
                states[rel] = "written"
    failed = args.check and any(state != "current" for state in states.values())
    reply = {"project": str(project), "files": states, "notices": wanted.notices}
    return Outcome(reply, EXIT_LOCAL if failed else EXIT_OK)
