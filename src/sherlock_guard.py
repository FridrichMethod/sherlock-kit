"""Pure, narrow PreToolUse decisions; unknown commands are never certified safe.

No network calls, filesystem inspection, local state, or command execution occurs.
Only recognized executable positions are checked; arbitrary shells are not parsed.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import posixpath
import shlex
import sys

MAX_INPUT_BYTES = 128 * 1024
MAX_COMMAND_CHARS = 32 * 1024
KNOWN_PURGE_HELPERS = frozenset({
    "sherlock-scratch-purge-refresh", "sherlock-scratch-purge-keepalive",
})
_SCHEDULER_READS = frozenset({"squeue", "sacct", "sstat", "sinfo"})
_HOSTS = frozenset({"sherlock", "sherlock-plain", "sherlock-dtn",
                    "sherlock.stanford.edu", "login.sherlock.stanford.edu",
                    "dtn.sherlock.stanford.edu"})
_SHELL_OPERATORS = frozenset({";", "&&", "||", "|", "&", "<", ">", "<<", ">>", "(", ")"})


@dataclass(frozen=True)
class GuardDecision:
    status: str = "unknown"
    reason: str = "No proven violation recognized; normal permissions and policy apply"
    rule: str | None = None


def _tokens(command):
    if not isinstance(command, str) or not command or len(command) > MAX_COMMAND_CHARS or "\0" in command:
        return None
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None
    # Only a deliberately small simple-command grammar. Shell syntax is unknown.
    if any(token in _SHELL_OPERATORS for token in tokens):
        return None
    return tokens or None


def _name(token):
    return posixpath.basename(token)


def _remote(tokens):
    """Recognize a direct OpenSSH command and a literal Sherlock destination."""
    if not tokens or _name(tokens[0]) != "ssh":
        return None
    index = 1
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        if option == "--":
            index += 1
            break
        if option in {"-T", "-t", "-tt", "-n", "-q", "-v", "-vv", "-vvv", "-x", "-4", "-6"}:
            index += 1
        elif option in {"-o", "-p", "-l", "-F", "-S", "-i", "-J"}:
            if index + 1 >= len(tokens):
                return None
            index += 2
        elif option[:2] in {"-o", "-p", "-l", "-F", "-S", "-i", "-J"} and len(option) > 2:
            index += 1
        else:
            return None
    if index >= len(tokens):
        return None
    host = tokens[index].rsplit("@", 1)[-1]
    if host not in _HOSTS and not host.endswith(".sherlock.stanford.edu"):
        return None
    args = tokens[index + 1:]
    return _tokens(args[0]) if len(args) == 1 else (args or None)


def _watch(tokens):
    if not tokens or _name(tokens[0]) != "watch":
        return None
    # WATCH_INTERVAL can override the executable's default. With no explicit -n,
    # cadence is unknown to a pure command-string guard.
    interval = None
    index = 1
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        value = None
        if option == "--":
            index += 1
            break
        if option in {"-n", "--interval"}:
            if index + 1 >= len(tokens):
                return None
            value = tokens[index + 1]
            index += 2
        elif option.startswith("--interval="):
            value = option.split("=", 1)[1]
            index += 1
        elif option.startswith("-n") and len(option) > 2:
            value = option[2:]
            index += 1
        elif option in {"-d", "--differences", "-t", "--no-title", "-e", "--errexit",
                        "-g", "--chgexit", "-p", "--precise", "-x", "--exec", "-c", "--color"}:
            index += 1
        else:
            return None
        if value is not None:
            try:
                interval = float(value)
            except ValueError:
                return None
            if not math.isfinite(interval) or interval < 0:
                return None
    args = tokens[index:]
    return interval, (_tokens(args[0]) if len(args) == 1 else (args or None))


def _purge_helper(tokens, catalog):
    if not tokens:
        return False
    name = _name(tokens[0])
    if name in catalog:
        return True
    # Explicit interpreter + a named, reviewed script; never inspect script contents.
    return (name in {"python", "python3", "bash", "sh"} and len(tokens) > 1
            and not tokens[1].startswith("-") and _name(tokens[1]) in catalog)


def decide(command, *, known_purge_helpers=KNOWN_PURGE_HELPERS):
    """Return deny only for a narrowly proven pattern, otherwise unknown.

Additional helper basenames must come from a reviewed catalog establishing purge
evasion intent. Do not populate the catalog from filename substrings or user paths.
"""
    tokens = _tokens(command)
    if tokens is None:
        return GuardDecision()
    remote = _remote(tokens)
    if _purge_helper(tokens, known_purge_helpers) or _purge_helper(remote, known_purge_helpers):
        return GuardDecision("deny", "Known Sherlock scratch purge-evasion helper is prohibited; retain a legitimate durable copy instead", "scratch_purge_helper")
    watched = _watch(remote) if remote else None
    if watched and watched[0] is not None and watched[1] and _name(watched[1][0]) in _SCHEDULER_READS and watched[0] < 60:
        return GuardDecision("deny", "Sherlock scheduler checks require at least 60 seconds between checks; use dependencies or a bounded shared status query", "scheduler_poll_cadence")
    watched = _watch(tokens)
    if watched and watched[0] is not None and watched[0] < 60:
        watched_remote = _remote(watched[1])
        if watched_remote and _name(watched_remote[0]) in _SCHEDULER_READS:
            return GuardDecision("deny", "Sherlock scheduler checks require at least 60 seconds between checks; use dependencies or a bounded shared status query", "scheduler_poll_cadence")
    return GuardDecision()


def _claude_command(payload):
    if payload.get("tool_name") != "Bash":
        return None
    values = payload.get("tool_input")
    return values.get("command") if isinstance(values, dict) else None


def _codex_command(payload):
    # 0.161.0 normalizes unified exec to Bash/command. The raw form is compatible
    # with direct transport fixtures, without inspecting nested code-mode programs.
    values = payload.get("tool_input")
    if not isinstance(values, dict):
        return None
    if payload.get("tool_name") == "Bash":
        return values.get("command")
    if payload.get("tool_name") == "exec_command":
        return values.get("cmd")
    return None


def adapt_event(client, payload):
    """Map each client event to the shared decision and portable blocking JSON."""
    if client not in {"claude", "codex"}:
        raise ValueError("Unsupported hook client")
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
        return {}
    command = _claude_command(payload) if client == "claude" else _codex_command(payload)
    decision = decide(command)
    if decision.status != "deny":
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                    "permissionDecision": "deny",
                                    "permissionDecisionReason": decision.reason}}


def main(argv=None):
    """CLI implementation for the toolkit's `shk guard --client` entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("claude", "codex"), required=True)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            print("sherlock-kit guard: oversized input; no decision", file=sys.stderr)
            return 0
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        print("sherlock-kit guard: malformed input; no decision", file=sys.stderr)
        return 0
    output = adapt_event(args.client, payload)
    if output:
        print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
