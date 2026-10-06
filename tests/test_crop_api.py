from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image

from app import LocalServer
from scanner import Photo, Scan, validate_photo
from workspace import Workspace


class CropApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = LocalServer(("127.0.0.1", 0), Path(self.tmp.name) / "server")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        image = np.full((260, 320, 3), 40, np.uint8)
        image[30:230, 40:280] = 240
        image[45:190, 55:265] = [20, 90, 150]
        self.scan = Scan(image, "测试原图.png")
        self.job = self.server.store(self.scan, b"test")
        self.photo = Photo(np.array([[40, 30], [279, 30], [279, 229], [40, 229]], np.float32),
                           np.array([[55, 45], [264, 45], [264, 189], [55, 189]], np.float32)).as_dict()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def post(self, endpoint, **data):
        request = Request(self.url + endpoint, json.dumps({"job": self.job, "photo": self.photo, **data}).encode(),
                          {"X-Session-Token": self.server.token, "Content-Type": "application/json"})
        return json.load(urlopen(request, timeout=30))

    def test_analysis_refine_and_snap_are_readonly(self):
        original = self.scan.image.copy()
        analysis = self.post("/api/crop-analysis")
        self.assertIn("needs_review", analysis)
        self.assertIn("review_reasons", analysis)
        self.assertIn("suggested_rotation", analysis["orientation"])
        refined = self.post("/api/crop-refine")
        validate_photo(refined["photo"], self.scan.image.shape)
        snapped = self.post("/api/crop-snap", target="outer", index=0, point=[42, 32], radius=12)
        self.assertEqual(len(snapped["point"]), 2)
        np.testing.assert_array_equal(original, self.scan.image)
        with self.assertRaises(HTTPError) as error:
            self.post("/api/crop-snap", target="outer", index=4, point=[42, 32])
        self.assertEqual(error.exception.code, 400)

    def test_loupe_returns_original_pixels_and_padded_boundary(self):
        path = f"/api/crop-tile?job={self.job}&x=80&y=80&size=64&token={self.server.token}"
        with Image.open(BytesIO(urlopen(self.url + path).read())) as tile:
            np.testing.assert_array_equal(np.array(tile), self.scan.image[48:112, 48:112])
        path = f"/api/crop-tile?job={self.job}&x=0&y=0&size=64&token={self.server.token}"
        with Image.open(BytesIO(urlopen(self.url + path).read())) as tile:
            self.assertEqual(tile.size, (64, 64))
            np.testing.assert_array_equal(np.array(tile)[32:, 32:], self.scan.image[:32, :32])
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + f"/api/crop-tile?job={self.job}&x=0&y=0")
        self.assertEqual(error.exception.code, 403)

    def test_clockwise_corner_labels_survive_diagonal_tie(self):
        quad = [[160, 31], [290, 160], [160, 230], [30, 160]]
        # First and last corners have almost the same x+y. A small drag must
        # retain index 0 even when the last corner becomes the minimum.
        quad[0] = [163, 31]
        photo = validate_photo({"outer": quad, "inner": None}, self.scan.image.shape)
        np.testing.assert_array_equal(photo.outer, quad)
        snapped = self.post("/api/crop-snap", photo={"outer": quad, "inner": None},
                            target="outer", index=0, point=quad[0], radius=12)
        self.assertLess(np.linalg.norm(np.asarray(snapped["point"])-quad[0]), 13)

    def test_project_keeps_geometry_orientation_and_review_state(self):
        root = Path(self.tmp.name)
        source = root / "测试照片.png"
        Image.fromarray(self.scan.image).save(source)
        workspace = Workspace(root / "workspace")
        imported = workspace.import_file(source, "photo")
        page = workspace.page_response(imported["pages"][0])
        photo = deepcopy(page["photos"][0])
        photo.update(outer=self.photo["outer"], inner=self.photo["inner"])
        photo["presentation"] = {"angle": .2, "perspective_x": .5, "perspective_y": -.3,
                                 "trim_top": 2, "trim_right": 3, "trim_bottom": 4, "trim_left": 1,
                                 "orientation_confirmed": True, "review_confirmed": True}
        workspace.update_page(page["id"], [photo], page["revision"])
        project = root / "新版.polascan"
        workspace.write_project(project)
        reopened = Workspace(root / "reopened")
        reopened.open_project(project)
        actual = reopened.page_response(page["id"])["photos"][0]
        self.assertEqual(actual["presentation"], photo["presentation"])
        self.assertIn("review_reasons", actual)
        self.assertIn("needs_review", actual)
        self.assertTrue(reopened.render(photo["id"]).width > 100)

    def test_invalid_geometry_cannot_replace_persisted_edits(self):
        root = Path(self.tmp.name)
        source = root / "照片.png"
        Image.fromarray(self.scan.image).save(source)
        workspace = Workspace(root / "invalid")
        imported = workspace.import_file(source, "photo")
        page = workspace.page_response(imported["pages"][0])
        photo = deepcopy(page["photos"][0])
        photo["presentation"] = {"angle": 10}
        with self.assertRaises(ValueError):
            workspace.update_page(page["id"], [photo], page["revision"])
        self.assertEqual(workspace.page_response(page["id"]), page)


if __name__ == "__main__":
    unittest.main()
