"""Run the whole offline suite: ``python tests/run_suite.py``.

When CI or SHK_REQUIRE_ALL is set (to anything but "", "0", "false" or "no"),
any skipped test fails the run, so missing rsync, shellcheck, dash or packaging
prerequisites cannot pass silently. SHK_BUILD_PYTHON (an interpreter with
setuptools>=80 and wheel) is inherited by the packaging tests; when set it must
name an existing file. A file entry point keeps multiprocessing spawn workers
from re-running the suite on import.
"""
import os
from pathlib import Path
import sys
import unittest


def _flag(name):
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def main():
    build_python = os.environ.get("SHK_BUILD_PYTHON")
    if build_python and not Path(build_python).is_file():
        print(f"SHK_BUILD_PYTHON={build_python} does not exist", file=sys.stderr)
        return 1
    tests = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(str(tests), top_level_dir=str(tests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == 0:
        print("no tests were discovered", file=sys.stderr)
        return 1
    if result.skipped and (_flag("CI") or _flag("SHK_REQUIRE_ALL")):
        print("skipped tests are failures under CI / SHK_REQUIRE_ALL:", file=sys.stderr)
        for test, reason in result.skipped:
            print(f"  {test.id()}: {reason}", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
