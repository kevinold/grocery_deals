"""Project entry point — delegates to grocery_deals.main.

`uv init` scaffolds this file; the real CLI lives in grocery_deals.py,
which also works standalone via its PEP 723 shebang. This shim exists so
`uv run python main.py …` also works.
"""

from grocery_deals import main

if __name__ == "__main__":
    raise SystemExit(main())
