"""Exit codes, ShkError and Outcome: the result contract of every command (frozen 0.4.0).

Exit codes: 0 ok; 1 local error (config, spec, verification, validator, malformed
read); 2 usage; 3 refused by policy or Slurm; 4 Sherlock unreachable without a new
login; 5 outcome unknown after a dispatched mutation (next: shk status >= 60 s
later); 6 status ok but the newest job of a requested name has an anomaly.
ShkError(code 1-6, snake_case token, message, hint=None) is printed as
{"ok": false, "error": token, "message": ..., "hint": ...}. Handlers return
Outcome(payload, code=0): JSON ("ok" defaults to code in (0, 6)), or the string
under a "text" key written verbatim (Outcome.text(s)).
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

    @classmethod
    def text(cls, text: str, code: int = EXIT_OK) -> Outcome:
        return cls({"text": text}, code)
