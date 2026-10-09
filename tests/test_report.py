#!/usr/bin/env python3
"""The saved results: report.html and index.html are well-formed static pages
that no file name can inject anything into; data.json.gz round-trips every
name exactly; the text helpers; long lists are capped in the page but whole
in summary.txt; the duplicates page's script, run in headless Chrome
(tests/ui/report.mjs; skipped without node 22+ and Chrome)."""

import base64
import errno
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from urllib.parse import unquote

from helpers import HERE, IS_ROOT, Base

from cleanup import runs
from cleanup.fsutil import printable
from cleanup.report import data, html, text

# Every tag the pages use. Anything else (img, iframe, form, ...) could only
# have come from a name that wasn't escaped; a <script> is allowed only as the
# duplicates page's own, checked against its text and hash.
ALLOWED = {"html", "head", "meta", "title", "style", "body", "main", "a", "h1", "h2", "h4",
           "span", "p", "section", "div", "ul", "ol", "li", "details", "summary", "table",
           "thead", "tbody", "tr", "th", "td", "code", "footer", "dl", "dt", "dd", "nav",
           "input", "select", "option", "label", "script", "b", "meter"}
VOID = {"meta", "input"}
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"
BAD_TEXT = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e"
                      "\u2066-\u2069\u2028\u2029]")

NASTY = ["<script>alert(1)", "<b onmouseover=alert(1)>hover", "a&amp;b &lt;",
         "\"double\" 'single'", "new\nline", "tab\there", "evil\u202egpj.exe",
         "esc\x1b[31mred", "c1\x85x", "sep\u2028x", "café ü 日本"]
NASTY_BYTES = b"bad\xffbyte"


class Checker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.problems, self.hrefs, self.text = [], [], [], []
        self.csp = None
        self.scripts = []
        self.ids = set()

    def handle_decl(self, decl):
        if decl.lower() != "doctype html":
            self.problems.append(f"declaration {decl!r}")

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.problems.append(f"<{tag}> at {self.getpos()}")
        for name, value in attrs:
            if name.startswith("on") or name in ("src", "srcdoc", "style", "action", "formaction"):
                self.problems.append(f"{name}= on <{tag}> at {self.getpos()}")
            if name == "href":
                self.hrefs.append(value)
            if name == "id":
                if value in self.ids:
                    self.problems.append(f"id {value!r} twice")
                self.ids.add(value)
        a = dict(attrs)
        if tag == "meta" and a.get("http-equiv") == "Content-Security-Policy":
            self.csp = a.get("content")
        if tag == "script":
            if attrs:
                self.problems.append(f"<script> with attributes at {self.getpos()}")
            self.scripts.append("")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.problems.append(f"<{tag}/> at {self.getpos()}")
            self.stack.pop()

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> at {self.getpos()} closes {self.stack[-1:]}")
        else:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack[-1:] == ["script"]:
            self.scripts[-1] += data
        elif self.stack[-1:] != ["style"]:
            self.text.append(data)

    def handle_comment(self, data):
        self.problems.append(f"comment {data!r}")

    def handle_pi(self, data):
        self.problems.append(f"processing instruction {data!r}")


class PageTest(Base):
    def check_page(self, source, folder=None, script=None):
        """Assert `source` (a file, or the page itself) is a well-formed page
        with nothing active in it but `script` (exactly that text, allowed by
        its hash and nothing else); with `folder`, every link must lead to a
        file there. Returns the page's visible text."""
        if folder is None and os.path.isfile(source):
            folder = os.path.dirname(source)
        if os.path.isfile(source):
            with open(source, "rb") as f:
                source = f.read().decode("utf-8")       # strict: must be valid UTF-8
        c = Checker()
        c.feed(source)
        c.close()
        self.assertEqual(c.problems, [])
        self.assertEqual(c.stack, [])
        self.assertTrue(source.startswith("<!DOCTYPE html>\n"))
        if script is None:
            self.assertEqual(c.csp, CSP)
            self.assertEqual(c.scripts, [])
            self.assertEqual(source.lower().count("<script"), 0)
        else:
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            self.assertEqual(c.csp, f"{CSP}; script-src 'sha256-{digest}'")
            self.assertEqual(c.scripts, [script])
            self.assertEqual(source.lower().count("<script"), 1)
        visible = "".join(c.text)
        self.assertIsNone(BAD_TEXT.search(visible), repr(BAD_TEXT.search(visible)))
        for link in c.hrefs:
            self.assertNotRegex(link, r"^([a-zA-Z][a-zA-Z0-9+.-]*:|/)", "links stay relative")
            if link.startswith("#"):
                self.assertIn(link[1:], c.ids, "links within the page lead somewhere")
            elif folder:
                target = os.path.normpath(os.path.join(folder, unquote(link)))
                self.assertTrue(os.path.exists(target), link)
        return visible

    def nasty_tree(self, base):
        """An empty folder for each nasty name inside `base`."""
        for name in NASTY:
            os.makedirs(os.path.join(base, name, "inner", name))
        os.makedirs(os.path.join(os.fsencode(base), NASTY_BYTES))
        return [printable(n) for n in NASTY] + ["bad\\xffbyte"]

    def page_run(self, name, status="done", **meta):
        folder = self.fake_run(name, status=status, argv=["empty", self.root], **meta)
        runs.rebuild_index(self.data_dir)       # the page links back to it
        return runs.load(folder)


