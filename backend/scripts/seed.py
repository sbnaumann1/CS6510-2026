#!/usr/bin/env python
"""Host-side shim for `python -m checkout_data.seed` (same flags, same exit code).

The implementation lives in the shared data-access library so the compose
`seed`/`tools` containers and the host test suite run identical code.
"""

import sys

from checkout_data.seed import main

if __name__ == "__main__":
    sys.exit(main())
