"""
A dashboard for tests: python3 serve.py DATA_DIR OPENED_FILE

Serves DATA_DIR on a free port and prints one JSON line,
{"port", "url" (a launch link), "cookie" (the signed-in Cookie header)},
then serves until its stdin closes; a job still running then is cancelled
and waited for. Folders the dashboard would show in the file manager are
written to OPENED_FILE instead, one per line.

Only ever run inside the sealed namespace (tests/helpers.py SEAL): its jobs
remove and restore files for real.
"""

import json
import os
import sys
import threading

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from cleanup.web import server  # noqa: E402


def main(data_dir, opened):
    def opener(path):
        with open(opened, "a", encoding="utf-8", errors="surrogateescape") as f:
            f.write(path + "\n")

    app = server.App(data_dir, opener=opener)
    srv = server.Server(app, 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
    print(json.dumps({"port": app.port, "url": app.launch_url(),
                      "cookie": f"{app.cookie_name}={app.secret}"}), flush=True)
    sys.stdin.read()
    job = app.jobs.active()
    if job is not None:
        job.cancel()
        job.wait(60)
    srv.shutdown()
    srv.server_close()


if __name__ == "__main__":
    main(*sys.argv[1:3])
