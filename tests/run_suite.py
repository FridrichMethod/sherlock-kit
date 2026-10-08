"""Run the full offline CI suite, requiring isolated packaging and no skips.

Use a file entry point so multiprocessing spawn/forkserver workers can import
the main module without recursively executing the suite.
"""
import os
from pathlib import Path
import sys
import unittest


def main():
    build_python = os.environ.get("SHERLOCK_KIT_BUILD_PYTHON")
    if not build_python or not Path(build_python).is_file():
        raise SystemExit("Missing isolated packaging interpreter")
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("CI forbids skipped checks:", result.skipped, file=sys.stderr)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
