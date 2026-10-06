from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np
from PIL import Image

from scanner import (Scan, Settings, compose, containment, crops, detect,
                     export_to_directory, inset_quad, order_quad, read_scans,
                     refine_quad, validate_photo, warp)


def mixed_scan():
    """Ground truth: varied sizes, rotations, white/black/colour frames + unknown."""
    canvas = np.full((1750, 2050, 3), 240, np.uint8)
    layouts = [
        (120, 95, 270, 430, -7, (20, 25, 250, 335), (253, 253, 250)),
        (630, 100, 360, 430, 6, (25, 25, 335, 335), (253, 253, 250)),
        (1220, 120, 540, 430, -4, (24, 25, 516, 335), (253, 253, 250)),
        (125, 760, 442, 538, 5, (30, 30, 412, 420), (253, 253, 250)),
        (700, 820, 269, 333, -11, (20, 18, 249, 250), (25, 25, 30)),
        (1170, 800, 580, 290, 9, (25, 25, 555, 265), (220, 105, 120)),
        (1100, 1380, 120, 180, -8, (9, 10, 111, 145), (253, 253, 250)),
    ]
    truth = []
    for i, (x, y, w, h, angle, bounds, colour) in enumerate(layouts):
        paper = np.full((h, w, 3), colour, np.uint8)
        x0, y0, x1, y1 = bounds
        yy, xx = np.mgrid[:y1-y0, :x1-x0]
        content = np.stack([60 + 35 * np.sin(xx / 17) + yy / max(1, y1-y0) * 45,
                            85 + 28 * np.cos(yy / 29) + xx / max(1, x1-x0) * 30,
                            100 + 30 * np.sin((xx + yy) / 41)], axis=-1)
        paper[y0:y1, x0:x1] = np.clip(content, 0, 255).astype(np.uint8)
        src = np.array([[0, 0], [w-1, 0], [w-1, h-1], [0, h-1]], np.float32)
        center = np.array([(w-1)/2, (h-1)/2])
        theta = np.deg2rad(angle)
        rotation = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
        dst = ((src - center) @ rotation.T + center + [x, y]).astype(np.float32)
        matrix = cv2.getPerspectiveTransform(src, dst)
        cv2.fillConvexPoly(canvas, (dst + [3, 4]).astype(np.int32), (160, 160, 160))
        warped = cv2.warpPerspective(paper, matrix, (canvas.shape[1], canvas.shape[0]))
        mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), matrix, (canvas.shape[1], canvas.shape[0]))
        canvas[mask > 128] = warped[mask > 128]
        inner = np.array([[x0, y0], [x1-1, y0], [x1-1, y1-1], [x0, y1-1]], np.float32)
        truth.append((order_quad(dst), order_quad(cv2.perspectiveTransform(inner[None], matrix)[0])))
    return Scan(canvas, "mixed.png", (127, 127)), truth


class ScannerTests(unittest.TestCase):
    def test_mixed_formats_detection_and_boundaries(self):
        scan, truth = mixed_scan()
        photos = detect(scan)
        print("Mixed count:", len(photos))
        self.assertEqual(len(photos), len(truth), "must handle unequal sizes and arbitrary rectangular formats")
        matched = []
        for outer, inner in truth:
            photo = max(photos, key=lambda p: containment(outer, p.outer)[1])
            overlap = containment(outer, photo.outer)[1]
            print("Mixed overlap:", round(overlap, 4), photo.method, photo.inner is not None)
            self.assertGreater(overlap, .95)
            self.assertIsNotNone(photo.inner)
            self.assertGreater(containment(inner, photo.inner)[1], .95)
            matched.append(id(photo))
        self.assertEqual(len(set(matched)), len(truth))

    def test_full_resolution_refinement_reduces_error(self):
        scan, truth = mixed_scan()
        q = truth[0][0]
        perturbed = q + np.array([[3, -4], [-4, 3], [4, -3], [-3, 4]], np.float32)
        refined = refine_quad(scan.image, perturbed, radius=9, paper=True)
        before = np.linalg.norm(perturbed - q, axis=1).mean()
        after = np.linalg.norm(refined - q, axis=1).mean()
        print("Refinement corner error:", round(float(before), 2), "->", round(float(after), 2), "px")
        self.assertLess(after, 2)
        self.assertLess(after, before)

    def test_composition_follows_picture_and_preserves_background(self):
        paper = np.full((210, 180, 3), 240, np.uint8)
        for w, h in [(120, 120), (92, 124), (198, 124), (123, 125)]:
            image = np.zeros((h, w, 3), np.uint8)
            image[:] = (30, 80, 120)
            output = compose(paper, image)
            expected = (min(w, h), min(w, h)) if abs(w / h - 1) < .025 else (w, h)
            self.assertEqual(output.size, expected)
            np.testing.assert_array_equal(np.array(output)[0, 0], image[0, 0])

    def test_trim_in_source_pixels_and_no_mutation(self):
        q = order_quad([[10, 10], [110, 10], [110, 160], [10, 160]])
        original = q.copy()
        result = inset_quad(q, 2)
        np.testing.assert_allclose(result, [[12, 12], [108, 12], [108, 158], [12, 158]], atol=.001)
        np.testing.assert_array_equal(q, original)
        with self.assertRaises(ValueError):
            inset_quad(q, 51)

    def test_export_three_modes_and_no_overwrite(self):
        scan, truth = mixed_scan()
        from scanner import Photo
        photo = Photo(*truth[0])
        before = scan.image.copy()
        (Path.cwd() / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp") as temporary:
            first, manifest = export_to_directory(scan, [photo], temporary)
            second, _ = export_to_directory(scan, [photo], temporary)
            self.assertNotEqual(first, second)
            self.assertEqual(len(manifest["photos"][0]["files"]), 3)
            for mode in ("paper", "image", "composition"):
                with Image.open(first / mode / "001.png") as image:
                    self.assertGreater(min(image.size), 100)
            review = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
            restored = validate_photo(review["photos"][0], scan.image.shape)
            np.testing.assert_array_equal(restored.outer, photo.outer)
        np.testing.assert_array_equal(scan.image, before)

    def test_file_formats_and_multipage_tiff(self):
        images = [Image.new("RGB", (120, 180), (30, 60, 90)), Image.new("RGB", (200, 160), (90, 60, 30))]
        for format_ in ["JPEG", "PNG", "TIFF", "BMP", "WEBP"]:
            buffer = BytesIO()
            images[0].save(buffer, format=format_, dpi=(300, 300))
            scan = next(iter(read_scans(BytesIO(buffer.getvalue()), "test")))
            self.assertEqual(scan.image.shape[:2], (180, 120))
        buffer = BytesIO()
        images[0].save(buffer, format="TIFF", save_all=True, append_images=images[1:])
        pages = list(read_scans(BytesIO(buffer.getvalue()), "pages.tiff"))
        self.assertEqual([p.page for p in pages], [1, 2])
        self.assertEqual(pages[1].image.shape[:2], (160, 200))

    def test_invalid_regions_and_empty_scan(self):
        for q in [[[0, 0]] * 4, [[0, 0], [10, 0], [20, 0], [30, 0]]]:
            with self.assertRaises(ValueError):
                order_quad(q)
        with self.assertRaises(ValueError):
            validate_photo({"outer": [[0, 0], [100, 0], [100, 100], [0, 100]], "inner": [[0, 0], [120, 0], [120, 110], [0, 110]]}, (200, 200, 3))
        self.assertEqual(detect(Scan(np.full((800, 600, 3), 245, np.uint8), "empty.png")), [])


if __name__ == "__main__":
    unittest.main()
