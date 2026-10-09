"""Exceptions shared by the commands."""


class UsageError(Exception):
    """Bad input or a refused request: printed as `clean: error: ...`, exit 2."""


class DriveGone(Exception):
    """The drive holding a scanned folder disappeared or stopped responding."""


class Cancelled(Exception):
    """Raised inside worker threads once Ctrl-C has been pressed."""
