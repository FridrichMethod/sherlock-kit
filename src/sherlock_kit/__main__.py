"""``python -m sherlock_kit``: the same entry point as the ``shk`` console script."""

import sys

from sherlock_kit.cli import main

if __name__ == "__main__":
    sys.exit(main())
