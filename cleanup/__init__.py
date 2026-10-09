"""cleanup: the code behind the `clean` command (safe cleanup for big drives)."""

import sys

# Keep the tool folder free of __pycache__ folders.
sys.dont_write_bytecode = True

__version__ = "3.0.0"

# This file has to stay readable by older Pythons (no f-strings here), so
# that they print the message below instead of a syntax error.
MIN_PYTHON = (3, 10)


def unsupported(version=None, platform=None):
    """Why clean can't run here (one sentence), or None if it can."""
    version = tuple((version or sys.version_info)[:2])
    platform = platform or sys.platform
    if not platform.startswith("linux"):
        return ("clean runs on Linux only; this system is %s. It reads Linux's own "
                "mount table, drive ids and Trash layout." % platform)
    if version < MIN_PYTHON:
        return "clean needs Python %d.%d or newer; this is Python %d.%d." % (MIN_PYTHON + version)
    return None


def exit_if_unsupported():
    """Stop with code 2 and the reason when clean can't run here."""
    problem = unsupported()
    if problem:
        sys.stderr.write(problem + "\n")
        sys.exit(2)
