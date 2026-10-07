"""Windows desktop entry point; no external browser or remote image service."""
from __future__ import annotations
import argparse
from io import BytesIO
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import secrets
from pathlib import Path
import sys
import threading
import time
import traceback
import zipfile
import tempfile

from app import LocalServer, build_export, export_options, photos_from_data, save_project

VERSION = "1.0.4"
TITLE = "InstaCrop 相纸扫描裁剪"


class DesktopAPI:
    def __init__(self, server):
        self._server = server
        self._window = None

    def _check_workspace_origin(self):
        origin = f"http://127.0.0.1:{self._server.server_port}/"
        if not self._window or not (self._window.get_current_url() or "").startswith(origin):
            raise ValueError("只能从软件内部打开或保存")
        return self._server.service()

    def import_workspace(self, kind="scan", folder=False):
        try:
            import webview
            service = self._check_workspace_origin()
            if kind not in ("scan", "photo"):
                raise ValueError("未知导入类型")
            if folder:
                chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
                if not chosen:
                    return {"cancelled": True}
                directory = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)
                allowed = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
                if kind == "photo":
                    allowed |= {".heic", ".heif"}
                paths = [p for p in sorted(directory.iterdir()) if p.is_file() and p.suffix.lower() in allowed]
            else:
                types = ("静态照片 (*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.tif;*.tiff;*.heic;*.heif)",) if kind == "photo" else ("扫描图片 (*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.tif;*.tiff)",)
                chosen = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=types)
                if not chosen:
                    return {"cancelled": True}
                paths = chosen
            if not paths:
                raise ValueError("文件夹里没有可导入的文件（不扫描子目录）")
            return service.import_paths(paths, kind)
        except Exception as exc:
            logging.exception("Workspace import failed")
            return {"error": str(exc)}

    def open_workspace_project(self):
        try:
            import webview
            service = self._check_workspace_origin()
            chosen = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=("相纸项目 (*.polascan)",))
            if not chosen:
                return {"cancelled": True}
            if any(t["state"] in ("queued", "running", "paused", "cancelling") for t in service.queue.snapshot()):
                raise ValueError("先取消或完成任务，再打开另一个项目")
            service.workspace.open_project(Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen))
            return service.snapshot()
        except Exception as exc:
            logging.exception("Project open failed")
            return {"error": str(exc)}

    def save_workspace(self, kind, data):
        try:
            import webview
            service = self._check_workspace_origin()
            if kind == "folder":
                chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
                if not chosen:
                    return {"cancelled": True}
                parent = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)
                destination = parent / "相纸批次"
                index = 2
                while destination.exists():
                    destination = parent / f"相纸批次_{index}"
                    index += 1
            elif kind in ("project", "export"):
                filename = {"project":"相纸批次-v2.polascan", "export":"相纸批次成图.zip"}[kind]
                file_type = "相纸项目 (*.polascan)" if kind == "project" else "传输包 (*.zip)"
                chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=filename, file_types=(file_type,))
                if not chosen:
                    return {"cancelled": True}
                destination = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)
                suffix = ".polascan" if kind == "project" else ".zip"
                if destination.suffix.lower() != suffix:
                    destination = destination.with_name(destination.name + suffix)
            else:
                raise ValueError("未知保存类型")
            return service.save(kind, data, destination)
        except Exception as exc:
            logging.exception("Workspace save failed")
            return {"error": str(exc)}

    def save_artifact(self, kind, data):
        try:
            import webview
            if kind not in ("project", "export"):
                raise ValueError("未知保存类型")
            origin = f"http://127.0.0.1:{self._server.server_port}"
            if not self._window or not (self._window.get_current_url() or "").startswith(origin + "/"):
                raise ValueError("只能从软件内部保存")
            scan, _ = self._server.load(data.get("job"))
            from scanner import safe_stem
            suffix = ".polascan" if kind == "project" else ".zip"
            filename = safe_stem(scan.name) + suffix
            chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=filename,
                                                    file_types=("相纸项目 (*.polascan)",) if kind == "project" else ("图片结果 ZIP (*.zip)",))
            if not chosen:
                return {"cancelled": True}
            destination = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)
            if destination.suffix.lower() != suffix:
                destination = destination.with_name(destination.name + suffix)
            occupancy, trim = export_options(data)
            photos = photos_from_data(scan, data)
            content = save_project(self._server, data) if kind == "project" else build_export(scan, photos, occupancy, trim, bool(data.get("friendly_names")))
            # The path comes exclusively from the user's native Save dialog.
            temporary = destination.with_name(destination.name + "." + secrets.token_hex(6) + ".polascan-writing")
            try:
                temporary.write_bytes(content)
                temporary.replace(destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
            return {"cancelled": False, "filename": destination.name}
        except Exception as exc:
            logging.exception("Save failed")
            return {"error": str(exc)}


def self_test(report_path):
    import cv2
    import numpy as np
    from PIL import Image
    from scanner import Photo, Scan, order_quad, validate_photo
    from rendering import render_photo
    from restoration import defaults
    image = np.full((180, 160, 3), 246, np.uint8)
    image[20:130, 20:140] = (80, 110, 140)
    image[140:144, 65:69] = 25
    scan = Scan(image, "self-test.png")
    photo = Photo(order_quad([[0,0],[159,0],[159,179],[0,179]]), order_quad([[20,20],[139,20],[139,129],[20,129]]))
    photo.restoration = {**defaults(), "enabled":True, "sensitivity":60}
    rendered = render_photo(scan, photo)
    assert rendered.stats["masked_pixels"] > 0
    assert rendered.images["paper"][141,66,0] > 180
    assert np.array_equal(rendered.original["paper"], image)
    archive = build_export(scan, [photo])
    with zipfile.ZipFile(BytesIO(archive)) as saved:
        assert {"带白边/001_带白边.png", "照片画面/001_照片画面.png", "背景成图/001_背景成图.png"}.issubset(saved.namelist())
        assert all(name.split("/")[0] in {"带白边", "照片画面", "背景成图"} for name in saved.namelist())
    from workspace import Workspace
    from crop_detection import _face_cascade
    assert _face_cascade() is not None, "本机朝向模型未完整打包"
    parent = Path(report_path).resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent, prefix="desktop-selftest-") as folder:
        folder = Path(folder)
        workspace = Workspace(folder / "workspace")
        for index in range(5):
            source = folder / f"photo{index}.png"
            Image.new("RGB",(100,120),(30+index*20,90,140)).save(source)
            workspace.import_file(source,"photo")
        first = workspace.snapshot()["pages"][0]
        first["photos"][0]["rotation"] = 1
        workspace.update_page(first["id"],first["photos"],first["revision"])
        saved = folder / "batch.polascan"
        workspace.write_project(saved)
        restored = Workspace(folder / "restored"); restored.open_project(saved)
        assert len(restored.snapshot()["pages"]) == 5
        assert restored.snapshot()["pages"][0]["photos"][0]["rotation"] == 1
        exported = restored.export(folder / "photos.zip")
        assert exported["success"] == 5 and not exported["failed"]
    report = {"status":"ok", "version":VERSION, "opencv":cv2.__version__, "numpy":np.__version__,
              "dust_pixels":rendered.stats["masked_pixels"], "batch_sources":5,"project_version":2,
              "crop_orientation_detector":type(_face_cascade()).__name__,
              "static_files":all((Path(__file__).parent / "web" / name).is_file() for name in ("index.html","app.js","restoration.js","gallery.js","workspace.js","crop.js","style.css"))}
    assert report["static_files"]
    Path(report_path).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", type=Path)
    parser.add_argument("--smoke-test", type=Path)
    args = parser.parse_args()
    for report_path in (args.self_test, args.smoke_test):
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
    if args.self_test:
        try:
            self_test(args.self_test)
        except Exception:
            args.self_test.write_text(json.dumps({"status":"error", "trace":traceback.format_exc()}),encoding="utf-8")
            raise
        return
    import webview
    # Windows WebView2 is used in an ordinary desktop window; the local service
    # starts and stops with that window and chooses its own free loopback port.
    server = LocalServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    api = DesktopAPI(server)
    window = webview.create_window(f"{TITLE}  {VERSION}", f"http://127.0.0.1:{server.server_port}/", js_api=api,
                                  width=1380,height=920,min_size=(980,700),text_select=True,
                                  hidden=bool(args.smoke_test),background_color="#e9eef2")
    api._window = window
    webview.settings["ALLOW_DOWNLOADS"] = False  # Saves use the native Save dialog.
    webview.settings["ALLOW_FILE_URLS"] = False
    result = {}
    def smoke():
        try:
            if not window.events.loaded.wait(35):
                raise RuntimeError("桌面界面未在 35 秒内载入")
            title = window.evaluate_js("document.title")
            controls = window.evaluate_js("Boolean(document.getElementById('galleryGrid') && document.getElementById('editDialog') && document.getElementById('saveProject') && document.getElementById('fineAngle') && document.getElementById('trimTop') && document.getElementById('cornerLoupe') && document.getElementById('onlyReview'))")
            logic = window.evaluate_js("Boolean(window.Gallery && window.RestUI && window.Workspace && window.CropUI && window.pywebview && window.pywebview.api && window.pywebview.api.save_workspace && window.pywebview.api.import_workspace)")
            if title != TITLE or not controls or not logic:
                raise RuntimeError("桌面控件未完整载入")
            until = time.monotonic() + 20
            while not window.evaluate_js("Boolean(token && Workspace.state.id)"):
                if time.monotonic() > until:
                    raise RuntimeError("工作区未能载入")
                time.sleep(.15)
            navigation = window.evaluate_js("Boolean(document.querySelector('.mode-nav [data-mode=library]') && document.querySelector('.mode-nav [data-mode=crop]') && document.querySelector('.mode-nav [data-mode=photo]') && document.getElementById('sourceGrid'))")
            window.evaluate_js("Gallery.setMode('crop')")
            scan_scope = window.evaluate_js("Gallery.mode === 'crop' && Gallery.visible().every(item => item.source.kind === 'scan')")
            window.evaluate_js("Gallery.setMode('photo')")
            photo_scope = window.evaluate_js("Gallery.mode === 'photo' && Gallery.visible().every(item => item.source.kind === 'photo')")
            if not navigation or not scan_scope or not photo_scope:
                raise RuntimeError("图片库与裁剪页面未正确分开")
            result.update(status="ok",title=title,version=VERSION,renderer="edgechromium",repair_logic=True,native_save_bridge=True,batch_logic=True,crop_logic=True,workspace_loaded=True,separate_pages=True)
        except Exception:
            result.update(status="error",trace=traceback.format_exc())
        finally:
            args.smoke_test.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
            window.destroy()
    try:
        webview.start(smoke if args.smoke_test else None, gui="edgechromium", private_mode=True)
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=3)
    if args.smoke_test and result.get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    if not any(arg in sys.argv for arg in ("--self-test","--smoke-test")):
        log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PolaScanCrop"
        log_dir.mkdir(parents=True,exist_ok=True)
        handler = RotatingFileHandler(log_dir / "desktop.log",maxBytes=1_000_000,backupCount=2,encoding="utf-8")
        logging.basicConfig(level=logging.INFO,handlers=[handler])
    try:
        main()
    except Exception as exc:
        logging.exception("Desktop failed")
        if any(arg in sys.argv for arg in ("--self-test","--smoke-test")):
            raise
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"启动失败：{exc}\n请检查 Windows 的 Microsoft Edge WebView2 Runtime 是否可用。", TITLE, 0x10)
