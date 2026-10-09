"""`python3 -m cleanup ...` works the same as `clean ...`."""

import sys

import cleanup

cleanup.exit_if_unsupported()

from cleanup.cli import main  # noqa: E402

sys.exit(main())
