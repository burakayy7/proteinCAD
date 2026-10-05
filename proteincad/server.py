"""Static file server for the viewer, plus the JSON API.

Standard library only, so `python -m proteincad` works on a fresh machine with
no install step. If you later want FastAPI or similar, the routes in api.py are
plain functions and port over directly.
"""

from __future__ import annotations

import gzip
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

from . import api
from .config import ROOT, Config, from_env
from .design import build_runners
from .jobs import JobQueue
from .scans import ScanStore

DEFAULT_WEB_DIR = ROOT / "web"
DEFAULT_DATA_DIR = ROOT / "data"


# A browser that closes a kept-alive connection leaves a thread blocked in
# readline(), and socketserver's default handler prints a full traceback for it.
# The request itself already finished, so these say nothing worth reading.
QUIET_ERRORS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        kind = sys.exc_info()[0]
        if kind is not None and issubclass(kind, QUIET_ERRORS):
            return
        super().handle_error(request, client_address)


class Context:
    """What every request handler gets: settings, the job queue, the scans."""

    def __init__(self, config: Config):
        config.ensure_dirs()
        self.config = config
        self.jobs = JobQueue(
            build_runners(config),
            jobs_dir=config.jobs_dir,
            max_designs=config.max_designs,
        )
        self.scans = ScanStore(config.scans_dir)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".pdb": "chemical/x-pdb; charset=utf-8",
    ".ent": "chemical/x-pdb; charset=utf-8",
    ".cif": "chemical/x-cif; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}

COMPRESSIBLE = (".js", ".css", ".html", ".json", ".svg", ".pdb", ".cif", ".ent", ".txt")
GZIP_THRESHOLD = 8192
# Content types whose bodies are compressed already.
ALREADY_COMPRESSED = ("application/gzip", "application/x-gzip", "application/zip",
                      "image/", "video/", "audio/")


class Handler(BaseHTTPRequestHandler):
    server_version = "proteinCAD"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- plumbing

    @property
    def context(self):
        return self.server.context

    @property
    def config(self):
        return self.server.context.config

    def log_message(self, fmt, *args):
        if self.config.verbose:
            sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, body: bytes, status: int = 200, content_type: str = "text/plain", headers=None):
        extra = dict(headers or {})
        # Compressing something that is already compressed costs CPU on both
        # ends and saves nothing. An EMDB map is served as the .map.gz it
        # arrived as, and gzipping that again would make the browser unwrap one
        # layer it did not need before finding the one it did.
        compressible = not content_type.startswith(ALREADY_COMPRESSED)
        if (compressible and len(body) > GZIP_THRESHOLD
                and "gzip" in self.headers.get("Accept-Encoding", "")):
            body = gzip.compress(body, 6)
            extra["Content-Encoding"] = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # A handler that knows better wins: a map is tens of megabytes and does
        # not change, so re-fetching it on every reload is worth avoiding.
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except QUIET_ERRORS:
                # Reader went away mid-response (navigated off a large file).
                self.close_connection = True

    def _error(self, message: str, status: int = 400):
        self._send(
            json.dumps({"error": message}).encode("utf-8"),
            status=status,
            content_type="application/json",
        )

    # -------------------------------------------------------------- methods

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path.startswith("/api/"):
            return self._api(path, parsed.query, b"")
        return self._static(path)

    def do_HEAD(self):
        return self.do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/api/"):
            return self._error("POST is only supported under /api/", 404)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        return self._api(path, parsed.query, body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ----------------------------------------------------------------- work

    def _api(self, path: str, query: str, body: bytes):
        request = api.Request(
            method=self.command,
            path=path,
            query={k: v[0] for k, v in parse_qs(query).items()},
            body=body,
            context=self.context,
        )
        try:
            response = api.dispatch(request)
        except api.ApiError as error:
            return self._error(str(error), error.status)
        except Exception as error:  # a handler blew up: report it, keep serving
            if self.config.verbose:
                import traceback
                traceback.print_exc()
            return self._error(f"{type(error).__name__}: {error}", 500)
        return self._send(
            response.body,
            status=response.status,
            content_type=response.content_type,
            headers=response.headers,
        )

    def _static(self, path: str):
        if path.startswith("/data/"):
            base = self.config.data_dir
            relative = path[len("/data/"):]
        else:
            base = self.config.web_dir
            relative = path.lstrip("/") or "index.html"

        target = (base / relative).resolve()
        if not str(target).startswith(str(base.resolve())):
            return self._error("forbidden", 403)
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return self._error(f"not found: {path}", 404)

        content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        try:
            body = target.read_bytes()
        except OSError as error:
            return self._error(str(error), 500)
        return self._send(body, content_type=content_type)


def serve(
    host: str | None = None,
    port: int | None = None,
    web_dir: Path | None = None,
    data_dir: Path | None = None,
    open_browser: bool = True,
    verbose: bool | None = None,
    ready: threading.Event | None = None,
    config: Config | None = None,
) -> ThreadingHTTPServer:
    """Start the server and block until interrupted.

    Settings come from the environment (see config.py); anything passed here
    overrides them, which is how the command line flags work.
    """
    config = config or from_env(
        host=host, port=port, web_dir=web_dir, data_dir=data_dir, verbose=verbose
    )
    if not (config.web_dir / "index.html").is_file():
        raise SystemExit(f"no web app found at {config.web_dir}")

    httpd = Server((config.host, config.port), Handler)
    httpd.context = Context(config)

    url = f"http://{config.host}:{httpd.server_address[1]}/"

    # A checkout is often both the thing being developed and the thing that was
    # deployed, and config.json belongs to the deployment: it tells the viewer
    # to call an API Gateway instead of whatever is serving the page. Served
    # from here that is almost never what was meant -- this process answers
    # nothing, and every route it has that the deployment has not reads as a
    # missing feature. It is worth a line at startup, because the alternative is
    # finding out from a panel that says a feature does not exist.
    deployment_config = config.web_dir / "config.json"
    if deployment_config.is_file():
        try:
            target = json.loads(deployment_config.read_text()).get("api", "")
        except (OSError, json.JSONDecodeError):
            target = ""
        print()
        print(f"! {deployment_config} is present.")
        print(f"  The browser will call {target or 'the API named in it'}, not this server,")
        print("  so anything this server has and that deployment does not will look missing.")
        url = f"{url}?local=1"
        print(f"  Opening with ?local=1, which ignores it: {url}")
        print()

    print(f"proteinCAD running at {url}")
    print(f"design runners: {', '.join(httpd.context.jobs.runner_names())}")
    if not config.compute_url:
        print("no compute endpoint set (PROTEINCAD_COMPUTE_URL) -- mock runner only")
    print("press ctrl-c to stop")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    if ready is not None:
        ready.set()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return httpd
