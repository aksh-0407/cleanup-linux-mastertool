"""
Text output: what a run prints in the terminal and saves as summary.txt
(which `clean show` prints again later). The terminal version shortens long
lists; summary.txt keeps them whole.
"""

import os
import shlex
from datetime import datetime

from cleanup.fsutil import human, printable, why

RULE = "=" * 60
TERMINAL_ISSUES = 50        # problems listed in the terminal; summary.txt has all
TERMINAL_GROUPS = 20        # duplicate groups listed in the terminal
FILE_GROUPS = 200           # ... and in summary.txt (files.tsv has every one)
TERMINAL_FAMILIES = 10      # folder families / covered folders in the terminal
FAMILIES = 500              # ... and in summary.txt (folders.tsv has every one)
DIFF_ITEMS = 10             # paths per difference list in summary.txt


# ---------------------------------------------------------------- helpers

def outermost(paths):
    """[(folder, [folders inside it])] for a set of folders: the ones whose
    parent isn't in the set, each with the rest of the set inside it. An empty
    folder's subfolders are all empty too, so one line per outermost folder
    says everything."""
    order = sorted(paths)       # a parent sorts before anything inside it
    known = set(order)
    top = {}                    # folder -> its outermost folder
    groups = {}
    for p in order:
        parent = os.path.dirname(p)
        outer = top[parent] if parent in known else p
        top[p] = outer
        if outer == p:
            groups[p] = []
        else:
            groups[outer].append(p)
    return list(groups.items())


def plural(n, one, many=None):
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def duration(seconds):
    s = int(round(seconds or 0))
    if seconds is not None and seconds < 10:
        return f"{seconds:.1f} s"
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s"
    return f"{s // 3600} h {s % 3600 // 60} min"


def when(ts):
    """'Tue 7 Oct 2026, 14:30' (the same in every locale)."""
    try:
        d = datetime.fromtimestamp(ts)
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"
    return (f"{('Mon','Tue','Wed','Thu','Fri','Sat','Sun')[d.weekday()]} {d.day} "
            f"{('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')[d.month - 1]} "
            f"{d.year}, {d:%H:%M}")


def command_line(argv, drop=(), add=()):
    """`clean ...` as a shell command, e.g. to repeat a dry run with --delete."""
    args = [a for a in argv if a not in drop] + list(add)
    return printable("clean " + " ".join(shlex.quote(a) for a in args))


def _issues(lines, issues, cap):
    shown = issues if cap is None else issues[:cap]
    for path, msg in shown:
        lines.append(f"  - {printable(path)}\n      {msg}")
    if len(issues) > len(shown):
        lines.append(f"  ... and {len(issues) - len(shown):,} more (all in summary.txt)")


def _saved(lines, run):
    if run is None:
        return
    lines.append(f"Run:             {run.name}")
    lines.append("Report:          " + ("NOT SAVED (see the warnings)" if "report.html" in run.unsaved
                                        else printable(run.file("report.html"))))
    others = [n for n in run.unsaved if n != "report.html"]
    if others:
        lines.append(f"Not saved:       {', '.join(others)}  (see the warnings)")


# ---------------------------------------------------------------- empty

def empty_list(info):
    """The folders a run deleted, or would delete, outermost first."""
    delete = info["delete"]
    done = [p for status, _, p, _ in info["rows"]
            if status == ("deleted" if delete else "would-delete")]
    if not done:
        return []
    groups = outermost(done)
    head = "Deleted" if delete else "Empty folders that WOULD be deleted"
    where = f", in {plural(len(groups), 'place')}" if len(groups) > 1 else ""
    lines = [f"{head} ({len(done):,}{where}):"]
    for outer, inner in groups:
        lines.append(f"  {printable(outer)}" + (f"  (+{len(inner):,} inside)" if inner else ""))
    return lines + [""]


