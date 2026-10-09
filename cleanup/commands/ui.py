"""
`clean ui`: the dashboard, a page in the browser served by this process
from 127.0.0.1 only. It runs scans and removals as `clean ...` child
processes, so they go through exactly the checks they do in a terminal.
"""

import argparse
import errno
import os
import shutil
import signal
import subprocess
import sys
import urllib.parse

from cleanup import paths
from cleanup import settings as _settings
from cleanup.errors import UsageError
from cleanup.fsutil import printable
from cleanup.report.text import command_line
from cleanup.web import server as _server

EXAMPLES = """\
examples:
  clean ui                  start the dashboard and open it in the browser
  clean ui --no-browser     only print the link to open
  clean ui --port 8765      on a fixed port (default: any free one; setting ui.port)
  clean ui ~/Pictures       open on a new duplicates scan of ~/Pictures (nothing
                            starts until you press Start)
  clean ui --scan space /media/$USER/USB
                            the same, for where the space goes on the drive

The dashboard listens on 127.0.0.1 only, and opens only through the link it
gives: that link works once, for 10 minutes, and signs the browser in. Run
`clean ui` again for a new link to the same dashboard. Ctrl-C stops it. If a
scan or removal is running, it stops once that ends; a second Ctrl-C cancels
the job the way Ctrl-C does in a terminal (what was done so far is saved).

The Files app can open these pages too: `install.sh --nautilus` adds Find
duplicates here, Find empty folders here and Where the space goes to its
right-click menu (under Scripts > clean).
"""


def add_parser(subparsers):
    p = subparsers.add_parser(
        "ui",
        help="the dashboard: scans, results, removal, history and settings in the browser",
        description="Start the dashboard: a page in the browser to run scans, choose\n"
                    "what goes, remove it, undo, and see the history and settings.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("folders", nargs="*", metavar="FOLDER",
                   help="open on a new scan with these folders filled in (it waits for "
                        "you to start it)")
    p.add_argument("--scan", choices=SCANS, metavar="KIND",
                   help="open on a new scan of this kind: " + ", ".join(SCANS)
                        + " (default with folders: dupes)")
    p.add_argument("--no-browser", action="store_true",
                   help="don't open the browser: print the link instead")
    p.add_argument("--if-running", action="store_true",
                   help="only open a dashboard that is already running; if there is none, "
                        "say so and exit with code 1")
    p.add_argument("--port", type=_port, metavar="N",
                   help="the port to listen on (0: any free one; default: setting ui.port)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print every request")
    p.set_defaults(func=run)
    return p


SCANS = ("dupes", "empty", "space")


def _page(args):
    """The page to open first: "/" or a new scan with the folders filled in."""
    if not args.folders and not args.scan:
        return "/"
    q = [("kind", args.scan or "dupes")]
    for folder in args.folders:
        path = os.path.abspath(os.path.expanduser(folder))
        if not os.path.isdir(path):
            raise UsageError(f"not a folder: {printable(folder)}")
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:
            raise UsageError(f"the dashboard can't take a folder whose name isn't valid text: "
                             f"{printable(path)} (scan it in the terminal instead)")
        q.append(("path", path))
    return "/scan?" + urllib.parse.urlencode(q)


def _link(url, page):
    return url if page == "/" else f"{url}&next={urllib.parse.quote(page, safe='')}"


def _port(text):
    try:
        n = int(text)
    except ValueError:
        n = -1
    if not 0 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r} (0 to 65535)")
    return n


def open_url(url):
    """Ask the desktop to open `url` in the browser; False if it can't."""
    opener = shutil.which("xdg-open")
    if opener is None:
        return False
    try:
        subprocess.Popen([opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


def _show(url, browser, again=False):
    where = "the dashboard that is already running" if again else "the dashboard"
    if browser and open_url(url):
        print(f"Opening {where} in the browser.")
        print("If it doesn't open, run `clean ui --no-browser` for a link.")
    else:
        print(f"Open {where} with this link (it works once, within 10 minutes):")
        print(f"  {url}")


def run(args):
    s = _settings.current()
    data_dir = paths.data_dir()
    browser = not args.no_browser and s.get("ui.open_browser")
    page = _page(args)
    url = _server.running_dashboard(data_dir)
    if url is not None:
        _show(_link(url, page), browser, again=True)
        return 0
    if args.if_running:
        print("No dashboard is running: start one with `clean ui`.", file=sys.stderr)
        return 1
    port = args.port if args.port is not None else s.get("ui.port")
    app = _server.App(data_dir)
    try:
        srv = _server.Server(app, port, verbose=args.verbose)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            raise UsageError(f"port {port} is in use: pick another with --port, or 0 for any "
                             "free one")
        raise UsageError(f"can't start the dashboard: {e.strerror or e}")
    hard = []

    def stop_now(signum, frame):        # kill, or the terminal closed: no one to ask
        hard.append(signum)
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) is not signal.SIG_IGN:
            signal.signal(sig, stop_now)
    try:
        _server.write_ui_file(app)
        print(f"Dashboard at http://127.0.0.1:{app.port}/ (only this computer can reach it)")
        _show(_link(app.launch_url(), page), browser)
        print("Stop it with Ctrl-C.")
        sys.stdout.flush()
        _serve(srv, app, hard)
    finally:
        _server.remove_ui_file(app)
        srv.server_close()
    print("Dashboard stopped.")
    return 0


def _serve(srv, app, hard):
    """Serve until Ctrl-C. With a job running, the first Ctrl-C only warns
    and stops the dashboard once the job ends; the second (or a kill)
    cancels the job and waits for it to stop."""
    import threading
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.25},
                         name="clean-ui", daemon=True)
    t.start()
    try:
        try:
            while t.is_alive():
                t.join(0.5)
            return
        except KeyboardInterrupt:
            pass
        job = app.jobs.active()
        if job is None:
            return
        if not hard:
            print(f"\nA job is running: {command_line(job.argv)}\n"
                  "The dashboard stops when it ends. Press Ctrl-C again to cancel it now "
                  "(what it did so far is saved).", flush=True)
            try:
                while not job.wait(0.5):
                    pass
                return
            except KeyboardInterrupt:
                pass
        print("\nCancelling the job...", flush=True)
        job.cancel()
        while not job.wait(1.0):
            pass
        print(f"The job stopped (exit code {job.code}).")
    finally:
        srv.shutdown()
