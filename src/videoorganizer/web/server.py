"""Flask app for browsing, queueing, and exporting a media library.

Serves from the local machine by default. Every path the browser can reach is
resolved against the library root and refused if it escapes.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

from flask import Flask, abort, g, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from .. import db, export, media, search, thumbs

HEIF_EXTS = {".heic", ".heif"}
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def create_app(library) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["LIBRARY"] = library

    # ── Plumbing ──────────────────────────────────────────────────────────

    def get_conn() -> sqlite3.Connection:
        if "conn" not in g:
            g.conn = db.connect(library.database)
        return g.conn

    @app.teardown_appcontext
    def close_conn(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.errorhandler(HTTPException)
    def handle_http_error(exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": exc.description or exc.name, "status": exc.code}), exc.code
        return exc

    @app.errorhandler(Exception)
    def handle_error(exc):
        if request.path.startswith("/api/"):
            app.logger.exception("API error on %s", request.path)
            return jsonify({"error": str(exc) or "Internal server error", "status": 500}), 500
        raise exc

    def is_loopback() -> bool:
        return (request.remote_addr or "").strip() in LOOPBACK

    def enrich(item: dict, conn) -> dict:
        item["live_photo_mov"] = media.find_live_photo(conn, item)
        item["owner_name"] = item.get("person") or "Unknown"
        return item

    # ── Pages ─────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            title=library.web.title,
            kicker=library.web.kicker,
            default_export_dir=str(library.exports_dir / "recap-01"),
        )

    # ── API ───────────────────────────────────────────────────────────────

    @app.route("/api/stats")
    def api_stats():
        conn = get_conn()
        overview = db.stats(conn)
        return jsonify(
            {
                "people": [entry["person"] for entry in overview["by_person"]],
                "dates": [entry["date"] for entry in overview["by_date"]],
                "total": overview["total"],
                "tagged": overview["tagged"],
                "title": library.web.title,
                "kicker": library.web.kicker,
            }
        )

    @app.route("/api/search")
    def api_search():
        conn = get_conn()
        try:
            page = max(1, int(request.args.get("page", 1)))
            per = min(500, max(1, int(request.args.get("per", 60))))
        except ValueError:
            abort(400, "page and per must be integers")

        results = search.search(
            conn,
            query=request.args.get("q", ""),
            person=request.args.get("person", "").strip() or None,
            date=request.args.get("date", "").strip() or None,
            file_type=request.args.get("type", "").strip() or None,
            limit=per,
            offset=(page - 1) * per,
        )
        return jsonify([enrich(item, conn) for item in results])

    @app.route("/api/export", methods=["POST"])
    def api_export():
        payload = request.get_json(silent=True) or {}
        queue = payload.get("items") or []
        if not queue:
            return jsonify({"error": "Choose at least one item before exporting."}), 400

        try:
            export_dir = export.resolve_export_dir(
                library,
                payload.get("export_dir") or "",
                allow_outside=is_loopback(),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        result = export.export_queue(library, get_conn(), queue, export_dir)
        return jsonify(result), (200 if result["written"] else 500)

    @app.route("/thumb/<int:media_id>")
    def thumb(media_id: int):
        path = library.thumb_path(media_id)
        if not path.exists():
            row = get_conn().execute(
                "SELECT rel_path FROM media WHERE id = ?", (media_id,)
            ).fetchone()
            if not row:
                abort(404)
            try:
                source = library.resolve(row["rel_path"])
            except ValueError:
                abort(403)
            if not thumbs.generate(source, path, library.video_exts):
                abort(404)
        return send_file(path, mimetype="image/jpeg")

    @app.route("/media/<path:rel_path>")
    def serve_media(rel_path: str):
        try:
            path = library.resolve(rel_path)
        except ValueError:
            abort(403)
        if not path.exists():
            abort(404)

        # Browsers cannot display HEIC, so convert on the fly.
        if path.suffix.lower() in HEIF_EXTS:
            try:
                image = thumbs.load_image(path).convert("RGB")
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=90)
                buffer.seek(0)
                return send_file(
                    buffer, mimetype="image/jpeg", download_name=f"{path.stem}.jpg"
                )
            except Exception:
                app.logger.exception("HEIC conversion failed for %s", path.name)
                abort(500)

        return send_file(path, conditional=True)

    return app


def run(library, host: str | None = None, port: int | None = None, debug: bool = False) -> None:
    app = create_app(library)
    host = host or library.web.host
    port = port or library.web.port
    shown = "localhost" if host in {"127.0.0.1", "0.0.0.0"} else host
    print(f"{library.web.title} running at http://{shown}:{port}")
    app.run(host=host, port=port, debug=debug)