class TestReports(PageTest):
    def test_empty_report(self):
        root = os.path.join(self.root, '<i>root & "co"\nnl')
        os.makedirs(root)
        shown = self.nasty_tree(root)
        os.makedirs(os.path.join(root, "full"))
        with open(os.path.join(root, "full", "<b>file<b>"), "w") as f:
            f.write("x")
        r = self.empty(root)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = self.last_run()
        page = self.check_page(run.file("report.html"))
        for name in shown:
            self.assertIn(name, page)
        self.assertIn(printable(root), page)
        self.assertIn("+2 inside", page)
        self.assertIn("Would be deleted", page)
        self.assertIn("run the same command with --delete: clean empty ", page)
        self.assertNotIn("full", page.replace("fully", ""))
        # data.json.gz: the exact names, invalid UTF-8 included.
        d = data.read(run.file(data.NAME))
        paths = data.paths_of(d["dirs"])
        listed = {paths[i] for i, _, status, _ in d["folders"] if status == "would-delete"}
        expect = {os.path.join(root, n) for n in NASTY}
        expect |= {os.path.join(root, n, "inner") for n in NASTY}
        expect |= {os.path.join(root, n, "inner", n) for n in NASTY}
        expect.add(os.path.join(root, os.fsdecode(NASTY_BYTES)))
        self.assertEqual(listed, expect)
        index = self.check_page(os.path.join(self.data_dir, "index.html"))
        self.assertIn(printable(root), index)

        r = self.empty(root, "--delete")
        self.assertEqual(r.returncode, 0, r.stderr)
        page = self.check_page(self.last_run().file("report.html"))
        self.assertIn("Deleted", page)
        self.assertIn("Only empty folders were removed", page)
        self.assertEqual(sorted(os.listdir(root)), ["full"])

    @unittest.skipIf(IS_ROOT, "root can remove anything")
    def test_failures_are_listed_with_reasons(self):
        locked = self.mk("<script>locked", "<i>inner")
        os.chmod(os.path.dirname(locked), 0o555)
        self.mk("fine")
        r = self.empty(self.root, "--delete")
        self.assertEqual(r.returncode, 1, r.stderr)
        page = self.check_page(self.last_run().file("report.html"))
        self.assertIn("Not deleted", page)
        self.assertIn("Problems", page)
        self.assertIn(os.path.join(self.root, "<script>locked", "<i>inner"), page)
        self.assertIn("Permission denied", page)
        with open(self.last_run().file("summary.txt")) as f:
            summary = f.read()
        self.assertIn("Permission denied", summary)

    def test_dupes_report(self):
        for i, name in enumerate(NASTY):
            self.write(f"a/{name}", "same bytes")
            self.write(f"b/{name}.copy", "same bytes")
            # A folder family whose names are all nasty: p and q identical,
            # r holding more (so p and q are partial copies with a diff).
            for top in ("p", "q", "r"):
                self.write(f"{top}/{name}/f", f"content {i}" * 30)
            self.write(f"r/{name}/{name} more", f"more {i}" * 30)
        with open(os.path.join(os.fsencode(self.root), b"a", NASTY_BYTES), "w") as f:
            f.write("same bytes")
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = self.last_run()
        page = self.check_page(run.file("report.html"), script=html.DUPES_SCRIPT)
        self.assertIn(f"{len(NASTY) * 2 + 1} copies", page)
        for name in NASTY:
            self.assertIn(printable(os.path.join(self.root, "a", name)), page)
            self.assertIn(printable(f"{name}/{name} more"), page)       # in a diff
        self.assertIn(os.path.join(self.root, "a", "bad\\xffbyte"), page)
        self.assertIn(f"{self.root}/r", page)
        self.assertIn("Nothing was deleted", page)
        self.check_page(os.path.join(self.data_dir, "index.html"))

    def test_pages_of_runs_that_stopped(self):
        root = self.root
        result = {"removed": 0, "skipped": 0, "failed": 0, "gone": 0, "not_done": 0,
                  "fatal": None, "interrupted": True}
        scan = {"delete": True, "roots": [root], "scanned": 12, "jobs": 1, "quiet": False,
                "rows": [], "errors": [], "root_left_empty": [], "stage": "scan",
                "drive_gone": None, "result": result}
        run = self.page_run("2026-01-01_100000-empty", "interrupted", mode="delete",
                            headline="interrupted while scanning; nothing was deleted")
        page = self.check_page(html.empty_page(run, scan), run.folder)
        self.assertIn("The scan did not finish, so nothing was deleted", page)
        self.assertIn('class="callout s-interrupted"', html.empty_page(run, scan))

        mid = dict(scan, stage="delete", drive_gone="the drive /media/x went away",
                   rows=[("deleted", 1, f"{root}/a", ""), ("not-done", 1, f"{root}/b", "the run stopped first"),
                         ("gone", 1, f"{root}/c", "already gone"), ("kept", 1, f"{root}/<d>", "x")],
                   result=dict(result, interrupted=False, removed=1, not_done=1, gone=1, skipped=1,
                               fatal=(f"{root}/b", OSError(errno.EIO, "I/O error"))))
        run = self.page_run("2026-01-01_100001-empty", "drive-gone", mode="delete",
                            headline="stopped (drive problem): 1 of 4 empty folders deleted")
        source = html.empty_page(run, mid)
        page = self.check_page(source, run.folder)
        self.assertIn('class="badge s-drive-gone">drive gone<', source)
        self.assertIn("Not deleted", page)
        for path in ("c", "<d>"):
            self.assertIn(f"{root}/{path}", page)
        # not-done folders share one reason: said once, with the list folded away
        self.assertIn("Not reached 1", page)
        self.assertIn('<span class="path">b</span>', source.split("Not reached", 1)[1])
        self.assertIn("once the drive is back, run the same command again", page)
        self.assertEqual(source.count('<details class="block">'), 1)
        self.assertNotIn("the run stopped first", page)
        summary = text.empty_summary(mid, run)
        self.assertIn("STOPPED at", summary)
        self.assertIn("STOPPED: the drive /media/x went away", summary)

        info = {"groups": [], "errors": [], "roots": [root], "quiet": False, "jobs": 1,
                "stopped": "interrupted while scanning; no results were saved"}
        run = self.page_run("2026-01-01_100002-dupes", "interrupted", headline=info["stopped"])
        page = self.check_page(html.dupes_page(run, info), run.folder)
        self.assertIn("The scan did not finish, so there are no results.", page)
        self.assertNotIn("files scanned", page)

    def test_index(self):
        alive = {"pid": os.getpid(), "proc_start": runs.proc_start(os.getpid()),
                 "host": os.uname().nodename}
        files = ["report.html", "summary.txt"]
        self.fake_run("2026-01-01_100000-empty", files=files, headline="<script>alert(1)</script>")
        self.fake_run("2026-01-02_100000-empty", "interrupted", files=files, mode="delete")
        self.fake_run("2026-01-03_100000-dupes", "drive-gone", files=["summary.txt"],
                      roots=[{"path": "/a\nb"}, {"path": "/c"}, {"path": "/d"}])
        self.fake_run("2026-01-04_100000-dupes", "failed", headline="RuntimeError: <boom>")
        self.fake_run("2026-01-05_100000-empty", None)
        self.fake_run("2026-01-06_100000-empty", "running", mode="delete", **alive)
        self.fake_run("2026-01-07_100000-empty", "running")
        r = self.run_clean("history")
        self.assertEqual(r.returncode, 0, r.stderr)
        index = os.path.join(self.data_dir, "index.html")
        page = self.check_page(index)
        with open(index) as f:
            source = f.read()
        for status in ("done", "interrupted", "drive-gone", "failed", "unfinished", "running"):
            self.assertIn(f'class="badge s-{status}"', source)
        self.assertIn("<script>alert(1)</script>", page)        # as text
        self.assertIn("RuntimeError: <boom>", page)
        self.assertIn("/a\\nb +2 more", page)
        self.assertIn("still running", page)
        self.assertIn("stopped without finishing", page)
        self.assertIn("7 runs in", page)
        self.assertIn('href="runs/2026-01-04_100000-dupes/"', source)     # no files: the folder
        self.assertIn('href="runs/2026-01-03_100000-dupes/summary.txt"', source)
        self.assertNotIn('href="runs/2026-01-03_100000-dupes/report.html"', source)

    def test_empty_index(self):
        self.run_clean("history")
        page = self.check_page(os.path.join(self.data_dir, "index.html"))
        self.assertIn("0 runs in", page)
        self.assertIn("No runs yet", page)
        self.run_clean("history", env={"CLEAN_HISTORY_KEEP_DAYS": "0"})
        with open(os.path.join(self.data_dir, "index.html")) as f:
            self.assertIn("Runs are kept until you delete them.", f.read())

    def test_member_note_counts(self):
        one = {"role": "partial", "files": 1, "diff": {}}
        self.assertEqual(text.member_note(one), "its only file is also in the kept folder")
        many = {"role": "partial", "files": 1200, "diff": {}}
        self.assertEqual(text.member_note(many), "all 1,200 files are also in the kept folder")


