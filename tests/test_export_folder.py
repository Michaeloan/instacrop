from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from PIL import Image
from app import LocalServer
from desktop import DesktopAPI


class FolderExportTests(unittest.TestCase):
    def wait(self, service, task_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            task = next(t for t in service.queue.snapshot() if t["id"] == task_id)
            if task["state"] not in {"running", "queued"}:
                self.assertEqual(task["state"], "completed", task)
                return task["results"][0]["result"]
            time.sleep(.02)
        self.fail("Export task did not complete")

    def test_desktop_folder_dialog_saves_pngs_without_a_zip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server = LocalServer(("127.0.0.1", 0), root / "data")
            service = server.service()
            try:
                source = root / "photo.png"
                Image.new("RGB", (80, 100), "orange").save(source)
                service.workspace.import_file(source, "photo")
                api = DesktopAPI(server)
                class Window:
                    def get_current_url(self):
                        return f"http://127.0.0.1:{server.server_port}/"
                    def create_file_dialog(self, *args, **kwargs):
                        return (str(root),)
                api._window = Window()
                result = self.wait(service, api.save_workspace("folder", {})["task_id"])
                output = Path(result["path"])
                self.assertEqual(output, (root / "相纸批次").resolve())
                self.assertEqual({p.name for p in output.iterdir()}, {"带白边", "照片画面", "背景成图"})
                self.assertEqual(len(list(output.rglob("*.png"))), 3)
                self.assertFalse(list(root.rglob("*.zip")))
                with patch.object(api._window, "create_file_dialog", return_value=None):
                    self.assertTrue(api.save_workspace("folder", {})["cancelled"])
            finally:
                service.queue.close()
                server.server_close()

    def test_browser_folder_export_returns_local_folder_and_zip_remains_optional(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server = LocalServer(("127.0.0.1", 0), root / "data")
            service = server.service()
            try:
                source = root / "photo.png"
                Image.new("RGB", (80, 100), "orange").save(source)
                service.workspace.import_file(source, "photo")
                result = self.wait(service, service.post("/api/workspace/export", {"zip_output": False})["task_id"])
                output = Path(result["path"])
                self.assertTrue(output.is_dir())
                self.assertEqual(output.suffix, "")
                self.assertEqual(len(list(output.rglob("*.png"))), 3)
                zipped = self.wait(service, service.post("/api/workspace/export", {"zip_output": True})["task_id"])
                self.assertIn("artifact_id", zipped)
                self.assertEqual(zipped["report"]["success"], 1)
            finally:
                service.queue.close()
                server.server_close()
