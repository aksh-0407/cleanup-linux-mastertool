"""
What `clean undo` shows and saves: what would come back before it asks, then
summary.txt, report.html and data.json.gz of the undo run. One row per step
of the removal being undone:

    {"step", "kind", "path", "status": restored | already | conflict |
     missing | skipped | failed | not-done, "note"}
"""

from cleanup.actions.undo import STATUSES
from cleanup.fsutil import printable
from cleanup.report import data
from cleanup.report import html as _html
from cleanup.report.text import RULE, TERMINAL_ISSUES, plural, when

PREVIEW = 25
TITLES = (
    ("conflict", "Not put back: something new is in the way (nothing was overwritten)"),
    ("missing", "Can't come back: not in the Trash any more"),
    ("skipped", "Left as they are: changed since"),
    ("failed", "Failed"),
    ("offline", "Not now: their drive is not connected"),
    ("not-done", "Not done: the undo stopped first"),
)


def new_info(source, kinds, notes, dry_run):
    return {"source": source.name, "source_ts": source.started_ts,
            "source_label": source.label, "roots": source.roots, "kinds": kinds,
            "notes": list(notes), "dry_run": dry_run, "rows": [], "result": None,
            "status": None, "fatal": None, "stopped": False, "log_error": None}


def add_outcomes(info, outcomes):
    """Fill the rows from Undoer results (steps newest first, as undone)."""
    kinds = info["kinds"]
    rows = []
    for o in outcomes:
        kind, path = kinds.get(o.step, (None, None))
        rows.append({"step": o.step, "kind": kind or "", "path": path or o.path,
                     "status": o.status, "note": o.note, "partly": o.partly})
    info["rows"] = rows
    info["result"] = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}
    info["partly"] = sum(1 for r in rows if r["partly"])


def totals(info):
    t = {"items": len(info["rows"])}
    t.update({k.replace("-", "_"): v for k, v in (info["result"] or {}).items()})
    return t


def complete(info):
    """Everything is back, or can never come back: the removal's log has
    nothing left to undo."""
    res = info["result"] or {}
    return not any(res.get(k) for k in ("conflict", "skipped", "failed", "offline",
                                        "not-done"))


def _what(r):
    return {"folder": "folder", "file": "file", "merge": "merge"}.get(r["kind"], "item")


def headline(info):
    res = info["result"] or {}
    back = res.get("restored", 0)
    if info["dry_run"]:
        head = f"would put back {plural(back, 'item')} from run {info['source']}"
    elif info.get("log_error"):
        return "failed: the log of what was done could not be written"
    else:
        head = f"put back {plural(back, 'item')} from run {info['source']}"
    if info["stopped"] or info["fatal"]:
        head = ("interrupted: " if info["stopped"] else "stopped (drive problem): ") + head
    extra = [f"{res[k]:,} {label}" for k, label in (
        ("already", "already back"), ("conflict", "in the way"), ("missing", "not in the Trash"),
        ("skipped", "changed since"), ("failed", "failed"), ("offline", "not connected"),
        ("not-done", "not done")) if res.get(k)]
    return " · ".join([head] + extra)


def _line(r):
    out = f"  {_what(r):<7} {printable(r['path'])}"
    if r["note"]:
        out += f"\n          {printable(r['note'])}"
    return out


def preview(info, cap=PREVIEW):
    rows = info["rows"]
    back = [r for r in rows if r["status"] == "restored"]
    lines = []
    if back:
        lines.append(f"Would put back ({len(back):,}):")
        shown = back if cap is None else back[:cap]
        lines += [_line(r) for r in shown]
        if len(back) > len(shown):
            lines.append(f"  ... and {len(back) - len(shown):,} more (`clean undo -n` lists "
                         f"them all)")
    already = sum(1 for r in rows if r["status"] == "already")
    if already:
        lines.append(f"Already back: {plural(already, 'item')}.")
    for status, title in TITLES:
        items = [r for r in rows if r["status"] == status]
        if items:
            lines.append(f"{title} ({len(items):,}):")
            shown = items if cap is None else items[:cap]
            lines += [_line(r) for r in shown]
            if len(items) > len(shown):
                lines.append(f"  ... and {len(items) - len(shown):,} more")
    return "\n".join(lines) + "\n\n"


def to_do(info):
    """Is there anything to put back (some of an item counts)?"""
    return bool((info["result"] or {}).get("restored") or info.get("partly"))


def question(info):
    n, m = (info["result"] or {}).get("restored", 0), info.get("partly", 0)
    what = " and ".join(x for x in (
        plural(n, "item") if n else "",
        (f"part of {m:,} more" if n else f"part of {plural(m, 'item')}") if m else "") if x)
    return f"Put back {what} removed by run {info['source']}?"


