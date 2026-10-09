"""
The HTML pages: report.html for each run, and index.html (the run history) in
the data folder.

They are plain static files that open straight from disk: nothing from the
network (the page's Content-Security-Policy forbids it), light and dark to
match the system. Every name shown passes through esc(): file names are text,
never markup, so a folder called <script> or with invalid UTF-8 shows as
exactly that. The duplicates page has one script of its own (filters, sort,
search), allowed by its hash and nothing else; it reads the page, never
writes markup, and the page works without it.
"""

import base64
import hashlib
import html
import os
from datetime import datetime
from urllib.parse import quote

from cleanup import settings as _settings
from cleanup.fsutil import human, is_within, printable
from cleanup.report.text import (DIFF_TEXT, KIND_TEXT, command_line, duration,
                                 member_note, outermost, plural, when)

MAX_OUTER = 2000        # outermost folders listed in an empty report
MAX_INNER = 200         # folders listed inside each "+N inside"
MAX_INNER_TOTAL = 5000  # ... and in the whole page
MAX_ISSUES = 1000       # rows in a problems table
MAX_FAMILIES = 2000     # folder families in a dupes report
MAX_COVERED = 2000      # covered folders
MAX_GROUPS = 2000       # file groups (not inside a folder result)
MAX_FOLDED = 1000       # file groups inside a folder result
MAX_LOOKALIKES = 2000   # look-alike sets
MAX_MEMBERS = 50        # folders shown per family
MAX_COPIES = 50         # paths shown per duplicate group
MAX_DIFF = 100          # paths per difference list


def esc(text):
    return html.escape(printable(str(text)), quote=True)


def esc_lines(text):
    """esc() for text of several lines (summary.txt, a command's output):
    its line breaks stay line breaks."""
    return "\n".join(esc(line) for line in str(text).split("\n"))


def href(path):
    return html.escape(quote(path), quote=True)


FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_FILES = (("Clean Sans", 400, "plex-sans-400.woff2"), ("Clean Sans", 500, "plex-sans-500.woff2"),
              ("Clean Sans", 600, "plex-sans-600.woff2"), ("Clean Mono", 400, "plex-mono-400.woff2"))


def _font_faces():
    """The pages' typeface, IBM Plex Sans and Mono subset to Latin and renamed
    (fonts/OFL.txt), inlined so a report needs nothing but itself. A font
    that can't be read is left out: the fallbacks in --sans/--mono take over."""
    out = []
    for family, weight, name in FONT_FILES:
        try:
            with open(os.path.join(FONTS, name), "rb") as f:
                data = base64.b64encode(f.read()).decode()
        except OSError:
            continue
        out.append(f'@font-face {{ font-family: "{family}"; font-weight: {weight}; '
                   f'font-style: normal; font-display: swap; '
                   f'src: url(data:font/woff2;base64,{data}) format("woff2"); }}\n')
    return "".join(out)


LIGHT = """
  --bg: #fbfaf8; --panel: #ffffff; --soft: #f1efeb; --line: #e6e3de; --line-strong: #d2cec7;
  --text: #1c1a17; --muted: #6a655d; --faint: #96918a;
  --accent: #a84a17; --accent-soft: rgba(168, 74, 23, 0.09); --on-accent: #ffffff;
  --ok: #2d7a4c; --ok-bg: rgba(45, 122, 76, 0.10); --warn: #85650a; --warn-bg: rgba(178, 135, 20, 0.13);
  --bad: #bf3a2e; --bad-bg: rgba(191, 58, 46, 0.09); --info: #45413b; --info-bg: #ece9e4;
  --idle: #6a655d; --idle-bg: #ece9e4; --shadow: 0 8px 28px rgba(28, 26, 23, 0.14);"""
DARK = """
  --bg: #0b0b0a; --panel: #131312; --soft: #1a1918; --line: #242321; --line-strong: #353330;
  --text: #e9e7e3; --muted: #98948c; --faint: #6c6963;
  --accent: #ec8c4f; --accent-soft: rgba(236, 140, 79, 0.13); --on-accent: #0b0b0a;
  --ok: #79c08f; --ok-bg: rgba(121, 192, 143, 0.12); --warn: #d8b45a; --warn-bg: rgba(216, 180, 90, 0.12);
  --bad: #ef6b60; --bad-bg: rgba(239, 107, 96, 0.12); --info: #cbc7c0; --info-bg: #201f1d;
  --idle: #98948c; --idle-bg: #201f1d; --shadow: 0 10px 32px rgba(0, 0, 0, 0.6);"""

