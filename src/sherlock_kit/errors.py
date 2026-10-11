"""Exit codes, ShkError and Outcome: the result contract of every command (frozen 0.4.0).

0 ok; 1 local error; 2 usage; 3 refused by policy or Slurm; 4 Sherlock unreachable
without a new login; 5 outcome unknown after a dispatched mutation (shk status >= 60 s
later); 6 status ok but the newest job of a requested name has an anomaly.
ShkError(code 1-6, snake_case token, message, hint=None) prints {"ok": false, "error":
token, "message", "hint"}. Outcome(payload dict, code 0-6): JSON ("ok" defaults to code
in (0, 6)) or the str under "text" verbatim (Outcome.text(s)); anything else is a TypeError.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

EXIT_OK, EXIT_LOCAL, EXIT_USAGE, EXIT_REFUSED, EXIT_UNREACHABLE, EXIT_UNKNOWN, EXIT_ANOMALY = range(7)


class ShkError(Exception):
    def __init__(self, code: int, token: str, message: str, hint: str | None = None):
        if type(code) is not int or not 1 <= code <= 6 or not re.fullmatch(r"[a-z][a-z0-9_]*", str(token)):
            raise ValueError(f"ShkError needs an exit code 1-6 and a snake_case token, not {code!r}, {token!r}")
        super().__init__(message)
        self.code, self.token, self.message, self.hint = code, token, message, hint

    def payload(self) -> dict:
        return {"ok": False, "error": self.token, "message": self.message, "hint": self.hint}


@dataclass(frozen=True)
class Outcome:
    payload: dict
    code: int = EXIT_OK

    def __post_init__(self):
        text = self.payload.get("text", "") if isinstance(self.payload, dict) else None
        if not isinstance(text, str) or type(self.code) is not int or not EXIT_OK <= self.code <= EXIT_ANOMALY:
            raise TypeError(f"Outcome needs a dict payload (a str under 'text') and an exit code 0-6, not "
                            f"{type(self.payload).__name__}, {self.code!r}")

    @classmethod
    def text(cls, text: str, code: int = EXIT_OK) -> Outcome:
        return cls({"text": text}, code)