def empty_summary(info, run=None, terminal=False):
    """The summary block; with terminal=False also the full list above it."""
    delete, res, errors = info["delete"], info["result"], info["errors"]
    lines = []
    if not terminal or (not delete and not info.get("quiet")):
        lines += empty_list(info)
    lines.append(RULE)
    lines.append(f"Mode:            {'DELETE' if delete else 'DRY RUN (nothing deleted)'}")
    for i, root in enumerate(info["roots"]):
        lines.append(f"{'Path:' if i == 0 else '':<17}{printable(root)}")
    lines.append(f"Folders scanned: {info['scanned']:,}  (jobs={info['jobs']})")
    if info["stage"] == "scan":
        lines.append("Stopped while scanning: nothing was deleted.")
    else:
        lines.append(f"Empty folders {'deleted' if delete else 'that WOULD be deleted'}: "
                     f"{res['removed']:,}")
    if res["skipped"]:
        lines.append(f"Kept:            {res['skipped']:,}  (a folder inside could not be removed)")
    if res["not_done"]:
        lines.append(f"Not done:        {res['not_done']:,}  (the run stopped first)")
    lines.append(f"Errors:          {len(errors):,}")
    _saved(lines, run)
    if res["interrupted"] and info["stage"] == "delete":
        lines += ["", "INTERRUPTED: the folders deleted so far are listed in the report."]
    if res["fatal"]:
        path, e = res["fatal"]
        lines += ["", f"STOPPED at {printable(path)}:\n  {why(e)}: is the drive still "
                      f"connected and writable? Folders deleted so far are in the report."]
    if info.get("drive_gone"):
        lines += ["", f"STOPPED: {info['drive_gone']}"]
    stopped = res["interrupted"] or res["fatal"] or info["stage"] == "scan"
    if info["root_left_empty"] and not stopped:
        lines.append("")
        for root in info["root_left_empty"]:
            lines.append(f"Note: {printable(root)} itself {'is now' if delete else 'would be left'} "
                         f"empty; folders you name are never removed.")
    if not delete and res["removed"] and not stopped:
        lines.append("")
        if terminal and info.get("quiet"):
            lines.append("Re-run without -q to see the list, or with --delete to remove them.")
        else:
            lines.append("Review the list above, then re-run with --delete to remove them.")
    if errors:
        lines += ["", "Per-folder issues (nothing was force-removed):"]
        _issues(lines, errors, TERMINAL_ISSUES if terminal else None)
    lines.append(RULE)
    return "\n".join(lines) + "\n"


def empty_headline(info):
    delete, res = info["delete"], info["result"]
    if info["stage"] == "scan":
        return ("interrupted while scanning; nothing was deleted" if res["interrupted"]
                else "stopped while scanning (drive problem); nothing was deleted")
    total = res["removed"] + res["skipped"] + res["failed"] + res["gone"] + res["not_done"]
    if res["interrupted"] or res["fatal"]:
        why_ = "interrupted" if res["interrupted"] else "stopped (drive problem)"
        return f"{why_}: {res['removed']:,} of {plural(total, 'empty folder')} deleted"
    if delete:
        head = f"{plural(res['removed'], 'empty folder')} deleted"
    elif res["removed"]:
        head = f"{plural(res['removed'], 'empty folder')} would be deleted"
    else:
        head = "no empty folders"
    extra = []
    if res["skipped"]:
        extra.append(f"{res['skipped']:,} kept")
    if info["errors"]:
        extra.append(plural(len(info["errors"]), "error"))
    return " · ".join([head] + extra)


# ---------------------------------------------------------------- dupes

ROLE_TEXT = {"identical": "identical", "renamed": "renamed", "partial": "partial",
             "diverged": "diverged", "keep": "keep", "covered": "covered"}
KIND_TEXT = {"identical": "identical copies", "renamed": "the same files, renamed or moved",
             "partial": "partial copies", "diverged": "diverged copies (keep both)",
             "mixed": "copies"}
DIFF_TEXT = (("only_here", "only here"), ("only_there", "only in the kept folder"),
             ("changed", "changed"), ("moved", "moved or renamed"))


def dupes_headline(info):
    if info.get("stopped"):
        return info["stopped"]
    t = info.get("totals") or {}
    if not t.get("groups") and not t.get("families"):
        head = "no duplicates"
    else:
        head = (f"{plural(t['groups'], 'duplicate group')} · "
                f"{plural(t['redundant'], 'redundant copy', 'redundant copies')} · "
                f"{human(t['reclaimable'])} reclaimable")
        if t.get("families"):
            head += " · " + plural(t["families"], "folder family", "folder families")
        if t.get("covered"):
            head += " · " + plural(t["covered"], "covered folder")
    if t.get("lookalikes"):
        head += " · " + plural(t["lookalikes"], "look-alike set")
    if info["errors"]:
        head += " · " + plural(len(info["errors"]), "error")
    return head