# One look for the reports and the dashboard (which serves this before its
# own app.css). Sizes keep to one scale: 11, 12, 13, 14, 16, 20 px. Sections
# are told apart by hairlines and space, not boxes; colour means something
# (the copper accent is what you can act on, the rest is state).
CSS = _font_faces() + """
:root {
  color-scheme: light;@LIGHT@
  --sans: "Clean Sans", "IBM Plex Sans", Ubuntu, Cantarell, "Noto Sans", sans-serif;
  --mono: "Clean Mono", "IBM Plex Mono", "Ubuntu Mono", "DejaVu Sans Mono", monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { color-scheme: dark;@DARK@ }
}
:root[data-theme="dark"] { color-scheme: dark;@DARK@ }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 13px/1.5 var(--sans); font-synthesis: none; -webkit-font-smoothing: antialiased;
}
main { max-width: 1200px; margin: 0 auto; padding: 18px 24px 40px; }
a {
  color: inherit; text-decoration: underline; text-decoration-color: var(--line-strong);
  text-decoration-thickness: 1px; text-underline-offset: 3px;
}
a:hover { text-decoration-color: currentColor; }
h1 { font-size: 20px; line-height: 1.25; font-weight: 600; margin: 2px 0; letter-spacing: -0.01em; text-wrap: balance; }
h2 {
  font-size: 11px; line-height: 1.4; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase;
  color: var(--muted); margin: 26px 0 6px; text-wrap: balance;
}
h2 .count { color: var(--faint); font-weight: 500; margin-left: 4px; font-variant-numeric: tabular-nums; }
p { margin: 4px 0; text-wrap: pretty; }
code, .mono { font-family: var(--mono); font-size: 12px; }
.path { font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; }
.muted { color: var(--muted); }
.crumb { font-size: 12px; color: var(--muted); }
p.crumbs { font: 12px/1.5 var(--mono); color: var(--faint); margin: 6px 0; overflow-wrap: anywhere; }
.meta { color: var(--muted); font-size: 12px; }
.hint { color: var(--muted); border-left: 2px solid var(--line-strong); padding: 1px 0 1px 10px; margin: 10px 0; }
.tag, .badge {
  display: inline-block; font-size: 11px; font-weight: 500; line-height: 17px; padding: 0 5px;
  border-radius: 3px; vertical-align: 2px; white-space: nowrap; background: var(--idle-bg); color: var(--idle);
}
h1 .tag { vertical-align: 4px; }
.s-done { background: var(--ok-bg); color: var(--ok); }
.s-running { background: var(--accent-soft); color: var(--accent); }
.s-interrupted, .s-unfinished { background: var(--warn-bg); color: var(--warn); }
.s-drive-gone, .s-failed { background: var(--bad-bg); color: var(--bad); }
.callout { margin: 12px 0 16px; padding: 0; background: none; color: var(--text); }
.callout .headline { font-size: 16px; font-weight: 600; line-height: 1.35; text-wrap: balance; }
.callout p { color: var(--muted); }
.callout code, p.muted code, .hint code {
  background: var(--soft); color: var(--text); padding: 1px 5px; border-radius: 3px; overflow-wrap: anywhere;
}
.stats {
  display: flex; flex-wrap: wrap; gap: 8px 36px; margin: 14px 0; padding: 9px 0;
  border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
}
.stat .n { font-size: 16px; font-weight: 600; line-height: 1.3; font-variant-numeric: tabular-nums; }
.stat .l { color: var(--muted); font-size: 11px; }
.stat.bad .n { color: var(--bad); }
.panel { margin: 0 0 4px; border-top: 1px solid var(--line); }
.panel-head {
  padding: 7px 0; border-bottom: 1px solid var(--line);
  display: flex; flex-wrap: wrap; gap: 2px 10px; align-items: baseline;
}
.panel-head .path { font-weight: 500; }
ul.rows { list-style: none; margin: 0; padding: 0; }
ul.rows > li { padding: 6px 0; border-top: 1px solid var(--line); }
ul.rows > li:first-child { border-top: 0; }
ul.rows > li.more { color: var(--muted); font-size: 12px; }
details { display: inline; }
details > summary {
  display: inline; cursor: pointer; list-style: none; margin-left: 6px; font-size: 12px;
  color: var(--muted); text-decoration: underline dotted; text-underline-offset: 3px; user-select: none;
}
details > summary::-webkit-details-marker { display: none; }
details > summary:hover, details[open] > summary { color: var(--text); }
details > ul { list-style: none; margin: 4px 0 2px; padding: 0 0 0 12px; border-left: 1px solid var(--line-strong); }
details > ul > li { padding: 1px 0; color: var(--muted); }
details.block { display: block; margin: 6px 0; }
details.block > summary { margin-left: 0; }
.plain-more { margin-left: 6px; font-size: 12px; color: var(--muted); }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; vertical-align: top; padding: 5px 14px 5px 0; border-top: 1px solid var(--line); }
th:last-child, td:last-child { padding-right: 0; }
th { font-size: 11px; color: var(--muted); font-weight: 500; border-top: 0; padding-top: 7px; padding-bottom: 4px; }
td.why { color: var(--muted); width: 40%; overflow-wrap: anywhere; }
td.what { width: 22%; overflow-wrap: anywhere; }
.group .panel-head .size { font-weight: 600; }
.hash { color: var(--faint); font-size: 11px; }
footer {
  margin-top: 36px; padding-top: 12px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px;
}
footer dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px; margin: 0; }
footer dt { font-weight: 500; }
footer dd { margin: 0; overflow-wrap: anywhere; }
footer a { margin-right: 12px; color: var(--text); }
.empty-state { padding: 10px 0; color: var(--muted); }
ol.runs { list-style: none; margin: 0; padding: 0; }
ol.runs > li {
  display: grid; grid-template-columns: 2.2em 1fr auto; gap: 0 10px;
  padding: 7px 0; border-top: 1px solid var(--line);
}
ol.runs > li:first-child { border-top: 0; }
span.num { color: var(--faint); font-variant-numeric: tabular-nums; text-align: right; }
.run-line { display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: baseline; }
.run-what { font-weight: 600; }
.run-what a { text-decoration: none; }
.run-what a:hover { text-decoration: underline; }
.run-when { color: var(--muted); font-size: 12px; }
.run-roots { color: var(--faint); font: 11px/1.5 var(--mono); overflow-wrap: anywhere; }
.run-links { white-space: nowrap; font-size: 12px; }
.run-links a { color: var(--muted); text-decoration: none; }
.run-links a:hover { color: var(--text); text-decoration: underline; }
.run-links a + a { margin-left: 12px; }
nav.tabs { display: flex; flex-wrap: wrap; gap: 0 22px; margin: 18px 0 0; border-bottom: 1px solid var(--line); }
nav.tabs a {
  padding: 6px 0; margin-bottom: -1px; border-bottom: 2px solid transparent;
  color: var(--muted); font-size: 13px; font-weight: 500; text-decoration: none;
}
nav.tabs a .count { color: var(--faint); font-weight: 400; margin-left: 4px; font-variant-numeric: tabular-nums; }
nav.tabs a.on { color: var(--text); border-bottom-color: var(--accent); }
nav.tabs a:hover { color: var(--text); text-decoration: none; }
.toolbar { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; margin: 10px 0 8px; }
.toolbar input, .toolbar select {
  font: inherit; font-size: 12px; color: var(--text); background: var(--bg); min-width: 0;
  border: 1px solid var(--line-strong); border-radius: 4px; padding: 3px 6px; height: 26px;
}
.toolbar input[type=search] { flex: 1 1 220px; }
.toolbar label { display: inline-flex; gap: 5px; align-items: center; font-size: 12px; color: var(--muted); }
.toolbar .shown { margin-left: auto; font-size: 12px; color: var(--muted); white-space: nowrap; }
.card { margin: 0 0 14px; border-top-color: var(--line-strong); }
.card .panel-head { padding: 7px 8px; }
.card ul.rows > li { padding: 7px 8px; }
.card .panel-head .id { font: 12px/1.5 var(--mono); color: var(--faint); }
.card .panel-head .big { font-weight: 600; }
.card .panel-head .save { color: var(--ok); font-weight: 500; }
.member { display: grid; grid-template-columns: 6em 1fr; gap: 1px 10px; }
.member .facts { color: var(--muted); font-size: 12px; }
.member .note { font-size: 12px; color: var(--info); }
.r-keep { background: var(--ok-bg); color: var(--ok); }
.r-identical, .r-renamed, .r-partial, .r-covered { background: var(--info-bg); color: var(--info); }
.r-diverged { background: var(--warn-bg); color: var(--warn); }
.diff { margin-top: 3px; }
.diff details > summary { margin-left: 0; }
.diff h4 { margin: 6px 0 2px; font-size: 11px; color: var(--muted); font-weight: 500; }
.diff ul { list-style: none; margin: 0; padding: 0 0 0 12px; border-left: 1px solid var(--line-strong); }
.diff li { padding: 1px 0; font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; }
.diff .arrow { color: var(--faint); font-family: var(--sans); }
.where { color: var(--muted); font-size: 12px; }
.where .path { color: var(--text); }
.ex { font-size: 11px; font-weight: 500; margin-left: 6px; }
.ver {
  display: inline-block; min-width: 1.6em; margin-right: 6px; font: 600 11px/17px var(--mono);
  text-align: center; border-radius: 3px; background: var(--idle-bg); color: var(--idle); vertical-align: 1px;
}
.ver.best { background: var(--ok-bg); color: var(--ok); }
.facts .best { color: var(--ok); }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
td.n { white-space: nowrap; }
th { overflow-wrap: normal; }
dl.facts { display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px; margin: 0; padding: 6px 0; }
dl.facts dt { color: var(--muted); font-size: 12px; }
dl.facts dd { margin: 0; overflow-wrap: anywhere; }
p.note { color: var(--warn); border-left: 2px solid var(--warn); padding: 1px 0 1px 10px; }
.small { font-size: 12px; }
meter {
  display: block; width: 100%; height: 4px; margin-top: 5px;
  border-radius: 0; background: var(--soft); border: 0;
}
meter::-webkit-meter-bar { background: var(--soft); border: 0; border-radius: 0; height: 4px; }
meter::-webkit-meter-optimum-value { background: var(--accent); border-radius: 0; }
meter::-webkit-meter-suboptimum-value { background: var(--warn); border-radius: 0; }
meter::-webkit-meter-even-less-good-value { background: var(--bad); border-radius: 0; }
meter::-moz-meter-bar { background: var(--accent); border-radius: 0; }
.drive-line { display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: baseline; }
.drive-free { margin-left: auto; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
li.drive { padding-top: 8px !important; padding-bottom: 8px !important; }
.tree { margin: 6px 0 2px; border-bottom: 1px solid var(--line); }
.tree details { display: block; }
.tree details > summary {
  display: block; margin: 0; font-size: inherit; color: inherit; text-decoration: none; user-select: auto;
}
.node-row {
  display: grid; grid-template-columns: minmax(0, 1fr) 140px 70px 92px; gap: 0 12px;
  align-items: center; padding: 2px 4px 2px 0; border-top: 1px solid var(--line);
}
.tree summary:hover > .node-row { background: var(--soft); }
.node-row .nm { font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; min-width: 0; }
.node-row .nm::before { content: ""; display: inline-block; width: 14px; color: var(--faint); }
.tree summary .nm::before { content: "\\25B8"; }
.tree details[open] > summary .nm::before { content: "\\25BE"; }
.node-row .sz, .node-row .fl { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.node-row .fl { color: var(--faint); font-size: 11px; }
.node-row.rest, .node-row.file { color: var(--muted); }
.node-row.rest .nm { font-family: var(--sans); }
.node-row .tag { vertical-align: 1px; margin-left: 4px; }
meter.bar { margin: 0; height: 5px; }
meter.bar::-webkit-meter-bar { height: 5px; }
meter.bar.dim::-webkit-meter-optimum-value { background: var(--line-strong); }
meter.bar.dim::-moz-meter-bar { background: var(--line-strong); }
.nm.d1 { padding-left: 14px; }
.nm.d2 { padding-left: 28px; }
.nm.d3 { padding-left: 42px; }
.nm.d4 { padding-left: 56px; }
.nm.d5 { padding-left: 70px; }
.nm.d6 { padding-left: 84px; }
.nm.d7 { padding-left: 98px; }
.nm.d8 { padding-left: 112px; }
.nm.d9 { padding-left: 126px; }
.nm.d10 { padding-left: 140px; }
.nm.d11 { padding-left: 154px; }
.nm.d12 { padding-left: 168px; }
.nm.d13 { padding-left: 182px; }
.nm.d14 { padding-left: 196px; }
.nm.d15 { padding-left: 210px; }
.nm.d16 { padding-left: 224px; }
.nm.d17 { padding-left: 238px; }
.nm.d18 { padding-left: 252px; }
.nm.d19 { padding-left: 266px; }
.nm.d20 { padding-left: 280px; }
.nm.d21 { padding-left: 294px; }
.nm.d22 { padding-left: 308px; }
.nm.d23 { padding-left: 322px; }
.nm.d24 { padding-left: 336px; }
.faint { color: var(--faint); }
.cache-line { display: flex; flex-wrap: wrap; gap: 2px 10px; align-items: baseline; }
.cache-size { margin-left: auto; font-weight: 600; font-variant-numeric: tabular-nums; }
li.cache .how { margin-top: 3px; }
li.cache .how code { color: var(--text); }
ul.clutter > li { padding: 4px 0; }
table.files td.path { width: auto; }
[hidden] { display: none !important; }
@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; scroll-behavior: auto !important; }
}
@media (max-width: 640px) {
  main { padding: 14px 16px 32px; }
  h1 { font-size: 18px; }
  .member { grid-template-columns: 1fr; }
  .drive-free { margin-left: 0; flex-basis: 100%; }
  .node-row { grid-template-columns: minmax(0, 1fr) 48px 64px; gap: 0 8px; }
  .node-row .fl { display: none; }
  .nm.d1 { padding-left: 9px; }
  .nm.d2 { padding-left: 18px; }
  .nm.d3 { padding-left: 27px; }
  .nm.d4 { padding-left: 36px; }
  .nm.d5 { padding-left: 45px; }
  .nm.d6 { padding-left: 54px; }
  .nm.d7 { padding-left: 63px; }
  .nm.d8 { padding-left: 72px; }
  .nm.d9 { padding-left: 81px; }
  .nm.d10 { padding-left: 90px; }
  .nm.d11 { padding-left: 99px; }
  .nm.d12 { padding-left: 108px; }
  .nm.d13 { padding-left: 117px; }
  .nm.d14 { padding-left: 126px; }
  .nm.d15 { padding-left: 135px; }
  .nm.d16 { padding-left: 144px; }
  .nm.d17 { padding-left: 153px; }
  .nm.d18 { padding-left: 162px; }
  .nm.d19 { padding-left: 171px; }
  .nm.d20 { padding-left: 180px; }
  .nm.d21 { padding-left: 189px; }
  .nm.d22 { padding-left: 198px; }
  .nm.d23 { padding-left: 207px; }
  .nm.d24 { padding-left: 216px; }
  .toolbar .shown { margin-left: 0; }
  dl.facts { grid-template-columns: 1fr; gap: 0; }
  dl.facts dd { margin-bottom: 6px; }
  .stats { gap: 8px 24px; }
  /* a table of a few short columns (.num) keeps them; any other stacks its
     cells, each value labelled by its column where the label is needed */
  table.num th, table.num td { padding: 5px 10px 5px 0; }
  table:not(.num), table:not(.num) tbody, table:not(.num) tr,
  table:not(.num) td { display: block; width: auto; }
  table:not(.num) thead { display: none; }
  table:not(.num) tr { border-top: 1px solid var(--line); padding: 5px 0; }
  table:not(.num) tbody tr:first-child { border-top: 0; }
  table:not(.num) td { border: 0; padding: 1px 0; text-align: left; }
  td[data-label]::before { content: attr(data-label) " "; color: var(--muted); font-size: 12px; }
  td.why, td.what { width: auto; }
  ol.runs > li { grid-template-columns: 1.8em 1fr; }
  .run-links { grid-column: 2; }
  footer dl { grid-template-columns: 1fr; gap: 0; }
  footer dd { margin-bottom: 6px; }
}
""".replace("@LIGHT@", LIGHT).replace("@DARK@", DARK)


