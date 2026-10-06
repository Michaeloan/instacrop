from copy import deepcopy
from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image, ImageCms

from workspace import Workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = Workspace(self.root / "work")

    def tearDown(self):
        self.tmp.cleanup()

    def picture(self, name="照片.png", color="orange", pages=1):
        path = self.root / name
        image = Image.new("RGB", (100, 80), color)
        image.save(path, save_all=pages > 1,
                   append_images=[Image.new("RGB", (100, 80), "blue") for _ in range(pages - 1)])
        return path

    def add(self, name="照片.png"):
        source = self.workspace.import_file(self.picture(name), "photo")
        return self.workspace.page_response(source["pages"][0])

    def test_append_multipage_and_recover_without_three_source_eviction(self):
        first = self.add()
        photo = deepcopy(first["photos"][0])
        photo["rotation"] = 1
        self.workspace.update_page(first["id"], [photo], first["revision"])
        for i in range(5):
            self.add(f"新增{i}.png")
        source = self.workspace.import_file(self.picture("多页.tiff", pages=3), "photo")
        self.assertEqual(len(source["pages"]), 3)
        reopened = Workspace(self.workspace.root)
        self.assertEqual(len(reopened.snapshot()["sources"]), 7)
        self.assertEqual(len(reopened.snapshot()["pages"]), 9)
        self.assertEqual(reopened.page_response(first["id"])["photos"][0]["rotation"], 1)
        self.assertTrue(reopened.render(photo["id"]).size[1] > reopened.render(photo["id"]).size[0])
        self.assertLessEqual(len(reopened._cache), 2)

    def test_validated_updates_batch_preserves_strokes_and_undo_keeps_later_edits(self):
        first, second = self.add(), self.add("第二张.png")
        photo = deepcopy(first["photos"][0])
        strokes = [{"mode": "paint", "radius": 2, "points": [[25, 25]]}]
        photo["restoration"]["strokes"] = strokes
        self.workspace.update_page(first["id"], [photo])
        token = self.workspace.batch_apply([photo["id"], second["photos"][0]["id"]],
                                          {"restoration": {"brightness": 8}, "presentation": {"trim": 1}})
        current = self.workspace.page_response(first["id"])
        self.assertEqual(current["photos"][0]["restoration"]["strokes"], strokes)
        self.assertEqual(current["photos"][0]["outer"], photo["outer"])
        with self.assertRaises(ValueError):
            self.workspace.batch_apply([photo["id"]], {"restoration": {"strokes": []}})
        with self.assertRaises(ValueError):
            self.workspace.update_page(first["id"], current["photos"], first["revision"])
        current["photos"][0]["rotation"] = 2
        current["photos"][0]["restoration"]["brightness"] = 15
        self.workspace.update_page(first["id"], current["photos"], current["revision"])
        self.workspace.undo(token)
        restored = self.workspace.page_response(first["id"])["photos"][0]
        self.assertEqual(restored["restoration"]["brightness"], 15)
        self.assertEqual(restored["rotation"], 2)
        self.assertEqual(restored["restoration"]["strokes"], strokes)
        self.assertEqual(self.workspace.page_response(second["id"])["photos"][0]["restoration"]["brightness"], 0)


    def test_legacy_import_and_reject_traversal_or_bad_hash_without_replacing_workspace(self):
        source = self.picture().read_bytes()
        legacy_photo = self.add()["photos"][0]
        project = self.root / "旧项目.polascan"
        meta = {"version": 1, "source_name": "照片.png", "page": 1,
                "source_sha256": hashlib.sha256(source).hexdigest(), "photos": [legacy_photo], "trim": 2, "occupancy": .7}
        with zipfile.ZipFile(project, "w") as archive:
            archive.writestr("project.json", json.dumps(meta))
            archive.writestr("source.bin", source)
        self.workspace.open_project(project)
        self.assertEqual(len(self.workspace.snapshot()["sources"]), 1)
        self.assertEqual(self.workspace.snapshot()["pages"][0]["photos"][0]["presentation"]["trim"], 2)
        before = self.workspace.snapshot()
        with zipfile.ZipFile(project, "a") as archive:
            archive.writestr("../evil", b"x")
        with self.assertRaises(ValueError):
            self.workspace.open_project(project)
        self.assertEqual(self.workspace.snapshot(), before)
        with self.assertRaises(ValueError):
            self.workspace.asset("../outside.png")

    def test_export_has_only_three_flat_categories_with_unique_names_and_final_pixels(self):
        first, second = self.add(), self.add()
        multi = self.workspace.import_file(self.picture("多页.tiff", pages=2), "photo")
        changed = deepcopy(first["photos"])
        changed[0]["restoration"].update(enabled=True, adjustments=True, brightness=20)
        self.workspace.update_page(first["id"], changed)
        report = self.workspace.export(self.root / "三类", zip_output=False)
        self.assertEqual((report["success"], report["failed"]), (4, 0), report)
        output = Path(report["path"])
        categories = {"带白边", "照片画面", "背景成图"}
        self.assertEqual({p.name for p in output.iterdir()}, categories)
        for category in categories:
            files = list((output / category).iterdir())
            self.assertEqual(len(files), 4)
            self.assertTrue(all(p.is_file() and p.suffix == ".png" for p in files))
        names = {p.name for p in (output / "带白边").iterdir()}
        self.assertIn("照片_第001页_照片001.png", names)
        self.assertIn("照片_2_第001页_照片001.png", names)
        self.assertIn("多页_第002页_照片001.png", names)
        expected = self.workspace.render(changed[0]["id"])
        with Image.open(output / report["photos"][0]["files"]["paper"]) as image:
            self.assertEqual(image.convert("RGB").tobytes(), expected.convert("RGB").tobytes())
        zipped = self.workspace.export(self.root / "三类.zip")
        with zipfile.ZipFile(zipped["path"]) as archive:
            self.assertEqual({n.split("/")[0] for n in archive.namelist()}, categories)
            self.assertTrue(all(len(n.strip("/").split("/")) <= 2 for n in archive.namelist()))
            self.assertEqual(sum(not i.is_dir() for i in archive.infolist()), 12)
        repeated = self.workspace.export(output, zip_output=False)
        self.assertNotEqual(repeated["path"], report["path"])
        self.assertEqual(len(list(output.rglob("*.png"))), 12)

    def test_export_rolls_back_photo_if_a_category_cannot_be_committed(self):
        self.add()
        import workspace
        replace = workspace.os.replace
        def fail_second(source, target):
            if Path(target).parent.name == "照片画面":
                raise OSError("simulated disk full")
            return replace(source, target)
        with patch("workspace.os.replace", side_effect=fail_second):
            report = self.workspace.export(self.root / "失败", zip_output=False)
        self.assertEqual((report["success"], report["failed"]), (0, 1))
        output = Path(report["path"])
        self.assertEqual({p.name for p in output.iterdir()}, {"带白边", "照片画面", "背景成图"})
        self.assertFalse(list(output.rglob("*.png")))

    def test_stream_export_grouping_selection_failures_and_cancel(self):
        first, second = self.add(), self.add("第二张.png")
        self.workspace.select([second["photos"][0]["id"]], False)
        report = self.workspace.export(self.root / "导出.zip")
        self.assertEqual((report["success"], report["failed"]), (1, 0), report)
        with zipfile.ZipFile(report["path"]) as archive:
            files = archive.namelist()
            self.assertTrue(any("带白边" in item for item in files))
            self.assertTrue(any("照片画面" in item for item in files))
            self.assertTrue(any("背景成图" in item for item in files))
        invalid = deepcopy(first["photos"][0])
        invalid["presentation"] = {"trim": 50}
        with self.assertRaises(ValueError):
            self.workspace.update_page(first["id"], [invalid])
        from workspace import render_photo as render
        def fail_one(scan, photo, *args, **kwargs):
            if scan.name == first["name"]:
                raise ValueError("模拟素材读取失败")
            return render(scan, photo, *args, **kwargs)
        with patch("workspace.render_photo", side_effect=fail_one):
            report = self.workspace.export(self.root / "部分导出", [invalid["id"], second["photos"][0]["id"]], zip_output=False)
        self.assertEqual((report["success"], report["failed"]), (1, 1))
        cancelled = threading.Event()
        cancelled.set()
        report = self.workspace.export(self.root / "取消.zip", cancel_event=cancelled)
        self.assertTrue(report["cancelled"])
        self.assertFalse((self.root / "取消.zip").exists())



    def test_scan_batch_with_tiff_and_no_photos_keeps_source_failures_separate(self):
        from test_scanner import mixed_scan
        scan, truth = mixed_scan()
        file = self.root / "扫描.png"
        Image.fromarray(scan.image).save(file)
        source = self.workspace.import_file(file, "scan")
        self.assertEqual(len(self.workspace.page_response(source["pages"][0])["photos"]), len(truth))
        with patch("workspace.detect", return_value=[]):
            multi = self.workspace.import_file(self.picture("空白.tiff", pages=3), "scan")
        self.assertEqual(len(multi["pages"]), 3)
        self.assertTrue(all(self.workspace.page_response(key)["status"] == "empty" for key in multi["pages"]))
        broken = self.root / "坏图片.png"
        broken.write_bytes(b"not-an-image")
        failed = self.workspace.import_file(broken)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.workspace.snapshot()["sources"][0]["status"], "ready")

    def test_project_bad_hash_does_not_mutate_current_workspace(self):
        self.add()
        project = self.workspace.write_project(self.root / "good.polascan")
        bad = self.root / "bad.polascan"
        with zipfile.ZipFile(project) as source, zipfile.ZipFile(bad, "w") as target:
            for entry in source.infolist():
                data = source.read(entry.filename)
                if entry.filename.startswith("sources/"):
                    data = b"tampered-source"
                target.writestr(entry.filename, data)
        before = self.workspace.snapshot()
        with self.assertRaisesRegex(ValueError, "校验"):
            self.workspace.open_project(bad)
        self.assertEqual(self.workspace.snapshot(), before)


    def test_project_commit_failure_restores_previous_source_assets(self):
        page = self.add()
        project = self.workspace.write_project(self.root / "rollback.polascan")
        before = self.workspace.snapshot()
        with patch.object(self.workspace, "_persist", side_effect=OSError("simulated disk full")):
            with self.assertRaises(OSError):
                self.workspace.open_project(project)
        self.assertEqual(self.workspace.snapshot(), before)
        self.assertEqual(self.workspace.render(page["photos"][0]["id"]).getpixel((20, 20)), (255, 165, 0))
        reopened = Workspace(self.workspace.root)
        self.assertEqual(reopened.snapshot(), before)

    def test_memory_budget_can_evict_decoded_pages_without_losing_edits(self):
        page = self.add()
        changed = deepcopy(page["photos"])
        changed[0]["rotation"] = 2
        self.workspace.update_page(page["id"], changed)
        with patch("workspace.CACHE_BYTES", 100):
            self.workspace.read_page(page["id"])
            self.assertEqual(len(self.workspace._cache), 0)
        self.assertEqual(self.workspace.page_response(page["id"])["photos"][0]["rotation"], 2)
        for bad in ("live/CON.png", "live/file.png:stream", "live/trailing.", "live/../outside.png"):
            with self.assertRaises(ValueError):
                self.workspace.asset(bad)

    def test_redetect_honors_controls_and_changes_only_selected_page(self):
        first, second = self.add(), self.add("保留.png")
        original = self.workspace.page_response(second["id"])
        from scanner import validate_photo
        result = validate_photo(first["photos"][0], (first["height"], first["width"], 3))
        with patch("workspace.detect", return_value=[result]) as detect:
            self.workspace.redetect(first["id"], sensitivity=.85, min_area=.003)
            settings = detect.call_args.args[1]
            self.assertEqual(settings.sensitivity, .85)
            self.assertEqual(settings.min_area, .003)
        self.assertEqual(self.workspace.page_response(second["id"]), original)
        before = self.workspace.snapshot()
        with self.assertRaises(ValueError):
            self.workspace.redetect(first["id"], sensitivity=float("nan"))
        self.assertEqual(self.workspace.snapshot(), before)

    def test_direct_photo_icc_transforms_lab_to_srgb_preserving_original(self):
        lab = Image.new("LAB", (100, 80), (120, 160, 130))
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB"))
        destination_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
        expected = ImageCms.profileToProfile(lab, profile, destination_profile, outputMode="RGB")
        path = self.root / "带配置照片.tiff"
        lab.save(path, icc_profile=profile.tobytes())
        original = path.read_bytes()
        source = self.workspace.import_file(path, "photo")
        self.assertEqual(source["status"], "ready", source)
        page = self.workspace.page_response(source["pages"][0])
        self.assertEqual(page["color"]["profile"], "sRGB")
        self.assertTrue(page["color"]["converted"])
        self.assertEqual(self.workspace.render(page["photos"][0]["id"]).getpixel((20, 20)), expected.getpixel((20, 20)))
        self.assertEqual(self.workspace.asset(source["asset"]).read_bytes(), original)

    def test_direct_high_bit_depth_photo_warns_and_keeps_original(self):
        import numpy as np
        path = self.root / "高位深.tiff"
        Image.fromarray(np.full((80, 100), 51400, dtype=np.uint16)).save(path)
        original = path.read_bytes()
        source = self.workspace.import_file(path, "photo")
        self.assertEqual(source["status"], "ready", source)
        page = self.workspace.page_response(source["pages"][0])
        self.assertTrue(page["color"]["hdr_decoded_8bit"])
        self.assertTrue(any("8 位 SDR" in warning for warning in page["photos"][0]["warnings"]))
        self.assertEqual(self.workspace.render(page["photos"][0]["id"]).getpixel((20, 20)), (200, 200, 200))
        self.assertEqual(self.workspace.asset(source["asset"]).read_bytes(), original)

    def test_retry_partial_tiff_keeps_successful_page_ids_and_user_edits(self):
        from scanner import Photo
        import numpy as np
        path = self.picture("可恢复多页.tiff", pages=2)
        def detect_page(scan):
            if scan.page == 2:
                raise RuntimeError("transient detector failure")
            quad = np.array([[0,0],[99,0],[99,79],[0,79]], np.float32)
            return [Photo(quad, quad.copy())]
        with patch("workspace.detect", side_effect=detect_page):
            source = self.workspace.import_file(path, "scan")
        self.assertEqual(source["status"], "partial")
        self.assertEqual(source["failed_pages"], [2])
        page = self.workspace.page_response(source["pages"][0])
        page["photos"][0]["rotation"] = 1
        edited = self.workspace.update_page(page["id"], page["photos"])
        path.unlink()  # Retry must use the workspace's immutable original.
        quad = np.array([[0,0],[99,0],[99,79],[0,79]], np.float32)
        with patch("workspace.detect", return_value=[Photo(quad, quad.copy())]) as retry_detector:
            result = self.workspace.retry_source(source["id"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["id"], source["id"])
        self.assertEqual(len(result["pages"]), 2)
        self.assertEqual(len(self.workspace.snapshot()["sources"]), 1)
        self.assertEqual(self.workspace.page_response(page["id"]), edited)
        self.assertEqual(retry_detector.call_count, 1)
        self.assertEqual(retry_detector.call_args.args[0].page, 2)

    def test_partial_import_closes_tiff_even_with_retained_exception_traceback(self):
        from scanner import Photo
        import numpy as np
        path = self.picture("句柄释放.tiff", pages=2)
        failure = RuntimeError("retained transient failure")
        quad = np.array([[0,0],[99,0],[99,79],[0,79]], np.float32)
        try:
            with patch("workspace.detect", side_effect=[[Photo(quad, quad.copy())], failure]):
                source = self.workspace.import_file(path, "scan")
            self.assertEqual(source["status"], "partial")
            stored = self.workspace.asset(source["asset"])
            renamed = stored.with_name("renamed.tiff")
            stored.rename(renamed)
            renamed.rename(stored)
            with patch("workspace.detect", return_value=[Photo(quad, quad.copy())]):
                self.workspace.retry_source(source["id"])
            stored.rename(renamed)
            renamed.unlink()  # No gc.collect() is allowed to make handles close.
        finally:
            failure.__traceback__ = None

    def test_partial_photo_import_closes_decoder_when_saving_page_fails(self):
        path = self.picture("照片解码句柄.tiff", pages=2)
        failure = RuntimeError("retained save failure")
        save = Image.Image.save
        counter = [0]
        def save_page(image, *args, **kwargs):
            counter[0] += 1
            if counter[0] == 2:
                raise failure
            return save(image, *args, **kwargs)
        try:
            with patch.object(Image.Image, "save", new=save_page):
                source = self.workspace.import_file(path, "photo")
            self.assertEqual(source["status"], "partial")
            stored = self.workspace.asset(source["asset"])
            renamed = stored.with_name("photo-renamed.tiff")
            stored.rename(renamed)
            renamed.rename(stored)
            self.workspace.retry_source(source["id"])
            stored.rename(renamed)
            renamed.unlink()
        finally:
            failure.__traceback__ = None


if __name__ == "__main__":
    unittest.main()
