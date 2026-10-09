#!/bin/sh
# Install or remove `clean` for the current user (no sudo needed).
# Needs Linux and Python 3.10 or newer; nothing else.
#
#   ./install.sh                        link the tool into ~/.local/bin
#   ./install.sh --nautilus             ... and add it to the Files right-click menu:
#                                       Scripts > clean > Find duplicates here,
#                                       Find empty folders here, Where the space goes
#   ./install.sh uninstall              remove both (keeps your settings and run history)
#   ./install.sh uninstall --nautilus   remove only the Files menu entries
#   ./install.sh test                   run the test suite (a few minutes)
#   ./install.sh test --slow            also the slow tests on big trees
#
# Install somewhere else with BIN_DIR, e.g.  BIN_DIR=~/bin ./install.sh
set -e

here=$(cd "$(dirname "$0")" && pwd -P)
bin_dir=${BIN_DIR:-$HOME/.local/bin}
bin="$bin_dir/clean"
target="$here/clean"
scripts_dir="${XDG_DATA_HOME:-$HOME/.local/share}/nautilus/scripts/clean"
script="$here/nautilus/clean-nautilus"

# The Files menu entries: each is a link to $script, whose name says which
# scan it opens.
entries="Find duplicates here
Find empty folders here
Where the space goes"

points_here() {  # is $bin a symlink to this copy of the tool?
    [ -L "$bin" ] && [ "$(readlink -f "$bin")" = "$(readlink -f "$target")" ]
}

ours() {  # is $1 a symlink to this copy's Files script?
    [ -L "$1" ] && [ "$(readlink -f "$1")" = "$(readlink -f "$script")" ]
}

install_nautilus() {
    # (called with ||, so set -e is off in here: each step checks itself)
    mkdir -p "$scripts_dir" && chmod +x "$script" || return 1
    status=0
    while IFS= read -r name; do
        link="$scripts_dir/$name"
        if ours "$link"; then
            continue
        elif [ -e "$link" ]; then
            echo "$link already exists and is not this tool's; leaving it alone." >&2
            status=1
            continue
        fi
        ln -sfn "$script" "$link" || status=1   # (also replaces a link to a copy that is gone)
    done <<ENTRIES
$entries
ENTRIES
    echo "Files menu: right-click a folder > Scripts > clean ($scripts_dir)"
    if ! command -v nautilus >/dev/null 2>&1; then
        echo "Note: Files (nautilus) is not installed here; the entries show up once it is."
    fi
    return $status
}

uninstall_nautilus() {  # says nothing if there is nothing to remove
    removed=
    while IFS= read -r name; do
        link="$scripts_dir/$name"
        if ours "$link"; then
            rm -f "$link"
            removed=yes
        elif [ -e "$link" ] || [ -L "$link" ]; then
            echo "$link is not this tool's; leaving it alone." >&2
        fi
    done <<ENTRIES
$entries
ENTRIES
    rmdir "$scripts_dir" 2>/dev/null || true
    if [ -n "$removed" ]; then
        echo "Removed the Files menu entries from $scripts_dir."
    fi
}

action=${1:-install}
case "$action" in
--nautilus) action=install ;;
*) [ $# -eq 0 ] || shift ;;
esac
nautilus=
case "$action:$*" in
install: | uninstall: | test: | test:--slow) ;;
install:--nautilus | uninstall:--nautilus) nautilus=yes ;;
*)
    echo "usage: $0 [install [--nautilus] | uninstall [--nautilus] | test [--slow]]" >&2
    exit 2
    ;;
esac

if [ "$action" != uninstall ]; then
    # The check `clean` makes when it starts: Linux, Python 3.10 or newer.
    if ! command -v python3 >/dev/null 2>&1; then
        echo "python3 is required (on Ubuntu: sudo apt install python3)." >&2
        exit 1
    fi
    python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import cleanup
sys.exit(cleanup.unsupported())' "$here" || exit 1
fi

case "$action" in
uninstall)
    uninstall_nautilus
    if [ -n "$nautilus" ]; then
        [ -n "$removed" ] || echo "The Files menu entries are not installed."
        exit 0
    fi
    if points_here; then
        rm -f "$bin"
        echo "clean removed from $bin_dir."
        echo "Kept (delete them to remove everything):"
        echo "  code:     $here"
        "$target" config path 2>/dev/null | sed 's/^/  /' || true
    elif [ -e "$bin" ] || [ -L "$bin" ]; then
        echo "$bin is not a link to this tool; leaving it alone." >&2
        exit 1
    else
        echo "clean is not installed in $bin_dir."
    fi
    exit 0
    ;;
test)
    if [ "${1:-}" = --slow ]; then
        CLEAN_SLOW_TESTS=1
        export CLEAN_SLOW_TESTS
    fi
    exec python3 -B -m unittest discover -s "$here/tests" -t "$here/tests"
    ;;
esac

if [ -L "$bin" ] && ! points_here && [ -e "$bin" ]; then
    echo "$bin already links to $(readlink -f "$bin"), which is another program." >&2
    echo "Remove it first if you want this one: rm '$bin'" >&2
    exit 1
elif [ -e "$bin" ] && [ ! -L "$bin" ]; then
    echo "$bin already exists and is not a link to this tool; not overwriting it." >&2
    exit 1
fi

mkdir -p "$bin_dir"
chmod +x "$target"
ln -sfn "$target" "$bin"
"$bin" --version >/dev/null

echo "Installed: $bin -> $target"
case ":$PATH:" in
*":$bin_dir:"*)
    found=$(command -v clean || true)
    if [ "$found" != "$bin" ]; then
        echo "Note: 'clean' currently runs $found, which comes earlier in PATH." >&2
    fi
    ;;
*)
    echo "Note: $bin_dir is not in your PATH. Add this to ~/.bashrc and open a new terminal:"
    echo "    export PATH=\"$bin_dir:\$PATH\""
    ;;
esac
if [ -n "$nautilus" ]; then
    install_nautilus || exit 1
fi
echo "Try: clean --help"