def summary(info, run=None, terminal=False):
    rows, res = info["rows"], info["result"] or {}
    lines = []
    if not terminal:
        back = [r for r in rows if r["status"] == "restored"]
        if back:
            lines.append(("Would put back" if info["dry_run"] else "Put back")
                         + f" ({len(back):,}):")
            lines += [_line(r) for r in back]
            lines.append("")
    cap = TERMINAL_ISSUES if terminal else None
    for status, title in TITLES:
        items = [r for r in rows if r["status"] == status]
        if items:
            lines.append(f"{title} ({len(items):,}):")
            lines += [_line(r) for r in (items if cap is None else items[:cap])]
            if cap is not None and len(items) > cap:
                lines.append(f"  ... and {len(items) - cap:,} more (all in summary.txt)")
            lines.append("")
    lines.append(RULE)
    lines.append("Mode:            " + ("DRY RUN (nothing moved)" if info["dry_run"]
                                        else "UNDO: back from the Trash"))
    lines.append(f"Undoing:         run {info['source']} ({info['source_label']}, "
                 f"{when(info['source_ts'])})")
    if info.get("log_error"):
        lines.append(f"FAILED:          {info['log_error']}")
    counts = [f"{res[k]:,} {label}" for k, label in (
        ("restored", "would come back" if info["dry_run"] else "put back"),
        ("already", "already back"), ("conflict", "in the way"),
        ("missing", "not in the Trash"), ("skipped", "changed since"),
        ("failed", "failed"), ("offline", "not connected"), ("not-done", "not done"))
        if res.get(k)]
    lines.append(f"Items:           {len(rows):,}" + (": " + ", ".join(counts) if counts else ""))
    for note in info["notes"]:
        lines.append(f"Note:            {note}")
    if run is not None:
        lines.append(f"Run:             {run.name}")
        lines.append("Report:          " + ("NOT SAVED (see the warnings)"
                                            if "report.html" in run.unsaved
                                            else printable(run.file("report.html"))))
    if info["stopped"]:
        lines += ["", "INTERRUPTED: run the same command again to finish; what is back "
                      "already is skipped."]
    if info["fatal"]:
        lines += ["", f"STOPPED: {info['fatal']}", "Once the drive is back, run the same "
                  "command again to finish."]
    if info["dry_run"] and to_do(info):
        lines += ["", f"To go ahead:     clean undo {info['source']}"]
    if res.get("conflict") and not info["dry_run"]:
        lines += ["", "What is in the way was left alone. Move it aside and run "
                      f"`clean undo {info['source']}` again, or look in the Trash: "
                      "clean trash list"]
    lines.append(RULE)
    return "\n".join(lines) + "\n"


def data_of(info):
    return {"format": data.FORMAT, "kind": "undo", "source": info["source"],
            "dry_run": info["dry_run"], "columns": ["step", "kind", "path", "status", "note"],
            "items": [[r["step"], r["kind"], r["path"], r["status"], r["note"]]
                      for r in info["rows"]],
            "result": info["result"]}


def page(run, info):
    esc = _html.esc
    rows, res = info["rows"], info["result"] or {}
    detail = (f"Undoing run <code>{esc(info['source'])}</code> "
              f"({esc(info['source_label'])}, {esc(when(info['source_ts']))}).")
    if info["dry_run"]:
        detail += f" Nothing was moved. To go ahead: <code>clean undo {esc(info['source'])}</code>"
    body = [_html._header(run, "Undo a removal", "dry run" if info["dry_run"] else "undo"),
            _html._callout(run, run.headline, detail)]
    cards = [(f"{res.get('restored', 0):,}", "would come back" if info["dry_run"]
              else "put back", False)]
    for key, label in (("already", "already back"), ("conflict", "in the way"),
                       ("missing", "not in the Trash"), ("skipped", "changed since"),
                       ("failed", "failed"), ("offline", "not connected"),
                       ("not-done", "not done")):
        if res.get(key):
            cards.append((f"{res[key]:,}", label, key in ("failed", "conflict")))
    body.append(_html._stats(cards))
    for note in info["notes"]:
        body.append(f'<p class="muted">{esc(note)}</p>\n')
    groups = [("restored", "Would come back" if info["dry_run"] else "Put back")]
    groups += list(TITLES) + [("already", "Already back")]
    for status, title in groups:
        body.append(_html._issues_table(title, [(r["path"], r["note"] or _what(r))
                                                for r in rows if r["status"] == status]))
    body.append(_html._footer(run, ("summary.txt", "actions.tsv", "data.json.gz",
                                    "run.json")))
    return _html.page(f"Undo · {run.name}", "".join(body))
