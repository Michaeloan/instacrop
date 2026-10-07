"""Local image-processing service used inside the Windows desktop window."""
from __future__ import annotations
import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import threading
import urllib.parse
import webbrowser
import zipfile
import numpy as np
from PIL import Image
from scanner import Settings, detect, find_inner, format_hints, png_bytes, read_scans, safe_stem, validate_photo, preview_image
from rendering import render_photo
from restoration import defaults

STATIC = Path(__file__).parent / "web"
MAX_UPLOAD = 80 * 1024 * 1024
MAX_JSON = 6 * 1024 * 1024
PROJECT_VERSION = 1


def thumbnail_data(image, size=900):
    pil = Image.fromarray(image) if isinstance(image, np.ndarray) else image.copy()
    pil.thumbnail((size, size), Image.Resampling.LANCZOS)
    return "data:image/png;base64," + base64.b64encode(png_bytes(pil)).decode("ascii")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, workspace_root=None):
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        self.jobs, self.sources = {}, {}
        self.lock = threading.RLock()
        self.workspace_root = Path(workspace_root or os.environ.get("POLASCAN_DATA_DIR") or (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PolaScanCrop" / "workspace"))
        self.work = None

    def service(self):
        with self.lock:
            if self.work is None:
                from work_service import WorkService
                self.work = WorkService(self.workspace_root)
            return self.work

    def server_close(self):
        if self.work is not None:
            self.work.close()
        super().server_close()

    def store(self, scan, source):
        job_id = secrets.token_urlsafe(20)
        with self.lock:
            while len(self.jobs) >= 3:
                expired = next(iter(self.jobs))
                self.jobs.pop(expired); self.sources.pop(expired, None)
            self.jobs[job_id], self.sources[job_id] = scan, source
        return job_id

    def load(self, job_id):
        if self.work is not None:
            try:
                return self.work.workspace.read_page(job_id)
            except (ValueError, KeyError):
                pass
        with self.lock:
            scan, source = self.jobs.get(job_id), self.sources.get(job_id)
        if scan is None:
            raise ValueError("扫描任务已过期，请重新打开扫描图或项目")
        return scan, source


def decode_scan(source, name, page=1):
    if not 0 < len(source) <= MAX_UPLOAD:
        raise ValueError("单张扫描文件最大 80 MB")
    with Image.open(BytesIO(source)) as opened:
        if opened.format not in {"JPEG", "PNG", "TIFF", "BMP", "WEBP"}:
            raise ValueError("请选择 JPG、PNG、TIFF、BMP 或 WebP 扫描图")
        pages = getattr(opened, "n_frames", 1) if opened.format == "TIFF" else 1
    if not 1 <= page <= pages:
        raise ValueError("页码超出了扫描文件页数")
    scan = next(s for s in read_scans(BytesIO(source), name) if s.page == page)
    return scan, pages


def scan_response(server, scan, source, pages, photos):
    job_id = server.store(scan, source)
    return {"job": job_id, "name": scan.name, "width": scan.image.shape[1], "height": scan.image.shape[0],
            "dpi": scan.dpi, "page": scan.page, "pages": pages, "photos": [p.as_dict(scan.dpi) for p in photos]}


def export_options(data):
    occupancy, trim = float(data.get("occupancy", .78)), float(data.get("trim", 0))
    if not .2 <= occupancy <= .95 or not 0 <= trim <= 50:
        raise ValueError("请检查中央相纸大小与收边像素")
    return occupancy, trim


def photos_from_data(scan, data):
    records = data.get("photos", [])
    if not isinstance(records, list) or not 1 <= len(records) <= 100:
        raise ValueError("请选择 1–100 张照片")
    return [validate_photo(p, scan.image.shape) for p in records]


def build_export(scan, photos, occupancy=.78, trim=0, friendly_names=False):
    if not any(p.enabled for p in photos):
        raise ValueError("没有勾选要导出的照片")
    stream = BytesIO()
    modes = {"paper": "带白边", "image": "照片画面", "composition": "背景成图"}
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for folder in modes.values():
            archive.writestr(folder + "/", b"")
        for i, photo in enumerate(photos, 1):
            if not photo.enabled:
                continue
            rendered = render_photo(scan, photo, trim, occupancy)
            for mode, img in rendered.images.items():
                filename = f"{modes[mode]}/{i:03d}_{modes[mode]}.png"
                archive.writestr(filename, png_bytes(img, scan.dpi if mode != "composition" else None))
    return stream.getvalue()


def save_project(server, data):
    scan, source = server.load(data.get("job"))
    photos = photos_from_data(scan, data)
    occupancy, trim = export_options(data)
    metadata = {"version": PROJECT_VERSION, "source_name": scan.name, "page": scan.page,
                "source_sha256": hashlib.sha256(source).hexdigest(), "occupancy": occupancy, "trim": trim,
                "photos": [p.as_dict(scan.dpi) for p in photos]}
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("project.json", json.dumps(metadata, ensure_ascii=False))
        archive.writestr("source.bin", source)
    return stream.getvalue()


def load_project(server, body):
    try:
        with zipfile.ZipFile(BytesIO(body)) as archive:
            if sorted(archive.namelist()) != ["project.json", "source.bin"]:
                raise ValueError("项目文件内容不正确")
            if archive.getinfo("source.bin").file_size > MAX_UPLOAD or archive.getinfo("project.json").file_size > MAX_JSON:
                raise ValueError("项目文件内容过大")
            meta = json.loads(archive.read("project.json"))
            source = archive.read("source.bin")
    except (zipfile.BadZipFile, RuntimeError, KeyError) as exc:
        raise ValueError("无法读取项目文件") from exc
    if not isinstance(meta, dict) or meta.get("version") != PROJECT_VERSION:
        raise ValueError("不支持这个版本的项目文件")
    if hashlib.sha256(source).hexdigest() != meta.get("source_sha256"):
        raise ValueError("项目中的原图校验失败")
    scan, pages = decode_scan(source, Path(meta["source_name"]).name, int(meta["page"]))
    photos = photos_from_data(scan, meta)
    occupancy, trim = export_options(meta)
    return {**scan_response(server, scan, source, pages, photos), "occupancy": occupancy, "trim": trim}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, data, kind="application/json; charset=utf-8", status=200, headers=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def host_valid(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def do_GET(self):
        if not self.host_valid():
            self.send({"error": "只接受本机访问"}, status=403); return
        url = urllib.parse.urlparse(self.path)
        if url.path.startswith(("/api/workspace", "/api/tasks", "/api/artifacts/")):
            try:
                from work_service import send_file
                query = urllib.parse.parse_qs(url.query)
                service = self.server.service()
                if url.path == "/api/workspace":
                    self.send(service.snapshot())
                elif url.path == "/api/tasks":
                    self.send({"tasks": service.queue.snapshot()})
                elif url.path == "/api/workspace/preview":
                    kind = query.get("kind", ["paper"])[0]
                    if kind not in ("paper", "image", "composition"):
                        raise ValueError("未知成图类型")
                    send_file(self, service.preview(query["photo_id"][0], kind))
                elif url.path.startswith("/api/artifacts/"):
                    if self.headers.get("X-Session-Token") != self.server.token and query.get("token", [""])[0] != self.server.token:
                        self.send({"error": "请从本机软件读取媒体"}, status=403); return
                    send_file(self, service.artifacts[url.path.rsplit("/", 1)[1]], True)
                else:
                    self.send({"error": "没有这个页面"}, status=404)
            except (KeyError, ValueError, OSError, StopIteration) as exc:
                self.send({"error": str(exc)}, status=404)
            return
        if url.path == "/api/session":
            self.send({"token": self.server.token})
        elif url.path == "/api/crop-tile":
            try:
                query = urllib.parse.parse_qs(url.query)
                if self.headers.get("X-Session-Token") != self.server.token and query.get("token", [""])[0] != self.server.token:
                    self.send({"error": "请从本机软件读取原图"}, status=403); return
                scan, _ = self.server.load(query.get("job", [""])[0])
                x, y = float(query["x"][0]), float(query["y"][0])
                size = int(query.get("size", ["256"])[0])
                if not np.isfinite([x, y]).all() or not 32 <= size <= 512:
                    raise ValueError("放大镜坐标或范围不正确")
                height, width = scan.image.shape[:2]
                if not 0 <= x < width or not 0 <= y < height:
                    raise ValueError("放大镜坐标超出原图")
                left, top = round(x) - size // 2, round(y) - size // 2
                right, bottom = left + size, top + size
                tile = Image.new("RGB", (size, size), "white")
                box = (max(0, left), max(0, top), min(width, right), min(height, bottom))
                tile.paste(Image.fromarray(scan.image[box[1]:box[3], box[0]:box[2]]), (box[0] - left, box[1] - top))
                self.send(png_bytes(tile), "image/png")
            except (ValueError, KeyError, OSError) as exc:
                self.send({"error": str(exc)}, status=400)
        elif url.path == "/api/scan-preview":
            try:
                scan, _ = self.server.load(urllib.parse.parse_qs(url.query).get("job", [""])[0])
                thumb = Image.fromarray(scan.image); thumb.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
                stream = BytesIO(); thumb.save(stream, "JPEG", quality=93)
                self.send(stream.getvalue(), "image/jpeg")
            except ValueError as exc:
                self.send({"error": str(exc)}, status=404)
        else:
            allowed = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                       "/restoration.js": ("restoration.js", "text/javascript; charset=utf-8"), "/gallery.js":("gallery.js","text/javascript; charset=utf-8"), "/workspace.js":("workspace.js","text/javascript; charset=utf-8"), "/crop.js":("crop.js","text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            entry = allowed.get(url.path)
            self.send((STATIC / entry[0]).read_bytes(), entry[1]) if entry else self.send({"error": "没有这个页面"}, status=404)

    def do_POST(self):
        try:
            if not self.host_valid() or self.headers.get("X-Session-Token") != self.server.token:
                self.send({"error": "请从本机软件发起操作"}, status=403); return
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                self.send({"error": "不接受其他来源的请求"}, status=403); return
            url = urllib.parse.urlparse(self.path)
            if url.path.startswith(("/api/workspace/", "/api/tasks/")):
                service = self.server.service()
                query = urllib.parse.parse_qs(url.query)
                if url.path in ("/api/workspace/import", "/api/workspace/project-open"):
                    length = int(self.headers.get("Content-Length", "0"))
                    limit = 2 * 1024 ** 3 if url.path == "/api/workspace/project-open" else 1024 ** 3 if query.get("name", [""])[0].lower().endswith(".mov") else MAX_UPLOAD
                    if not 0 < length <= limit:
                        raise ValueError("文件过大或为空")
                    name = Path(query.get("name", ["upload.polascan" if url.path.endswith("project-open") else "upload.png"])[0]).name
                    upload = service.uploads / (secrets.token_hex(12) + "_" + name)
                    with upload.open("wb") as destination:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("上传未完成")
                            destination.write(chunk); remaining -= len(chunk)
                    if url.path == "/api/workspace/import":
                        self.send(service.import_paths([str(upload)], query.get("kind", ["scan"])[0], [name]))
                    elif url.path == "/api/workspace/project-open":
                        if any(t["state"] in ("running", "queued", "paused", "cancelling") for t in service.queue.snapshot()):
                            raise ValueError("先取消或完成当前任务，再打开项目")
                        service.workspace.open_project(upload); self.send(service.snapshot())
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_JSON:
                    raise ValueError("操作参数过大")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("操作参数格式错误")
                self.send(service.post(url.path, data)); return
            limit = MAX_UPLOAD + MAX_JSON if url.path == "/api/project-open" else MAX_UPLOAD if url.path == "/api/scan" else MAX_JSON
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= limit:
                raise ValueError("上传文件或设置过大：扫描图最大 80 MB")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("读取未完成，请重试")
            if url.path == "/api/scan":
                query = urllib.parse.parse_qs(url.query)
                scan, pages = decode_scan(body, Path(query.get("name", ["scan"])[0]).name, int(query.get("page", ["1"])[0]))
                settings = Settings(min_area=float(query.get("min_area", [".002"])[0]), sensitivity=float(query.get("sensitivity", ["1"])[0]))
                photos = detect(scan, settings)
                for photo in photos:
                    photo.restoration = {**defaults(), "enabled":True, "sensitivity":25, "max_diameter":8}
                self.send(scan_response(self.server, scan, body, pages, photos)); return
            if url.path == "/api/project-open":
                self.send(load_project(self.server, body)); return
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("操作参数格式不正确")
            scan, source = self.server.load(data.get("job"))
            if url.path == "/api/redetect":
                scan, pages = decode_scan(source, scan.name, int(data.get("page", scan.page)))
                settings = Settings(min_area=float(data.get("min_area", .002)), sensitivity=float(data.get("sensitivity", 1)))
                photos = detect(scan, settings)
                for photo in photos:
                    photo.restoration = {**defaults(), "enabled":True, "sensitivity":25, "max_diameter":8}
                self.send(scan_response(self.server, scan, source, pages, photos))
            elif url.path == "/api/results":
                photos = photos_from_data(scan, data)
                occupancy, trim = export_options(data)
                results = []
                for photo in photos:
                    try:
                        rendered = render_photo(scan, photo, trim, occupancy)
                        results.append({"images":{key:thumbnail_data(value,440) for key,value in rendered.images.items()},"warnings":photo.warnings,"ready":True})
                    except ValueError as exc:
                        raw = validate_photo({**photo.as_dict(),"inner":None,"restoration":{},"presentation":{"trim":0}},scan.image.shape)
                        rendered = render_photo(scan,raw,0,occupancy)
                        results.append({"images":{"paper":thumbnail_data(rendered.images["paper"],440)},"warnings":[str(exc)],"ready":False})
                self.send({"photos":results})
            elif url.path in ("/api/crop-analysis", "/api/crop-snap", "/api/crop-refine"):
                from crop_detection import analyze_photo, refine_edges, snap_point
                photo = validate_photo(data["photo"], scan.image.shape)
                if url.path == "/api/crop-analysis":
                    self.send(analyze_photo(scan.image, photo))
                elif url.path == "/api/crop-snap":
                    target = data.get("target", "outer")
                    if target not in ("outer", "inner") or getattr(photo, target) is None:
                        raise ValueError("请先框选四角")
                    index = data.get("index")
                    if isinstance(index, bool) or not isinstance(index, int) or index not in range(4):
                        raise ValueError("角点编号不正确")
                    point = np.asarray(data.get("point"), dtype=np.float32)
                    radius = float(data.get("radius", 12))
                    if point.shape != (2,) or not np.isfinite(point).all() or not np.isfinite(radius) or not 1 <= radius <= 40:
                        raise ValueError("吸附坐标或范围不正确")
                    height, width = scan.image.shape[:2]
                    if not 0 <= point[0] <= width - 1 or not 0 <= point[1] <= height - 1:
                        raise ValueError("角点超出了扫描图范围")
                    result = snap_point(scan.image, getattr(photo, target), index, point, radius, paper=target == "outer")
                    self.send({"point": np.asarray(result).tolist(), "snapped": bool(np.linalg.norm(result-point) > .1)})
                else:
                    photo.outer, _ = refine_edges(scan.image, photo.outer, paper=True, radius=16)
                    if photo.inner is not None:
                        photo.inner, _ = refine_edges(scan.image, photo.inner, radius=12)
                    photo = validate_photo(photo.as_dict(), scan.image.shape)
                    analysis = analyze_photo(scan.image, photo)
                    photo.crop_analysis = analysis
                    self.send({"photo": photo.as_dict(scan.dpi), "analysis": analysis})
            elif url.path == "/api/inner":
                photo = validate_photo({**data["photo"], "inner": None}, scan.image.shape)
                photo.inner = find_inner(scan.image, photo.outer)
                photo.warnings = [] if photo.inner is not None else ["未找到可靠的内部画面，请手动框选四角"]
                self.send(photo.as_dict(scan.dpi))
            elif url.path == "/api/preview":
                photo = validate_photo(data["photo"], scan.image.shape)
                occupancy, trim = export_options(data)
                result = render_photo(scan, photo, trim, occupancy)
                overlay = np.zeros((*result.mask.shape, 4), np.uint8)
                overlay[:, :, :3] = (170, 56, 220); overlay[:, :, 3] = np.where(result.mask > 0, 125, 0).astype(np.uint8)
                self.send({"images": {k: thumbnail_data(v) for k, v in result.images.items()},
                           "original_images": {k: thumbnail_data(v) for k, v in result.original.items()},
                           "repair": {"original": thumbnail_data(result.original["paper"], 1600), "result": thumbnail_data(result.images["paper"], 1600),
                                      "mask": thumbnail_data(Image.fromarray(overlay), 1600), "paper_size": [result.mask.shape[1], result.mask.shape[0]],
                                      "source_to_paper": result.source_to_paper.tolist(), "stats": result.stats, "active": result.active},
                           "format_hints": format_hints(photo.outer, photo.inner, scan.dpi)})
            elif url.path in ("/api/export", "/api/project-save"):
                photos = photos_from_data(scan, data); occupancy, trim = export_options(data)
                project = url.path == "/api/project-save"
                content = save_project(self.server, data) if project else build_export(scan, photos, occupancy, trim, bool(data.get("friendly_names")))
                filename = safe_stem(scan.name) + (".polascan" if project else f"_page{scan.page}.zip")
                self.send(content, "application/zip", headers={"Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(filename)})
            else:
                self.send({"error": "没有这个操作"}, status=404)
        except (ValueError, KeyError, TypeError, OSError, Image.DecompressionBombError, StopIteration) as exc:
            self.send({"error": str(exc) or "无法读取扫描图或项目"}, status=400)
        except Exception as exc:
            print(f"处理失败: {type(exc).__name__}: {exc}")
            self.send({"error": "处理失败，请重新打开扫描图或查看日志"}, status=500)


def main():
    parser = argparse.ArgumentParser(description="相纸修复工作台开发预览服务")
    parser.add_argument("--port", type=int, default=8765); parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        server = LocalServer(("127.0.0.1", args.port))
    except OSError:
        server = LocalServer(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"开发预览: {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
