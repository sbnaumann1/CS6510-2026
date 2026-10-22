#!/usr/bin/env python
"""Host-side entry for the cross-store correctness check (same flags, same exit code).

The implementation lives in tools/verify_invariant.py, which the `checkout-tools` image
also runs, so the host and the compose stack run identical code. It opens no database:
each service describes its own data through /internal/reports/*.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

from verify_invariant import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