def member_note(m):
    """What sets a family member apart, in a few words."""
    d = m.get("diff") or {}
    counts = [f"{d[k]['count']:,} {label}" for k, label in DIFF_TEXT
              if d.get(k, {}).get("count")]
    if m["role"] == "identical":
        return ""
    if m["role"] == "renamed":
        return ", ".join(counts) or "the same files, organised differently"
    if m["role"] == "partial":
        whole = ("its only file is" if m["files"] == 1
                 else f"all {plural(m['files'], 'file')} are")
        return (f"{whole} also in the kept folder"
                + ("; " + ", ".join(counts) if counts else ""))
    shared = f"{m['shared']:.0%} shared" if m.get("shared") is not None else "similar"
    return shared + ("; " + ", ".join(counts) if counts else "") + " — keep both"


def _diff_lines(m, indent, cap):
    d = m.get("diff") or {}
    out = []
    for key, label in DIFF_TEXT:
        part = d.get(key)
        if not part or not part["count"]:
            continue
        out.append(f"{indent}{label}:")
        shown = 0
        for *names, n in part.get("folders", [])[:cap]:
            out.append(f"{indent}  " + "  ->  ".join(printable(x or ".") + "/" for x in names)
                       + f"  ({plural(n, 'file')})")
            shown += n
        for item in part["items"][:max(0, cap - len(part.get("folders", [])))]:
            if key == "moved":
                out.append(f"{indent}  {printable(item[0])}  ->  {printable(item[1])}")
            else:
                out.append(f"{indent}  {printable(item)}")
            shown += 1
        if part["count"] > shown:
            out.append(f"{indent}  ... and {part['count'] - shown:,} more")
    return out


def dupes_families(info, cap, diff_cap=0):
    fams = info["families"]
    if not fams:
        return []
    lines = [f"FOLDER FAMILIES ({len(fams):,}): folders that are copies of each other; "
             f"keep the first of each", ""]
    for f in fams[:cap]:
        size = f"{plural(f['files'], 'file')}, {human(f['bytes'])}"
        lines.append(f"{f['id']}  {KIND_TEXT.get(f['kind'], f['kind'])} — "
                     f"{len(f['members']) + 1} folders; the kept one has {size}"
                     + (f"; {human(f['freed'])} reclaimable" if f["freed"] else ""))
        lines.append(f"    {'keep':<10} {printable(f['anchor'])}")
        for m in f["members"]:
            lines.append(f"    {ROLE_TEXT[m['role']]:<10} {printable(m['path'])}")
            note = member_note(m)
            if note:
                lines.append(f"    {'':<10} {note}")
            if diff_cap:
                lines += _diff_lines(m, " " * 17, diff_cap)
        lines.append("")
    if len(fams) > cap:
        lines += [f"... and {len(fams) - cap:,} more folder families (the report and "
                  f"folders.tsv have every one).", ""]
    return lines


def dupes_covered(info, cap):
    cov = info["covered"]
    if not cov:
        return []
    lines = [f"COVERED FOLDERS ({len(cov):,}): every file in them also exists elsewhere", ""]
    for c in cov[:cap]:
        lines.append(f"{c['id']}  {printable(c['path'])}")
        lines.append(f"    {plural(c['files'], 'file')}, {human(c['bytes'])}"
                     + (f"; {human(c['freed'])} reclaimable" if c["freed"] else "")
                     + f"; the copies are in {plural(c['where_total'], 'place')}"
                     + (", mostly:" if c["where_total"] > len(c["where"]) else ":"))
        for path, n in c["where"]:
            lines.append(f"      {printable(path)}  ({plural(n, 'file')})")
        lines.append("")
    if len(cov) > cap:
        lines += [f"... and {len(cov) - cap:,} more covered folders (the report and "
                  f"folders.tsv have every one).", ""]
    return lines


