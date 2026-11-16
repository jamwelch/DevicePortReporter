"""Local-only Flask web UI for the Device Port Reporter tool.

Run with:

    python device-port-reporter/web/app.py

By default the server binds to 127.0.0.1:5173 (loopback only — nothing
is exposed on the network) and auto-opens the user's default browser.
Flask's debug / reloader / werkzeug stack trace page is kept disabled
to avoid leaking source code or allowing remote code execution via the
debugger pin.

PRIVACY GUARANTEE (restricted-data handling)
--------------------------------------------
Uploaded configurations, parsed reports, and generated XLSX files are
**never** written to disk, logged to a file, or transmitted anywhere
outside the loopback interface.  Specifically:

  * Uploaded ``show-run`` text is read into memory via
    ``request.files[...].read()`` and immediately handed to the parser.
  * The XLSX workbook is written to an in-memory ``io.BytesIO`` and
    streamed directly to the browser via ``send_file``.
  * No ``tempfile``, ``Path.write_*``, database, or cache is used.
  * Responses carry ``Cache-Control: no-store`` so neither the browser
    nor any intermediary may retain a copy.
  * The Werkzeug access log (request line only, no bodies) can be
    silenced entirely with ``--quiet`` for maximum discretion.
  * No outbound HTTP client, no CDN, no telemetry, no error reporting
    service — the application has zero network egress.

If you modify this file, keep the audit-friendly invariant: anything
derived from a user-supplied config must stay in memory for the
lifetime of the request and be eligible for GC immediately after.
"""

from __future__ import annotations

import argparse
import io
import sys
import webbrowser
from pathlib import Path
from threading import Timer

# Allow ``python device-port-reporter/web/app.py`` from any cwd by
# adding the parent directory (which contains device_port_reporter.py)
# to sys.path.
_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from flask import (  # noqa: E402  (import after sys.path adjust)
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
)
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge  # noqa: E402

from device_port_reporter import (  # noqa: E402
    build_report_payload,
    parse_config,
    write_xlsx,
)


# 20 MB upload cap — matches the CLI's _read_input() cap exactly so the
# two front-ends behave identically.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_MB = MAX_UPLOAD_BYTES // (1024 * 1024)

# Only accept these extensions.  We still validate content below; the
# extension check is purely for the file-picker's accept hint and a
# friendlier error.
ALLOWED_EXTENSIONS = {".txt", ".cfg", ".conf", ".log", ".ios", ".nxos", ""}


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    # ------------------------------------------------------------------
    # Response hardening: no caching anywhere for API responses,
    # and conservative security headers on every response.
    #
    # Why no-store (not just no-cache)?  "no-cache" still permits
    # intermediaries/browsers to cache the entity and re-validate; the
    # customer's requirement is that restricted data never persists, so
    # we use "no-store" + "must-revalidate" to forbid any copy.
    # ------------------------------------------------------------------
    @app.after_request
    def _apply_security_headers(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate, private, max-age=0"
            )
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        # Permissive CSP for a same-origin single-page app: scripts and
        # styles come only from /static; no inline scripts; no external
        # anything.  This also kills third-party pixels / telemetry if
        # anyone ever adds them by accident.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "form-action 'self'; "
            "base-uri 'self'; "
            "frame-ancestors 'none';"
        )
        return response

    # ------------------------------------------------------------------
    # Error handlers — return JSON for /api/* and HTML elsewhere.
    # ------------------------------------------------------------------
    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        if request.path.startswith("/api/"):
            return (
                jsonify({"error": exc.description or exc.name}),
                exc.code or 500,
            )
        return exc

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(_exc):
        return (
            jsonify({"error": f"Configuration file exceeds {MAX_UPLOAD_MB} MB limit."}),
            413,
        )

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html", max_mb=MAX_UPLOAD_MB)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/api/parse")
    def api_parse():
        text = _get_config_text()
        interfaces = parse_config(text)
        payload = build_report_payload(interfaces)
        return jsonify(payload)

    @app.post("/api/download")
    def api_download():
        text = _get_config_text()
        interfaces = parse_config(text)
        buf = io.BytesIO()
        write_xlsx(interfaces, buf)
        buf.seek(0)
        return send_file(
            buf,
            mimetype=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            as_attachment=True,
            download_name="interfaces.xlsx",
            max_age=0,
        )

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_config_text() -> str:
    """Return the uploaded config text.

    Accepts either:
      * A multipart ``config`` file field (preferred, used by the UI
        and ``curl -F``).
      * A ``config_text`` form field (used by the UI's download button
        so the user doesn't have to re-upload the file).

    Raises an HTTP 400/413 (handled above) on malformed input.
    """
    from flask import abort

    if "config" in request.files and request.files["config"].filename:
        file_storage = request.files["config"]
        filename = file_storage.filename or ""
        # Lightweight extension check — informational only.
        suffix = Path(filename).suffix.lower()
        if suffix and suffix not in ALLOWED_EXTENSIONS:
            abort(
                400,
                description=(
                    f"Unsupported file type '{suffix}'. "
                    "Upload a plain-text show-run capture "
                    "(.txt, .cfg, .conf, .log, .nxos, .ios)."
                ),
            )
        raw = file_storage.read()
    elif request.form.get("config_text"):
        raw = request.form["config_text"].encode("utf-8")
    else:
        abort(400, description="No configuration file provided.")

    if not raw:
        abort(400, description="Uploaded file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        abort(413, description=f"Config exceeds {MAX_UPLOAD_MB} MB limit.")

    # Decode defensively.  Cisco captures occasionally contain non-UTF-8
    # bytes (e.g. from a copy-paste out of PuTTY) so we never raise on
    # decode — we substitute replacement chars and let the parser skip
    # over garbled lines.
    return raw.decode("utf-8", errors="replace")


def _open_browser(url: str) -> None:
    try:
        webbrowser.open_new_tab(url)
    except Exception:
        # The CLI prints the URL already; opening a browser is best-effort.
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="device-port-reporter-web",
        description=("Launch the local-only Device Port Reporter web UI. "
                     "Binds to 127.0.0.1 by default. Restricted-data safe: "
                     "nothing is written to disk or sent off-host."),
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1 = loopback only). "
                             "Override at your own risk.")
    parser.add_argument("--port", type=int, default=5173,
                        help="Bind port (default: 5173).")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not auto-open a browser window.")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Suppress the Werkzeug access log entirely. "
                             "The log never contains upload bodies, only "
                             "request lines, but you can silence it for "
                             "extra discretion.")
    args = parser.parse_args(argv)

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"[warn] Binding to {args.host!r}; the UI will be reachable from "
            "other hosts. Only do this on a trusted network.",
            file=sys.stderr,
        )

    if args.quiet:
        # Silence the WSGI access log (never contained upload bodies, only
        # request lines like 'POST /api/parse 200 -') but kill it anyway
        # for customers with the strictest discretion requirements.
        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

    app = create_app()
    url = f"http://{args.host}:{args.port}"
    print(f"Device Port Reporter web UI running on {url}")
    print("Privacy: restricted-data safe — no uploads, reports, or XLSX "
          "files are written to disk or sent off this host.")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        Timer(1.0, _open_browser, args=[url]).start()

    # debug/reloader off: the Werkzeug debugger must never be enabled on
    # a web-facing process without a PIN; keeping it off removes that
    # attack surface entirely.
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
