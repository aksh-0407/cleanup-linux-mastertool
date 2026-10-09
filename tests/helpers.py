"""
Shared test setup. Every test builds real folder trees in a fresh temp folder
and runs the real `clean` command on them.

Each test also gets its own settings, data and cache folders (XDG_CONFIG_HOME,
XDG_DATA_HOME, XDG_CACHE_HOME point into its temp folder, and CLEAN_*
variables from the caller's environment are dropped), so tests never read or
change your real settings or run history.

    ./install.sh test                 all tests (or: python3 -B -m unittest discover -s tests)
    ./install.sh test --slow          also the slow ones (CLEAN_SLOW_TESTS=1)
    python3 -B tests/test_empty.py    one area

Tests needing a mount (mount points, read-only drives, unplugging) use an
unprivileged user+mount namespace (`unshare -rm`) and are skipped where that
isn't allowed.
"""

import atexit
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
CLEAN = os.path.join(TOOL, "clean")
sys.dont_write_bytecode = True  # keep the tool folder free of __pycache__
if TOOL not in sys.path:
    sys.path.insert(0, TOOL)


def can_unshare():
    if not shutil.which("unshare"):
        return False
    r = subprocess.run(["unshare", "-rm", "true"], capture_output=True)
    return r.returncode == 0


HAVE_UNSHARE = can_unshare()
IS_ROOT = os.geteuid() == 0
SLOW = os.environ.get("CLEAN_SLOW_TESTS") == "1"

# The start of every run_sealed() script.
SEAL = r"""
set -e
for d in /media /mnt /run/media /run/user "$HOME/.local/share/Trash"; do
  if [ -d "$d" ]; then mount -t tmpfs -o size=1m,mode=755 seal "$d"; fi
done
unset DBUS_SESSION_BUS_ADDRESS
export XDG_RUNTIME_DIR="$T/run" GIO_USE_VOLUME_MONITOR=unix
mkdir -p "$T/run"; chmod 700 "$T/run"
"""

# Shell functions for sealed() scripts: `step NAME ARG...` runs `clean ARG...`
# with no terminal, saving its output and exit status for step(NAME);
# `pystep NAME SCRIPT ARG...` does the same for a python script (one that
# patches something, then calls cleanup.cli.main).
STEP = r"""
mkdir -p "$T/steps"
pystep() {
  _n=$1; shift
  set +e
  "$PY" "$@" >"$T/steps/$_n.out" 2>"$T/steps/$_n.err" </dev/null
  echo $? >"$T/steps/$_n.rc"
  set -e
}
step() { _s=$1; shift; pystep "$_s" "$CLEAN" "$@"; }
"""

needs_unshare = unittest.skipUnless(
    HAVE_UNSHARE, "needs unprivileged user namespaces (unshare -rm)")
slow = unittest.skipUnless(SLOW, "slow; run with ./install.sh test --slow")


