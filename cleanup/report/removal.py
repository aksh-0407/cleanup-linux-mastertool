"""
What `clean remove` shows and saves: the plan before it starts (and the
question it asks), then summary.txt, report.html and data.json.gz of the
run. The rows, one per item of the plan, are filled in as the run goes:

    {"step", "kind": folder | file | merge, "path", "into" (merge), "why",
     "files", "bytes", "moving" (merge: files that move in),
     "status": would | removed | linked | merged | gone | skipped | failed |
               partial | not-done, "note", "done_files", "done_bytes"}
"""

from cleanup.actions.trash import Trash
from cleanup.fsutil import human, printable
from cleanup.report import data
from cleanup.report import html as _html
from cleanup.report.text import RULE, TERMINAL_ISSUES, command_line, plural, when

PREVIEW = 25            # items listed in the terminal before asking
ITEMS = 5000            # items listed in summary.txt (data.json.gz has every one)
MAX_ROWS = 2000         # items per table in report.html

STATUSES = ("removed", "linked", "merged", "gone", "skipped", "failed", "partial", "not-done")
DONE = ("removed", "linked", "merged")
ISSUE_TITLES = (
    ("skipped", "Skipped: left exactly as they were"),
    ("failed", "Failed"),
    ("partial", "Partly done"),
)


# ---------------------------------------------------------------- building

def new_info(source, m, plan, args, unavailable, notes, missing=()):
    return {"source": source.name, "source_ts": source.started_ts, "roots": list(m.roots),
            "method": args.method, "link": args.link, "merge": args.merge,
            "conflicts": args.conflicts, "dry_run": bool(getattr(args, "dry_run", False)),
            "unavailable": list(unavailable), "missing_roots": list(missing),
            "notes": list(notes), "offline": 0, "missing": 0,
            "planned": plan.totals(),
            "rows": [], "blocked": [(it.kind, it.path, reason) for it, reason in plan.blocked],
            "result": None, "freed": 0, "trashes": [], "status": None,
            "fatal": None, "log_error": None, "stopped": False}


def plan_rows(m, plan):
    rows = []
    for step, it in enumerate(plan.items, 1):
        files = plan.files_of(it)
        row = {"step": step, "kind": it.kind, "path": it.path, "why": it.why,
               "into": m.paths[it.into] if it.into is not None else None,
               "status": "would", "note": "", "done_files": 0, "done_bytes": 0}
        seen, size = set(), 0
        if it.kind == "merge":
            there = {m.files[f][4] for f in m.files_in(it.into)}
            row["moving"] = sum(1 for f in files if m.files[f][4] not in there)
            files = [f for f in files if m.files[f][4] in there]
        for f in files:
            key = m.inode_key(f)
            if key not in seen:
                seen.add(key)
                size += m.files[f][2]
        row["files"] = len(plan.files_of(it))
        row["bytes"] = size
        rows.append(row)
    return rows


def add_results(info, remover):
    result = dict.fromkeys(STATUSES, 0)
    for d in remover.results if remover is not None else ():
        row = info["rows"][d.step - 1]
        row.update(status=d.status, note=d.note, done_files=d.files, done_bytes=d.size)
    for row in info["rows"]:
        if row["status"] == "would":
            row["status"] = "not-done"
        result[row["status"]] += 1
    info["result"] = result
    if remover is not None:
        info["freed"] = remover.freed_bytes
        info["fatal"] = remover.fatal
        info["stopped"] = remover.stopped
        info["trashes"] = sorted({t.path for t in remover.trashes.values()
                                  if isinstance(t, Trash)})


def totals(info):
    res = info["result"] or {}
    t = {"items": len(info["rows"]), "blocked": len(info["blocked"]),
         "files": _done_files(info), "bytes": _done_bytes(info), "freed": info["freed"]}
    if info["dry_run"]:
        size, files, folders = info["planned"]
        t.update(files=files, bytes=size)
    t.update({k.replace("-", "_"): v for k, v in res.items()})
    return t


def _done_files(info):
    return sum(r["done_files"] for r in info["rows"] if r["status"] in DONE + ("partial",))


def _done_bytes(info):
    return sum(r["done_bytes"] for r in info["rows"] if r["status"] in DONE + ("partial",))


# ---------------------------------------------------------------- words

