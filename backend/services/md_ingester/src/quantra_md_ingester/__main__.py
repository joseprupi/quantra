"""``python -m quantra_md_ingester`` entry point.

Delegates straight to :func:`quantra_md_ingester.cli.main` so the same
code path runs whether the operator invokes the package via ``python
-m`` or via the ``quantra-md-ingester`` console script defined in
``pyproject.toml``.
"""

from __future__ import annotations

import sys

from quantra_md_ingester.cli import main

if __name__ == "__main__":
    sys.exit(main())