def page(title, body, script=None):
    """A whole page. `script` (constant text, never data) runs if given: the
    Content-Security-Policy allows exactly that script, by its hash."""
    csp = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"
    tail = ""
    if script:
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        csp += f"; script-src 'sha256-{digest}'"
        tail = f"<script>{script}</script>\n"
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta name=\"color-scheme\" content=\"light dark\">\n"
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"{csp}\">\n"
        "<meta name=\"referrer\" content=\"no-referrer\">\n"
        f"<title>{esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n<main>\n"
        f"{body}</main>\n{tail}</body>\n</html>\n")


STATUS_TEXT = {
    "done": "done", "running": "running", "interrupted": "interrupted",
    "drive-gone": "drive gone", "failed": "failed", "unfinished": "unfinished",
}


def badge(status):
    return f'<span class="badge s-{esc(status)}">{esc(STATUS_TEXT.get(status, status))}</span>'


def _header(run, title, mode_tag):
    m = run.meta
    took = m.get("duration_s")
    bits = [esc(when(run.started_ts))]
    if isinstance(took, (int, float)):
        bits.append("took " + esc(duration(took)))
    return (f'<a class="crumb" href="../../index.html">&larr; All runs</a>\n'
            f'<h1>{esc(title)} <span class="tag">{esc(mode_tag)}</span></h1>\n'
            f'<p class="meta">{" · ".join(bits)}</p>\n')