def _verb(info, past):
    """'move ... to the Trash', 'delete ... for good', 'replace ... with
    hard links', in the tense wanted."""
    if info["link"]:
        return ("replaced", "with hard links") if past else ("replace", "with hard links")
    if info["method"] == "permanent":
        return ("deleted", "for good") if past else ("delete", "for good")
    return ("moved", "to the Trash") if past else ("move", "to the Trash")


def _files_text(info):
    """'2 folders and 3 files (40 files in all, 1.2 GB)', or '' when only
    merges are planned."""
    size, files, folders = info["planned"]
    if not files:
        return ""
    single = sum(1 for r in info["rows"] if r["kind"] == "file")
    if folders:
        out = plural(folders, "folder") + (f" and {plural(single, 'file')}" if single else "")
        return out + f" ({plural(files, 'file')} in all, {human(size)})"
    return f"{plural(files, 'file')} ({human(size)})"


def _merges_text(info):
    """'1 folder into the folder that stays', or ''."""
    n = sum(1 for r in info["rows"] if r["kind"] == "merge")
    if not n:
        return ""
    return plural(n, "folder") + (" into the folder that stays" if n == 1
                                  else " into the folders that stay")


def _planned_text(info):
    """What is planned, as a verb phrase: 'move 2 folders and 3 files (40
    files in all, 1.2 GB) to the Trash, and merge 1 folder into the folder
    that stays'. A merge on its own says where the rest of the folder goes."""
    verb, how = _verb(info, False)
    files, merges = _files_text(info), _merges_text(info)
    if not merges:
        return f"{verb} {files} {how}"
    if files:
        return f"{verb} {files} {how}, and merge {merges}"
    rest = ("then goes to the Trash" if info["method"] == "trash"
            else "is then deleted for good")
    many = not merges.startswith("1 ")
    return f"merge {merges} (what is left of {'them' if many else 'it'} {rest})"


def question(info):
    p = _planned_text(info)
    q = f"{p[0].upper()}{p[1:]}?"
    if info["method"] == "permanent":
        q += " This can't be undone."
    return q


def headline(info):
    verb, how = _verb(info, info["status"] is not None and not info["dry_run"])
    blocked = len(info["blocked"])
    if info["dry_run"]:
        head = f"would {_planned_text(info)}"
        return head + (f" · {blocked:,} blocked" if blocked else "")
    if info.get("log_error"):
        return "failed: the log of what was done could not be written"
    res = info["result"] or {}
    files, size = _done_files(info), _done_bytes(info)
    head = f"{verb} {plural(files, 'file')} ({human(size)}) {how}"
    merged = res.get("merged", 0)
    if merged:
        head += f", merged {plural(merged, 'folder')}"
    if info["stopped"] or info["fatal"]:
        done = sum(res.get(k, 0) for k in DONE + ("gone",))
        why_ = "interrupted" if info["stopped"] else "stopped (drive problem)"
        head = f"{why_} after {done:,} of {plural(len(info['rows']), 'item')}: {head}"
    extra = [f"{res[k]:,} {k.replace('-', ' ')}" for k in ("skipped", "failed", "partial")
             if res.get(k)]
    if res.get("gone"):
        extra.append(f"{res['gone']:,} {_gone_label(info)}")
    return " · ".join([head] + extra)


def _row_line(r):
    kind = r["kind"]
    if kind == "merge":
        n = r.get("moving", 0)
        facts = f"{plural(n, 'file')} only there {'moves' if n == 1 else 'move'} in"
        return (f"  merge   {printable(r['path'])}\n          into {printable(r['into'])}"
                f"  ({facts})")
    facts = (f"{plural(r['files'], 'file')}, {human(r['bytes'])}" if kind == "folder"
             else human(r["bytes"]))
    return (f"  {kind:<7} {printable(r['path'])}\n          {facts} · "
            f"{printable(r['why'])}")


def plan_text(info, cap=PREVIEW):
    rows = info["rows"]
    lines = [f"Would {_planned_text(info)}:"]
    shown = rows if cap is None else rows[:cap]
    lines += [_row_line(r) for r in shown]
    if len(rows) > len(shown):
        lines.append(f"  ... and {len(rows) - len(shown):,} more (`clean remove -n` "
                     f"lists them all)")
    lines += _blocked_lines(info, cap)
    if info["offline"]:
        lines.append(f"Not connected: {plural(info['offline'], 'item')} on a drive that "
                     f"isn't connected; left alone.")
    if info.get("missing"):
        lines.append(f"Not there:     {plural(info['missing'], 'item')} in a folder that is "
                     f"not there any more; left alone.")
    return "\n".join(lines) + "\n\n"


