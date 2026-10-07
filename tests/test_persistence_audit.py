"""Regression cases for failed commits and exact export selections."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image
import workspace as workspace_module
from workspace import Workspace
from work_service import WorkService


class PersistenceAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = Workspace(self.root / "data")
        self.image = self.root / "synthetic.png"
        Image.new("RGB", (100, 80), "orange").save(self.image)
        self.source = self.ws.import_file(self.image, "photo")
        self.page = self.ws.page_response(self.source["pages"][0])
        self.pid = self.page["photos"][0]["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def assert_commit_failure_preserves_state(self, operation):
        before = self.ws.snapshot()
        undo_before = deepcopy(self.ws._undo)
        disk_before = self.ws.path.read_bytes()
        with patch("workspace._atomic_json", side_effect=OSError("simulated disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                operation()
        self.assertEqual(self.ws.snapshot(), before)
        self.assertEqual(self.ws._undo, undo_before)
        self.assertEqual(self.ws.path.read_bytes(), disk_before)
        self.ws.save()
        self.assertEqual(Workspace(self.ws.root).snapshot(), before)

    def test_failed_edit_remove_select_new_and_batch_never_leak_into_later_save(self):
        changed = deepcopy(self.page["photos"])
        changed[0]["rotation"] = 1
        operations = {
            "edit": lambda: self.ws.update_page(self.page["id"], changed),
            "remove": lambda: self.ws.remove_source(self.source["id"]),
            "select": lambda: self.ws.select([self.pid], False),
            "new": self.ws.new,
            "batch": lambda: self.ws.batch_apply([self.pid], {"restoration": {"brightness": 10}}),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                self.assert_commit_failure_preserves_state(operation)

    def test_failed_undo_preserves_token_and_can_be_retried(self):
        token = self.ws.batch_apply([self.pid], {"restoration": {"brightness": 10}})
        self.assert_commit_failure_preserves_state(lambda: self.ws.undo(token))
        self.ws.undo(token)
        self.assertEqual(self.ws.locate_photo(self.pid)[1]["restoration"]["brightness"], 0)

    def test_failed_new_manifest_commit_preserves_original_batch(self):
        atomic = workspace_module._atomic_json
        def fail_manifest(path, data):
            if path == self.ws.path:
                raise OSError("simulated disk full")
            return atomic(path, data)
        before = self.ws.snapshot()
        with patch("workspace._atomic_json", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                self.ws.new()
        self.assertEqual(self.ws.snapshot(), before)
        self.ws.save()
        self.assertEqual(Workspace(self.ws.root).snapshot(), before)

    def test_one_failed_page_commit_stays_retryable_without_phantom_pages(self):
        atomic = workspace_module._atomic_json
        calls = 0
        def fail_page_once(path, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated page commit failure")
            return atomic(path, data)
        with patch("workspace._atomic_json", side_effect=fail_page_once):
            failed = self.ws.import_file(self.image, "photo")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["pages"], [])
        self.assertEqual(len(self.ws.snapshot()["pages"]), 1)
        self.assertEqual(Workspace(self.ws.root).snapshot(), self.ws.snapshot())
        retried = self.ws.retry_source(failed["id"])
        self.assertEqual(retried["status"], "ready")
        self.assertEqual(len(retried["pages"]), 1)
        self.assertEqual(len(self.ws.snapshot()["pages"]), 2)

    def test_checkbox_revision_blocks_stale_editor_overwrite(self):
        self.ws.select([self.pid], False)
        current = self.ws.page_response(self.page["id"])
        self.assertEqual(current["revision"], self.page["revision"] + 1)
        with self.assertRaisesRegex(ValueError, "刷新"):
            self.ws.update_page(self.page["id"], self.page["photos"], self.page["revision"])
        self.assertFalse(self.ws.locate_photo(self.pid)[1]["enabled"])
        self.ws.select([self.pid], False)
        self.assertEqual(self.ws.page_response(self.page["id"])["revision"], current["revision"])

    def test_reopen_uses_import_pixel_limit(self):
        metadata = self.ws.snapshot()
        metadata["pages"][0].update(width=10208, height=14032)
        Workspace._validate_manifest(metadata)
        self.ws.manifest = metadata
        self.ws._persist()
        self.assertEqual(Workspace(self.ws.root).snapshot()["pages"][0]["width"], 10208)
        for width, height in ((20001, 10000), (True, 80), (100.0, 80)):
            invalid = deepcopy(metadata)
            invalid["pages"][0].update(width=width, height=height)
            with self.assertRaisesRegex(ValueError, "尺寸"):
                Workspace._validate_manifest(invalid)

    def test_non_object_project_metadata_preserves_existing_batch(self):
        before = self.ws.snapshot()
        project = self.root / "invalid.polascan"
        for data in ([], "not an object", None, 12):
            with self.subTest(data=data):
                with zipfile.ZipFile(project, "w") as archive:
                    archive.writestr("project.json", json.dumps(data))
                with self.assertRaisesRegex(ValueError, "格式"):
                    self.ws.open_project(project)
                self.assertEqual(self.ws.snapshot(), before)

    def test_empty_export_selection_and_invalid_selection_never_submit_all(self):
        service = WorkService(self.root / "service")
        try:
            with patch.object(service.queue, "start") as start:
                for kind in ("export", "folder"):
                    for ids in ([], "", False, 0, {}):
                        with self.subTest(kind=kind, ids=ids):
                            with self.assertRaises(ValueError):
                                service.save(kind, {"photo_ids": ids})
                with self.assertRaises(ValueError):
                    service.save("export", {"destination": "arbitrary"})
                start.assert_not_called()
            with self.assertRaisesRegex(ValueError, "没有勾选"):
                self.ws.export(self.root / "empty.zip", [])
            self.assertFalse((self.root / "empty.zip").exists())
            if hasattr(service, "export_live"):
                with self.assertRaisesRegex(ValueError, "选择"):
                    service.export_live(self.root / "empty-live.zip", [], threading.Event(), lambda _: None)
                self.assertFalse((self.root / "empty-live.zip").exists())
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
