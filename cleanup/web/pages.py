"""
The dashboard's pages. Each one is whole HTML made here (every name goes
through esc()), styled by /static/app.css and made interactive by
/static/app.js. A page has no inline script or style (the
Content-Security-Policy forbids both) and needs no network: data a script
needs travels in a <script type="application/json"> block, which is never run.

Every page shows the `clean ...` command that does the same in a terminal.
"""

import json
import os
import time

from cleanup import paths, runs
from cleanup import settings as _settings
from cleanup.fsutil import human, printable, why
from cleanup.report import data as _data
from cleanup.report import html as H
from cleanup.commands import space as _space_cmd
from cleanup.report import space as _space
from cleanup.report.removal import DONE
from cleanup.report.text import command_line, duration, plural, when
from cleanup.scan import filters as _filters
from cleanup import volumes as _volumes
from cleanup.web import api, view
from cleanup.web.server import PREVIEW_TYPES, RUN_FILES, HTTPError, html_reply

esc = H.esc

NAV = (("/", "Home"), ("/scan", "New scan"), ("/space", "Space"), ("/history", "History"),
       ("/trash", "Trash"), ("/settings", "Settings"))
RECENT = 6                  # runs on the home page
HISTORY_MAX = 300           # runs listed at once on the history page
TRASH_ROWS = 300            # items listed per Trash
SUMMARY_MAX = 400 << 10     # bytes of summary.txt shown on a run's page
ITEMS_MAX = 300             # items listed on a cleanup-completed page
SPACE_TREE = 120            # folders in the Space page's tree (the run's page has more)


# ---------------------------------------------------------------- the frame

def _theme():
    theme = _settings.current().get("ui.theme")
    return theme if theme in ("light", "dark") else "auto"


