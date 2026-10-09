"""
Where clean keeps its files, following the usual Linux (XDG) locations:

    settings   ~/.config/clean/settings.json      ($CLEAN_CONFIG_DIR overrides)
    data       ~/.local/share/clean/              (setting paths.data)
    cache      ~/.cache/clean/                    (the hash cache; safe to delete)

The code itself stays wherever the tool folder is.
"""

import os


def _xdg(var, fallback):
    """$var if it is set to an absolute path (the spec says to ignore a
    relative one), else ~/fallback."""
    value = os.environ.get(var, "")
    if os.path.isabs(value):
        return value
    return os.path.join(os.path.expanduser("~"), fallback)


def config_dir():
    override = os.environ.get("CLEAN_CONFIG_DIR", "")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "clean")


def settings_file():
    return os.path.join(config_dir(), "settings.json")


def default_data_dir():
    return os.path.join(_xdg("XDG_DATA_HOME", ".local/share"), "clean")


def cache_dir():
    return os.path.join(_xdg("XDG_CACHE_HOME", ".cache"), "clean")


def data_dir(settings=None):
    """The data folder: the paths.data setting, or the default."""
    if settings is None:
        from cleanup import settings as _settings
        settings = _settings.current()
    chosen = settings.get("paths.data")
    if chosen:
        return os.path.abspath(os.path.expanduser(chosen))
    return default_data_dir()


def tilde(path):
    """`path` with the home folder shown as ~, for help texts."""
    home = os.path.expanduser("~").rstrip("/")
    if home and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path
