#!/usr/bin/env python3
"""Serve the repo root, so the console can be worked on with no service.

`ui/replay.html` folds the acceptance journals, and those live in
`acceptance/` rather than inside `ui/` — copying 78 KB of rig-day record into
the front end to make a path work would be the wrong trade. So the offline
bench is served from the repo root, where both `ui/` and `acceptance/` are
reachable:

    python3 tools/serve_ui.py            # http://127.0.0.1:8901/ui/replay.html

With a service running there is no need for this: `python -m bace.service
--sim --fast --ui ui` mounts the same files at `/ui`, and then the stream is
real. Under that mount the journal fixtures 404 and the `ui/fixtures/` ones
still load, which is the honest split.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        # A fixture that came back from the browser's cache is a fixture that
        # is no longer the file on disk, and this directory is edited all day.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        if "404" in (fmt % args):
            super().log_message(fmt, *args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args(argv)

    handler = functools.partial(Handler, directory=REPO)
    server = http.server.ThreadingHTTPServer((a.host, a.port), handler)
    print(f"serving {REPO}")
    print(f"  console   http://{a.host}:{a.port}/ui/")
    print(f"  replay    http://{a.host}:{a.port}/ui/replay.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