class TestCaps(PageTest):
    """Pages stay a sensible size however long the lists; summary.txt and the
    TSVs keep everything."""

    def empty_info(self, rows, errors=()):
        res = {"removed": sum(r[0] == "would-delete" for r in rows), "skipped": 0, "failed": 0,
               "gone": 0, "not_done": 0, "fatal": None, "interrupted": False}
        return {"delete": False, "roots": [self.root], "scanned": len(rows), "jobs": 1,
                "quiet": False, "rows": rows, "errors": list(errors), "root_left_empty": [],
                "stage": "done", "drive_gone": None, "result": res}

    def test_empty_lists(self):
        rows = [("would-delete", 1, f"{self.root}/a-big", "")]
        rows += [("would-delete", 2, f"{self.root}/a-big/{i:03d}", "") for i in range(300)]
        rows += [("would-delete", 1, f"{self.root}/f{i:04d}", "") for i in range(2100)]
        errors = [(f"{self.root}/e{i}", "Permission denied") for i in range(1001)]
        info = self.empty_info(rows, errors)
        run = self.page_run("2026-01-01_100000-empty", headline="2,401 empty folders would be deleted")
        source = html.empty_page(run, info)
        page = self.check_page(source, run.folder)
        self.assertIn("... and 101 more places; folders.tsv and summary.txt have every one.", page)
        self.assertIn("+300 inside", page)
        self.assertIn("... and 100 more (folders.tsv has all)", page)
        self.assertIn("... and 1 more (all in summary.txt)", page)
        self.assertLess(len(source), 1_000_000)
        summary = text.empty_summary(info)
        self.assertIn("Empty folders that WOULD be deleted (2,401, in 2,101 places):", summary)
        self.assertIn(f"  {self.root}/a-big  (+300 inside)\n", summary)
        self.assertIn(f"  {self.root}/f2099\n", summary)
        self.assertIn(f"{self.root}/e1000\n", summary)
        terminal = text.empty_summary(info, terminal=True)
        self.assertIn("... and 951 more (all in summary.txt)", terminal)

    def test_inner_budget(self):
        rows = []
        for i in range(30):
            rows.append(("would-delete", 1, f"{self.root}/g{i:02d}", ""))
            rows += [("would-delete", 2, f"{self.root}/g{i:02d}/{j:03d}", "") for j in range(200)]
        run = self.page_run("2026-01-01_100000-empty")
        source = html.empty_page(run, self.empty_info(rows))
        self.check_page(source, run.folder)
        self.assertEqual(source.count("<details>"), 25)       # 25 x 200 = the 5,000 budget
        self.assertEqual(source.count('<span class="plain-more">+200 inside</span>'), 5)

    def big_dupes_info(self, nfam, ncov, ngroups, nfolded):
        root = self.root
        diff = {"only_here": {"count": 150, "items": [f"new{k}" for k in range(150)]},
                "only_there": {"count": 20000, "items": [f"old{k}" for k in range(10000)]},
                "changed": {"count": 0, "items": []},
                "moved": {"count": 1, "items": [["a/x", "b/x"]]}}
        fams = [{"id": f"F{i + 1}", "kind": "mixed", "anchor": f"{root}/keep{i}", "files": 10,
                 "bytes": 10 << 20, "freed": (nfam - i) << 20, "types": ["Images", "Other"],
                 "members": [{"path": f"{root}/copy{i}/{k}", "role": "partial" if k else "identical",
                              "files": 5, "bytes": 5 << 20, "freed": 5 << 20, "shared": 0.5,
                              "diff": diff if k else None} for k in range(60 if i == 0 else 1)]}
                for i in range(nfam)]
        cov = [{"id": f"C{i + 1}", "path": f"{root}/cov{i}", "files": 3, "bytes": 3000,
                "freed": 3000, "types": ["Audio"], "where": [[f"{root}/w{k}", 1] for k in range(3)],
                "where_total": 7} for i in range(ncov)]
        groups = [{"id": i + 1, "size": 1000, "hash": f"{i:0128x}", "category": "Other",
                   "folded": i >= ngroups, "files": [f"{root}/x{i}", f"{root}/y{i}"],
                   "explained": ["", "F1" if i >= ngroups else ""]}
                  for i in range(ngroups + nfolded)]
        groups.insert(0, {"id": 0, "size": 2 ** 30, "hash": "f" * 128, "category": "Video",
                          "folded": False, "files": [f"{root}/big{j}" for j in range(60)],
                          "explained": [""] * 60})
        totals = {"groups": len(groups), "groups_shown": ngroups + 1,
                  "redundant": len(groups) + 58, "reclaimable": 59 * 2 ** 30 + 1000 * len(groups),
                  "families": nfam, "family_freed": 1 << 30, "covered": ncov,
                  "covered_freed": 3000 * ncov, "folders_freed": 1 << 31,
                  "categories": {"Video": [1, 59, 59 * 2 ** 30], "Other": [len(groups) - 1, len(groups) - 1, 1000]}}
        return {"families": fams, "covered": cov, "groups": groups, "totals": totals,
                "errors": [], "roots": [root], "total_files": 99999, "jobs": 1, "hard_links": 3,
                "quiet": False, "skipped": {"junk": 2, "symlinks": 1}, "filters": [],
                "same_name": False, "same_date": False, "similar": 80, "hidden": False,
                "max_depth": None,
                "hashing": {"read": 10, "bytes": 1000, "cached": 5, "cache": "on", "stored": 10,
                            "pruned": 0}}

    def test_dupes_lists(self):
        info = self.big_dupes_info(html.MAX_FAMILIES + 3, html.MAX_COVERED + 2,
                                   html.MAX_GROUPS + 1, html.MAX_FOLDED + 4)
        run = self.page_run("2026-01-01_100000-dupes")
        source = html.dupes_page(run, info)
        page = self.check_page(source, run.folder, script=html.DUPES_SCRIPT)
        self.assertIn("... and 3 more folder families; folders.tsv has every one.", page)
        self.assertIn("... and 2 more covered folders; folders.tsv has every one.", page)
        self.assertIn("... and 2 more groups; files.tsv has every one.", page)     # + the big one
        self.assertIn("... and 4 more groups; files.tsv has every one.", page)     # folded
        self.assertIn("... and 10 more (files.tsv has all)", page)
        self.assertIn("... and 10 more folders (folders.tsv has all)", page)
        self.assertIn("... and 50 more (data.json.gz has up to 10,000)", page)
        self.assertIn("... and 19,900 more (data.json.gz has up to 10,000)", page)
        self.assertIn("... and 4 more places", page)
        self.assertIn("60 copies × 1.0 GB", page)
        self.assertEqual(source.count('class="panel card"'),
                         html.MAX_FAMILIES + html.MAX_COVERED + html.MAX_GROUPS + html.MAX_FOLDED)
        summary = text.dupes_summary(info)
        self.assertIn(f"... and {html.MAX_FAMILIES + 3 - text.FAMILIES:,} more folder families",
                      summary)
        self.assertIn(f"... and {html.MAX_GROUPS + 2 - text.FILE_GROUPS:,} more duplicate groups",
                      summary)
        self.assertIn("... and 19,990 more", summary)
        tsv = text.dupes_files_tsv(info["groups"])
        self.assertEqual(tsv.count("\n"), 1 + 60 + 2 * (html.MAX_GROUPS + 1 + html.MAX_FOLDED + 4))
        folders = text.dupes_folders_tsv(info)
        self.assertEqual(folders.count("\n"),
                         1 + 2 * (html.MAX_FAMILIES + 3) + 59 + html.MAX_COVERED + 2)

    def test_dupes_without_results(self):
        info = self.big_dupes_info(0, 0, 0, 0)
        info["groups"] = []
        info["totals"].update(groups=0, groups_shown=0, redundant=0, reclaimable=0,
                              categories={})
        run = self.page_run("2026-01-01_100000-dupes")
        page = self.check_page(html.dupes_page(run, info), run.folder, script=html.DUPES_SCRIPT)
        self.assertEqual(page.count("None found."), 3)
        self.assertEqual(text.dupes_headline(info), "no duplicates")


