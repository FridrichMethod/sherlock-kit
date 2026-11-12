"""Run the whole offline suite: ``python tests/run_suite.py``.

When CI or SHK_REQUIRE_ALL is set (to anything but "", "0", "false" or "no"),
any skipped test fails the run, so missing rsync, dash, ssh or packaging
prerequisites cannot pass silently (shellcheck runs in pre-commit). SHK_BUILD_PYTHON names
an absolute executable with build and backend requirements. SHK_TEST_WHEELHOUSE names an
absolute directory with runtime/ and build/ wheel directories. Supplied paths are validated
before discovery. A file entry point keeps multiprocessing spawn workers
from re-running the suite on import.

Importing fakes makes the run hermetic (see tests/fakes/__init__.py); a TMPDIR that
cannot execute files is refused before discovery, and a run in which a test reached
the Slurm tripwire fails.
"""

import os
import sys
import unittest
from pathlib import Path


def _flag(name):
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def main():
    for name in ("SHK_BUILD_PYTHON", "SHK_TEST_WHEELHOUSE"):
        if name not in os.environ:
            continue
        value = os.environ[name]
        path = Path(value)
        if name == "SHK_BUILD_PYTHON":
            valid = path.is_file() and os.access(path, os.X_OK)
            expected = "an absolute executable interpreter"
        else:
            valid = all((path / part).is_dir() for part in ("runtime", "build"))
            expected = "an absolute directory containing runtime/ and build/"
        if not value or not path.is_absolute() or not valid:
            print(f"{name}={value!r} must name {expected}", file=sys.stderr)
            return 1
    try:
        import fakes  # the hermetic setup runs on import
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    tests = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(str(tests), top_level_dir=str(tests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == 0:
        print("no tests were discovered", file=sys.stderr)
        return 1
    hits = fakes.tripwire_hits()
    if hits:
        print("tests reached the Slurm tripwire instead of a fake:", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        return 1
    if result.skipped and (_flag("CI") or _flag("SHK_REQUIRE_ALL")):
        print("skipped tests are failures under CI / SHK_REQUIRE_ALL:", file=sys.stderr)
        for test, reason in result.skipped:
            print(f"  {test.id()}: {reason}", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