def _blocked_lines(info, cap):
    blocked = info["blocked"]
    if not blocked:
        return []
    lines = [f"Blocked ({len(blocked):,}): picked to go, but they stay:"]
    shown = blocked if cap is None else blocked[:cap]
    for kind, path, reason in shown:
        lines.append(f"  - {printable(path)}{'/' if kind != 'file' else ''}\n      {reason}")
    if len(blocked) > len(shown):
        lines.append(f"  ... and {len(blocked) - len(shown):,} more")
    return lines


def nothing_text(info):
    lines = ["Nothing to remove: the scan's results hold no copies the rules would remove"
             if not info["blocked"] else "Nothing to remove that the rules allow."]
    lines += _blocked_lines(info, PREVIEW)
    return "\n".join(lines) + "\n"


def _gone_label(info):
    """What a re-run calls the items it finds done: gone, or (with --link)
    already hard links."""
    return "already done" if info["link"] else "already gone"


def _mode_line(info):
    if info["dry_run"]:
        return "DRY RUN (nothing removed)"
    if info["link"]:
        return ("HARD LINKS: copies replaced by links to the copy that stays"
                + ("; the copies themselves are in the Trash" if info["method"] == "trash"
                   else ""))
    if info["method"] == "permanent":
        return "PERMANENT: deleted for good"
    return "TRASH: `clean undo` puts everything back"


def summary(info, run=None, terminal=False, quiet=False):
    rows, res = info["rows"], info["result"] or {}
    lines = []
    if not terminal and rows:
        if info["dry_run"]:
            lines.append(plan_text(info, cap=ITEMS).rstrip("\n"))
            lines.append("")
        else:
            verb, how = _verb(info, True)
            done = [r for r in rows if r["status"] in DONE]
            if done:
                lines.append(f"Done ({len(done):,}):")
                for r in done[:ITEMS]:
                    lines.append(_row_line(r) + ("\n          " + printable(r["note"])
                                                 if r["kind"] == "merge" else ""))
                if len(done) > ITEMS:
                    lines.append(f"  ... and {len(done) - ITEMS:,} more (all in data.json.gz)")
                lines.append("")
    cap = TERMINAL_ISSUES if terminal else None
    if not info["dry_run"]:
        for status, title in ISSUE_TITLES:
            items = [r for r in rows if r["status"] == status]
            if items:
                lines.append(f"{title} ({len(items):,}):")
                for r in (items if cap is None else items[:cap]):
                    lines.append(f"  - {printable(r['path'])}\n      {printable(r['note'])}")
                if cap is not None and len(items) > cap:
                    lines.append(f"  ... and {len(items) - cap:,} more (all in summary.txt)")
                lines.append("")
        if not terminal:
            lines += _blocked_lines(info, None)
            if info["blocked"]:
                lines.append("")
    lines.append(RULE)
    lines.append(f"Mode:            {_mode_line(info)}")
    lines.append(f"Acting on:       run {info['source']} (the scan of "
                 f"{when(info['source_ts'])})")
    for i, root in enumerate(info["roots"]):
        off = ("  (not there any more: left alone)" if root in info.get("missing_roots", ())
               else "  (not connected: left alone)" if root in info["unavailable"] else "")
        lines.append(f"{'Path:' if i == 0 else '':<17}{printable(root)}{off}")
    if info.get("log_error"):
        lines.append(f"FAILED:          {info['log_error']}")
    if info["dry_run"]:
        if _files_text(info) or not _merges_text(info):
            lines.append(f"Would remove:    {_files_text(info) or 'nothing'}")
        if _merges_text(info):
            lines.append(f"Would merge:     {_merges_text(info)}")
    else:
        counts = [f"{res[k]:,} {label}" for k, label in (
            ("removed", "removed"), ("linked", "linked"), ("merged", "merged"),
            ("gone", _gone_label(info)), ("skipped", "skipped"), ("failed", "failed"),
            ("partial", "partly done"), ("not-done", "not done")) if res.get(k)]
        lines.append(f"Items:           {len(rows):,}" + (": " + ", ".join(counts)
                                                          if counts else ""))
        lines.append(f"Files:           {plural(_done_files(info), 'file')} "
                     f"({human(_done_bytes(info))})")
    if info["blocked"]:
        lines.append(f"Blocked:         {len(info['blocked']):,} (they stay; the reasons "
                     f"are in summary.txt)" if terminal else
                     f"Blocked:         {len(info['blocked']):,} (they stay)")
    if info["offline"]:
        lines.append(f"Not connected:   {plural(info['offline'], 'item')} left alone")
    if info.get("missing"):
        lines.append(f"Not there:       {plural(info['missing'], 'item')} left alone")
    if not info["dry_run"] and info["status"] is not None:
        freed = info["freed"]
        if info["method"] == "trash" and (freed or _done_files(info)):
            lines.append(f"Space freed:     {human(freed)} once the Trash is emptied")
            for t in info["trashes"]:
                lines.append(f"                 (in {printable(t)})")
        else:
            lines.append(f"Space freed:     {human(freed)}")
    if run is not None:
        lines.append(f"Run:             {run.name}")
        lines.append("Report:          " + ("NOT SAVED (see the warnings)"
                                            if "report.html" in run.unsaved
                                            else printable(run.file("report.html"))))
    if info["stopped"]:
        lines += ["", "INTERRUPTED: the items not reached are untouched. Run the same "
                      "command again to finish: what is already gone is skipped."]
    if info["fatal"]:
        lines += ["", f"STOPPED: {info['fatal']}", "Once the drive is back, run the same "
                  "command again to finish."]
    tips = _next_steps(info, run)
    if tips:
        lines.append("")
        lines += tips
    lines.append(RULE)
    return "\n".join(lines) + "\n"