def _callout(run, headline, detail_html=""):
    status = run.status
    return (f'<section class="callout s-{esc(status)}">'
            f'<div class="headline">{esc(headline)}</div>'
            f'<p>{badge(status)} {detail_html}</p></section>\n')


def _count(n, one, many):
    """A stat card's number and label: "1 file", "2 files"."""
    return f"{n:,}", one if n == 1 else many


def _stats(cards):
    out = ['<section class="stats">']
    for value, label, bad in cards:
        out.append(f'<div class="stat{" bad" if bad else ""}"><div class="n">{esc(value)}</div>'
                   f'<div class="l">{esc(label)}</div></div>')
    out.append("</section>\n")
    return "".join(out)


def _issues_table(title, rows, total_note=""):
    """rows: [(path, why)]."""
    if not rows:
        return ""
    shown = rows[:MAX_ISSUES]
    out = [f'<h2>{esc(title)} <span class="count">{len(rows):,}</span></h2>',
           '<div class="panel"><table><thead><tr><th>Where</th><th>What happened</th></tr></thead><tbody>']
    for path, why in shown:
        out.append(f'<tr><td class="path">{esc(path)}</td><td class="why">{esc(why)}</td></tr>')
    out.append("</tbody></table>")
    if len(rows) > len(shown):
        out.append(f'<ul class="rows"><li class="more">... and {len(rows) - len(shown):,} more'
                   f'{esc(total_note)}</li></ul>')
    out.append("</div>\n")
    return "".join(out)


def _footer(run, files):
    m = run.meta
    argv = m.get("argv") if isinstance(m.get("argv"), list) else []
    links = " ".join(f'<a href="{href(name)}">{esc(name)}</a>' for name in files
                     if run.has(name) or name in run.coming)
    rows = [("Command", f'<code>{esc(command_line([str(a) for a in argv]))}</code>')]
    if isinstance(m.get("cwd"), str):
        rows.append(("Run in", f'<span class="path">{esc(m["cwd"])}</span>'))
    rows += [("Saved as", f'<span class="path">{esc(run.folder)}</span>'),
             ("Files", links or "none"),
             ("Version", f'clean {esc(m.get("version", "?"))}')]
    body = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    return f"<footer><dl>{body}</dl></footer>\n"


def _root_info(run):
    """{root path: "on <drive> (<fstype>)"} from run.json."""
    out = {}
    for r in run.meta.get("roots") or []:
        if isinstance(r, dict) and isinstance(r.get("path"), str):
            bits = []
            if r.get("drive"):
                bits.append(f"on {r['drive']}")
            if r.get("fstype"):
                bits.append(str(r["fstype"]))
            out[r["path"]] = " · ".join(bits)
    return out


def _rel(path, base):
    return path[len(base.rstrip("/")) + 1:] if path != base else "."


# ---------------------------------------------------------------- empty

def empty_page(run, info):
    delete, res = info["delete"], info["result"]
    target = "deleted" if delete else "would-delete"
    done = [p for status, _, p, _ in info["rows"] if status == target]
    groups = outermost(done)
    headline = run.headline
    if info["stage"] == "scan":
        detail = "The scan did not finish, so nothing was deleted and there is no list."
    elif delete:
        detail = "Only empty folders were removed (rmdir refuses anything else)."
    elif done:
        argv = [a for a in run.meta.get("argv", []) if isinstance(a, str)]
        cmd = command_line(argv, drop=("-n", "--dry-run"), add=("--delete",))
        detail = ("Nothing was deleted. To delete these, run the same command with "
                  f"--delete: <code>{esc(cmd)}</code>")
    else:
        detail = "Nothing to delete."

    body = [_header(run, "Empty folders", "delete" if delete else "dry run"),
            _callout(run, headline, detail)]
    cards = [_count(info["scanned"], "folder scanned", "folders scanned"),
             (f"{len(done):,}", "deleted" if delete else "would be deleted"),
             _count(len(groups), "place (outermost folder)", "places (outermost folders)")]
    if res["skipped"]:
        cards.append((f"{res['skipped']:,}", "kept (something inside failed)"))
    if res["not_done"]:
        cards.append((f"{res['not_done']:,}", "not done (stopped first)"))
    cards.append(_count(len(info["errors"]), "error", "errors"))
    body.append(_stats([(v, label, label.startswith("error") and v != "0") for v, label in cards]))

    if groups:
        body.append(f'<h2>{"Deleted" if delete else "Would be deleted"} '
                    f'<span class="count">{len(done):,}</span></h2>\n')
        body.append('<p class="muted">Each line is an outermost empty folder; '
                    '“+N inside” counts the empty folders within it, which go with it.</p>\n')
        body.append(_folder_lists(groups, info["roots"], _root_info(run)))

    not_deleted = [(p, note) for status, _, p, note in info["rows"]
                   if status in ("kept", "gone")]
    body.append(_issues_table("Not deleted", not_deleted, " (all in folders.tsv)"))
    not_done = [p for status, _, p, _ in info["rows"] if status == "not-done"]
    if not_done:
        body.append(_not_reached(run, not_done, info["roots"]))
    body.append(_issues_table("Problems", info["errors"], " (all in summary.txt)"))
    body.append(_footer(run, ("summary.txt", "folders.tsv", "data.json.gz", "run.json")))
    return page(f"Empty folders · {run.name}", "".join(body))


