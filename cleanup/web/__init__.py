"""
`clean ui`: the dashboard, a small web app served on 127.0.0.1 only.

    server.py   the HTTP server: who may talk to it, security headers, routes
    api.py      the JSON API the pages call
    jobs.py     scans and removals, run as `clean ...` child processes
    view.py     a dupes run's results with the choices made: cards, footer
    pages.py    the pages themselves (HTML built here, names always escaped)
    static/     app.js and app.css: no build step, no CDN, works offline

Everything that changes files goes through the same `clean` commands the
terminal uses, so the dashboard has no deleting code of its own.
"""
