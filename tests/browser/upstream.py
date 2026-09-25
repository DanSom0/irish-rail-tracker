"""Local stand-in for getCurrentTrainsXML used by the browser review.

The file ``mode`` next to this script selects the response: ``seeded`` or ``empty``
serve ``trains_<mode>.xml``; ``fail`` answers 503.
"""

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        mode_file = HERE / "mode"
        mode = mode_file.read_text().strip() if mode_file.exists() else "seeded"
        if mode == "fail":
            self.send_response(503)
            self.end_headers()
            return
        body = (HERE / f"trains_{mode}.xml").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