def _not_reached(run, paths, roots):
    """The folders a stopped run never got to: they all share one reason, so
    say it once and keep the list folded away."""
    argv = [a for a in run.meta.get("argv", []) if isinstance(a, str)]
    again = "once the drive is back, run" if run.status == "drive-gone" else "run"
    groups = outermost(paths)
    return (f'<h2>Not reached <span class="count">{len(paths):,}</span></h2>\n'
            '<p class="muted">The run stopped before it got to these empty folders, so they '
            f'are all still there. To finish, {again} the same command again: '
            f'<code>{esc(command_line(argv))}</code></p>\n'
            f'<details class="block"><summary>Show them ({esc(plural(len(groups), "place"))})'
            f'</summary>{_folder_lists(groups, roots, {})}</details>\n')


def _folder_lists(groups, roots, root_info):
    by_root = {r: [] for r in roots}
    orphans = []
    for outer, inner in groups:
        root = next((r for r in roots if is_within(outer, r)), None)
        (by_root[root] if root else orphans).append((outer, inner))
    out = []
    shown, inner_budget = 0, MAX_INNER_TOTAL
    for root, items in list(by_root.items()) + [("/", orphans)]:
        if not items:
            continue
        info = root_info.get(root, "")
        out.append(f'<section class="panel"><div class="panel-head">'
                   f'<span class="path">{esc(root)}</span>'
                   + (f'<span class="muted">{esc(info)}</span>' if info else "")
                   + f'<span class="muted">{esc(plural(len(items), "place"))}</span></div>'
                   '<ul class="rows">')
        for outer, inner in items:
            if shown >= MAX_OUTER:
                break
            shown += 1
            line = f'<li><span class="path">{esc(_rel(outer, root))}</span>'
            if inner:
                if inner_budget > 0:
                    listed = inner[:min(MAX_INNER, inner_budget)]
                    inner_budget -= len(listed)
                    rows = "".join(f'<li class="path">{esc(_rel(p, outer))}</li>' for p in listed)
                    if len(inner) > len(listed):
                        rows += f'<li>... and {len(inner) - len(listed):,} more (folders.tsv has all)</li>'
                    line += (f'<details><summary>+{len(inner):,} inside</summary>'
                             f'<ul>{rows}</ul></details>')
                else:
                    line += f'<span class="plain-more">+{len(inner):,} inside</span>'
            out.append(line + "</li>")
        out.append("</ul></section>\n")
    total = len(groups)
    if total > shown:
        out.append(f'<p class="muted">... and {total - shown:,} more places; '
                   f'folders.tsv and summary.txt have every one.</p>\n')
    return "".join(out)


# ---------------------------------------------------------------- dupes

# The report's one script: tabs, search, filters and sort over the cards the
# page already holds. It only moves and hides elements and sets textContent,
# so no name can turn into markup.
DUPES_SCRIPT = r"""
(function () {
  "use strict";
  var tabs = Array.prototype.slice.call(document.querySelectorAll("nav.tabs a"));
  var sections = tabs.map(function (a) { return document.getElementById(a.hash.slice(1)); });
  var bar = document.getElementById("toolbar");
  var q = document.getElementById("q"), type = document.getElementById("type"),
      size = document.getElementById("size"), loc = document.getElementById("loc"),
      sort = document.getElementById("sort"), shown = document.getElementById("shown");
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var text = new Map();
  cards.forEach(function (c, i) { c.dataset.order = i; });
  var active = null;

  function lists(section) {
    return Array.prototype.slice.call(section.querySelectorAll(".cards"));
  }
  function select(section) {
    active = section;
    sections.forEach(function (s, i) {
      s.hidden = s !== section;
      tabs[i].classList.toggle("on", s === section);
      tabs[i].setAttribute("aria-selected", s === section ? "true" : "false");
    });
    bar.hidden = !section.querySelector(".card");
    apply();
  }
  function textOf(c) {
    if (!text.has(c)) { text.set(c, c.textContent.toLowerCase()); }
    return text.get(c);
  }
  function apply() {
    if (!active) { return; }
    var words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
    var t = type.value, min = Number(size.value), l = loc.value, how = sort.value;
    var total = 0, visible = 0;
    lists(active).forEach(function (list) {
      var items = Array.prototype.slice.call(list.children).filter(function (c) {
        return c.classList.contains("card");
      });
      items.forEach(function (c) {
        var ok = Number(c.dataset.size) >= min &&
          (!t || (" " + c.dataset.types + " ").indexOf(" " + t + " ") >= 0) &&
          (!l || (" " + c.dataset.loc + " ").indexOf(" " + l + " ") >= 0) &&
          words.every(function (w) { return textOf(c).indexOf(w) >= 0; });
        c.hidden = !ok;
        total += 1;
        if (ok) { visible += 1; }
      });
      items.sort(function (a, b) {
        if (how === "count") {
          return Number(b.dataset.count) - Number(a.dataset.count) ||
            Number(a.dataset.order) - Number(b.dataset.order);
        }
        if (how === "path") {
          return a.dataset.path < b.dataset.path ? -1 : a.dataset.path > b.dataset.path ? 1 : 0;
        }
        return Number(a.dataset.order) - Number(b.dataset.order);
      });
      var tail = list.lastElementChild && !list.lastElementChild.classList.contains("card")
        ? list.lastElementChild : null;
      items.forEach(function (c) { list.insertBefore(c, tail); });
    });
    // While filtering, a folded list opens if it holds a match (so what is
    // counted can be seen) and is hidden if it holds none.
    var filtering = words.length > 0 || t !== "" || min > 0 || l !== "";
    Array.prototype.slice.call(active.querySelectorAll("details.block")).forEach(function (d) {
      if (!d.querySelector(".card")) { return; }
      var match = d.querySelector(".card:not([hidden])");
      d.hidden = filtering && !match;
      if (filtering && match) { d.open = true; }
    });
    shown.textContent = visible === total ? total.toLocaleString() + " shown"
      : visible.toLocaleString() + " of " + total.toLocaleString() + " shown";
  }
  function fromHash() {
    var target = location.hash ? document.getElementById(location.hash.slice(1)) : null;
    var section = target ? sections.find(function (s) { return s.contains(target); }) : null;
    if (section) {
      select(section);
      var d = target.closest("details");
      if (d) { d.open = true; }
      if (target.hidden) { q.value = ""; type.value = ""; size.value = "0"; loc.value = ""; apply(); }
      target.scrollIntoView();
    }
    return section;
  }
  tabs.forEach(function (a, i) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      select(sections[i]);
      history.replaceState(null, "", a.hash);
    });
  });
  [q, type, size, loc, sort].forEach(function (el) {
    el.addEventListener(el === q ? "input" : "change", apply);
  });
  window.addEventListener("hashchange", fromHash);
  bar.hidden = false;
  if (!fromHash()) {
    select(sections.find(function (s) { return s.querySelector(".card"); }) || sections[0]);
  }
})();
"""

ROLE_LABEL = {"keep": "keep", "identical": "identical", "renamed": "renamed",
              "partial": "partial", "diverged": "diverged", "covered": "covered"}
SIZE_STEPS = ((0, "any size"), (1 << 20, "1 MB or more"), (10 << 20, "10 MB or more"),
              (100 << 20, "100 MB or more"), (1 << 30, "1 GB or more"))