def dupes_groups(info, cap):
    groups = info["groups"]
    shown = [g for g in groups if not g["folded"]]
    folded = len(groups) - len(shown)
    lines = []
    if folded and shown:
        lines += [f"FILE GROUPS: {plural(len(shown), 'more group')} of duplicate files "
                  f"({folded:,} others are inside the folder results above):"]
    elif folded:
        lines += [f"FILE GROUPS: all {plural(folded, 'group')} of duplicate files are "
                  f"inside the folder results above.", ""]
    elif shown:
        lines += [f"FILE GROUPS ({len(shown):,}): files with exactly the same bytes"]
    if shown and any(any(g["explained"]) for g in shown[:cap]):
        lines += ["(a tag such as [F3] means that copy is inside folder result F3)"]
    if shown:
        lines.append("")
    for g in shown[:cap]:
        n = len(g["files"])
        lines.append(f"DUPLICATE group {g['id']} — {n} copies, {human(g['size'])} each:")
        for p, why_ in zip(g["files"], g["explained"]):
            lines.append(f"    {printable(p)}" + (f"  [{why_}]" if why_ else ""))
        lines.append("")
    if len(shown) > cap:
        lines += [f"... and {len(shown) - cap:,} more duplicate groups (see the report; "
                  f"files.tsv has every one).", ""]
    return lines


def dupes_lookalikes(info, cap):
    looks = info.get("lookalikes") or []
    if not looks:
        return []
    lines = [f"LOOK-ALIKES ({len(looks):,}): the same photo or song in files whose bytes "
             f"differ; never picked for removal",
             "(letters name the versions; * marks the one that looks best)", ""]
    for g in looks[:cap]:
        lines.append(f"LOOK-ALIKE {g['id']} — {g['kind']}: {printable(g['label'])}, "
                     f"{plural(g['versions'], 'version')}:")
        for f in g["files"]:
            lines.append(f"  {f['letter']:>2}{'*' if f['best'] else ' '} {printable(f['path'])}"
                         f"  ({f['detail']}, {human(f['size'])})")
        lines.append("")
    if len(looks) > cap:
        lines += [f"... and {len(looks) - cap:,} more look-alike sets (see the report).", ""]
    return lines


def dupes_by_type(info):
    cats = (info.get("totals") or {}).get("categories") or {}
    if not cats:
        return []
    lines = [f"{'BY TYPE':<16}{'groups':>10}{'redundant':>11}{'reclaimable':>13}"]
    for cat, (g, c, b) in sorted(cats.items(), key=lambda kv: (-kv[1][2], kv[0])):
        lines.append(f"  {cat:<14}{g:>10,}{c:>11,}{human(b):>13}")
    return lines + [""]


def _skipped_text(skipped):
    from cleanup.scan.tree import SKIP_LABELS
    parts = [plural(skipped[key], one, many) for key, one, many in SKIP_LABELS
             if skipped.get(key)]
    return ", ".join(parts)


