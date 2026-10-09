#!/usr/bin/env python3
"""Never lose the last copy: random trees, scanned, then changed in random
ways behind the scan's back, then removed for real under random rules
(Trash or --permanent, --merge, --link, --keep-in, --remove-from, --rank,
--only), all inside a sealed namespace. After each removal:

  * every content that existed just before (apart from OS junk, which goes
    with its folder) still exists somewhere in the tree;
  * nothing that stayed was changed, and anything new in the tree (merged
    in) is a content that existed before;
  * what an item did matches its status: removed or merged items are gone,
    skipped, failed and not-done ones are all still there;
  * running it again changes nothing;
  * for the Trash: `clean undo` brings back exactly the tree as it was,
    byte for byte, folders included.

Half of the cases live on a tmpfs drive (its .Trash-0 is used), half next
to the home Trash. The work runs in one process inside the namespace (this
file with --worker); a failure names its seed, which rebuilds the same case."""

import contextlib
import io
import json
import os
import random
import shutil
import sys
import time
import traceback
import unittest

WORKER = __name__ == "__main__" and sys.argv[1:2] == ["--worker"]
if WORKER:
    # inside the sealed namespace, with the test's environment: helpers
    # would swap it for another throwaway one
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    Base, SLOW = object, False

    def needs_unshare(cls):
        return cls

    def blob(tag, size=3000):
        seed = tag.encode()
        out = bytearray()
        while len(out) < size:
            out += seed + b"|" + str(len(out)).encode() + b"\n"
        return bytes(out[:size])
else:
    from helpers import SLOW, Base, blob, needs_unshare

JUNK = ("Thumbs.db", ".DS_Store", "desktop.ini")


# ---------------------------------------------------------------- the worker