class _Places:
    """Where each path is, for the location filter: its root, and the folder
    (or file) at the top of that root it is in."""

    def __init__(self, roots):
        self.roots = sorted(roots, key=len, reverse=True)
        self.keys = {}      # (root, top or "") -> number
        self.counts = {}

    def of(self, path):
        root = next((r for r in self.roots if is_within(path, r)), None)
        if root is None:
            return []
        rest = path[len(root.rstrip("/")) + 1:] if path != root else ""
        top = rest.split("/", 1)[0] if rest else ""
        out = [self._key(root, "")]
        if top:
            out.append(self._key(root, top))
        return out

    def _key(self, root, top):
        k = self.keys.setdefault((root, top), len(self.keys) + 1)
        self.counts[k] = self.counts.get(k, 0) + 1
        return k

    def attr(self, paths):
        return " ".join(str(k) for k in sorted({k for p in paths for k in self.of(p)}))

    def options(self, limit=300):
        tops = sorted(((r, t), k) for (r, t), k in self.keys.items())
        if len(tops) > limit:
            keep = set(sorted(self.counts, key=lambda k: -self.counts[k])[:limit])
            tops = [x for x in tops if x[1] in keep or not x[0][1]]
        out = []
        for (root, top), k in tops:
            label = root if not top else (" " + top)
            out.append(f'<option value="{k}">{esc(label)}</option>')
        return "".join(out)


def _card_head(rid, bits, freed):
    return (f'<div class="panel-head"><span class="id">{esc(rid)}</span>'
            + "".join(bits)
            + (f'<span class="save">{esc(human(freed))} reclaimable</span>' if freed else "")
            + "</div>")


def _member_row(role, path, facts, note="", diff_html=""):
    return (f'<li class="member"><span class="role"><span class="badge r-{esc(role)}">'
            f'{esc(ROLE_LABEL[role])}</span></span><div>'
            f'<div class="path">{esc(path)}</div>'
            f'<div class="facts">{esc(facts)}</div>'
            + (f'<div class="note">{esc(note)}</div>' if note else "")
            + diff_html + "</div></li>")


def _diff_html(d):
    if not d:
        return ""
    parts = []
    total = 0
    for key, label in DIFF_TEXT:
        part = d.get(key)
        if not part or not part["count"]:
            continue
        total += part["count"]
        folders = part.get("folders", [])[:MAX_DIFF]
        items = part["items"][:MAX_DIFF - len(folders)]
        rows = []
        shown = 0
        for *names, n in folders:
            rows.append("<li>" + ' <span class="arrow">&rarr;</span> '.join(
                esc(x or ".") + "/" for x in names)
                + f' <span class="muted">({esc(plural(n, "file"))})</span></li>')
            shown += n
        for item in items:
            if key == "moved":
                rows.append(f'<li>{esc(item[0])} <span class="arrow">&rarr;</span> {esc(item[1])}</li>')
            else:
                rows.append(f"<li>{esc(item)}</li>")
            shown += 1
        if part["count"] > shown:
            rows.append(f'<li class="muted">... and {part["count"] - shown:,} more '
                        f'(data.json.gz has up to 10,000)</li>')
        parts.append(f'<h4>{esc(label)} ({part["count"]:,})</h4><ul>{"".join(rows)}</ul>')
    if not parts:
        return ""
    return (f'<div class="diff"><details><summary>{esc(plural(total, "difference"))}</summary>'
            + "".join(parts) + "</details></div>")


def _folder_facts(files, nbytes, freed=None):
    text = f"{plural(files, 'file')} · {human(nbytes)}"
    if freed is not None:
        text += f" · removing it frees {human(freed)}" if freed else " · removing it frees nothing"
    return text


def _family_card(f, places):
    members = f["members"]
    paths = [f["anchor"]] + [m["path"] for m in members]
    attrs = (f'id="{esc(f["id"])}" data-size="{f["freed"]}" data-count="{len(paths)}" '
             f'data-types="{esc(" ".join(f["types"]))}" data-loc="{places.attr(paths)}" '
             f'data-path="{esc(f["anchor"])}"')
    head = _card_head(f["id"], [
        f'<span class="big">{esc(plural(len(paths), "folder"))}</span>',
        f'<span class="tag">{esc(KIND_TEXT.get(f["kind"], f["kind"]))}</span>',
        f'<span class="muted">{esc(", ".join(f["types"][:3]))}</span>' if f["types"] else ""],
        f["freed"])
    rows = [_member_row("keep", f["anchor"], _folder_facts(f["files"], f["bytes"]),
                        "the most complete copy; the others are compared with it")]
    for m in members[:MAX_MEMBERS]:
        rows.append(_member_row(m["role"], m["path"],
                                _folder_facts(m["files"], m["bytes"], m["freed"]),
                                member_note(m), _diff_html(m.get("diff"))))
    if len(members) > MAX_MEMBERS:
        rows.append(f'<li class="more">... and {len(members) - MAX_MEMBERS:,} more folders '
                    f'(folders.tsv has all)</li>')
    return (f'<section class="panel card" {attrs}>{head}<ul class="rows">'
            + "".join(rows) + "</ul></section>\n")


def _covered_card(c, places):
    attrs = (f'id="{esc(c["id"])}" data-size="{c["freed"]}" data-count="{c["where_total"]}" '
             f'data-types="{esc(" ".join(c["types"]))}" data-loc="{places.attr([c["path"]])}" '
             f'data-path="{esc(c["path"])}"')
    head = _card_head(c["id"], [
        f'<span class="big">{esc(plural(c["files"], "file"))} · {esc(human(c["bytes"]))}</span>',
        f'<span class="muted">copies in {esc(plural(c["where_total"], "place"))}</span>'],
        c["freed"])
    where = "".join(f'<li><span class="path">{esc(w)}</span> '
                    f'<span class="muted">({esc(plural(n, "file"))})</span></li>'
                    for w, n in c["where"])
    more = c["where_total"] - len(c["where"])
    if more > 0:
        where += f'<li class="muted">... and {more:,} more places</li>'
    return (f'<section class="panel card" {attrs}>{head}<ul class="rows">'
            f'{_member_row("covered", c["path"], _folder_facts(c["files"], c["bytes"], c["freed"]), "every file in it also exists elsewhere:")}'
            f'<li class="where"><ul class="rows">{where}</ul></li></ul></section>\n')


