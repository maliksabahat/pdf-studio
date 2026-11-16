"""PDF Studio - local web application.

Run with:  python app.py
"""

from __future__ import annotations

import io
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from pathlib import Path

import pymupdf as fitz
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge

import engine

# When frozen with PyInstaller, code and static files live inside the bundle
# (a temporary folder for one-file builds), but the work folder must sit next
# to the EXE so it is writable and survives while the app is running.
if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    DATA_DIR = Path(sys.executable).resolve().parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    DATA_DIR = BUNDLE_DIR

STATIC_DIR = BUNDLE_DIR / "static"
WORK_DIR = DATA_DIR / "work"
UPLOAD_DIR = WORK_DIR / "uploads"
THUMB_DIR = WORK_DIR / "thumbs"
OUTPUT_DIR = WORK_DIR / "outputs"

for folder in (UPLOAD_DIR, THUMB_DIR, OUTPUT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

# A fresh session on every launch keeps the work folder from growing forever.
if os.environ.get("PDF_STUDIO_KEEP_WORK") != "1":
    for entry in WORK_DIR.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    for folder in (UPLOAD_DIR, THUMB_DIR, OUTPUT_DIR):
        folder.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
app.config["JSON_SORT_KEYS"] = False

UPLOADS: dict[str, dict] = {}
OUTPUTS: dict[str, dict] = {}
LOCK = threading.Lock()

MAX_PDF_PAGES = 1500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _json_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _save_upload(file_storage) -> tuple[str, Path, str]:
    original = file_storage.filename or "file"
    ext = Path(original).suffix.lower()
    uid = "u" + uuid.uuid4().hex
    path = UPLOAD_DIR / (uid + ext)
    file_storage.save(path)
    return uid, path, original


def _lookup(upload_id: str) -> dict | None:
    with LOCK:
        return UPLOADS.get(upload_id)


def _register_output(path: Path, name: str) -> str:
    token = "o" + uuid.uuid4().hex
    with LOCK:
        OUTPUTS[token] = {"path": str(path), "name": name}
    return token


def _open_pdf(path: Path) -> fitz.Document:
    doc = fitz.open(str(path))
    if doc.needs_pass:
        doc.close()
        raise ValueError("This PDF is password protected. Please remove the password first.")
    return doc


def _pdf_pages_meta(uid: str, path: Path) -> list[dict]:
    doc = _open_pdf(path)
    try:
        count = len(doc)
        if count == 0:
            raise ValueError("This PDF has no pages.")
        if count > MAX_PDF_PAGES:
            raise ValueError(f"This PDF has {count} pages. The limit is {MAX_PDF_PAGES}.")
        pages = []
        for index in range(count):
            page = doc[index]
            thumb_path = THUMB_DIR / f"{uid}_{index}_0.jpg"
            if not thumb_path.exists():
                thumb_path.write_bytes(engine.render_thumbnail(path, index, 0))
            pages.append({
                "index": index,
                "thumb": f"/api/thumb/{uid}/{index}",
                "w": round(page.rect.width, 1),
                "h": round(page.rect.height, 1),
            })
        return pages
    finally:
        doc.close()


def _image_meta(uid: str, path: Path) -> dict:
    data, width, height = engine.normalize_image_file(path)
    thumb_path = THUMB_DIR / f"{uid}_0_0.jpg"
    thumb_path.write_bytes(engine.render_thumbnail(path, 0, 0))
    return {"w": width, "h": height, "thumb": f"/api/thumb/{uid}/0"}


def _output_name(raw: str | None, fallback: str) -> str:
    name = engine.safe_filename(raw or fallback, fallback)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def _text_like(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:8192]
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            sample.decode("cp1252")
            return True
        except UnicodeDecodeError:
            return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.post("/api/pages/upload")
def pages_upload():
    files = request.files.getlist("files")
    if not files:
        return _json_error("No files were received.")

    result = []
    errors = []
    for storage in files:
        name = storage.filename or "file"
        ext = Path(name).suffix.lower()
        uid = None
        path = None
        try:
            if ext != ".pdf" and ext not in engine.IMAGE_EXTS:
                errors.append(f"{name}: only PDF and image files can be added here.")
                continue
            uid, path, original = _save_upload(storage)
            size = path.stat().st_size
            if size == 0:
                raise ValueError("The file is empty.")
            if ext == ".pdf":
                pages = _pdf_pages_meta(uid, path)
                with LOCK:
                    UPLOADS[uid] = {
                        "id": uid, "path": str(path), "name": original,
                        "ext": ext, "kind": "pdf", "size": size,
                        "page_count": len(pages), "pages": pages,
                    }
                result.append({
                    "id": uid, "name": original, "kind": "pdf", "size": size,
                    "pageCount": len(pages), "pages": pages,
                })
            else:
                meta = _image_meta(uid, path)
                page = {"index": 0, "thumb": meta["thumb"], "w": meta["w"], "h": meta["h"]}
                with LOCK:
                    UPLOADS[uid] = {
                        "id": uid, "path": str(path), "name": original,
                        "ext": ext, "kind": "image", "size": size,
                        "page_count": 1, "pages": [page],
                    }
                result.append({
                    "id": uid, "name": original, "kind": "image", "size": size,
                    "pageCount": 1, "pages": [page],
                })
        except Exception as exc:
            if path and path.exists():
                path.unlink(missing_ok=True)
            if uid:
                with LOCK:
                    UPLOADS.pop(uid, None)
            errors.append(f"{name}: {exc}")
    if not result and errors:
        return _json_error(" ".join(errors))
    return jsonify({"files": result, "errors": errors})


@app.post("/api/images/upload")
def images_upload():
    storage = request.files.get("file")
    if storage is None:
        return _json_error("No file was received.")
    name = storage.filename or "document.pdf"
    if Path(name).suffix.lower() != ".pdf":
        return _json_error("Please choose a PDF file.")
    uid, path, original = _save_upload(storage)
    try:
        pages = _pdf_pages_meta(uid, path)
        with LOCK:
            UPLOADS[uid] = {
                "id": uid, "path": str(path), "name": original, "ext": ".pdf",
                "kind": "pdf", "size": path.stat().st_size,
                "page_count": len(pages), "pages": pages,
            }
        preview = [page["thumb"] for page in pages]
        return jsonify({
            "id": uid, "name": original, "pageCount": len(pages),
            "pages": preview, "size": path.stat().st_size,
        })
    except Exception as exc:
        path.unlink(missing_ok=True)
        with LOCK:
            UPLOADS.pop(uid, None)
        return _json_error(str(exc))


@app.post("/api/mixed/upload")
def mixed_upload():
    files = request.files.getlist("files")
    if not files:
        return _json_error("No files were received.")
    result = []
    errors = []
    for storage in files:
        name = storage.filename or "file"
        ext = Path(name).suffix.lower()
        if ext in engine.LEGACY_OFFICE:
            errors.append(
                f"{name}: legacy {ext} files are not supported. "
                f"Open it in Office and save it as {ext}x first."
            )
            continue
        uid = None
        path = None
        try:
            uid, path, original = _save_upload(storage)
            size = path.stat().st_size
            if size == 0:
                raise ValueError("The file is empty.")
            kind = engine.file_kind(path)
            if kind == "unknown":
                if not _text_like(path):
                    raise ValueError(f"Unsupported file type: {ext or 'unknown'}")
                kind = "text"
            with LOCK:
                UPLOADS[uid] = {
                    "id": uid, "path": str(path), "name": original,
                    "ext": ext, "kind": kind, "size": size,
                }
            result.append({
                "id": uid, "name": original, "kind": kind, "ext": ext, "size": size,
            })
        except Exception as exc:
            if path and path.exists():
                path.unlink(missing_ok=True)
            if uid:
                with LOCK:
                    UPLOADS.pop(uid, None)
            errors.append(f"{name}: {exc}")
    if not result and errors:
        return _json_error(" ".join(errors))
    return jsonify({"items": result, "errors": errors})


@app.post("/api/pages/build")
def pages_build():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    options = payload.get("options") or {}
    if not items:
        return _json_error("Add some pages first.")
    try:
        doc = engine.build_pages_pdf(items, options, _lookup)
        name = _output_name(options.get("name"), "organized")
        path = OUTPUT_DIR / (uuid.uuid4().hex + ".pdf")
        doc.save(str(path), garbage=4, deflate=True)
        page_count = len(doc)
        doc.close()
        token = _register_output(path, name)
        return jsonify({"url": f"/api/download/{token}", "name": name, "pages": page_count})
    except Exception as exc:
        if not isinstance(exc, ValueError):
            traceback.print_exc()
        return _json_error(str(exc))


@app.post("/api/images/convert")
def images_convert():
    payload = request.get_json(silent=True) or {}
    upload_id = payload.get("uploadId")
    meta = _lookup(upload_id)
    if meta is None:
        return _json_error("Please upload a PDF first.")
    if meta["kind"] != "pdf":
        return _json_error("This file is not a PDF.")
    try:
        fmt = payload.get("format", "png")
        dpi = payload.get("dpi", 150)
        quality = payload.get("quality", 90)
        page_spec = payload.get("range", "")
        pages = engine.parse_page_range(page_spec, meta.get("page_count", 1))
        if len(pages) == 1:
            data, name = engine.pdf_page_image(
                meta["path"], pages[0], fmt=fmt, dpi=dpi, quality=quality,
                stem_name=Path(meta["name"]).stem,
            )
            suffix = Path(name).suffix.lower()
            path = OUTPUT_DIR / (uuid.uuid4().hex + suffix)
            path.write_bytes(data)
            token = _register_output(path, name)
            return jsonify({
                "url": f"/api/download/{token}", "name": name,
                "count": 1, "size": len(data), "single": True,
            })
        data, count, zip_name = engine.pdf_to_images_zip(
            meta["path"],
            fmt=fmt,
            dpi=dpi,
            quality=quality,
            page_spec=page_spec,
            stem_name=Path(meta["name"]).stem,
        )
        path = OUTPUT_DIR / (uuid.uuid4().hex + ".zip")
        path.write_bytes(data)
        token = _register_output(path, zip_name)
        return jsonify({
            "url": f"/api/download/{token}", "name": zip_name,
            "count": count, "size": len(data),
        })
    except Exception as exc:
        if not isinstance(exc, ValueError):
            traceback.print_exc()
        return _json_error(str(exc))


@app.get("/api/images/page/<uid>/<int:page_index>")
def images_page(uid: str, page_index: int):
    meta = _lookup(uid)
    if meta is None or meta.get("kind") != "pdf":
        abort(404)
    if page_index < 0 or page_index >= meta.get("page_count", 0):
        abort(404)
    try:
        dpi = int(request.args.get("dpi", "150"))
        quality = int(request.args.get("quality", "90"))
    except ValueError:
        dpi, quality = 150, 90
    try:
        data, name = engine.pdf_page_image(
            meta["path"], page_index,
            fmt=request.args.get("format", "png"),
            dpi=dpi, quality=quality,
            stem_name=Path(meta["name"]).stem,
        )
    except Exception:
        abort(404)
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp",
    }.get(Path(name).suffix.lstrip(".").lower(), "application/octet-stream")
    return send_file(io.BytesIO(data), mimetype=mime, as_attachment=True,
                     download_name=name, max_age=0)