def _browser():
    """(node, chrome) when both are here, else None. node needs its own
    WebSocket (22+)."""
    node = shutil.which("node")
    chrome = next(filter(None, map(shutil.which, ("google-chrome", "chromium",
                                                  "chromium-browser"))), None)
    if not node or not chrome:
        return None
    r = subprocess.run([node, "-e", "process.exit(typeof WebSocket === 'function' ? 0 : 1)"],
                       capture_output=True)
    return (node, chrome) if r.returncode == 0 else None


BROWSER = _browser()


@unittest.skipUnless(BROWSER, "needs node 22+ and Chrome")
class TestInBrowser(PageTest):
    """What a person sees in the duplicates page as they use it."""

    def browse(self, page, steps):
        with open(os.path.join(self.tmp, "steps.json"), "w") as f:
            json.dump(steps, f)
        node, chrome = BROWSER
        r = subprocess.run([node, os.path.join(HERE, "ui", "report.mjs"), page, f.name],
                           capture_output=True, env=dict(self.env, CHROME=chrome, TMPDIR=self.tmp), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace"))
        out = json.loads(r.stdout)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["requests"], [], "the page asks for nothing")
        self.assertEqual(len(out["states"]), len(steps) + 1)
        return out["states"]

    def test_script_is_valid(self):
        path = os.path.join(self.tmp, "dupes.js")
        with open(path, "w") as f:
            f.write(html.DUPES_SCRIPT)
        r = subprocess.run([BROWSER[0], "--check", path], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def blob(self, rel, tag, size):
        self.write(rel, (tag.encode() * (size // len(tag) + 1))[:size])

    def test_tabs_filters_sort_and_links(self):
        for top in ("Photos", "Backup/Photos"):             # F1: 2 MB of images
            self.blob(f"{top}/a.jpg", "photo a", 2 << 20)
            self.blob(f"{top}/b.png", "photo b", 3000)
        for i in range(4):                                  # F2: 3 folders of documents
            self.blob(f"Docs/{i}.pdf", f"doc {i}", 5000 + i)
            if i < 2:
                self.blob(f"Old/Docs/{i}.pdf", f"doc {i}", 5000 + i)
        self.blob("Older/0.pdf", "doc 0", 5000)
        self.blob("Downloads/stuff/s.mp3", "song", 7000)     # C1: scattered elsewhere
        self.blob("Downloads/stuff/t.zip", "zip", 8000)
        self.blob("Downloads/own.bin", "own", 900)
        self.blob("Music/s.mp3", "song", 7000)
        self.blob("Music/m.mp3", "other song", 7100)
        self.blob("Archive/t.zip", "zip", 8000)
        self.blob("Archive/u.zip", "other zip", 8100)
        self.blob("x.txt", "x", 600)                         # the one loose group
        self.blob("notes/x copy.txt", "x", 600)
        self.blob("notes/n.txt", "n", 650)
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        page = self.last_run().file("report.html")
        self.check_page(page, script=html.DUPES_SCRIPT)
        d = data.read(self.last_run().file("data.json.gz"))
        paths = data.paths_of(d["dirs"])
        rel = lambda i: os.path.relpath(paths[i], self.root)
        self.assertEqual([(f["id"], rel(f["anchor"]), sorted(rel(m["folder"]) for m in f["members"]))
                          for f in d["families"]],
                         [("F1", "Photos", ["Backup"]), ("F2", "Docs", ["Old", "Older"])])
        self.assertEqual([rel(c["folder"]) for c in d["covered"]], ["Downloads/stuff"])
        folded = [f"G{g['id']}" for g in d["groups"] if g["folded"]]
        self.assertEqual(len(folded), 6)
        self.assertEqual([g["id"] for g in d["groups"] if not g["folded"]], [7])

        audio = [f"G{g['id']}" for g in d["groups"] if g["category"] == "Audio"]
        self.assertEqual(len(audio), 1)
        self.assertIn(audio[0], folded)
        families = ["F1", "F2"]
        steps = [
            ({"click": "nav.tabs a[href='#covered']"}, "covered", ["C1"], "1 shown"),
            ({"click": "nav.tabs a[href='#groups']"}, "groups", ["G7"], "7 shown"),
            ({"click": "nav.tabs a[href='#families']"}, "families", families, "2 shown"),
            ({"set": "#q", "value": "DOCS"}, "families", ["F2"], "1 of 2 shown"),
            ({"set": "#q", "value": "/older docs"}, "families", ["F2"], "1 of 2 shown"),
            ({"set": "#q", "value": "/older photos"}, "families", [], "0 of 2 shown"),
            ({"set": "#q", "value": ""}, "families", families, "2 shown"),
            ({"set": "#type", "value": "Images"}, "families", ["F1"], "1 of 2 shown"),
            ({"set": "#type", "value": "Audio"}, "families", [], "0 of 2 shown"),
            ({"set": "#type", "value": ""}, "families", families, "2 shown"),
            ({"set": "#size", "value": str(1 << 20)}, "families", ["F1"], "1 of 2 shown"),
            ({"set": "#size", "value": "0"}, "families", families, "2 shown"),
            ({"pick": "#loc", "label": "Older"}, "families", ["F2"], "1 of 2 shown"),
            ({"pick": "#loc", "label": "Backup"}, "families", ["F1"], "1 of 2 shown"),
            ({"pick": "#loc", "label": self.root}, "families", families, "2 shown"),
            ({"pick": "#loc", "label": "everywhere"}, "families", families, "2 shown"),
            ({"set": "#sort", "value": "count"}, "families", ["F2", "F1"], "2 shown"),
            ({"set": "#sort", "value": "path"}, "families", ["F2", "F1"], "2 shown"),
            ({"set": "#sort", "value": "space"}, "families", families, "2 shown"),
            # filters stay as set when the tab changes
            ({"set": "#type", "value": "Audio"}, "families", [], "0 of 2 shown"),
            ({"click": "nav.tabs a[href='#covered']"}, "covered", ["C1"], "1 shown"),
            # a match among the groups folded away opens their list
            ({"click": "nav.tabs a[href='#groups']"}, "groups", audio, "1 of 7 shown"),
            ({"set": "#q", "value": "x.txt"}, "groups", [], "0 of 7 shown"),
            # a link to a result that a filter hides shows it again
            ({"hash": "F2"}, "families", families, "2 shown"),
            # a group folded into a folder result opens its list
            ({"hash": folded[2]}, "groups", ["G7"] + folded, "7 shown"),
            ({"click": "nav.tabs a[href='#covered']"}, "covered", ["C1"], "1 shown"),
            ({"reload": True}, "covered", ["C1"], "1 shown"),
            ({"reload": f"#{folded[0]}"}, "groups", ["G7"] + folded, "7 shown"),
            ({"reload": "#nothing"}, "families", families, "2 shown"),
        ]
        states = self.browse(page, [s[0] for s in steps])
        self.assertEqual(states[0], {"tab": "families", "shown": "2 shown", "toolbar": True,
                                     "visible": families, "open": [], "overflow": False})
        for (step, tab, visible, shown), state in zip(steps, states[1:]):
            self.assertEqual((state["tab"], state["visible"], state["shown"], state["toolbar"]),
                             (tab, visible, shown, True), step)
        self.assertEqual(states[22]["open"], folded)
        self.assertEqual(states[23]["open"], folded)      # still open, but hidden: no match
        self.assertEqual(states[25]["open"], folded)
        self.assertEqual(states[-2]["open"], folded)
        self.assertEqual(states[-1]["open"], [])
        self.assertEqual(states[2]["open"], [])

        # On a phone nothing is wider than the screen, in any tab.
        phone = [{"size": [360, 740]}, {"click": "nav.tabs a[href='#covered']"},
                 {"click": "nav.tabs a[href='#groups']"}, {"hash": folded[0]}]
        states = self.browse(page, phone)
        self.assertEqual([s["overflow"] for s in states], [False] * 5)
        self.assertEqual([s["tab"] for s in states[1:]],
                         ["families", "covered", "groups", "groups"])

    def test_nasty_names(self):
        for i, name in enumerate(NASTY):
            self.write(f"a/{name}", "same bytes")
            self.write(f"b/{name}.copy", "same bytes")
            for top in ("p", "q"):
                self.write(f"{top}/{name}/f", f"content {i}" * 30)
        r = self.dupes(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        page = self.last_run().file("report.html")
        # F1: p and q; F2: a and b, whose diff lists every name; G12: the
        # copies of "same bytes", 11 of them inside a itself, so not folded.
        # G1 and G9, folded into F1, hold "café ü 日本/f" and "new\nline/f".
        states = self.browse(page, [{"set": "#q", "value": "<script>"},
                                    {"set": "#q", "value": "日本"},
                                    {"click": "nav.tabs a[href='#groups']"},
                                    {"set": "#q", "value": "new\\nline"},     # as shown
                                    {"set": "#q", "value": "new\nline"},
                                    {"size": [360, 740]}])
        self.assertEqual([s["visible"] for s in states],
                         [["F1", "F2"], ["F2"], ["F2"], ["G12", "G1"], ["G12", "G9"], [], []])
        self.assertEqual(states[-1]["overflow"], False)


class TestData(unittest.TestCase):
    def test_round_trip(self):
        roots = ["/media/drive", "/home/u/a b"]
        table = data.PathTable(roots)
        names = ["/media/drive/x/y/z", "/media/drive/x", "/home/u/a b/c\nd",
                 "/home/u/a b/" + os.fsdecode(b"\xff\xfe"), "/elsewhere/q", "/media/drive"]
        ids = [table.add(p) for p in names]
        self.assertEqual(table.add("/media/drive/x/y/z"), ids[0])      # stored once
        obj = {"format": data.FORMAT, "dirs": table.rows, "ids": ids,
               "file": table.split("/media/drive/x/" + os.fsdecode(b"f\x80.txt"))}
        blob = data.encode(obj)
        self.assertEqual(blob, data.encode(obj))         # deterministic
        self.assertEqual(blob[4:8], b"\0\0\0\0")         # no timestamp in the gzip header
        back = __import__("json").loads(gzip.decompress(blob).decode("utf-8"))
        paths = data.paths_of(back["dirs"])
        self.assertEqual([paths[i] for i in back["ids"]], names)
        folder, name = back["file"]
        self.assertEqual(os.fsencode(os.path.join(paths[folder], name)), b"/media/drive/x/f\x80.txt")
        self.assertEqual(len(table.rows), len(set(map(tuple, table.rows))))

    def test_deep_paths(self):
        deep = "/r" + "/d" * 5000
        table = data.PathTable(["/r"])
        i = table.add(deep)
        self.assertEqual(data.paths_of(table.rows)[i], deep)
        self.assertEqual(len(table.rows), 5001)


class TestText(unittest.TestCase):
    def test_outermost(self):
        o = text.outermost
        self.assertEqual(o([]), [])
        self.assertEqual(o({"/a", "/a/b", "/a/b/c", "/c", "/ab", "/a/d"}),
                         [("/a", ["/a/b", "/a/b/c", "/a/d"]), ("/ab", []), ("/c", [])])
        self.assertEqual(o(["/x/a b", "/x/a", "/x/a/b"]), [("/x/a", ["/x/a/b"]), ("/x/a b", [])])

    def test_printable(self):
        cases = {"plain café 日本": "plain café 日本", "a\tb": "a\\tb", "a\nb": "a\\nb",
                 "a\rb": "a\\rb", "\x1b[2J": "\\x1b[2J", "\x7f": "\\x7f", "\x85": "\\u0085",
                 "evil\u202egpj.exe": "evil\\u202egpj.exe", "\u2066x\u2069": "\\u2066x\\u2069",
                 "a\u2028b": "a\\u2028b", os.fsdecode(b"bad\xff"): "bad\\xff"}
        for raw, shown in cases.items():
            self.assertEqual(printable(raw), shown, repr(raw))

    def test_when_and_duration(self):
        self.assertRegex(text.when(1791316365), r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} "
                                                r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
                                                r"\d{4}, \d\d:\d\d$")
        for bad in (None, "soon", 1e20, float("nan")):
            self.assertEqual(text.when(bad), "?", bad)
        for seconds, shown in ((None, "0 s"), (0, "0.0 s"), (3.14, "3.1 s"), (42, "42 s"),
                               (125, "2 min 5 s"), (3725, "1 h 2 min")):
            self.assertEqual(text.duration(seconds), shown)

    def test_command_line(self):
        cl = text.command_line
        self.assertEqual(cl(["empty", "/a b", "-n"], drop=("-n", "--dry-run"), add=("--delete",)),
                         "clean empty '/a b' --delete")
        self.assertEqual(cl(["empty", "it's\nhere"]), "clean empty 'it'\"'\"'s\\nhere'")
        self.assertEqual(text.plural(1, "copy", "copies"), "1 copy")
        self.assertEqual(text.plural(1234, "run"), "1,234 runs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
