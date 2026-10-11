"""Exit codes, ShkError and Outcome: the result contract of every command (frozen 0.4.0).

0 ok; 1 local error; 2 usage; 3 refused by policy or Slurm; 4 Sherlock unreachable without a
new login; 5 outcome unknown after a dispatched mutation (shk status >= 60 s later); 6 status
ok but the newest job of a requested name has an anomaly. A ShkError prints {"ok": false,
"error": token, "message", "hint"}. An Outcome prints its payload as JSON ("ok" defaults to
code in (0, 6)), or the text of Outcome.text() verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EXIT_OK = 0
EXIT_LOCAL = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_UNREACHABLE = 4
EXIT_UNKNOWN = 5
EXIT_ANOMALY = 6


class ShkError(Exception):
    """A failed command: exit code 1-6, a snake_case token, a message and an optional hint."""

    def __init__(self, code: int, token: str, message: str, hint: str | None = None) -> None:
        """Keep the parts of the error payload; the message is also the exception text."""
        super().__init__(message)
        self.code = code
        self.token = token
        self.message = message
        self.hint = hint

    def payload(self) -> dict[str, Any]:
        """The JSON error document."""
        return {"ok": False, "error": self.token, "message": self.message, "hint": self.hint}


@dataclass(frozen=True)
class Outcome:
    """A command result: a JSON payload (or {"text": ...}) and its exit code."""

    payload: dict[str, Any]
    code: int = EXIT_OK

    @classmethod
    def text(cls, text: str, code: int = EXIT_OK) -> Outcome:
        """A result printed verbatim instead of as JSON."""
        return cls({"text": text}, code)