def isolated_env(tmp, extra=None):
    """The environment for a `clean` run that can't touch the real settings
    or data: XDG folders inside `tmp`, no CLEAN_* variables, no bytecode."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLEAN_")}
    env.update(XDG_CONFIG_HOME=os.path.join(tmp, "xdg", "config"),
               XDG_DATA_HOME=os.path.join(tmp, "xdg", "data"),
               XDG_CACHE_HOME=os.path.join(tmp, "xdg", "cache"),
               PYTHONDONTWRITEBYTECODE="1")
    env.update(extra or {})
    return env


def pretend(tmp, code):
    """Environment additions that make a child Python run `code` before
    anything else (from a sitecustomize.py), e.g. to pretend to be another
    system or an older Python."""
    site = os.path.join(tmp, "pretend")
    os.makedirs(site, exist_ok=True)
    with open(os.path.join(site, "sitecustomize.py"), "w") as f:
        f.write("import sys\n" + code + "\n")
    return {"PYTHONPATH": f"{site}:{TOOL}"}


# Tests that import cleanup and call it directly get the same isolation: this
# process's own environment points at a throwaway folder too.
_SESSION = tempfile.mkdtemp(prefix="clean-tests-")
atexit.register(shutil.rmtree, _SESSION, True)
_isolated = isolated_env(_SESSION)
os.environ.clear()
os.environ.update(_isolated)


class Step(tuple):
    """What a step() of a sealed script did."""
    __slots__ = ()

    def __new__(cls, rc, out, err):
        return super().__new__(cls, (rc, out, err))

    rc = property(lambda self: self[0])
    out = property(lambda self: self[1])
    err = property(lambda self: self[2])

    def __repr__(self):
        return f"rc={self.rc}\n--- stdout:\n{self.out}\n--- stderr:\n{self.err}"


def blob(tag, size=3000):
    """`size` bytes, different for each tag (equal tags: equal bytes)."""
    seed = tag.encode()
    out = bytearray()
    while len(out) < size:
        out += seed + b"|" + str(len(out)).encode() + b"\n"
    return bytes(out[:size])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clean-test-")
        self.root = os.path.join(self.tmp, "root")
        os.mkdir(self.root)
        self.env = isolated_env(self.tmp)
        self.settings_file = os.path.join(self.tmp, "xdg", "config", "clean", "settings.json")
        self.data_dir = os.path.join(self.tmp, "xdg", "data", "clean")

    def tearDown(self):
        for dirpath, dirnames, _ in os.walk(self.tmp):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mk(self, *rel, files=()):
        """Create folders (and files inside the last one) under self.root."""
        path = os.path.join(self.root, *rel)
        os.makedirs(path, exist_ok=True)
        for name in files:
            with open(os.path.join(path, name), "w") as f:
                f.write("x")
        return path

    def write(self, rel, data):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        return path

    def run_clean(self, *args, env=None, wrap=None, cwd=None):
        cmd = [sys.executable, CLEAN, *args]
        if wrap:
            cmd = wrap + cmd
        return subprocess.run(cmd, capture_output=True, cwd=cwd,
                              env=dict(self.env, **(env or {})))

    def run_shell(self, script, **kw):
        """`sh -c script` in a private user+mount namespace, with the test's
        environment (skipped where such namespaces aren't allowed)."""
        if not HAVE_UNSHARE:
            self.skipTest("needs unprivileged user namespaces (unshare -rm)")
        return subprocess.run(["unshare", "-rm", "sh", "-c", script],
                              capture_output=True, env=self.env, **kw)

    def run_sealed(self, script, timeout=120, **kw):
        """`sh -c script` in a private user+mount namespace sealed off from
        everything real: /media, /mnt, /run/media, /run/user (the session
        bus) and your home Trash are covered by empty tmpfs mounts, and the
        Trash, data and runtime folders point into the test folder ($T).
        Destructive tests (removal, Trash, undo) run in here only; where
        such namespaces aren't allowed, the test is skipped."""
        if not HAVE_UNSHARE:
            self.skipTest("needs unprivileged user namespaces (unshare -rm)")
        env = dict(self.env, T=self.tmp, PYTHONPATH=TOOL, CLEAN=CLEAN,
                   PY=sys.executable, **kw.pop("env", {}))
        return subprocess.run(["unshare", "-rm", "sh", "-c", SEAL + script],
                              capture_output=True, env=env, timeout=timeout, **kw)

    def sealed(self, script, timeout=120, **kw):
        """run_sealed() with the step() shell function; the script itself
        must succeed. Read each step's result with step(NAME)."""
        r = self.run_sealed(STEP + script, timeout=timeout, stdin=subprocess.DEVNULL, **kw)
        self.assertEqual(r.returncode, 0, "sealed script failed:\n"
                         + r.stdout.decode(errors="replace") + r.stderr.decode(errors="replace"))
        return r

    def step(self, name):
        """(exit status, stdout, stderr) of `step NAME ...` in a sealed() script."""
        base = os.path.join(self.tmp, "steps", name)
        with open(base + ".rc") as f:
            rc = int(f.read())
        outs = []
        for ext in (".out", ".err"):
            with open(base + ext, encoding="utf-8", errors="replace") as f:
                outs.append(f.read())
        return Step(rc, *outs)

    def clean_sealed(self, *args, timeout=120):
        """One `clean ARG...` in a sealed namespace (no terminal):
        (exit status, stdout, stderr)."""
        self._sealed_n = getattr(self, "_sealed_n", 0) + 1
        name = f"one{self._sealed_n}"
        self.sealed("step " + name + " " + " ".join(shlex.quote(a) for a in args),
                    timeout=timeout)
        return self.step(name)

    def snapshot(self, top=None):
        """{relative path: bytes} of every file under `top` (default: root);
        symlinks as ("link", target)."""
        top = top or self.root
        out = {}
        for dirpath, _, filenames in os.walk(top):
            for name in filenames:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, top)
                if os.path.islink(path):
                    out[rel] = ("link", os.readlink(path))
                else:
                    with open(path, "rb") as f:
                        out[rel] = f.read()
        return out

    def empty(self, *args, **kw):
        return self.run_clean("empty", *args, **kw)

    def dupes(self, *args, **kw):
        return self.run_clean("dupes", *args, **kw)

    def tree(self):
        """Every path under root, relative, as a set (folders end in '/')."""
        out = set()
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel = os.path.relpath(dirpath, self.root)
            for d in dirnames:
                out.add(os.path.normpath(os.path.join(rel, d)) + "/")
            for f in filenames:
                out.add(os.path.normpath(os.path.join(rel, f)))
        return out

    def runs(self, data_dir=None):
        """The saved runs, newest first (cleanup.runs.Run objects)."""
        from cleanup import runs
        return runs.history(data_dir or self.data_dir)

    def last_run(self, data_dir=None):
        found = self.runs(data_dir)
        self.assertTrue(found, "no run was saved")
        return found[0]

    def run_meta(self, run=None):
        """run.json of `run` (default: the newest), as saved."""
        run = run or self.last_run()
        with open(run.file("run.json"), encoding="utf-8") as f:
            return json.load(f)

    def folder_rows(self, status=None, run=None):
        """folders.tsv of an empty run (default: the newest) as
        [status, depth, path, note] rows, optionally only one status."""
        run = run or self.last_run()
        with open(run.file("folders.tsv"), encoding="utf-8") as f:
            self.assertEqual(next(f), "status\tdepth\tpath\tnote\n")
            rows = [line.rstrip("\n").split("\t") for line in f]
        return [r for r in rows if status is None or r[0] == status]

    def deleted(self, run=None):
        """The folders an empty --delete run removed, relative to root, sorted."""
        return sorted(os.path.relpath(r[2], self.root)
                      for r in self.folder_rows("deleted", run))

    def report_groups(self, run=None):
        """files.tsv of a dupes run (default: the newest), as sorted groups
        of paths relative to root."""
        run = run or self.last_run()
        groups = {}
        with open(run.file("files.tsv"), encoding="utf-8") as f:
            self.assertEqual(next(f), "group\tsize_bytes\thash\texplained_by\tpath\n")
            for line in f:
                g, size, digest, explained, path = line.rstrip("\n").split("\t")
                groups.setdefault(g, []).append(os.path.relpath(path, self.root))
        return sorted(sorted(ps) for ps in groups.values())

    def fake_run(self, name, status="done", headline="", files=(), data_dir=None, **meta):
        """A run folder made by hand, e.g. fake_run("2020-01-02_030405-empty")
        (status None: no run.json at all). Returns the folder."""
        folder = os.path.join(data_dir or self.data_dir, "runs", name)
        os.makedirs(folder)
        if status is not None:
            from cleanup import runs
            m = {"format": 1, "name": name, "command": runs.NAME_RE.match(name).group(3),
                 "mode": "dry-run", "argv": [], "roots": [{"path": self.root}],
                 "status": status, "headline": headline,
                 "pid": 999999999, "boot_id": runs.boot_id()}
            m.update(meta)
            with open(os.path.join(folder, "run.json"), "w") as f:
                json.dump(m, f)
        for f in files:
            with open(os.path.join(folder, f), "w") as out:
                if f == "actions.tsv":      # a log naming one step
                    out.write("step\twhen\taction\tpath\tto\tnote\n"
                              "1\t2020-01-01T00:00:00\ttrash\t/x\t/t/files/x\t\n")
                else:
                    out.write(f"{f} of {name}\n")
        return folder

    def leftovers(self, folder=None):
        """Temp files clean left behind in `folder` (default: the test folder)."""
        return [n for n in os.listdir(folder or self.tmp) if n.startswith(".clean-")]
