"""Serve the customer page on http://127.0.0.1:5173.

A static file server and nothing more — the page holds no secrets and needs no
build step, so there is no bundler here and nothing to compile. The backend
stays where it is on 127.0.0.1:8001; this only hands the browser the HTML, CSS
and JavaScript.

    python serve.py

Port 5173 was chosen because 3000 and 3100 are already taken on this machine by
an unrelated project, and 8000/8001 belong to the banking backend.
"""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 5173
ROOT = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    """Static files, never cached.

    Caching is off so an edit to app.js is visible on reload. Without this a
    stale script is served and the page looks broken for no visible reason.
    """

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        # Quieter than the default, and never logs a request body.
        print(f"{self.address_string()} {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", default=HOST)
    options = parser.parse_args()

    handler = partial(Handler, directory=str(ROOT))
    with ThreadingHTTPServer((options.host, options.port), handler) as server:
        print(f"ABC Demo Bank voice banking page: http://{options.host}:{options.port}")
        print("Backend expected at http://127.0.0.1:8001. Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
