"""`shk rules` and `shk install-skill`: the packaged agent rules and skill.

`shk rules KIND` prints a packaged rules file verbatim.

`shk install-skill --project DIR` copies the packaged skill to
DIR/.claude/skills/sherlock-kit/ (Claude Code) and DIR/.agents/skills/sherlock-kit/
(Codex), with a marker line after the front matter that names the shk version.
`--rules KIND` also copies the rules to DIR/.claude/rules/sherlock-kit.md, unless
~/.claude/rules/sherlock-kit.md already loads in Claude Code (`--force` copies
anyway). Codex has no rules import, so it gets a hint instead of a file.

Copies are idempotent. `--check` writes nothing and exits 1 when a copy is missing
or stale. It never writes into HOME itself or through a link that leaves DIR.
"""
import argparse
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from sherlock_kit import __version__
from sherlock_kit.errors import EXIT_LOCAL, EXIT_OK, Outcome, ShkError

KINDS = ("workstation", "cluster")
CLIENTS = ("claude", "codex")
SKILL_COPIES = {"claude": ".claude/skills/sherlock-kit/SKILL.md", "codex": ".agents/skills/sherlock-kit/SKILL.md"}
RULES_COPY = ".claude/rules/sherlock-kit.md"
PERSONAL_SKILL = ".claude/skills/sherlock-kit"
MARKER = f"<!-- installed by sherlock-kit {__version__} via shk install-skill; edits are overwritten -->\n"
FRONT_MATTER_END = "\n---\n"


@dataclass
class Wanted:
    """What install-skill would write: project-relative path -> text, plus notices for the reply."""

    files: dict[str, str] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)


def packaged(*parts: str) -> str:
    return (resources.files("sherlock_kit") / "data").joinpath(*parts).read_text(encoding="utf-8")


def register(subparsers) -> None:
    rules = subparsers.add_parser("rules", help="print the packaged Sherlock rules for agents")
    rules.add_argument("kind", choices=KINDS)
    rules.set_defaults(handler=print_rules)

    skill = subparsers.add_parser("install-skill", help="copy the sherlock-kit skill (and rules) into a project")
    skill.add_argument("--project", required=True, type=Path, metavar="DIR")
    skill.add_argument("--client", choices=(*CLIENTS, "all"), default="all")
    skill.add_argument("--rules", choices=KINDS, help=f"also copy these rules to DIR/{RULES_COPY}")
    skill.add_argument("--check", action="store_true", help="write nothing; exit 1 if a copy is missing or stale")
    skill.add_argument("--force", action="store_true", help=f"copy --rules even when ~/{RULES_COPY} exists")
    skill.set_defaults(handler=install)


def print_rules(args: argparse.Namespace, ctx: object) -> Outcome:
    return Outcome.text(packaged("rules", f"{args.kind}.md"))


def marked_skill() -> str:
    """The packaged SKILL.md with the marker right after its front matter, which must stay first."""
    text = packaged("skills", "sherlock-kit", "SKILL.md")
    end = text.index(FRONT_MATTER_END, 3) + len(FRONT_MATTER_END)
    return text[:end] + MARKER + text[end:]


def wanted_files(args: argparse.Namespace, home: Path) -> Wanted:
    clients = CLIENTS if args.client == "all" else (args.client,)
    wanted = Wanted()
    skill = marked_skill()
    for client in clients:
        wanted.files[SKILL_COPIES[client]] = skill
    if "claude" in clients and (home / PERSONAL_SKILL).exists():
        wanted.notices.append(f"~/{PERSONAL_SKILL} exists: Claude Code uses that personal skill, not this copy")
    if args.rules and "claude" in clients:
        if (home / RULES_COPY).exists() and not args.force:
            wanted.notices.append(f"~/{RULES_COPY} already loads in Claude Code: --rules skipped "
                                  "(--force copies anyway)")
        else:
            wanted.files[RULES_COPY] = packaged("rules", f"{args.rules}.md")
    if args.rules and "codex" in clients:
        wanted.notices.append(f"Codex has no rules import: append the output of `shk rules {args.rules}` to the "
                              "project's AGENTS.md if Codex should load it")
    return wanted


def refuse_links_out_of(project: Path, paths: list[str]) -> None:
    """A link inside the project is fine; one that leads elsewhere (HOME, a dotfiles submodule) is not."""
    outside = []
    for rel in paths:
        resolved = (project / rel).resolve()
        if not resolved.is_relative_to(project):
            outside.append(f"{rel} -> {resolved}")
    if outside:
        raise ShkError(EXIT_LOCAL, "target_outside_project",
                       f"{'; '.join(outside)}: a link leads out of {project}; nothing written")


def copy_state(path: Path, text: str) -> str:
    """missing, current (exactly the text install-skill would write now) or stale."""
    if not path.exists():
        return "missing"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return "current"
    return "stale"


def install(args: argparse.Namespace, ctx: object) -> Outcome:
    home = Path.home().resolve()
    project = args.project.expanduser().resolve()
    if not project.is_dir() or project == home:
        raise ShkError(EXIT_LOCAL, "project_invalid", f"--project {args.project}: not a directory or HOME itself")
    wanted = wanted_files(args, home)
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
