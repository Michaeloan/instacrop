from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError

from PIL import Image
from app import Handler, LocalServer
from work_service import WorkService


class RuntimeAuditTests(unittest.TestCase):
    def test_thumbnail_and_loupe_do_not_reread_the_entire_original(self):
        with TemporaryDirectory() as folder:
            server = LocalServer(("127.0.0.1", 0), Path(folder) / "workspace")
            service = server.service()
            photo = Path(folder) / "photo.png"
            Image.new("RGB", (80, 90), "blue").save(photo)
            source = service.workspace.import_file(photo, "photo")
            page = service.workspace.page_response(source["pages"][0])
            original = service.workspace.asset(source["asset"])
            read_bytes = Path.read_bytes
            def guarded_read(path):
                if path == original:
                    raise AssertionError("Preview reread the original source bytes")
                return read_bytes(path)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(Path, "read_bytes", guarded_read):
                    for endpoint in (f"scan-preview?job={page['id']}", f"crop-tile?job={page['id']}&x=20&y=20&size=96&token={server.token}"):
                        with urlopen(f"http://127.0.0.1:{server.server_port}/api/{endpoint}", timeout=10) as response:
                            self.assertGreater(len(response.read()), 50)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_disconnected_asset_client_does_not_raise(self):
        from work_service import send_file
        with TemporaryDirectory() as folder:
            asset = Path(folder) / "preview.mp4"
            asset.write_bytes(b"synthetic asset")
            for phase in ("headers", "body"):
                with self.subTest(phase=phase):
                    handler = Mock()
                    handler.headers = {}
                    if phase == "headers":
                        handler.end_headers.side_effect = ConnectionAbortedError("closed")
                    else:
                        handler.wfile.write.side_effect = ConnectionAbortedError("closed")
                    send_file(handler, asset)

    def test_long_upload_name_is_preserved_without_exceeding_filename_limit(self):
        with TemporaryDirectory() as folder:
            server = LocalServer(("127.0.0.1", 0), Path(folder) / "workspace")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            data = BytesIO()
            Image.new("RGB", (80, 90), "blue").save(data, "PNG")
            name = "x" * 240 + ".png"
            try:
                request = Request(f"http://127.0.0.1:{server.server_port}/api/workspace/import?" + urlencode({"kind": "photo", "name": name}),
                                  data.getvalue(), {"X-Session-Token": server.token, "Content-Type": "application/octet-stream"})
                with urlopen(request, timeout=10) as response:
                    task = json.load(response)["task_id"]
                server.service().queue.futures[task].result(timeout=10)
                snapshot = server.service().workspace.snapshot()
                self.assertEqual(snapshot["sources"][0]["name"], name)
                self.assertEqual(snapshot["sources"][0]["status"], "ready")
                self.assertTrue(all(len(path.name) < 40 for path in server.service().uploads.iterdir()))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_failed_project_upload_does_not_leave_unowned_staging_file(self):
        with TemporaryDirectory() as folder:
            server = LocalServer(("127.0.0.1", 0), Path(folder) / "workspace")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = Request(f"http://127.0.0.1:{server.server_port}/api/workspace/project-open", b"invalid project",
                                  {"X-Session-Token": server.token, "Content-Type": "application/octet-stream"})
                with self.assertRaises(HTTPError):
                    urlopen(request, timeout=10)
                self.assertEqual(list(server.service().uploads.iterdir()), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
    def test_disconnected_client_during_headers_or_body_does_not_raise(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase):
                handler = Mock()
                handler.wfile = Mock()
                if phase == "headers":
                    handler.end_headers.side_effect = ConnectionAbortedError("client closed")
                else:
                    handler.wfile.write.side_effect = ConnectionAbortedError("client closed")
                Handler.send(handler, {"status": "ok"})

    def test_project_restore_blocks_new_submissions_until_commit(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            saved = WorkService(root / "saved")
            picture = root / "photo.png"
            Image.new("RGB", (80, 90), "blue").save(picture)
            saved.workspace.import_file(picture, "photo")
            expected = saved.workspace.snapshot()["id"]
            project = root / "project.polascan"
            saved.workspace.write_project(project)
            saved.close()
            server = LocalServer(("127.0.0.1", 0), root / "active")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            service = server.service()
            entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
            observed, errors = [], []
            restore = service.workspace.open_project
            def delayed_restore(path):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Restore release timed out")
                return restore(path)
            def request_restore():
                try:
                    request = Request(f"http://127.0.0.1:{server.server_port}/api/workspace/project-open",
                                      project.read_bytes(), {"X-Session-Token": server.token,
                                      "Content-Type": "application/octet-stream"})
                    with urlopen(request, timeout=10) as response:
                        json.load(response)
                except Exception as error:
                    errors.append(error)
            def addition():
                task = service.queue.start("import", [1], lambda *args: observed.append(service.workspace.snapshot()["id"]))
                submitted.set()
                service.queue.futures[task["task_id"]].result(timeout=5)
            request_thread = threading.Thread(target=request_restore)
            addition_thread = threading.Thread(target=addition)
            try:
                with patch.object(service.workspace, "open_project", side_effect=delayed_restore):
                    request_thread.start()
                    self.assertTrue(entered.wait(3))
                    addition_thread.start()
                    self.assertFalse(submitted.wait(.1))
                    release.set()
                    request_thread.join(10)
                    addition_thread.join(10)
                self.assertEqual(errors, [])
                self.assertEqual(observed, [expected])
                self.assertEqual(service.workspace.snapshot()["id"], expected)
            finally:
                release.set()
                request_thread.join(10)
                if addition_thread.ident:
                    addition_thread.join(10)
                server.shutdown()
                server.server_close()
                thread.join(5)