def dupes_summary(info, run=None, terminal=False):
    errors = info["errors"]
    lines = []
    if not (terminal and info.get("quiet")) and not info.get("stopped"):
        lines += dupes_families(info, TERMINAL_FAMILIES if terminal else FAMILIES,
                                0 if terminal else DIFF_ITEMS)
        lines += dupes_covered(info, TERMINAL_FAMILIES if terminal else FAMILIES)
        lines += dupes_groups(info, TERMINAL_GROUPS if terminal else FILE_GROUPS)
        lines += dupes_lookalikes(info, TERMINAL_GROUPS if terminal else FILE_GROUPS)
        lines += dupes_by_type(info)
    lines.append(RULE)
    lines.append("Mode:            REPORT ONLY (nothing deleted)")
    for i, root in enumerate(info["roots"]):
        lines.append(f"{'Path:' if i == 0 else '':<17}{printable(root)}")
    if info.get("stopped"):
        lines.append(f"STOPPED:         {info['stopped']}")
        lines.append("                 (a scan with gaps would miss duplicates, so it "
                     "lists none)")
        _saved(lines, run)
        lines.append(RULE)
        return "\n".join(lines) + "\n"
    if info.get("filters"):
        lines.append(f"Filters:         {printable(' · '.join(info['filters']))}")
    extra = [w for w, on in (("same name", info.get("same_name")),
                             ("same date", info.get("same_date"))) if on]
    if extra or info.get("similar", 80) != 80:
        lines.append("Matching:        same content" + "".join(f" + {w}" for w in extra)
                     + f"; folders sharing {info.get('similar', 80)}% are diverged copies")
    if info.get("max_depth") is not None:
        lines.append(f"Depth:           {info['max_depth']} folder levels below each path")
    lines.append(f"Files scanned:   {info['total_files']:,}  (jobs={info['jobs']})")
    if info["hard_links"]:
        lines.append(f"Hard links:      {info['hard_links']:,} extra names of the same "
                     f"file, not counted")
    skipped = _skipped_text(info.get("skipped") or {})
    if skipped:
        lines.append(f"Skipped:         {skipped}")
    h = info.get("hashing")
    if h:
        line = f"Hashing:         {h['read']:,} files read ({human(h['bytes'])})"
        if h["cache"] == "on":
            line += f", {h['cached']:,} hashes from the cache"
            if h.get("trimmed"):
                line += (f"; {h['trimmed']:,} old entries dropped to keep the cache "
                         f"under cache.max_mb")
        elif h["cache"] == "failed":
            line += "; the hash cache could not be used"
        lines.append(line)
    t = info.get("totals") or {}
    lines.append(f"Folder families: {t.get('families', 0):,}"
                 + (f"  ({human(t['family_freed'])} in redundant copies)"
                    if t.get("family_freed") else ""))
    lines.append(f"Covered folders: {t.get('covered', 0):,}"
                 + (f"  ({human(t['covered_freed'])})" if t.get("covered_freed") else ""))
    lines.append(f"Duplicate groups: {t.get('groups', 0):,}")
    lines.append(f"Redundant copies: {t.get('redundant', 0):,}")
    lines.append(f"Space reclaimable if you kept one of each: {human(t.get('reclaimable', 0))}")
    if t.get("folders_freed"):
        lines.append(f"  ... by removing the redundant folders alone: "
                     f"{human(t['folders_freed'])}")
    ms = info.get("media_stats")
    if info.get("media") and ms:
        lines.append(f"Look-alikes:     {plural(t.get('lookalikes', 0), 'set')} among "
                     f"{plural(ms['tagged'], 'tagged photo and song', 'tagged photos and songs')}"
                     f" ({ms['read']:,} read, {ms['cached']:,} from the cache"
                     + (f", {ms['unreadable']:,} unreadable" if ms["unreadable"] else "")
                     + ")")
    lines.append(f"Errors:          {len(errors):,}")
    _saved(lines, run)
    if errors:
        _issues(lines, errors, TERMINAL_ISSUES if terminal else None)
    lines.append(RULE)
    return "\n".join(lines) + "\n"


def dupes_files_tsv(groups):
    """files.tsv: one row per file; rows with the same group number are
    copies of each other. explained_by names the folder result (F3, C7) the
    file's folder belongs to, if any."""
    out = ["group\tsize_bytes\thash\texplained_by\tpath\n"]
    for g in groups:
        for p, why_ in zip(g["files"], g["explained"]):
            out.append(f"{g['id']}\t{g['size']}\t{g['hash']}\t{why_}\t{printable(p)}\n")
    return "".join(out)


def dupes_folders_tsv(info):
    """folders.tsv: every folder result. A family is its kept folder (role
    keep) and its members; freed_bytes is what removing that folder frees."""
    out = ["result\trole\tfiles\tsize_bytes\tfreed_bytes\tpath\n"]
    for f in info["families"]:
        out.append(f"{f['id']}\tkeep\t{f['files']}\t{f['bytes']}\t0\t{printable(f['anchor'])}\n")
        for m in f["members"]:
            out.append(f"{f['id']}\t{m['role']}\t{m['files']}\t{m['bytes']}\t{m['freed']}\t"
                       f"{printable(m['path'])}\n")
    for c in info["covered"]:
        out.append(f"{c['id']}\tcovered\t{c['files']}\t{c['bytes']}\t{c['freed']}\t"
                   f"{printable(c['path'])}\n")
    return "".join(out)