def _group_card(g, places):
    n = len(g["files"])
    gid = f"G{g['id']}"
    attrs = (f'id="{gid}" data-size="{g["size"] * (n - 1)}" data-count="{n}" '
             f'data-types="{esc(g["category"])}" data-loc="{places.attr(g["files"])}" '
             f'data-path="{esc(g["files"][0])}"')
    head = _card_head(gid, [
        f'<span class="big">{n} copies × {esc(human(g["size"]))}</span>',
        f'<span class="tag">{esc(g["category"])}</span>',
        f'<span class="hash mono" title="blake2b {esc(g["hash"])}">{esc(g["hash"][:16])}</span>'],
        g["size"] * (n - 1))
    rows = []
    for p, ex in list(zip(g["files"], g["explained"]))[:MAX_COPIES]:
        tag = (f'<a class="ex" href="#{esc(ex)}" title="this folder is in result {esc(ex)}">'
               f'{esc(ex)}</a>' if ex else "")
        rows.append(f'<li><span class="path">{esc(p)}</span>{tag}</li>')
    if n > MAX_COPIES:
        rows.append(f'<li class="more">... and {n - MAX_COPIES:,} more (files.tsv has all)</li>')
    return (f'<section class="panel card" {attrs}>{head}<ul class="rows">'
            + "".join(rows) + "</ul></section>\n")


def lookalike_head(rid, g, extra=""):
    """The head of a look-alike card (the report's and the dashboard's)."""
    return (f'<div class="panel-head"><span class="id">{esc(rid)}</span>'
            f'<span class="big">{esc(g["label"])}</span>'
            f'<span class="tag">{esc(g["kind"])}</span>'
            f'<span class="muted">{esc(plural(g["versions"], "version"))} in '
            f'{esc(plural(len(g["files"]), "file"))}, not byte-identical</span>{extra}'
            + (f'<span class="save">up to {esc(human(g["spare"]))}</span>' if g["spare"] else "")
            + "</div>")


def lookalike_facts(letter, best, detail, size, when):
    """'A' badge and '4032×3024 JPEG · 3.1 MB · modified 7 Oct 2026 · likely the best'."""
    badge = (f'<span class="ver{" best" if best else ""}" title="version {esc(letter)}">'
             f'{esc(letter)}</span>')
    bits = [esc(x) for x in (detail, human(size), when) if x]
    if best:
        bits.append('<span class="best">likely the best</span>')
    return badge, " · ".join(bits)


def _lookalike_card(g, places):
    gid = f"L{g['id']}"
    paths = [f["path"] for f in g["files"]]
    attrs = (f'id="{gid}" data-size="{g["spare"]}" data-count="{len(paths)}" '
             f'data-types="{"Images" if g["kind"] == "photo" else "Audio"}" '
             f'data-loc="{places.attr(paths)}" data-path="{esc(paths[0])}"')
    rows = []
    for f in g["files"][:MAX_COPIES]:
        badge, facts = lookalike_facts(f["letter"], f["best"], f["detail"], f["size"],
                                       f"modified {_day(f['mtime'])}")
        rows.append(f'<li>{badge}<span class="path">{esc(f["path"])}</span>'
                    f'<div class="facts">{facts}</div></li>')
    if len(g["files"]) > MAX_COPIES:
        rows.append(f'<li class="more">... and {len(g["files"]) - MAX_COPIES:,} more</li>')
    return (f'<section class="panel card" {attrs}>{lookalike_head(gid, g)}<ul class="rows">'
            + "".join(rows) + "</ul></section>\n")


def _day(ns):
    try:
        return datetime.fromtimestamp(ns / 1e9).strftime("%-d %b %Y")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _cards(items, cap, make, places, what, where="folders.tsv"):
    out = ['<div class="cards">']
    out += [make(x, places) for x in items[:cap]]
    if len(items) > cap:
        out.append(f'<p class="muted">... and {len(items) - cap:,} more {what}; '
                   f'{where} has every one.</p>')
    out.append("</div>\n")
    return "".join(out)


def _toolbar(info, places):
    cats = sorted({t for f in info["families"] for t in f["types"]}
                  | {t for c in info["covered"] for t in c["types"]}
                  | {g["category"] for g in info["groups"]}
                  | {"Images" if g["kind"] == "photo" else "Audio"
                     for g in info.get("lookalikes") or ()})
    types = "".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in cats)
    sizes = "".join(f'<option value="{n}">{esc(label)}</option>' for n, label in SIZE_STEPS)
    return (
        '<div class="toolbar" id="toolbar" hidden>'
        '<input type="search" id="q" placeholder="Search paths and names" aria-label="Search">'
        f'<label>Type <select id="type"><option value="">all</option>{types}</select></label>'
        f'<label>Reclaimable <select id="size">{sizes}</select></label>'
        f'<label>In <select id="loc"><option value="">everywhere</option>{places.options()}'
        '</select></label>'
        '<label>Sort <select id="sort"><option value="space">most space first</option>'
        '<option value="count">most copies first</option><option value="path">by path</option>'
        '</select></label><span class="shown" id="shown"></span></div>\n')


def _by_type(info):
    cats = (info.get("totals") or {}).get("categories") or {}
    if not cats:
        return ""
    rows = "".join(f'<tr><td>{esc(cat)}</td><td class="n">{g:,}</td><td class="n">{c:,}</td>'
                   f'<td class="n">{esc(human(b))}</td></tr>'
                   for cat, (g, c, b) in sorted(cats.items(), key=lambda kv: (-kv[1][2], kv[0])))
    return ('<h2>By type</h2><div class="panel"><table class="num"><thead><tr><th>Type</th>'
            '<th class="n">Groups</th><th class="n">Redundant copies</th>'
            f'<th class="n">Reclaimable</th></tr></thead><tbody>{rows}</tbody></table></div>\n')


def _scan_facts(info):
    from cleanup.report.text import _skipped_text
    rows = []
    rows.append(("Compared", "file contents (blake2b)"
                 + "".join(f" + {w}" for w, on in (("same name", info.get("same_name")),
                                                    ("same date", info.get("same_date"))) if on)))
    rows.append(("Diverged", f"folders sharing at least {info.get('similar', 80)}% of their "
                             "files are shown as diverged copies"))
    if info.get("filters"):
        rows.append(("Filters", " · ".join(info["filters"])))
    rows.append(("Hidden files", "included" if info.get("hidden") else
                 "not compared (--hidden includes them)"))
    if info.get("max_depth") is not None:
        rows.append(("Depth", f"{info['max_depth']} folder levels below each path"))
    rows.append(("Files scanned", f"{info.get('total_files', 0):,} (jobs={info.get('jobs')})"))
    if info.get("hard_links"):
        rows.append(("Hard links", f"{info['hard_links']:,} extra names of the same file, "
                                   "not counted as copies"))
    skipped = _skipped_text(info.get("skipped") or {})
    if skipped:
        rows.append(("Skipped", skipped))
    h = info.get("hashing")
    if h:
        text = f"{h['read']:,} files read ({human(h['bytes'])})"
        if h["cache"] == "on":
            text += f", {h['cached']:,} hashes from the cache"
            if h.get("trimmed"):
                text += (f"; {h['trimmed']:,} old entries dropped to keep the cache "
                         f"under cache.max_mb")
        elif h["cache"] == "failed":
            text += "; the hash cache could not be used"
        else:
            text += "; hash cache off"
        rows.append(("Hashing", text))
    ms = info.get("media_stats")
    if info.get("media") and ms:
        rows.append(("Look-alikes", f"{ms['tagged']:,} of {ms['candidates']:,} photos and songs "
                     f"had tags to compare ({ms['read']:,} read, {ms['cached']:,} from the cache"
                     + (f", {ms['unreadable']:,} unreadable" if ms["unreadable"] else "") + ")"))
    body = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
    return f'<h2>About this scan</h2><div class="panel"><dl class="facts">{body}</dl></div>\n'