@app.post("/api/mixed/build")
def mixed_build():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    options = payload.get("options") or {}
    if not items:
        return _json_error("Add some files first.")

    output = fitz.open()
    try:
        for item in items:
            upload_id = item.get("uploadId") if isinstance(item, dict) else item
            meta = _lookup(upload_id)
            if meta is None:
                raise ValueError("An uploaded file is missing. Please add it again.")
            try:
                doc = engine.mixed_file_to_doc(meta["path"], options)
            except Exception as exc:
                raise ValueError(f"{meta['name']}: {exc}") from exc
            try:
                output.insert_pdf(doc)
            finally:
                doc.close()

        if len(output) == 0:
            raise ValueError("The result is empty.")
        name = _output_name(options.get("name"), "mixed-document")
        path = OUTPUT_DIR / (uuid.uuid4().hex + ".pdf")
        output.save(str(path), garbage=4, deflate=True)
        page_count = len(output)
        output.close()
        token = _register_output(path, name)
        return jsonify({"url": f"/api/download/{token}", "name": name, "pages": page_count})
    except Exception as exc:
        output.close()
        if not isinstance(exc, ValueError):
            traceback.print_exc()
        return _json_error(str(exc))


@app.get("/api/thumb/<uid>/<int:page_index>")
def thumbnail(uid: str, page_index: int):
    meta = _lookup(uid)
    if meta is None:
        abort(404)
    if page_index < 0 or page_index >= meta.get("page_count", 1):
        abort(404)
    try:
        rotation = int(request.args.get("rot", "0")) % 360
    except ValueError:
        rotation = 0
    rotation = (rotation // 90) * 90
    full = request.args.get("full") == "1"
    if full:
        cache = THUMB_DIR / f"{uid}_{page_index}_{rotation}_full.jpg"
        target_width = 1600
    else:
        cache = THUMB_DIR / f"{uid}_{page_index}_{rotation}.jpg"
        target_width = 260
    if not cache.exists():
        try:
            cache.write_bytes(engine.render_thumbnail(
                meta["path"], page_index, rotation, target_width=target_width))
        except Exception:
            abort(404)
    return send_file(cache, mimetype="image/jpeg", max_age=3600, conditional=True)


def _mixed_preview(meta: dict, target_width: int) -> bytes:
    """Render the first page of a mixed-list file as a JPEG."""
    kind = meta.get("kind")
    path = meta["path"]
    if kind in ("pdf", "image"):
        return engine.render_thumbnail(path, 0, 0, target_width=target_width)

    # Office / text files are converted with the same defaults used by the
    # mixed builder, so the preview shows what the file will look like in the
    # finished PDF.
    options = {"pageSize": "a4", "orientation": "auto", "margin": 48}
    doc = engine.mixed_file_to_doc(path, options)
    try:
        if len(doc) == 0:
            raise ValueError("The file has no pages.")
        page = doc[0]
        zoom = target_width / max(page.rect.width, 1)
        zoom = max(0.05, min(zoom, 4.0))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("jpg", jpg_quality=82)
    finally:
        doc.close()


@app.get("/api/mixed/thumb/<uid>")
def mixed_thumbnail(uid: str):
    meta = _lookup(uid)
    if meta is None:
        abort(404)
    full = request.args.get("full") == "1"
    if full:
        cache = THUMB_DIR / f"{uid}_mixed_full.jpg"
        target_width = 1600
    else:
        cache = THUMB_DIR / f"{uid}_mixed.jpg"
        target_width = 260
    if not cache.exists():
        try:
            cache.write_bytes(_mixed_preview(meta, target_width))
        except Exception:
            abort(404)
    return send_file(cache, mimetype="image/jpeg", max_age=3600, conditional=True)


@app.get("/api/download/<token>")
def download(token: str):
    with LOCK:
        meta = OUTPUTS.get(token)
    if meta is None:
        abort(404)
    path = Path(meta["path"])
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=meta["name"], conditional=True)


@app.post("/api/shutdown")
def shutdown():
    if request.remote_addr not in ("127.0.0.1", "::1", None):
        abort(403)

    def stop():
        time.sleep(0.4)
        os._exit(0)

    threading.Thread(target=stop, daemon=True).start()
    return jsonify({"ok": True})


@app.errorhandler(RequestEntityTooLarge)
def too_large(_error):
    return _json_error("The file is too large. The limit is 500 MB.", 413)


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return _json_error("Not found.", 404)
    return send_from_directory(STATIC_DIR, "index.html")


@app.errorhandler(500)
def server_error(_error):
    return _json_error("Something went wrong on the server.", 500)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main() -> None:
    port = int(os.environ.get("PDF_STUDIO_PORT") or 0) or _free_port()
    url = f"http://127.0.0.1:{port}"
    print("=" * 62)
    print("  PDF Studio is running")
    print(f"  {url}")
    print("  Keep this window open. Close it to stop the app.")
    print("=" * 62)

    if os.environ.get("PDF_STUDIO_NO_BROWSER") != "1":
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()

    try:
        from waitress import serve

        serve(app, host="127.0.0.1", port=port, threads=8, channel_timeout=1800)
    except ImportError:
        app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