def _go_ahead(info, run):
    if info.get("go_ahead"):
        return info["go_ahead"]
    argv = [a for a in (run.meta.get("argv") if run is not None else []) or []
            if isinstance(a, str)]
    return command_line(argv, drop=("-n", "--dry-run")) if argv else "clean remove"


def _next_steps(info, run):
    name = run.name if run is not None else None
    if info["dry_run"]:
        if not info["rows"]:
            return []
        return [f"To go ahead: {_go_ahead(info, run)}"]
    if info.get("log_error") or not _done_files(info) and not info["result"].get("merged"):
        return []
    out = []
    if info["method"] == "trash":
        out.append(f"Undo:            clean undo{' ' + name if name else ''}")
        out.append(f"Free the space:  clean trash empty{' --run ' + name if name else ''}")
    return out


# ---------------------------------------------------------------- saved data

def data_of(info):
    rows = [[r["step"], r["kind"], r["path"], r["into"], r["why"], r["files"], r["bytes"],
             r["status"], r["note"], r["done_files"], r["done_bytes"]] for r in info["rows"]]
    return {"format": data.FORMAT, "kind": "remove", "source": info["source"],
            "roots": info["roots"], "unavailable": info["unavailable"],
            "missing_roots": info.get("missing_roots", []),
            "left_alone": {"offline": info["offline"], "missing": info.get("missing", 0)},
            "method": info["method"], "link": info["link"], "merge": info["merge"],
            "conflicts": info["conflicts"], "dry_run": info["dry_run"],
            "columns": ["step", "kind", "path", "into", "why", "files", "bytes", "status",
                        "note", "done_files", "done_bytes"],
            "items": rows, "blocked": [list(b) for b in info["blocked"]],
            "result": info["result"], "freed": info["freed"], "trashes": info["trashes"]}


# ---------------------------------------------------------------- report.html

def _items_table(title, rows, columns, total_note=""):
    if not rows:
        return ""
    esc = _html.esc
    shown = rows[:MAX_ROWS]
    out = [f'<h2>{esc(title)} <span class="count">{len(rows):,}</span></h2>',
           '<div class="panel"><table><thead><tr>'
           + "".join(f"<th>{esc(c)}</th>" for c in columns) + "</tr></thead><tbody>"]
    for cells in shown:
        out.append("<tr>" + "".join(
            f'<td class="{cls}">{esc(text)}</td>' for cls, text in cells) + "</tr>")
    out.append("</tbody></table>")
    if len(rows) > len(shown):
        out.append(f'<ul class="rows"><li class="more">... and {len(rows) - len(shown):,} '
                   f'more{esc(total_note)}</li></ul>')
    out.append("</div>\n")
    return "".join(out)


