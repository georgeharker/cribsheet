"""`python -m crib` entry point — used by the git merge driver, which needs a
stable, PATH-independent way to re-invoke the CLI (DESIGN §14)."""

import sys

from .entry import main  # applies --root/--store/--writable before crib loads

if __name__ == "__main__":
    sys.exit(main())
