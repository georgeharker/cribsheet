"""Enable ``python -m zsh_ai.render``."""
import sys

from .cli import main

try:
    sys.exit(main())
except BrokenPipeError:
    sys.exit(0)
