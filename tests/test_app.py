from io import BytesIO
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from PIL import Image

from app import LocalServer
from test_scanner import mixed_scan


class LocalAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LocalServer(("127.0.0.1", 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def post(self, path, body, token=True, extra=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Session-Token"] = self.server.token
        headers.update(extra or {})
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        return urlopen(Request(self.url + path, body, headers), timeout=30)

    def test_local_upload_preview_and_three_mode_zip(self):
        scan, truth = mixed_scan()
        stream = BytesIO()
        Image.fromarray(scan.image).save(stream, "PNG", dpi=scan.dpi)
        result = json.load(self.post("/api/scan?name=mixed.png", stream.getvalue()))
        self.assertEqual(len(result["photos"]), len(truth))
        self.assertTrue(all(p["restoration"]["enabled"] and p["restoration"]["auto_border"] for p in result["photos"]))
        self.assertTrue(all(not p["restoration"]["auto_image"] and not p["restoration"]["adjustments"] for p in result["photos"]))
        gallery=json.load(self.post("/api/results",{"job":result["job"],"photos":result["photos"]}))
        self.assertEqual(len(gallery["photos"]),len(truth))
        self.assertTrue(all(set(p["images"])=={"paper","image","composition"} for p in gallery["photos"]))
        needs_edit={**result["photos"][0],"outer":[[100,100],[130,100],[130,130],[100,130]],"inner":None,"presentation":{"trim":20}}
        partial=json.load(self.post("/api/results",{"job":result["job"],"photos":[needs_edit,result["photos"][1]]}))
        self.assertFalse(partial["photos"][0]["ready"])
        self.assertTrue(partial["photos"][1]["ready"])
        preview = json.load(self.post("/api/preview", {"job": result["job"], "photo": result["photos"][0], "trim": 2}))
        self.assertEqual(set(preview["images"]), {"paper", "image", "composition"})
        selected = result["photos"]
        for photo in selected[1:]:
            photo["enabled"] = False
        response = self.post("/api/export", {"job": result["job"], "photos": selected, "trim": 2})
        with zipfile.ZipFile(BytesIO(response.read())) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(len(manifest["photos"]), 1)
            self.assertEqual(manifest["trim_pixels"], 2)
            self.assertTrue({"paper/001.png", "image/001.png", "composition/001.png", "preview.png"}.issubset(archive.namelist()))
        friendly=self.post("/api/export",{"job":result["job"],"photos":selected,"friendly_names":True})
        with zipfile.ZipFile(BytesIO(friendly.read())) as archive:
            self.assertTrue({"带白边/001.png","照片画面/001.png","背景成图/001.png","原始版本/带白边/001.png"}.issubset(archive.namelist()))

    def test_reject_untrusted_origins_paths_and_bad_regions(self):
        for headers in [{}, {"Origin": "https://example.org"}]:
            with self.assertRaises(HTTPError) as caught:
                self.post("/api/export", {}, token=bool(headers), extra=headers)
            self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.url + "/scanner.py")
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/scan?name=bad.png", b"not an image")
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