def dupes_page(run, info):
    done = run.status == "done" and not info.get("stopped")
    t = info.get("totals") or {}
    if done:
        detail = ("Nothing was deleted. Files count as copies only when their bytes are "
                  "identical; keeping any one copy of each loses nothing.")
    else:
        detail = "The scan did not finish, so there are no results."
    body = [_header(run, "Duplicates", "report only"), _callout(run, run.headline, detail)]
    if done:
        body.append('<p class="hint">This page only shows the results. To choose what '
                    'goes and remove it, open it in the dashboard: <code>clean ui</code>. Or '
                    'remove the suggested copies from a terminal: <code>clean remove '
                    f'{esc(run.name)}</code>.</p>\n')
    errors = info["errors"]
    if done:
        body.append(_stats([
            (*_count(info["total_files"], "file scanned", "files scanned"), False),
            (*_count(t.get("families", 0), "folder family", "folder families"), False),
            (*_count(t.get("covered", 0), "covered folder", "covered folders"), False),
            (*_count(t.get("groups", 0), "duplicate group", "duplicate groups"), False),
        ] + ([(*_count(t.get("lookalikes", 0), "look-alike set", "look-alike sets"), False)]
             if info.get("media") else []) + [
            (human(t.get("reclaimable", 0)), "reclaimable", False),
            (*_count(len(errors), "error", "errors"), bool(errors)),
        ]))
        places = _Places(info["roots"])
        fams, cov, groups = info["families"], info["covered"], info["groups"]
        loose = [g for g in groups if not g["folded"]]
        folded = [g for g in groups if g["folded"]]
        sections = [
            ("families", "Folder families", len(fams),
             '<p class="muted">Folders that are copies of each other. The first folder of each '
             'card is the most complete; each other one is labelled with how it compares. '
             'Removing a redundant folder frees what is shown on its line.</p>\n'
             + _cards(fams, MAX_FAMILIES, _family_card, places, "folder families")),
            ("covered", "Covered folders", len(cov),
             '<p class="muted">Folders whose every file also exists somewhere else, spread over '
             'other folders rather than as one copy.</p>\n'
             + _cards(cov, MAX_COVERED, _covered_card, places, "covered folders")),
            ("groups", "File groups", len(loose),
             '<p class="muted">Files with exactly the same bytes, largest wasted space first. '
             'A tag such as F3 means that copy is in a folder result above.</p>\n'
             + _cards(loose, MAX_GROUPS, _group_card, places, "groups", "files.tsv")
             + (f'<details class="block"><summary>{esc(plural(len(folded), "more group"))} '
                f'inside the folder results</summary>'
                + _cards(folded, MAX_FOLDED, _group_card, places, "groups", "files.tsv")
                + "</details>\n" if folded else "")),
        ]
        if info.get("media"):
            looks = info.get("lookalikes") or []
            sections.append(
                ("lookalikes", "Look-alikes", len(looks),
                 '<p class="muted">The same photo (camera, time taken and size) or song '
                 '(artist, title and length) in files whose bytes differ: an edited or '
                 're-saved photo, a song in two formats. They are not duplicates, so nothing '
                 'here is ever picked for removal; each letter is one version, and the one '
                 'that looks best is marked.</p>\n'
                 + _cards(looks, MAX_LOOKALIKES, _lookalike_card, places, "look-alike sets",
                          "data.json.gz")))
        body.append('<nav class="tabs" role="tablist">' + "".join(
            f'<a href="#{key}" role="tab">{esc(title)}<span class="count">{n:,}</span></a>'
            for key, title, n, _ in sections) + "</nav>\n")
        body.append(_toolbar(info, places))
        for key, title, n, content in sections:
            empty = "" if n or (key == "groups" and folded) else \
                '<div class="panel"><div class="empty-state">None found.</div></div>'
            body.append(f'<section id="{key}" class="results"><h2>{esc(title)} '
                        f'<span class="count">{n:,}</span></h2>\n{content}{empty}</section>\n')
        body.append(_by_type(info))
        body.append(_scan_facts(info))
    body.append(_issues_table("Problems", errors, " (all in summary.txt)"))
    body.append(_footer(run, ("summary.txt", "folders.tsv", "files.tsv", "data.json.gz",
                              "run.json")))
    return page(f"Duplicates · {run.name}", "".join(body), DUPES_SCRIPT if done else None)


# ---------------------------------------------------------------- index

def index_page(runs, data_dir):
    s = _settings.current()
    days, keep = s.get("history.keep_days"), s.get("history.keep_runs")
    if days:
        policy = (f"Runs older than {plural(days, 'day')} are removed, except the newest "
                  f"{keep:,} and any a removal can still be undone from.")
    else:
        policy = "Runs are kept until you delete them."
    body = ['<h1>clean <span class="tag">history</span></h1>\n',
            f'<p class="meta">{esc(plural(len(runs), "run"))} in '
            f'<span class="path">{esc(data_dir)}</span></p>\n',
            f'<p class="meta">{esc(policy)} Settings: history.keep_days, history.keep_runs. '
            'Deleting a run folder removes it from this list.</p>\n',
            '<section class="panel">']
    if not runs:
        body.append('<div class="empty-state">No runs yet. <code>clean empty PATH</code> and '
                    '<code>clean dupes PATH</code> each save one here.</div>')
    else:
        body.append('<ol class="runs">')
        for run in runs:
            base = f"runs/{run.name}/"
            report = run.has("report.html")
            what = esc(run.label)
            if report:
                what = f'<a href="{href(base + "report.html")}">{what}</a>'
            roots = run.roots
            where = ""
            if roots:
                where = esc(roots[0]) + (f' <span class="muted">+{len(roots) - 1} more</span>'
                                         if len(roots) > 1 else "")
            links = []
            if report:
                links.append(f'<a href="{href(base + "report.html")}">report</a>')
            if run.has("summary.txt"):
                links.append(f'<a href="{href(base + "summary.txt")}">summary</a>')
            if not links:
                links.append(f'<a href="{href(base)}">folder</a>')
            headline = run.headline or {
                "running": "still running",
                "unfinished": "stopped without finishing (killed, crashed, or the computer went off)",
            }.get(run.status, "")
            body.append(
                f'<li><span class="num">{run.number}</span><div>'
                f'<div class="run-line"><span class="run-what">{what}</span>{badge(run.status)}'
                f'<span class="run-when">{esc(when(run.started_ts))}</span></div>'
                f'<div class="run-headline">{esc(headline)}</div>'
                + (f'<div class="run-roots path">{where}</div>' if where else "")
                + f'</div><div class="run-links">{"".join(links)}</div></li>')
        body.append("</ol>")
    body.append("</section>\n")
    return page("clean · history", "".join(body))