def _json(obj):
    """JSON for a data block: '<' can't end the block."""
    return json.dumps(obj, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")


def layout(title, body, page, cli=None, init=None, current=None, status=200, refresh=None,
           chrome=True):
    nav = "".join(f'<a href="{href}"{" class=on aria-current=page" if href == current else ""}>'
                  f'{esc(label)}</a>' for href, label in NAV)
    head = (
        "<!DOCTYPE html>\n"
        f'<html lang="en" data-theme="{_theme()}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        '<meta name="referrer" content="no-referrer">\n'
        + (f'<meta http-equiv="refresh" content="0;url={esc(refresh)}">\n' if refresh else "")
        + f"<title>{esc(title)} · clean</title>\n"
        '<link rel="stylesheet" href="/static/app.css">\n'
        + ('<script src="/static/app.js" defer></script>\n' if chrome else "")
        + "</head>\n")
    top = (f'<header class="top"><div class="top-in"><a class="brand" href="/">clean</a>'
           f'<nav class="main">{nav}</nav></div></header>\n' if chrome else "")
    clibox = ""
    if cli:
        clibox = ('<div class="cli"><span class="cli-label">Same in a terminal</span>'
                  f'<code id="cli">{esc(cli)}</code><button type="button" class="small" '
                  'data-act="copy-cli">Copy</button></div>\n')
    data = (f'<script type="application/json" id="init">{_json(init)}</script>\n'
            if init is not None else "")
    extras = ""
    if chrome:
        extras = (
            '<dialog id="confirm" class="dlg" aria-labelledby="dlg-title">'
            '<form class="dlg-body" id="dlg-form">'
            '<h2 id="dlg-title"></h2>'
            '<fieldset id="dlg-method" class="method" hidden><legend>How</legend>'
            '<label><input type="radio" name="method" value="trash"> Move to the Trash '
            '<span class="muted">(can be undone)</span></label>'
            '<label><input type="radio" name="method" value="permanent"> Delete for good '
            '<span class="muted">(no undo)</span></label></fieldset>'
            '<div id="dlg-html" class="dlg-html"></div>'
            '<label id="dlg-typed" class="typed" hidden>Type <b>delete</b> to confirm '
            '<input id="dlg-word" autocomplete="off" spellcheck="false" '
            'autocapitalize="off"></label>'
            '<p id="dlg-error" class="error" role="alert" hidden></p>'
            '<div class="cli" id="dlg-clibox" hidden><span class="cli-label">Same in a '
            'terminal</span><code id="dlg-cli"></code></div>'
            '<div class="dlg-buttons"><button type="button" data-act="close-dialog" '
            'id="dlg-cancel">Cancel</button>'
            '<button type="submit" id="dlg-go" class="primary">OK</button></div>'
            "</form></dialog>\n"
            '<dialog id="preview" class="dlg wide" aria-labelledby="pv-title">'
            '<div class="dlg-body"><h2 id="pv-title" class="path"></h2>'
            '<div id="pv-body" class="pv-body"></div><div class="dlg-buttons">'
            '<button type="button" data-act="close-preview">Close</button></div></div>'
            "</dialog>\n"
            '<div id="toast" class="toast" role="status" aria-live="polite" hidden></div>\n')
    return html_reply(
        head + f'<body data-page="{esc(page)}">\n{top}<main>\n{body}{clibox}</main>\n'
        + data + extras + "</body>\n</html>\n", status)


def message_page(title, text, status=200, refresh=None):
    body = (f'<section class="callout"><div class="headline">{esc(title)}</div>'
            f"<p>{esc(text)}</p></section>\n")
    if not refresh:
        body += '<p><a href="/">Home</a></p>\n'
    return layout(title, body, "message", status=status, refresh=refresh,
                  chrome=not refresh and status != 403)


def get(app, parts, q):
    if not parts:
        return home(app)
    if parts == ["scan"]:
        return scan_page(app, q)
    if len(parts) == 2 and parts[0] == "job":
        return job_page(app, parts[1])
    if len(parts) == 2 and parts[0] == "runs":
        return run_page(app, parts[1], q)
    if parts == ["history"]:
        return history_page(app, q)
    if parts == ["settings"]:
        return settings_page(app)
    if parts == ["trash"]:
        return trash_page(app)
    if parts == ["space"]:
        return space_page(app)
    raise HTTPError(404, "there is no such page")


def _when(ts):
    return when(ts) if ts else "?"


def _run_link(run):
    return f'<a href="/runs/{esc(run.name)}">{esc(run.label)}</a>'


def _roots_html(roots, cls="run-roots path"):
    if not roots:
        return ""
    more = (f' <span class="muted">+{len(roots) - 1} more</span>' if len(roots) > 1 else "")
    return f'<div class="{cls}">{esc(roots[0])}{more}</div>'


def _run_rows(found, actions=False):
    out = ['<ol class="runs">']
    for run in found:
        headline = run.headline or {
            "running": "still running",
            "unfinished": "stopped without finishing (killed, crashed, or the computer "
                          "went off)"}.get(run.status, "")
        links = [f'<a href="/runs/{esc(run.name)}">Open</a>']
        if run.has("report.html"):
            links.append(f'<a href="/runs/{esc(run.name)}/report.html">Report</a>')
        if actions:
            links.append(f'<button type="button" class="link" data-act="confirm" '
                         f'data-action="history_delete" data-run="{esc(run.name)}">'
                         'Delete</button>')
        out.append(
            f'<li><span class="num">{run.number}</span><div>'
            f'<div class="run-line"><span class="run-what">{_run_link(run)}</span>'
            f'{H.badge(run.status)}<span class="run-when">{esc(_when(run.started_ts))}</span>'
            f'</div><div class="run-headline">{esc(headline)}</div>{_roots_html(run.roots)}'
            f'</div><div class="run-links">{"".join(links)}</div></li>')
    out.append("</ol>")
    return "".join(out)


# ---------------------------------------------------------------- home

def drives():
    return _volumes.drives()


def _drives_html(found):
    if not found:
        return '<div class="empty-state">No disks found.</div>'
    out = ['<ul class="rows drives">']
    for name, point, fstype, total, used, free, ro in found:
        pct = used * 100 // total if total else 0
        out.append(
            f'<li class="drive"><div class="drive-line"><b>{esc(name)}</b> '
            f'<span class="path muted">{esc(point)}</span> <span class="tag">{esc(fstype)}'
            f'</span>{" <span class=tag>read-only</span>" if ro else ""}'
            f'<span class="drive-free">{esc(human(free))} free of {esc(human(total))}</span>'
            f'</div><meter min="0" max="{total}" value="{used}" low="{total * 7 // 10}" '
            f'high="{total * 9 // 10}" optimum="0" aria-label="{pct}% used">{pct}%</meter>'
            "</li>")
    out.append("</ul>")
    return "".join(out)


def _pending(app, found):
    """What is waiting: the newest scan to review, a removal still in the
    Trash. [(text, link, link text)]."""
    out = []
    seen = set()
    for run in found:
        cmd = run.command
        if cmd in seen or run.status != "done":
            continue
        if cmd == "dupes" and run.has(_data.NAME):
            seen.add(cmd)
            out.append((f"Last duplicate scan, {_when(run.started_ts)}: {run.headline}",
                        f"/runs/{run.name}", "Review"))
        elif cmd == "empty" and run.meta.get("mode") == "dry-run":
            seen.add(cmd)
            out.append((f"Last empty-folder scan, {_when(run.started_ts)}: {run.headline}",
                        f"/runs/{run.name}", "Review"))
        elif cmd == "remove" and run.meta.get("mode") == "trash":
            seen.add(cmd)
            if not run.meta.get("undone_by") and not run.meta.get("emptied_by"):
                out.append((f"Removal of {_when(run.started_ts)} is in the Trash: "
                            f"{run.headline}", f"/runs/{run.name}",
                            "Undo it, or free the space"))
    return out


def home(app):
    found = runs.history(app.data_dir)
    body = ['<div class="page-head"><div><h1>clean</h1><p class="meta">Find duplicate files '
            "and folders and empty folders, choose what goes, and clear them safely: to the "
            "Trash, with undo.</p></div>"
            '<nav class="actions" aria-label="Start">'
            '<a class="button primary" href="/scan?kind=dupes" title="Copies of files and of '
            'whole folders, by their content">Find duplicates</a>'
            '<a class="button" href="/scan?kind=empty" title="Folders holding nothing but '
            'other empty folders">Find empty folders</a>'
            '<a class="button" href="/space" title="Where the space goes, and what can go">'
            'Space</a></nav></div>\n']
    job = app.jobs.active()
    if job is not None:
        body.append(f'<section class="callout s-running"><div class="headline">A job is '
                    f'running</div><p><code>{esc(command_line(job.argv))}</code> '
                    f'<a href="/job/{esc(job.id)}">See its progress</a></p></section>\n')
    main, side = [], []
    pending = _pending(app, found)
    if pending:
        main.append('<h2>Waiting for you</h2><section class="panel"><ul class="rows">')
        for text, link, label in pending:
            main.append(f'<li class="pending"><span>{esc(text)}</span> '
                        f'<a class="go" href="{esc(link)}">{esc(label)} &rarr;</a></li>')
        main.append("</ul></section>\n")
    main.append(f'<h2>Recent runs <a class="small-link" href="/history">All '
                f'{len(found):,}</a></h2><section class="panel">')
    if found:
        main.append(_run_rows(found[:RECENT]))
    else:
        main.append('<div class="empty-state">No runs yet: start with Find duplicates or '
                    "Find empty folders.</div>")
    main.append("</section>\n")
    side.append('<h2>Disks</h2><section class="panel">' + _drives_html(drives())
                + "</section>\n")
    trashes = api.trash_summary(app)
    side.append('<h2>Trash <a class="small-link" href="/trash">Open</a></h2>'
                '<section class="panel"><ul class="rows">')
    if not trashes:
        side.append('<li class="muted">No Trash on any connected drive.</li>')
    for t in trashes:
        side.append(f'<li><b>{esc(t["where"])}</b> <span class="muted">'
                    f'{esc(plural(t["items"], "item"))} · {esc(human(t["bytes"]))}</span></li>')
    side.append("</ul></section>\n")
    body.append(f'<div class="cols"><div>{"".join(main)}</div>'
                f'<aside>{"".join(side)}</aside></div>\n')
    return layout("Home", "".join(body), "home", cli="clean history", current="/")


# ---------------------------------------------------------------- the scan wizard

VALUE_FLAGS = {"--type", "--ext", "--not-ext", "--min-size", "--max-size", "--newer-than",
               "--older-than", "--name", "--max-depth", "--similar", "-e", "--exclude",
               "-j", "--jobs"}


def given(argv):
    """{flag: [values as typed]} of a dupes or empty command line."""
    out, i, rest = {}, 1, False
    while i < len(argv):
        a = argv[i]
        i += 1
        if rest or not a.startswith("-") or a == "-":
            continue
        if a == "--":
            rest = True
            continue
        flag, eq, val = a.partition("=")
        if not eq and a[:2] in ("-e", "-j") and len(a) > 2:
            flag, eq, val = a[:2], "=", a[2:]
        if flag in VALUE_FLAGS:
            if not eq and i < len(argv):
                val = argv[i]
                i += 1
            out.setdefault({"-e": "--exclude", "-j": "--jobs"}.get(flag, flag), []).append(val)
        else:
            out.setdefault(a, []).append(True)
    return out


def _size_text(n):
    for unit, shift in (("G", 30), ("M", 20), ("K", 10)):
        if n and n % (1 << shift) == 0:
            return f"{n >> shift}{unit}"
    return str(n)


def _lines_text(values):
    """A textarea's text: one value per line (a value's own line break, if
    any, shows as \\n and keeps it one line)."""
    return "\n".join(esc(v) for v in values)


def _field(label, inner, help_=""):
    return (f'<div class="field"><label>{esc(label)}{inner}</label>'
            + (f'<div class="help">{esc(help_)}</div>' if help_ else "") + "</div>")


def _text(flag, value, default="", placeholder="", kind="text", width=""):
    return (f'<input type="text" data-flag="{esc(flag)}" data-kind="{kind}" '
            f'data-default="{esc(default)}" value="{esc(value)}" '
            f'placeholder="{esc(placeholder)}"{" class=" + width if width else ""} '
            'spellcheck="false" autocomplete="off">')


def _lines(flag, values, placeholder=""):
    shown = "&#10;".join(esc(x) for x in placeholder.split("\n"))      # a line break each
    return (f'<textarea data-flag="{esc(flag)}" data-kind="lines" rows="3" spellcheck="false" '
            f'placeholder="{shown}">{_lines_text(values)}</textarea>')


def _check(label, on_flag, off_flag, value, default, help_=""):
    return (f'<div class="field check"><label><input type="checkbox" data-kind="bool" '
            f'data-on="{esc(on_flag)}" data-off="{esc(off_flag)}" '
            f'data-default="{"1" if default else "0"}"{" checked" if value else ""}> '
            f'{esc(label)}</label>' + (f'<div class="help">{esc(help_)}</div>' if help_ else "")
            + "</div>")


def scan_page(app, q):
    kind = q.get("kind") if q.get("kind") in ("dupes", "empty", "space") else "dupes"
    roots, argv = [], None
    src = q.get("from")
    if src:
        run = app.run(src)
        if run.command in ("dupes", "empty", "space"):
            # A space run's folders can start a duplicates scan too (its "scan these").
            if run.command != "space" or kind == "space":
                kind = run.command
                a = run.meta.get("argv")
                if isinstance(a, list) and a and a[0] == kind \
                        and all(isinstance(x, str) for x in a):
                    argv = a
            roots = [r for r in run.roots if os.path.isdir(r)]
    for path in (q.every("path") if hasattr(q, "every") else [q.get("path")]):
        if path and os.path.isabs(path) and os.path.isdir(path):
            path = os.path.normpath(path)
            if path not in roots:
                roots.append(path)
    if kind == "space" and not roots:
        roots = [os.path.expanduser("~")]
    s = _settings.current()
    defaults = api.parse([kind, "/"])
    try:
        args = api.parse(argv) if argv else defaults
    except HTTPError:
        args = defaults
    had = given(argv) if argv else {}

    def last(flag, default=""):
        v = had.get(flag)
        return v[-1] if v and isinstance(v[-1], str) else default

    tabs = "".join(
        f'<a href="/scan?kind={k}"{" class=on" if k == kind else ""}>{esc(t)}</a>'
        for k, t in (("dupes", "Find duplicates"), ("empty", "Find empty folders"),
                     ("space", "Space")))
    body = ['<h1>New scan</h1><p class="meta">A scan only looks: nothing is deleted or moved. '
            "You choose what goes afterwards.</p>\n",
            f'<nav class="tabs">{tabs}</nav>\n',
            f'<form id="scan" class="wizard" data-kind="{kind}" autocomplete="off">\n'
            '<section class="panel step"><div class="panel-head"><span class="id">1</span>'
            '<b>Where to look</b><span class="muted">Add one or more folders. '
            + {"dupes": "Duplicates are found across all of them; of folders that are copies "
                        "of each other, the one in the first folder is kept by default.",
               "empty": "Each is searched; the folders themselves are kept.",
               "space": "Each is measured without leaving its drive: add another drive as a "
                        "folder of its own."}[kind]
            + '</span></div><div class="pad">'
            '<ul class="rows chosen" id="roots"></ul>'
            '<div class="browser" id="browser"><div class="places" id="places"></div>'
            '<div class="here"><button type="button" class="small" data-act="up" id="up">'
            '&uarr; Up</button><span class="path" id="here"></span></div>'
            '<ul class="dirs" id="dirs"></ul>'
            '<div class="browser-foot"><label class="inline"><input type="checkbox" '
            'id="show-hidden"> Show hidden folders</label>'
            '<button type="button" class="primary small" data-act="add-here" id="add-here">'
            "Add this folder</button></div></div>"
            '<div class="typed-path"><input type="text" id="typed-path" '
            'placeholder="or type a full path: /media/me/Drive/Photos" spellcheck="false">'
            '<button type="button" class="small" data-act="add-typed">Add</button></div>'
            "</div></section>\n"]
    if kind == "dupes":
        types = set(t for group in (args.type or []) for t in group) if args.type else \
            set(s.get("dupes.types"))
        default_types = ",".join(sorted(s.get("dupes.types")))
        boxes = "".join(
            f'<label class="inline"><input type="checkbox" value="{esc(t)}"'
            f'{" checked" if t in types else ""}> {esc(t.capitalize())}</label>'
            for t in _filters.TYPES)
        body.append(
            '<section class="panel step"><div class="panel-head"><span class="id">2</span>'
            '<b>Which files</b><span class="muted">Files left out are not compared.</span>'
            '</div><div class="pad grid">'
            f'<div class="field wide"><span class="flabel">Kinds of files</span>'
            f'<div class="set" data-flag="--type" data-kind="set" '
            f'data-default="{esc(default_types)}">{boxes}</div>'
            '<div class="help">None ticked: every kind.</div></div>'
            + _field("Only these extensions", _text("--ext", ",".join(had.get("--ext", [])),
                                                    placeholder="jpg,png"),
                     "comma-separated; with kinds ticked, these as well")
            + _field("Leave out extensions", _text("--not-ext",
                                                   ",".join(had.get("--not-ext", [])),
                                                   placeholder="tmp,part"))
            + _field("Smallest file", _text("--min-size", last(
                "--min-size", _size_text(s.get("dupes.min_size"))),
                _size_text(s.get("dupes.min_size")), "0"),
                "e.g. 4096, 500K, 10M; 0 = any")
            + _field("Biggest file", _text("--max-size", last(
                "--max-size", _size_text(s.get("dupes.max_size"))),
                _size_text(s.get("dupes.max_size")), "0"), "0 = no limit")
            + _field("Changed after", _text("--newer-than", last("--newer-than"),
                                            placeholder="2024-03-01 or 30d"),
                     "a date, or a time ago: 30d, 6w, 2y")
            + _field("Changed before", _text("--older-than", last("--older-than"),
                                             placeholder="2020-01-01 or 2y"))
            + _field("Names", _lines("--name", had.get("--name", []), "IMG_*\n*.mp4"),
                     "one pattern per line, any of them; empty = any name")
            + _field("How deep", f'<input type="number" min="0" data-flag="--max-depth" '
                                 f'data-kind="text" data-default="" '
                                 f'value="{esc(last("--max-depth"))}" placeholder="no limit">',
                     "0 = only the files right in the folders chosen")
            + _check("Include hidden files and folders in the results", "--hidden",
                     "--no-hidden", args.hidden, defaults.hidden,
                     "names starting with a dot; they always count as part of their folder")
            + _field("Leave out", _lines("--exclude", had.get("--exclude", []),
                                         "node_modules\n*.bak\n/media/me/Drive/old"),
                     "names, wildcards, or full paths, one per line; the ignore list in "
                     "Settings (scan.exclude) always applies too")
            + "</div></section>\n")
        body.append(
            '<section class="panel step"><div class="panel-head"><span class="id">3</span>'
            '<b>How they match</b><span class="muted">The content always has to be '
            "identical.</span></div><div class=\"pad grid\">"
            + _check("Names must match too", "--same-name", "--no-same-name", args.same_name,
                     defaults.same_name)
            + _check("Modification times must match too", "--same-date", "--no-same-date",
                     args.same_date, defaults.same_date)
            + _field("Diverged copies share at least",
                     f'<span class="unit"><input type="number" min="1" max="100" '
                     f'data-flag="--similar" data-kind="text" '
                     f'data-default="{defaults.similar}" value="{args.similar}"> %</span>',
                     "two folders that share this much, each with files of its own")
            + _check("Also find look-alike photos and songs", "--media", "--no-media",
                     args.media, defaults.media,
                     "the same photo or song in files whose bytes differ (by their tags); "
                     "shown, never picked for removal")
            + _check("Use the hash cache", "--cache", "--no-cache", args.cache, defaults.cache,
                     "files unchanged since an earlier scan aren't read again")
            + _walk_fields(args, defaults)
            + "</div></section>\n")
        step = 4
    elif kind == "space":
        body.append(
            '<section class="panel step"><div class="panel-head"><span class="id">2</span>'
            '<b>Options</b></div><div class="pad grid">'
            + _field("Leave out", _lines("--exclude", had.get("--exclude", []),
                                         "*.iso\n/media/me/Drive/old"),
                     "not counted at all: names, wildcards, or full paths, one per line; the "
                     "ignore list in Settings (scan.exclude) always applies too")
            + _check("Don't list clutter inside .git, node_modules, caches, Trash, ...",
                     "--default-excludes", "--no-default-excludes", args.default_excludes,
                     defaults.default_excludes,
                     "they are counted either way; programs need what is inside them intact")
            + "</div></section>\n")
        step = 3
    else:
        body.append(
            '<section class="panel step"><div class="panel-head"><span class="id">2</span>'
            '<b>Options</b></div><div class="pad grid">'
            + _field("Leave out", _lines("--exclude", had.get("--exclude", []),
                                         "node_modules\n.git"),
                     "names, wildcards, or full paths, one per line; the ignore list in "
                     "Settings (scan.exclude) always applies too")
            + _walk_fields(args, defaults) + "</div></section>\n")
        step = 3
    body.append(
        f'<section class="panel step"><div class="panel-head"><span class="id">{step}</span>'
        '<b>Start</b></div><div class="pad">'
        '<div class="cli"><span class="cli-label">The command</span><code id="scan-cli">'
        '</code></div><p id="scan-error" class="error" role="alert" hidden></p>'
        '<button type="submit" class="primary" id="start">Start the scan</button>'
        "</div></section></form>\n")
    init = {"kind": kind, "roots": roots, "home": os.path.expanduser("~")}
    return layout("New scan", "".join(body), "scan", init=init, current="/scan")


def _walk_fields(args, defaults):
    return (_check("Stay on one drive", "-x", "--no-one-file-system", args.one_file_system,
                   defaults.one_file_system, "don't go into other drives mounted inside")
            + _check("Skip the usual system and tool folders", "--default-excludes",
                     "--no-default-excludes", args.default_excludes,
                     defaults.default_excludes, ".git, node_modules, caches, Trash, ...")
            + _field("Parallel workers",
                     f'<input type="number" min="1" max="64" data-flag="--jobs" data-kind="text" '
                     f'data-default="{defaults.jobs}" value="{args.jobs}">',
                     "more than 1 helps only on SSDs; it slows spinning USB drives"))


# ---------------------------------------------------------------- a job

JOB_TITLES = {"scan": "Scanning for duplicates", "empty": "Empty folders",
              "remove": "Removing", "undo": "Undoing a removal", "trash": "Trash",
              "history": "History", "space": "Looking at where the space goes",
              "cache": "Hash cache"}


def job_page(app, jid):
    job = app.jobs.get(jid)
    if job is None:
        raise HTTPError(404, "no such job: the dashboard was restarted, or the link is old. "
                             "Its run is in History.")
    title = JOB_TITLES.get(job.kind, "Job")
    if job.kind == "empty":
        title = "Deleting empty folders" if "--delete" in job.argv else "Looking for empty folders"
    body = (f'<h1 id="job-title">{esc(title)}</h1>'
            f'<p class="meta">Started {esc(when(job.started))}</p>\n'
            '<section class="callout s-running" id="job-box"><div class="headline" '
            'id="job-head">Running...</div><p class="mono progress" id="job-progress"></p>'
            '<p class="job-acts"><button type="button" data-act="cancel-job" id="cancel">'
            'Cancel</button><span id="job-links"></span></p></section>\n'
            '<h2>Output</h2><pre class="out" id="job-out" tabindex="0"></pre>\n')
    return layout(title, body, "job", cli=command_line(job.argv),
                  init={"id": job.id, "kind": job.kind})


# ---------------------------------------------------------------- a run

def run_page(app, name, q):
    run = app.run(name)
    if run.command == "dupes" and run.status == "done":
        try:
            res = app.results(run)
        except HTTPError as e:
            return summary_page(app, run, problem=e.message)
        return results_page(app, run, res, q)
    if run.command == "remove" and run.meta.get("mode") in ("trash", "permanent") \
            and run.has(_data.NAME):
        return completed_page(app, run)
    if run.command == "space" and run.status == "done" and run.has(_data.NAME):
        try:
            doc, d = _space_doc(app, run)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
            return summary_page(app, run, problem=f"{_data.NAME} can't be read: {why(e)}")
        return space_run_page(app, run, doc, d, q)
    return summary_page(app, run)


def _crumb():
    return '<a class="crumb" href="/history">&larr; History</a>\n'


def _head(run, title, tag):
    took = run.meta.get("duration_s")
    bits = [esc(_when(run.started_ts))]
    if isinstance(took, (int, float)):
        bits.append("took " + esc(duration(took)))
    bits.append(f"run {run.number or '?'} · <span class=\"mono\">{esc(run.name)}</span>")
    return (_crumb() + f'<h1>{esc(title)} <span class="tag">{esc(tag)}</span></h1>\n'
            f'<p class="meta">{" · ".join(bits)}</p>\n')


def _file_links(run):
    links = [f'<a href="/runs/{esc(run.name)}/{esc(f)}">{esc(f)}</a>'
             for f in RUN_FILES if run.has(f)]
    return ('<p class="files">Saved with it: ' + " ".join(links) + "</p>\n") if links else ""


def summary_page(app, run, problem=None):
    titles = {"dupes": "Duplicates", "empty": "Empty folders", "remove": "Removal",
              "undo": "Undo", "trash": "Trash", "space": "Space"}
    body = [_head(run, titles.get(run.command, run.command), run.label)]
    headline = run.headline or {"running": "still running",
                                "unfinished": "stopped without finishing"}.get(run.status, "")
    body.append(H._callout(run, headline or run.status))
    if problem:
        body.append(f'<p class="note">{esc(problem)}</p>\n')
    if run.status == "running":
        job = next((j for j in app.jobs.all.values() if j.run_name == run.name), None)
        if job is not None:
            body.append(f'<p><a href="/job/{esc(job.id)}">See its progress</a></p>\n')
    acts = []
    if run.command == "empty" and run.meta.get("mode") == "dry-run" and run.status == "done":
        acts.append(f'<button type="button" class="primary" data-act="confirm" '
                    f'data-action="empty_delete" data-run="{esc(run.name)}">Delete these '
                    "empty folders...</button>")
        acts.append(f'<a class="button" href="/scan?from={esc(run.name)}">Scan again</a>')
    if run.command == "dupes":
        acts.append(f'<a class="button" href="/scan?from={esc(run.name)}">Scan again</a>')
    if run.command == "space" and run.status != "running":
        acts.append(f'<a class="button" href="/scan?kind=space&amp;from={esc(run.name)}">'
                    "Look again</a>")
    src = run.meta.get("source")
    if run.command in ("undo", "remove") and isinstance(src, str) and runs.NAME_RE.match(src):
        acts.append(f'<a class="button" href="/runs/{esc(src)}">The run it came from</a>')
    if run.has("report.html"):
        acts.append(f'<a class="button" href="/runs/{esc(run.name)}/report.html">View the '
                    "report</a>")
    if run.status != "running":
        acts.append(f'<button type="button" data-act="confirm" data-action="history_delete" '
                    f'data-run="{esc(run.name)}">Delete this run</button>')
    body.append('<p class="acts">' + "".join(acts) + "</p>\n")
    if run.has("summary.txt"):
        try:
            with open(run.file("summary.txt"), "rb") as f:
                raw = f.read(SUMMARY_MAX + 1)
            text = raw[:SUMMARY_MAX].decode("utf-8", "replace")
            if len(raw) > SUMMARY_MAX:
                text += "\n... (the rest is in summary.txt)"
            body.append(f'<h2>Summary</h2><pre class="out">{H.esc_lines(text)}</pre>\n')
        except OSError as e:
            body.append(f'<p class="note">summary.txt can\'t be read: {esc(why(e))}</p>\n')
    body.append(_file_links(run))
    return layout(run.label, "".join(body), "run", cli=command_line(["show", run.name]),
                  init={"run": run.name})


# ---------------------------------------------------------------- results (choosing)

AUTO = (("suggested", "As suggested", "the copy in the first folder you chose, then the "
                                      "oldest, then the shortest path stays"),
        ("newest", "Keep the newest copy", "in every result"),
        ("oldest", "Keep the oldest copy", "in every result"),
        ("one", "One copy per file group", "even where a folder result would keep more"),
        ("exts", "Only some extensions...", "remove copies with these extensions only"),
        ("merge", "Merge diverged folders", "copy what only the other has into the kept "
                                            "one, then remove it"),
        ("invert", "Invert", "of the results shown with these filters"),
        ("none", "Select nothing", "then tick what should go"))


def results_page(app, run, res, q):
    out = view.results(res, q)
    tab = out["tab"]
    roots = res.m.roots
    body = [_head(run, "Duplicates", "choose what goes")]
    notes = list(res.notes)
    for u in res.unavailable:
        notes.append(f"{printable(u)}: its drive is not connected, so nothing there is touched")
    for v in res.vanished:
        notes.append(f"{printable(v)} is not there any more")
    body.append(
        f'<section class="callout s-{esc(run.status)}"><div class="headline">'
        f'{esc(run.headline)}</div><p>Tick what should go. Your choices are saved as you go; '
        "nothing happens until you press Remove and confirm. A folder ticked goes with "
        "everything in it.</p>"
        + "".join(f'<p class="note">{esc(n)}</p>' for n in notes)
        + f'<p class="muted">Scanned: {esc(", ".join(printable(r) for r in roots))}</p>'
        + "</section>\n")
    tabs = "".join(
        f'<a href="?tab={k}" data-tab="{k}"{" class=on" if k == tab else ""}>{esc(t)}'
        f'<span class="count" data-count="{k}">{out["counts"][k]:,}</span></a>'
        for k, t in res.tabs)
    sel = lambda name, opts, cur: (  # noqa: E731
        f'<select name="{name}">' + "".join(
            f'<option value="{esc(v)}"{" selected" if str(v) == cur else ""}>{esc(t)}</option>'
            for v, t in opts) + "</select>")
    types = [("", "all")] + [(t, t) for t in sorted({t for cards in res.cards.values()
                                                       for c in cards for t in c.types})]
    sizes = [(str(n), t) for n, t in view.SIZES]
    locs = res.places.options()
    auto = "".join(f'<button type="button" class="menu-item" data-act="auto" data-how="{k}">'
                   f'<b>{esc(t)}</b><span class="muted">{esc(h)}</span></button>'
                   for k, t, h in AUTO)
    body.append(
        f'<nav class="tabs" id="tabs">{tabs}</nav>\n'
        '<form class="toolbar" id="filters" role="search">'
        f'<input type="search" name="q" value="{esc(q.get("q", ""))}" '
        'placeholder="Search paths and names" aria-label="Search">'
        f'<label>Type {sel("type", types, q.get("type", ""))}</label>'
        f'<label>Reclaimable {sel("size", sizes, q.get("size", "0"))}</label>'
        f'<label>Date {sel("date", view.DATES, q.get("date", ""))}</label>'
        f'<label>In <select name="loc"><option value="">everywhere</option>{locs}</select>'
        "</label>"
        f'<label>Status {sel("status", view.STATUS, q.get("status", ""))}</label>'
        f'<label>Sort {sel("sort", view.SORTS, q.get("sort", "space"))}</label>'
        f'<label class="folded" id="folded-box"{"" if tab == "groups" else " hidden"}>'
        f'<input type="checkbox" name="folded" value="1"'
        f'{" checked" if q.get("folded") == "1" else ""}> also the <span id="folded-n">'
        f'{out["folded"]:,}</span> inside folder results</label>'
        '<details class="menu" id="auto"><summary>Auto-select</summary>'
        f'<div class="menu-list">{auto}</div></details>'
        '<span class="shown" id="shown"></span></form>\n'
        f'<div id="cards" class="cards">{out["html"]}</div>\n'
        '<nav class="pager" id="pager"></nav>\n'
        '<div class="foot" id="foot"><div class="foot-in"><div class="foot-text">'
        '<b id="foot-text"></b> <span class="muted" id="foot-how"></span></div>'
        '<button type="button" class="primary" data-act="remove" id="remove-btn">'
        "Remove...</button></div></div>\n")
    body.append(_file_links(run))
    init = {"run": run.name, "q": {k: v for k, v in q.items() if k in (
                "tab", "q", "type", "size", "date", "loc", "status", "sort", "page", "folded")},
            "result": {k: out[k] for k in ("tab", "shown", "total", "page", "pages", "footer",
                                           "counts", "folded")},
            "preview": {e: k[0] for e, k in PREVIEW_TYPES.items()},
            "method": _settings.current().get("remove.method")}
    if "loc" in q:
        init["loc"] = q["loc"]
    return layout(f"Duplicates {run.name}", "".join(body), "results",
                  cli=api.results_cli(run), init=init)


# ---------------------------------------------------------------- cleanup completed

def completed_page(app, run):
    try:
        d = _data.read(run.file(_data.NAME))
        at = {c: i for i, c in enumerate(d["columns"])}
        items = d["items"]
    except (OSError, ValueError, KeyError, TypeError, EOFError) as e:
        return summary_page(app, run, problem=f"its results can't be read: {e}")
    method = d.get("method")
    link = bool(d.get("link"))
    col = lambda r, c: r[at[c]]  # noqa: E731
    done = [r for r in items if col(r, "status") in DONE]
    gone = [r for r in items if col(r, "status") == "gone"]
    trouble = [r for r in items if col(r, "status") not in DONE + ("gone",)]
    files = sum(col(r, "done_files") for r in items if col(r, "status") in DONE + ("partial",))
    size = sum(col(r, "done_bytes") for r in items if col(r, "status") in DONE + ("partial",))
    freed = d.get("freed") or 0
    src_name = d.get("source") if isinstance(d.get("source"), str) else None
    source = None
    if src_name and runs.NAME_RE.match(src_name):
        try:
            source = app.run(src_name, "dupes")
        except HTTPError:
            source = None
    remaining = None
    if source is not None:
        try:
            remaining = view.results(app.results(source), {"tab": "families"})["footer"]
        except HTTPError:
            remaining = None
    undone = run.meta.get("undone_by")
    emptied = run.meta.get("emptied_by")
    verb = "Replaced with hard links" if link else (
        "Deleted for good" if method == "permanent" else "Moved to the Trash")
    body = [_head(run, "Cleanup completed", run.label)]
    if method == "trash" and not link:
        space = (f"{human(freed)} is freed once these are emptied from the Trash."
                 if not emptied else f"Emptied from the Trash ({emptied}): the space is free.")
    else:
        space = f"{human(freed)} freed."
    body.append(H._callout(run, run.headline, esc(space)))
    if isinstance(undone, dict):
        body.append(f'<p class="note">Undone by run <a href="/runs/{esc(undone.get("run"))}">'
                    f'{esc(undone.get("run"))}</a>'
                    + ("" if undone.get("full") else ": some of it") + ".</p>\n")
    body.append(H._stats([
        (f"{len(done):,}", f"{verb.lower()}", False),
        (f"{files:,}", "files", False),
        (human(size), "in them", False),
        (f"{len(trouble):,}", "skipped or failed", bool(trouble)),
        (f"{len(gone):,}", "gone already", False),
        (f"{len(d.get('blocked') or []):,}", "blocked", False),
    ] + ([(f"{remaining['items']:,}", "still selected in the scan", False)]
         if remaining else [])))
    acts = []
    if source is not None:
        acts.append(f'<a class="button" href="/runs/{esc(source.name)}">Select more</a>')
        acts.append(f'<a class="button" href="/scan?from={esc(source.name)}">Scan again</a>')
    if run.has("report.html"):
        acts.append(f'<a class="button" href="/runs/{esc(run.name)}/report.html">View report'
                    "</a>")
    if method == "trash" and not link and not emptied and not (
            isinstance(undone, dict) and undone.get("full")):
        acts.append(f'<button type="button" data-act="confirm" data-action="undo" '
                    f'data-run="{esc(run.name)}">Undo</button>')
        acts.append(f'<button type="button" class="danger" data-act="confirm" '
                    f'data-action="trash_empty_run" data-run="{esc(run.name)}">Empty these '
                    "from the Trash</button>")
    body.append('<p class="acts">' + "".join(acts) + "</p>\n")
    if remaining and remaining["items"]:
        body.append(f'<p class="muted">The scan still has {esc(remaining["text"])} selected: '
                    "Select more to go on.</p>\n")
    if trouble:
        body.append(_items("Not done", trouble, col, why_col=True))
    if d.get("blocked"):
        rows = d["blocked"]
        body.append(f'<h2>Blocked <span class="count">{len(rows):,}</span></h2>'
                    '<div class="panel"><table><thead><tr><th>Where</th><th>Why</th></tr>'
                    "</thead><tbody>"
                    + "".join(f'<tr><td class="path">{esc(b[1])}</td><td class="why">'
                              f'{esc(b[2])}</td></tr>' for b in rows[:ITEMS_MAX])
                    + "</tbody></table></div>\n")
    body.append(_items(verb, done, col))
    if gone:
        body.append(_items("Gone already", gone, col, why_col=True))
    body.append(_file_links(run))
    return layout("Cleanup completed", "".join(body), "completed",
                  cli=command_line(["show", run.name]), init={"run": run.name})


def _items(title, rows, col, why_col=False):
    if not rows:
        return ""
    out = [f'<h2>{esc(title)} <span class="count">{len(rows):,}</span></h2>'
           '<div class="panel"><table class="num"><thead><tr><th>What</th>'
           + ('<th>Why</th>' if why_col else '<th class="n">Files</th><th class="n">Size</th>')
           + "</tr></thead><tbody>"]
    for r in rows[:ITEMS_MAX]:
        what = col(r, "path") + (f"  →  {col(r, 'into')}" if col(r, "kind") == "merge" else "")
        if why_col:
            note = col(r, "note") or col(r, "status")
            tail = (f'<td class="why"><span class="badge">{esc(col(r, "status"))}</span> '
                    f"{esc(note)}</td>")
        else:
            tail = (f'<td class="n">{col(r, "done_files"):,}</td>'
                    f'<td class="n">{esc(human(col(r, "done_bytes")))}</td>')
        out.append(f'<tr><td class="path">{esc(what)}</td>{tail}</tr>')
    out.append("</tbody></table>")
    if len(rows) > ITEMS_MAX:
        out.append(f'<ul class="rows"><li class="more">... and {len(rows) - ITEMS_MAX:,} more '
                   "(summary.txt and data.json.gz have all)</li></ul>")
    out.append("</div>\n")
    return "".join(out)


# ---------------------------------------------------------------- history

def history_page(app, q):
    found = runs.history(app.data_dir)
    cmd, status, words = q.get("cmd", ""), q.get("status", ""), q.get("q", "").lower().split()
    commands = sorted({r.command for r in found})
    statuses = sorted({r.status for r in found})
    shown = [r for r in found
             if (not cmd or r.command == cmd) and (not status or r.status == status)
             and all(w in " ".join([r.name, r.label, r.headline] + r.roots).lower()
                     for w in words)]
    s = _settings.current()
    days, keep = s.get("history.keep_days"), s.get("history.keep_runs")
    policy = (f"Runs older than {plural(days, 'day')} are removed, except the newest "
              f"{keep:,} and any a removal can still be undone from."
              if days else "Runs are kept until you delete them (history.keep_days is 0).")
    opt = lambda values, cur, any_: (  # noqa: E731
        f'<option value="">{esc(any_)}</option>' + "".join(
            f'<option value="{esc(v)}"{" selected" if v == cur else ""}>{esc(v)}</option>'
            for v in values))
    body = ['<h1>History</h1>',
            f'<p class="meta">{esc(plural(len(found), "run"))} in '
            f'<span class="path">{esc(app.data_dir)}</span>. {esc(policy)}</p>\n',
            '<form class="toolbar" id="history-filters" method="get" action="/history">'
            f'<input type="search" name="q" value="{esc(q.get("q", ""))}" '
            'placeholder="Search names, folders, results" aria-label="Search">'
            f'<label>Command <select name="cmd">{opt(commands, cmd, "all")}</select></label>'
            f'<label>Status <select name="status">{opt(statuses, status, "any")}</select>'
            '</label><button type="submit" class="small">Filter</button>'
            f'<span class="shown">{len(shown):,} of {len(found):,}</span>'
            '<button type="button" class="small" data-act="confirm" '
            'data-action="history_prune">Prune old runs...</button></form>\n',
            '<section class="panel">']
    if shown:
        body.append(_run_rows(shown[:HISTORY_MAX], actions=True))
        if len(shown) > HISTORY_MAX:
            body.append(f'<ul class="rows"><li class="more">... and '
                        f'{len(shown) - HISTORY_MAX:,} older: narrow the filters</li></ul>')
    else:
        body.append('<div class="empty-state">' + ("No runs match." if found else
                    "No runs yet: start with a scan.") + "</div>")
    body.append("</section>\n")
    return layout("History", "".join(body), "history", cli="clean history --all",
                  current="/history")


# ---------------------------------------------------------------- settings

GROUPS = (("scan", "Scanning"), ("dupes", "Duplicates"), ("empty", "Empty folders"),
          ("junk", "System junk files"), ("cache", "Hash cache"), ("remove", "Removing"),
          ("trash", "Trash"), ("space", "Space"), ("history", "History"), ("ui", "Dashboard"), ("paths", "Where data is kept"))


def _editor(key, value, env):
    dis = " disabled" if env else ""
    name = esc(key.name)
    if key.kind == "bool":
        return (f'<label class="inline"><input type="checkbox" data-input="{name}"'
                f'{" checked" if value else ""}{dis}> on</label>')
    if key.kind == "list":
        return (f'<textarea data-input="{name}" rows="{min(max(len(value), 2), 10)}" '
                f'spellcheck="false"{dis}>{_lines_text(value)}</textarea>')
    if key.choices and key.kind == "word":
        return (f'<select data-input="{name}"{dis}>' + "".join(
            f'<option value="{esc(c)}"{" selected" if c == value else ""}>{esc(c)}</option>'
            for c in key.choices) + "</select>")
    text = _size_text(value) if key.kind == "size" else _settings.text_value(key, value)
    return (f'<input type="text" data-input="{name}" value="{esc(text)}" spellcheck="false"'
            f'{dis}>')


def _setting_row(s, key):
    source = s.source(key.name)
    env = source.startswith("environment")
    value = s.get(key.name)
    default = _settings.format_value(key, key.default)
    if key.name == "paths.data":
        edit = (f'<div class="value path">{esc(_settings.format_value(key, value))}</div>'
                '<div class="help">Change it in a terminal: <code>clean config set paths.data '
                "FOLDER</code> (the dashboard keeps the folder it started with).</div>")
        acts = ""
    else:
        edit = _editor(key, value, env)
        acts = ('<div class="s-acts">'
                f'<button type="button" class="small" data-act="setting-save"{" disabled" if env else ""}>'
                'Save</button>'
                f'<button type="button" class="small" data-act="setting-reset"'
                f'{" disabled" if source == "default" or env else ""}>Reset to default</button>'
                "</div>")
    src_cls = "env" if env else ("file" if source == "settings file" else "default")
    return (f'<li class="setting" data-key="{esc(key.name)}" data-kind="{esc(key.kind)}">'
            f'<div class="s-name"><code>{esc(key.name)}</code> '
            f'<span class="badge src-{src_cls}">{esc(source)}</span></div>'
            f'<div class="s-help">{esc(key.help)}</div>'
            f'<div class="s-edit">{edit}</div>'
            f'<div class="s-meta">Default: <span class="mono">{esc(default)}</span>'
            + (f" · set by {esc(key.env)} in the environment, which the dashboard can't "
               "change" if env else "") + f"</div>{acts}</li>")


def _tree_size(path, budget=2.0):
    """(bytes, files, complete) of a folder, within `budget` seconds."""
    total = files = 0
    end = time.monotonic() + budget
    stack = [path]
    while stack:
        if time.monotonic() > end:
            return total, files, False
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files, True


def settings_page(app):
    s = _settings.reload()
    body = ['<h1>Settings</h1>',
            f'<p class="meta">Saved in <span class="path">{esc(s.shown_path)}</span>. A '
            "command-line option wins over an environment variable (CLEAN_...), which wins "
            "over this file, which wins over the default.</p>\n"]
    for w in s.check_all():
        body.append(f'<p class="note">{esc(w)}</p>\n')
    if s.broken:
        body.append(f'<p class="error">The settings file can\'t be changed: {esc(s.broken)}. '
                    "Fix it with <code>clean config edit</code>, or delete it.</p>\n")
    ex = _settings.KEYS["scan.exclude"]
    body.append('<h2>Ignore list</h2><section class="panel"><ul class="rows settings">'
                + _setting_row(s, ex) + "</ul></section>\n")
    for prefix, title in GROUPS:
        keys = [k for n, k in _settings.KEYS.items()
                if n.split(".")[0] == prefix and n != "scan.exclude"]
        if not keys:
            continue
        body.append(f'<h2>{esc(title)}</h2><section class="panel"><ul class="rows settings">'
                    + "".join(_setting_row(s, k) for k in keys) + "</ul></section>\n")
    places = []
    rows_n = len(runs.history(app.data_dir))
    for place, label, path in (("data", f"Runs and reports ({plural(rows_n, 'run')})",
                                app.data_dir),
                               ("config", "Settings", paths.config_dir()),
                               ("cache", "Hash cache (safe to delete any time)",
                                paths.cache_dir())):
        size, files, complete = _tree_size(path) if os.path.isdir(path) else (0, 0, True)
        amount = (f"{human(size)} in {plural(files, 'file')}" + ("" if complete else " or more")
                  if os.path.isdir(path) else "not created yet")
        places.append(f'<li class="place"><div><b>{esc(label)}</b> <span class="muted">'
                      f'{esc(amount)}</span></div><div class="path">{esc(path)}</div>'
                      + (f'<button type="button" class="small" data-act="open-place" '
                         f'data-place="{place}">Open folder</button>'
                         if os.path.isdir(path) else "") + "</li>")
    body.append('<h2>Data locations</h2><section class="panel"><ul class="rows">'
                + "".join(places) + "</ul></section>\n")
    return layout("Settings", "".join(body), "settings", cli="clean config list",
                  current="/settings")


# ---------------------------------------------------------------- the Trash

def trash_page(app):
    trashes = api.trash_summary(app, items=True)
    body = ['<h1>Trash</h1><p class="meta">Each drive has its own Trash: items stay on their '
            "drive, so their space is freed only when they are emptied from it.</p>\n"]
    if trashes:
        drives = "".join(f'<option value="{esc(t["drive"])}">{esc(t["where"])}</option>'
                         for t in trashes)
        body.append(
            '<form class="toolbar" id="trash-empty">'
            '<label>Empty items in the Trash for <input type="number" min="0" step="1" '
            'name="older_than" value="30" class="short"> days or more</label>'
            f'<label>on <select name="drive"><option value="">every drive</option>{drives}'
            '</select></label><button type="button" class="danger small" '
            'data-act="trash-empty">Empty...</button>'
            '<span class="muted">0 days: everything</span></form>\n')
    else:
        body.append('<section class="panel"><div class="empty-state">No Trash on any connected '
                    "drive.</div></section>\n")
    warn_gb = _settings.current().get("trash.warn_gb")
    for t in trashes:
        body.append(
            f'<section class="panel"><div class="panel-head"><b>{esc(t["where"])}</b>'
            f'<span class="path muted">{esc(t["path"])}</span>'
            f'<span class="save">{esc(plural(t["items"], "item"))} · {esc(human(t["bytes"]))}'
            "</span></div>")
        if warn_gb and t["bytes"] > warn_gb * 1024 ** 3:
            body.append(f'<p class="note">More than {warn_gb} GB (trash.warn_gb): that space '
                        "is only free once the Trash is emptied.</p>")
        if not t["entries"]:
            body.append('<div class="empty-state">Empty.</div></section>\n')
            continue
        body.append('<table class="trash"><thead><tr><th>Where it was</th><th>Deleted</th>'
                    '<th class="n">Size</th><th>By</th><th></th></tr></thead><tbody>')
        for e in t["entries"][:TRASH_ROWS]:
            if e["orphan"]:
                act = '<span class="muted">no record of where it was</span>'
            elif e["exists"]:
                act = '<span class="muted">something is there again</span>'
            else:
                act = (f'<button type="button" class="small" data-act="confirm" '
                       f'data-action="trash_restore" data-token="{esc(e["token"])}">Restore'
                       "</button>")
            by = (f'<a href="/runs/{esc(e["run"])}">clean</a>' if e["run"]
                  else '<span class="muted">another program</span>')
            body.append(f'<tr><td class="path">{esc(e["original"])}</td>'
                        f'<td data-label="Deleted">'
                        f'{esc((e["deleted"] or "?").replace("T", " ")[:16])}</td>'
                        f'<td class="n" data-label="Size">{esc(human(e["bytes"]))}</td>'
                        f'<td data-label="By">{by}</td>'
                        f"<td>{act}</td></tr>")
        body.append("</tbody></table>")
        if len(t["entries"]) > TRASH_ROWS:
            body.append(f'<ul class="rows"><li class="more">... and '
                        f'{len(t["entries"]) - TRASH_ROWS:,} more (clean trash list --all)'
                        "</li></ul>")
        body.append("</section>\n")
    return layout("Trash", "".join(body), "trash", cli="clean trash list", current="/trash")


# ---------------------------------------------------------------- space

def _space_doc(app, run):
    """A space run's data and its folder index, kept for the next page (a
    big home folder's takes a moment to read)."""
    hit = app.misc.get("space")
    if hit is not None and hit[0] == run.name:
        return hit[1], hit[2]
    doc = _data.read(run.file(_data.NAME))
    d = _space.Doc(doc)
    app.misc["space"] = (run.name, doc, d)
    return doc, d


def _trash_button(t):
    if not t["items"]:
        return ""
    return (f'<p class="acts"><button type="button" class="danger small" data-act="confirm" '
            f'data-action="trash_empty" data-drive="{esc(t["drive"])}" data-back="/space">'
            "Empty it...</button></p>")


def _clutter_line(doc, link):
    bits = []
    for kind, label, one in _space.CLUTTER:
        c = doc["clutter"][kind]
        if c["count"]:
            bits.append(plural(c["count"], one, label[0].lower() + label[1:])
                        + (f" ({human(c['bytes'])})" if c["bytes"] else ""))
    if not bits:
        return '<p class="muted">None found.</p>'
    return f'<p>{esc(", ".join(bits))} <a class="small-link" href="{esc(link)}">See them</a></p>'


def space_page(app):
    """Where the space goes: the drives and the newest look (`clean space`)
    as it was, and what clean can clear itself as it is now: the Trash and
    its own data."""
    home = os.path.expanduser("~")
    found = runs.history(app.data_dir)
    last = next((r for r in found
                 if r.command == "space" and r.status == "done" and r.has(_data.NAME)), None)
    look = f"/scan?kind=space&from={last.name}" if last else "/scan?kind=space"
    body = ['<div class="page-head"><div><h1>Space</h1><p class="meta">Where the space goes, '
            "and what can go. Looking deletes nothing; clean clears only the Trash and its own "
            "data, and names each program's own command for its cache.</p></div>"
            f'<nav class="actions" aria-label="Start"><a class="button primary" '
            f'href="{esc(look)}">{"Look again..." if last else "Look at a folder..."}</a>'
            "</nav></div>\n"]
    job = app.jobs.active()
    if job is not None:
        body.append(f'<section class="callout s-running"><div class="headline">A job is '
                    f'running</div><p><code>{esc(command_line(job.argv))}</code> '
                    f'<a href="/job/{esc(job.id)}">See its progress</a></p></section>\n')
    body.append("<h2>Drives</h2>" + _drives_html(drives()) + "\n")
    doc = None
    if last is None:
        body.append('<h2>Folders</h2><div class="empty-state">Nothing looked at yet. '
                    '<a href="/scan?kind=space">Look at your home folder</a>: the biggest '
                    "folders and files, the caches programs keep, and clutter. It only "
                    "reads.</div>\n")
    else:
        try:
            doc, d = _space_doc(app, last)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
            body.append(f'<p class="note">The newest look ({esc(last.name)}) can\'t be read: '
                        f"{esc(why(e))}</p>\n")
    if doc is not None:
        url = f"/runs/{last.name}"
        body.append(
            f'<h2>Folders <a class="small-link" href="{esc(url)}">The whole look</a></h2>'
            f'<p class="meta">{esc(_space.headline(doc))} · {esc(_when(last.started_ts))}</p>'
            + _space.tree_html(d, budget=SPACE_TREE,
                               focus=lambda n: f"{url}?dir={n}#folders") + "\n")
        body.append('<h2>Caches</h2><p class="muted">As they were then. clean never clears '
                    "them: each comes with its program's own command.</p>"
                    + (_space.caches_html(doc["caches"], home) if doc.get("caches")
                       else '<div class="empty-state">None found in these folders.</div>'))
        if doc.get("system_caches"):
            body.append('<h2>System caches</h2><p class="muted">These commands need sudo: '
                        "clean prints them and never runs them.</p>"
                        + _space.caches_html(doc["system_caches"], home, sudo=True))
        body.append("<h2>Clutter</h2>" + _clutter_line(doc, f"{url}#clutter"))
        dupes = dict(doc, dupes=_space_cmd.newest_dupes(doc["roots"], app.data_dir))
        body.append("<h2>Duplicates</h2>"
                    + _space.dupes_html(dupes, lambda name: f"/runs/{name}",
                                        f"/scan?kind=dupes&from={last.name}"))
    trashes = api.trash_summary(app)
    body.append('<h2>Trash <a class="small-link" href="/trash">Open</a></h2>'
                + _space.trash_html(trashes, _settings.current().get("trash.warn_gb"),
                                    button=_trash_button))
    own = _space_cmd.own_data(data_dir=app.data_dir)
    acts = []
    if own["prunable"]:
        acts.append('<button type="button" class="small" data-act="confirm" '
                    'data-action="history_prune" data-back="/space">Prune old runs...</button>')
    if own["cache"]["exists"]:
        acts.append('<button type="button" class="small" data-act="confirm" '
                    'data-action="cache_prune">Prune the hash cache...</button>'
                    '<button type="button" class="small" data-act="confirm" '
                    'data-action="cache_clear">Delete the hash cache...</button>')
    acts.append('<button type="button" class="small" data-act="open-place" '
                'data-place="data">Open the folder</button>')
    body.append("<h2>clean's own data</h2>" + _space.own_html(own, home)
                + '<p class="acts">' + "".join(acts) + "</p>\n")
    return layout("Space", "".join(body), "space", cli="clean space", current="/space")


def space_run_page(app, run, doc, d, q):
    start = None
    if q.get("dir"):
        try:
            start = int(q["dir"])
        except ValueError:
            raise HTTPError(400, "dir must be a folder's number")
        if not 0 <= start < len(d.folders):
            raise HTTPError(404, "this run has no such folder")
    url = f"/runs/{run.name}"
    body = [_head(run, "Space", "read-only"),
            H._callout(run, _space.headline(doc), "Read-only: nothing was deleted. What can go "
                                                  "is listed with the command for it.")]
    acts = [f'<a class="button" href="/scan?kind=space&amp;from={esc(run.name)}">Look again'
            "</a>"]
    if run.has("report.html"):
        acts.append(f'<a class="button" href="{esc(url)}/report.html">View the report</a>')
    acts.append(f'<button type="button" data-act="confirm" data-action="history_delete" '
                f'data-run="{esc(run.name)}">Delete this run</button>')
    body.append('<p class="acts">' + "".join(acts) + "</p>\n")
    body.append(_space.sections(
        doc, d, dupes_link=lambda name: f"/runs/{name}",
        focus=lambda n: url + (f"?dir={n}" if n is not None else "") + "#folders",
        start=start, trash_extra=' · or from the <a href="/space">Space page</a>',
        dupes_scan=f"/scan?kind=dupes&from={run.name}"))
    body.append(_file_links(run))
    return layout(run.label, "".join(body), "run", cli=command_line(["show", run.name]),
                  init={"run": run.name})
