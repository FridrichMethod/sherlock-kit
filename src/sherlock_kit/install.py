"""`shk rules KIND` prints packaged rules; `shk install-skill --project DIR` copies the skill to DIR/.claude
and DIR/.agents (marker after the front matter), --rules to DIR/.claude/rules unless ~/.claude/rules has it.
Idempotent; --check writes nothing and exits 1 when a copy differs. Never HOME itself, never via a symlink."""
from importlib import resources
from pathlib import Path

from sherlock_kit import __version__
from sherlock_kit.errors import EXIT_LOCAL, EXIT_OK, Outcome, ShkError

KINDS = ("workstation", "cluster")
SKILL = {"claude": ".claude/skills/sherlock-kit/SKILL.md", "codex": ".agents/skills/sherlock-kit/SKILL.md"}
RULES = ".claude/rules/sherlock-kit.md"
MARKER = f"<!-- installed by sherlock-kit {__version__} via shk install-skill; edits are overwritten -->\n"


def packaged(*parts: str) -> str:
    return (resources.files("sherlock_kit") / "data").joinpath(*parts).read_text(encoding="utf-8")


def register(subparsers) -> None:
    rules = subparsers.add_parser("rules", help="print the packaged Sherlock rules for agents")
    rules.add_argument("kind", choices=KINDS)
    rules.set_defaults(handler=lambda args, ctx: Outcome.text(packaged("rules", f"{args.kind}.md")))
    skill = subparsers.add_parser("install-skill", help="copy the sherlock-kit skill (and rules) into a project")
    skill.add_argument("--project", required=True, type=Path, metavar="DIR")
    skill.add_argument("--client", choices=("claude", "codex", "all"), default="all")
    skill.add_argument("--rules", choices=KINDS, help=f"also copy these rules to DIR/{RULES}")
    skill.add_argument("--check", action="store_true", help="write nothing; exit 1 if a copy is missing or stale")
    skill.add_argument("--force", action="store_true", help=f"copy --rules even when ~/{RULES} exists")
    skill.set_defaults(handler=install)


def wanted(args, home: Path) -> tuple[dict, list]:
    text, notices = packaged("skills", "sherlock-kit", "SKILL.md"), []
    end = text.index("\n---\n", 3) + 5  # front matter must stay first
    files = {SKILL[c]: text[:end] + MARKER + text[end:] for c in SKILL if args.client in (c, "all")}
    if args.client != "codex" and (home / ".claude/skills/sherlock-kit").exists():
        notices.append("~/.claude/skills/sherlock-kit exists: Claude Code uses that personal skill, not this copy")
    if args.rules and (home / RULES).exists() and not args.force:
        notices.append(f"~/{RULES} already loads in Claude Code: --rules skipped (--force copies anyway)")
    elif args.rules:
        files[RULES] = packaged("rules", f"{args.rules}.md")
    return files, notices


def install(args, ctx) -> Outcome:
    home, project = Path.home().resolve(), args.project.expanduser().resolve()
    if not project.is_dir() or project == home:
        raise ShkError(EXIT_LOCAL, "project_invalid", f"--project {args.project}: not a directory or HOME itself")
    files, notices = wanted(args, home)
    if linked := [str(project / rel) for rel in files if (project / rel).resolve() != project / rel]:
        raise ShkError(EXIT_LOCAL, "symlinked_target", f"{', '.join(linked)}: through a symlink; nothing written")
    states = {rel: "missing" if not (project / rel).exists() else "current" if (project / rel).is_file()
              and (project / rel).read_text(encoding="utf-8") == text else "stale" for rel, text in files.items()}
    for rel in [rel for rel, state in states.items() if state != "current" and not args.check]:
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(files[rel], encoding="utf-8")
        states[rel] = "written"
    failed = args.check and set(states.values()) != {"current"}
    return Outcome({"project": str(project), "files": states, "notices": notices}, EXIT_LOCAL if failed else EXIT_OK)