def _what(r, into=False):
    if r["kind"] == "merge":
        if into:
            return f"merge into {r['into']}"
        n = r.get("moving", 0)
        return f"merge · {plural(n, 'file')} only here {'moves' if n == 1 else 'move'} in"
    if r["kind"] == "folder":
        return f"folder · {plural(r['files'], 'file')} · {human(r['bytes'])}"
    return f"file · {human(r['bytes'])}"


def page(run, info):
    esc = _html.esc
    rows, res = info["rows"], info["result"] or {}
    if info["dry_run"]:
        detail = ("Nothing was removed. To go ahead, run "
                  f"<code>{esc(_go_ahead(info, run))}</code>")
        tag = "dry run"
    elif info["method"] == "permanent":
        detail = "Deleted for good: there is no undo."
        tag = "permanent"
    else:
        detail = (f"Everything removed is in the Trash of its drive. To put it back: "
                  f"<code>clean undo {esc(run.name)}</code>; to free the space: "
                  f"<code>clean trash empty --run {esc(run.name)}</code>")
        tag = "trash"
    if info["link"]:
        tag += " · hard links"
    body = [_html._header(run, "Remove duplicates", tag),
            _html._callout(run, run.headline, detail),
            f'<p class="meta">Acting on run <span class="path">{esc(info["source"])}</span>, '
            f'the scan of {esc(when(info["source_ts"]))}.</p>\n']
    if info["dry_run"]:
        size, files, folders = info["planned"]
        cards = [(*_html._count(len(rows), "item", "items"), False),
                 (*_html._count(files, "file", "files"), False),
                 (human(size), "would be removed", False)]
    else:
        cards = [(*_html._count(sum(res.get(k, 0) for k in DONE), "item done", "items done"),
                  False),
                 (*_html._count(_done_files(info), "file", "files"), False),
                 (human(info["freed"]), "freed" + (" once the Trash is emptied"
                                                   if info["method"] == "trash" else ""),
                  False)]
        for key, label in (("gone", _gone_label(info)), ("skipped", "skipped"),
                           ("failed", "failed"), ("partial", "partly done"),
                           ("not-done", "not done")):
            if res.get(key):
                cards.append((f"{res[key]:,}", label, key in ("failed", "partial")))
    if info["blocked"]:
        cards.append((f"{len(info['blocked']):,}", "blocked (they stay)", False))
    body.append(_html._stats(cards))
    for note in info["notes"]:
        body.append(f'<p class="muted">{esc(note)}</p>\n')

    def cells(r, why_key):
        return [("path", r["path"]), ("what", _what(r)), ("why", r[why_key] or r["why"])]

    if info["dry_run"]:
        body.append(_items_table("Would be removed", [cells(r, "why") for r in rows],
                                 ("Where", "What", "Why"), " (all in data.json.gz)"))
    else:
        done = [r for r in rows if r["status"] in DONE]
        body.append(_items_table("Done", [cells(r, "note") for r in done],
                                 ("Where", "What", "Why"), " (all in data.json.gz)"))
        for status, title in ISSUE_TITLES:
            body.append(_items_table(title, [[("path", r["path"]), ("why", r["note"])]
                                             for r in rows if r["status"] == status],
                                     ("Where", "What happened")))
        gone = [[("path", r["path"]), ("why", r["note"])]
                for r in rows if r["status"] == "gone"]
        body.append(_items_table(_gone_label(info).capitalize(), gone, ("Where", "")))
        not_done = [[("path", r["path"]), ("what", _what(r, into=True))]
                    for r in rows if r["status"] == "not-done"]
        if not_done:
            body.append('<p class="muted">The run stopped before it reached the items below; '
                        'they are untouched. Running the same command again finishes the '
                        'job.</p>\n')
        body.append(_items_table("Not done", not_done, ("Where", "What")))
    body.append(_items_table("Blocked: picked to go, but they stay",
                             [[("path", p), ("why", reason)] for _, p, reason in info["blocked"]],
                             ("Where", "Why")))
    body.append(_html._footer(run, ("summary.txt", "actions.tsv", "data.json.gz",
                                    "run.json")))
    return _html.page(f"Remove · {run.name}", "".join(body))