def make_tree(rng, base, pair=0.5):
    """A random tree under `base`: files from a small pool of contents, then
    copies of folders (identical, renamed, partial, diverged), scattered
    copies, a pair of folders that went separate ways (with chance `pair`),
    junk, hard links and things the scan can't vouch for."""
    folders = [""]
    for _ in range(rng.randint(2, 10)):
        folders.append(os.path.join(rng.choice(folders),
                                    rng.choice(["d", "d", "d", ".h"]) + str(len(folders))))
    pool = [blob(f"c{k}", rng.choice([0, 1, 50, 100, 3000])) for k in range(14)]
    made = []

    def put(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        t = time.time() - rng.choice([0, 3600, 86400, 86400 * 30])
        os.utime(path, (t, t))
        made.append(path)

    for k in range(rng.randint(3, 30)):
        put(os.path.join(base, rng.choice(folders), f"f{k}"), rng.choice(pool))
    os.makedirs(base, exist_ok=True)
    for k in range(rng.randint(0, 5)):
        src = os.path.join(base, rng.choice(folders))
        dst = os.path.join(base, rng.choice(folders), f"copy{k}")
        if (not os.path.isdir(src) or os.path.exists(dst)
                or (dst + "/").startswith(src.rstrip("/") + "/")):
            continue
        kind = rng.choice(["identical", "identical", "renamed", "partial", "diverged"])
        if kind == "renamed":           # same files, new names, flattened
            os.makedirs(dst)
            for i, (dp, _, ns) in enumerate(os.walk(src)):
                for n in ns:
                    p = os.path.join(dp, n)
                    if os.path.isfile(p) and not os.path.islink(p):
                        shutil.copy2(p, os.path.join(dst, f"r{i}-{n}"))
            continue
        shutil.copytree(src, dst, symlinks=True)
        inside = [os.path.join(dp, n) for dp, _, ns in os.walk(dst) for n in ns]
        if kind in ("partial", "diverged"):
            for f in rng.sample(inside, min(len(inside) // 3, rng.randint(1, 3))):
                os.unlink(f)
        if kind == "diverged":
            put(os.path.join(dst, "only-here"), blob(f"new{rng.random()}", 100))
            if inside and rng.random() < 0.5:     # a name taken by other content
                put(os.path.join(dst, os.path.relpath(inside[0], dst) + ".x"),
                    rng.choice(pool))
    for _ in range(rng.randint(0, 4)):          # scattered copies
        if made:
            src = rng.choice(made)
            if os.path.isfile(src):
                dst = os.path.join(base, rng.choice(folders), f"s{rng.randint(0, 99)}")
                if not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
    if rng.random() < pair:         # a folder and a copy that went separate ways
        a = os.path.join(base, rng.choice(folders), f"pair{rng.randint(0, 99)}")
        b = os.path.join(base, rng.choice(folders), f"pair{rng.randint(100, 199)}")
        if not os.path.exists(a) and not os.path.exists(b):
            names = [rng.choice(["", "sub/"]) + f"p{i}" for i in range(rng.randint(5, 10))]
            for n in names:
                put(os.path.join(a, n), blob(f"pair{rng.random()}", rng.choice([50, 3000])))
                os.makedirs(os.path.dirname(os.path.join(b, n)), exist_ok=True)
                shutil.copy2(os.path.join(a, n), os.path.join(b, n))
            for n in rng.sample(names, rng.randint(1, len(names) // 4)):
                os.unlink(os.path.join(b, n))
            for i in range(rng.randint(1, 2)):
                put(os.path.join(b, rng.choice(["", "sub/", "new/"]) + f"only{i}"),
                    blob(f"only{rng.random()}", 100))
            if rng.random() < 0.5:  # a name both have, with different content
                n = rng.choice(names)
                put(os.path.join(b, n), blob(f"other{rng.random()}", 100))
    every = [os.path.join(base, f[0], f[1]) for f in
             ((os.path.relpath(dp, base), n) for dp, _, ns in os.walk(base) for n in ns)]
    if rng.random() < 0.4:                      # junk, which goes with its folder
        d = os.path.join(base, rng.choice(folders))
        if os.path.isdir(d):
            put(os.path.join(d, rng.choice(JUNK)), blob(f"junk{rng.random()}", 20))
    if every and rng.random() < 0.3:            # a hard link
        src = rng.choice(every)
        if os.path.isfile(src) and not os.path.islink(src):
            os.link(src, os.path.join(os.path.dirname(src), "hl" + str(rng.randint(0, 9))))
    if rng.random() < 0.25:                     # taint: a symlink or a .git
        d = os.path.join(base, rng.choice(folders))
        if os.path.isdir(d):
            if rng.random() < 0.5:
                os.symlink("nowhere", os.path.join(d, "link-x"))
            else:
                put(os.path.join(d, ".git", "HEAD"), b"ref")
    return folders, pool


def mutate(rng, base, pool):
    """Change the tree behind the scan's back, in ways a removal must notice."""
    files = sorted(os.path.join(dp, n) for dp, _, ns in os.walk(base) for n in ns
                   if ".Trash" not in dp)
    files = [f for f in files if os.path.isfile(f) and not os.path.islink(f)]
    dirs = sorted(dp for dp, _, _ in os.walk(base) if ".Trash" not in dp)
    done = []
    for _ in range(rng.randint(1, 3)):
        if not files:
            break
        f = rng.choice(files)
        if not os.path.isfile(f) or os.path.islink(f):
            continue
        st = os.stat(f)
        what = rng.choice(["flip", "append", "delete", "add", "symlink", "hardlink",
                           "rmdir", "junk"])
        done.append((what, os.path.relpath(f, base)))
        if what == "flip" and st.st_size:       # same size, same date
            with open(f, "r+b") as out:
                out.seek(rng.randrange(st.st_size))
                b = out.read(1)
                out.seek(-1, 1)
                out.write(bytes([b[0] ^ 1]))
            os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
        elif what == "append":
            with open(f, "ab") as out:
                out.write(b"more")
        elif what == "delete":
            os.unlink(f)
        elif what == "add":
            d = rng.choice(dirs)
            if os.path.isdir(d):                # not one an earlier change removed
                with open(os.path.join(d, f"added{rng.randint(0, 99)}"), "wb") as out:
                    out.write(rng.choice(pool) if rng.random() < 0.5
                              else blob(f"a{rng.random()}"))
        elif what == "symlink":
            os.unlink(f)
            os.symlink(rng.choice(files), f)
        elif what == "hardlink":                # another file of the same content
            with open(f, "rb") as a:
                data = a.read()
            for g in files:
                if g != f and os.path.isfile(g) and not os.path.islink(g):
                    with open(g, "rb") as b:
                        if b.read() == data:
                            os.unlink(f)
                            os.link(g, f)
                            break
        elif what == "rmdir":
            d = os.path.dirname(f)
            if d != base:
                shutil.rmtree(d, ignore_errors=True)
        elif what == "junk":
            with open(os.path.join(os.path.dirname(f), rng.choice(JUNK)), "wb") as out:
                out.write(b"junk")
    return done


def snapshot(base):
    """{relative path: bytes / ("link", target) / "dir"} under base, Trash left out."""
    out = {}
    for dp, dns, ns in os.walk(base):
        dns[:] = [d for d in dns if not d.startswith(".Trash")]
        rel = os.path.relpath(dp, base)
        if rel != ".":
            out[rel + "/"] = "dir"
        for n in ns:
            p = os.path.join(dp, n)
            r = os.path.normpath(os.path.join(rel, n))
            if os.path.islink(p):
                out[r] = ("link", os.readlink(p))
            else:
                with open(p, "rb") as f:
                    out[r] = f.read()
        for d in dns:
            p = os.path.join(dp, d)
            if os.path.islink(p):
                out[os.path.normpath(os.path.join(rel, d))] = ("link", os.readlink(p))
    return out


def contents(snap):
    return {v for k, v in snap.items()
            if isinstance(v, bytes) and v and os.path.basename(k) not in JUNK}


def clean(*argv):
    """`clean ARGV...` in this process: (exit status, stdout, stderr)."""
    from cleanup.cli import main
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def items_of(run):
    from cleanup.report import data
    doc = data.read(run.file(data.NAME))
    cols = doc["columns"]
    return [dict(zip(cols, row)) for row in doc["items"]]


STATS = {}


def count(key, n=1):
    STATS[key] = STATS.get(key, 0) + n


def one_case(seed, top, data_dir):
    """Build, scan, change, remove, check: a list of problems (empty: fine)."""
    from cleanup import runs
    rng = random.Random(seed)
    base = os.path.join(top, f"case{seed}")
    permanent = rng.random() < 0.3
    merge = not permanent and rng.random() < 0.3
    folders, pool = make_tree(rng, base, pair=0.9 if merge else 0.5)
    roots = [base]
    tops = sorted(t for t in os.listdir(base) if os.path.isdir(os.path.join(base, t)))
    if len(tops) > 2 and rng.random() < 0.3:
        roots = [os.path.join(base, t) for t in rng.sample(tops, rng.randint(2, len(tops)))]
    scan = []
    if rng.random() < (0.8 if merge else 0.5):
        scan.append("--hidden")
    if merge or rng.random() < 0.3:
        scan += ["--similar", rng.choice(["30", "50", "50"] if merge else ["30", "50", "100"])]
    if rng.random() < 0.15:
        scan += ["--min-size", "60"]
    rc, out, err = clean("dupes", *roots, "-q", *scan)
    if rc != 0:
        return [f"scan: exit {rc}: {err[-2000:]}"]
    source = runs.history(data_dir)[0].name

    dirs = [os.path.join(base, d) for d in folders if os.path.isdir(os.path.join(base, d))]
    dirs = [d for d in dirs if d != base
            and any(d == r or d.startswith(r + "/") for r in roots)]
    args = [source, "-y", "-q"]
    for _ in range(rng.choice([0, 0, 1, 2]) if dirs else 0):
        args += ["--keep-in", rng.choice(dirs)]
    if dirs and rng.random() < 0.2:
        args += ["--remove-from", rng.choice(dirs)]
    if rng.random() < 0.4:
        args += ["--rank", ",".join(rng.sample(["first-root", "oldest", "newest", "shortest",
                                                "largest-folder"], 2))]
    if rng.random() < 0.2:
        args.append("--rank-folders")
    if rng.random() < 0.2:
        args += ["--only", rng.choice(["files", "folders"])]
    link = not merge and rng.random() < 0.15
    if permanent:
        args.append("--permanent")
    conflicts = None
    if merge:
        conflicts = rng.choice(["both", "skip", "newer"])
        args += ["--merge", "--conflicts", conflicts]
    if link:
        args.append("--link")
    changes = mutate(rng, base, pool) if rng.random() < (0.35 if merge else 0.6) else []
    where = f"changes {changes}, clean remove {' '.join(args[1:])}"

    before = snapshot(base)
    rc, out, err = clean("remove", *args)
    problems = []
    if rc not in (0, 1) or "Traceback" in err:
        return [f"remove: exit {rc} ({where}): {err[-3000:]}"]
    run = runs.history(data_dir)[0]
    after = snapshot(base)
    if run.name == source:                          # nothing to remove: no run
        count("nothing to remove")
        if rc != 0 or "Nothing to remove" not in out + err or after != before:
            return [f"no run, yet exit {rc} or the tree changed ({where}): {out[-2000:]}"]
        return []
    lost = contents(before) - contents(after)
    if lost:
        problems.append(f"LOST {len(lost)} contents ({where})")
    for k, v in after.items():
        if k in before:
            if before[k] != v and not (v == "dir" or before[k] == "dir"):
                if conflicts == "newer" and isinstance(v, bytes) and v in contents(before):
                    count("replaced by a newer version")    # the old one is kept elsewhere
                else:
                    problems.append(f"{k} changed ({where})")
        elif v != "dir" and not merge:
            problems.append(f"{k} is new ({where})")
        elif v != "dir" and isinstance(v, bytes) and v not in contents(before) | {b""}:
            problems.append(f"{k} is new with content seen nowhere before ({where})")
    count("permanent" if permanent else "merge" if merge else "link" if link else "trash")
    count("changed behind the scan's back" if changes else "unchanged")
    if lost:
        count("LOST")
    for it in items_of(run):
        count(f"item {it['kind']} {it['status']}")
        rel = os.path.relpath(it["path"], base)
        mine = [k for k in before if k == rel or k.startswith(rel + "/")]
        if it["status"] in ("removed", "merged"):
            left = [k for k in mine if k in after and not k.endswith("/")]
            if left:
                problems.append(f"{rel} is {it['status']} but {left[:3]} remain ({where})")
        elif it["status"] in ("skipped", "failed", "not-done"):
            gone = [k for k in mine if k not in after or after[k] != before[k]]
            if gone:
                problems.append(f"{rel} is {it['status']} ({it['note']}) but {gone[:3]} "
                                f"changed ({where})")
            if it["status"] != "skipped":
                problems.append(f"{rel}: {it['status']}: {it['note']} ({where})")
    if problems:
        return problems

    rc, out, err = clean("remove", *args)           # again: nothing more to do
    if rc not in (0, 1) or snapshot(base) != after:
        return [f"re-run: exit {rc}, changed the tree ({where}): {err[-2000:]}"]
    if permanent:
        return []
    rc, out, err = clean("undo", "-y", "-q", run.name)
    if rc != 0:
        return [f"undo: exit {rc} ({where}): {out[-2000:]} {err[-2000:]}"]
    count("undone")
    back = snapshot(base)
    if back != before:
        diff = sorted(k for k in set(back) | set(before) if back.get(k) != before.get(k))
        return [f"undo did not restore exactly: {diff[:8]} ({where})"]
    return []


def worker(first, n, top, report):
    """Run cases first..first+count-1; write {seed: problems} for failures."""
    data_dir = os.path.join(os.environ["XDG_DATA_HOME"], "clean")
    failed = {}
    for seed in range(first, first + n):
        try:
            problems = one_case(seed, top, data_dir)
        except Exception:
            problems = [traceback.format_exc()]
        if problems:
            failed[seed] = problems
        else:
            shutil.rmtree(os.path.join(top, f"case{seed}"), ignore_errors=True)
    with open(report, "w") as f:
        json.dump({"cases": n, "failed": failed, "stats": STATS}, f, indent=1)


# ---------------------------------------------------------------- the test

@needs_unshare
class TestNeverLoseTheLastCopy(Base):
    def run_cases(self, first, count, on_tmpfs):
        report = os.path.join(self.tmp, "fuzz.json")
        top = os.path.join(self.tmp, "fz")
        os.makedirs(top, exist_ok=True)
        mount = f'mount -t tmpfs fuzz "{top}"\n' if on_tmpfs else ""
        self.sealed(mount + f'"$PY" "{os.path.abspath(__file__)}" --worker {first} {count} '
                    f'"{top}" "{report}"', timeout=1800)
        with open(report) as f:
            result = json.load(f)
        self.assertEqual(result["cases"], count)
        self.assertEqual(result["failed"], {}, json.dumps(result["failed"], indent=1)[:20000])
        if os.environ.get("FUZZ_STATS"):
            print(json.dumps(result["stats"], indent=1, sort_keys=True), file=sys.stderr)
        return result["stats"]

    def test_home_trash(self):
        self.run_cases(1000, 150 if SLOW else 25, on_tmpfs=False)

    def test_drive_trash(self):
        self.run_cases(5000, 150 if SLOW else 25, on_tmpfs=True)


if __name__ == "__main__":
    if WORKER:
        import signal
        signal.signal(signal.SIGINT, signal.default_int_handler)
        worker(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5])
    else:
        unittest.main(verbosity=2)
