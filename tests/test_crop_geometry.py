"""Source-coordinate geometry, upright edge names and repair anchoring."""
import copy
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from crop_geometry import effective_photo, validate_geometry
from rendering import render_photo
from scanner import Photo, Scan, compose, crops, inset_quad, order_quad


def fixture():
    rng = np.random.default_rng(531)
    scan = Scan(rng.integers(0, 256, (600, 800, 3), dtype=np.uint8), "geometry.png")
    photo = Photo(order_quad([[150, 120], [650, 120], [650, 480], [150, 480]]),
                  order_quad([[190, 160], [610, 160], [610, 400], [190, 400]]))
    return scan, photo


class GeometryTests(unittest.TestCase):
    def test_defaults_preserve_existing_full_resolution_pixels(self):
        scan, photo = fixture()
        source = scan.image.copy()
        for rotation in range(4):
            photo.rotation = rotation
            for trim in (0, 3):
                paper, picture = crops(scan, photo, trim=trim)
                result = render_photo(scan, photo, trim=trim)
                np.testing.assert_array_equal(result.images["paper"], paper)
                np.testing.assert_array_equal(result.images["image"], picture)
                np.testing.assert_array_equal(np.array(result.images["composition"]),
                                              np.array(compose(paper, picture)))
        np.testing.assert_array_equal(scan.image, source)
        self.assertEqual(render_photo(scan, photo).images["paper"].shape[:2], (501, 361))

    def test_upright_top_maps_to_original_left_after_clockwise_turn(self):
        scan, photo = fixture()
        photo.rotation = 1
        photo.presentation = {"trim_top": 11}
        adjusted = effective_photo(scan, photo)
        expected = photo.outer.copy()
        expected[[0, 3], 0] += 11
        np.testing.assert_allclose(adjusted.outer, expected)
        expected_inner = photo.inner.copy()
        expected_inner[[0, 3], 0] += 11
        np.testing.assert_allclose(adjusted.inner, expected_inner)
        result = render_photo(scan, photo)
        self.assertEqual(result.images["paper"].shape[:2], (490, 361))

    def test_four_edges_are_independent_in_all_rotations(self):
        scan, photo = fixture()
        amounts = [2, 4, 7, 9]
        photo.presentation = dict(zip(("trim_top", "trim_right", "trim_bottom", "trim_left"), amounts))
        for rotation in range(4):
            photo.rotation = rotation
            adjusted = effective_photo(scan, photo)
            t, r, b, l = [amounts[(i + rotation) % 4] for i in range(4)]
            expected = [[150+l, 120+t], [650-r, 120+t], [650-r, 480-b], [150+l, 480-b]]
            np.testing.assert_allclose(adjusted.outer, expected)

    def test_explicit_side_overrides_uniform_fallback(self):
        scan, photo = fixture()
        photo.presentation = {"trim": 5, "trim_left": 0}
        adjusted = effective_photo(scan, photo, legacy_trim=12)
        np.testing.assert_allclose(adjusted.outer, [[150, 125], [645, 125], [645, 475], [150, 475]])
        photo.presentation = {"trim": 5}
        np.testing.assert_array_equal(effective_photo(scan, photo).outer, inset_quad(photo.outer, 5))

    def test_trims_preserve_actual_picture_boundary_for_restoration(self):
        scan, photo = fixture()
        photo.presentation = {"trim_top": 3, "trim_left": 5}
        adjusted = effective_photo(scan, photo)
        np.testing.assert_array_equal(adjusted._geometry_inner, photo.inner)
        self.assertFalse(np.array_equal(adjusted.inner, photo.inner))

    def test_geometry_keeps_colour_and_auto_border_protection_on_real_source_picture(self):
        scan, photo = fixture()
        photo.presentation = {"angle": 6, "perspective_x": 3}
        photo.restoration = {"enabled": True, "auto_border": False,
                             "adjustments": True, "brightness": 20}
        rendered = render_photo(scan, photo)
        actual_picture = cv2.perspectiveTransform(photo.inner[None], rendered.source_to_paper)[0]
        inside = np.zeros(rendered.mask.shape, np.uint8)
        cv2.fillConvexPoly(inside, np.rint(actual_picture).astype(np.int32), 255)
        np.testing.assert_array_equal(rendered.images["paper"][inside == 0],
                                      rendered.original["paper"][inside == 0])

    def test_clockwise_angle_and_single_source_sampling(self):
        scan, photo = fixture()
        scan.image[:] = 240
        # A red level horizon rotates downwards from left to right for a
        # positive (clockwise) adjustment, including after upright quarter turns.
        cv2.line(scan.image, (240, 300), (560, 300), (230, 10, 10), 3)
        photo.presentation = {"angle": 5}
        original_warp = cv2.warpPerspective
        with patch("crop_geometry.cv2.warpPerspective", wraps=original_warp) as sampling:
            rendered = render_photo(scan, photo)
        self.assertEqual(sampling.call_count, 2)
        for call in sampling.call_args_list:
            self.assertIs(call.args[0], scan.image)
        image = rendered.images["image"]
        yy, xx = np.where((image[:, :, 0] > 150) & (image[:, :, 1] < 80))
        slope = np.polyfit(xx, yy, 1)[0]
        self.assertAlmostEqual(slope, np.tan(np.radians(5)), delta=.008)

    def test_geometry_and_render_do_not_mutate_material_or_strokes(self):
        scan, photo = fixture()
        photo.rotation = 3
        photo.presentation = {"angle": 2, "perspective_x": 4, "perspective_y": -3,
                              "trim_top": 1, "trim_bottom": 3}
        photo.restoration = {"enabled": True, "auto_border": False, "strokes": [
            {"mode": "paint", "points": [[400, 300]], "radius": 6}]}
        original = copy.deepcopy(photo.as_dict())
        source = scan.image.copy()
        first = render_photo(scan, photo)
        second = render_photo(scan, photo)
        self.assertEqual(photo.as_dict(), original)
        np.testing.assert_array_equal(source, scan.image)
        np.testing.assert_array_equal(first.images["paper"], second.images["paper"])
        point = cv2.perspectiveTransform(np.array([[[400, 300]]], np.float32), first.source_to_paper)[0, 0]
        x, y = np.rint(point).astype(int)
        self.assertGreater(first.mask[y, x], 0)
        self.assertGreater(np.abs(first.images["image"].astype(int)-first.original["image"].astype(int)).max(), 0)

    def test_positive_angle_rotates_source_sampling_counterclockwise(self):
        scan, photo = fixture()
        photo.presentation = {"angle": 4}
        adjusted = effective_photo(scan, photo)
        self.assertLess(adjusted.outer[1, 1], adjusted.outer[0, 1])
        self.assertEqual(adjusted.rotation, photo.rotation)

    def test_fine_rotation_does_not_jump_quarter_turn_at_diagonal(self):
        scan, photo = fixture()
        angle = np.radians(45)
        rotate = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        photo.outer = order_quad(np.array([[-100, -150], [100, -150], [100, 150], [-100, 150]]) @ rotate.T + [400, 300])
        photo.inner = None
        before = render_photo(scan, photo)
        photo.presentation = {"angle": 2}
        after = render_photo(scan, photo)
        self.assertEqual(before.images["paper"].shape, after.images["paper"].shape)
        adjusted = effective_photo(scan, photo)
        self.assertFalse(np.array_equal(adjusted.outer, order_quad(adjusted.outer)))
        mapped = cv2.perspectiveTransform(adjusted.outer[None], after.source_to_paper)[0]
        h, w = after.images["paper"].shape[:2]
        np.testing.assert_allclose(mapped, [[0, 0], [w-1, 0], [w-1, h-1], [0, h-1]], atol=.001)

    def test_perspective_is_common_source_transform(self):
        scan, photo = fixture()
        photo.presentation = {"perspective_x": 8, "perspective_y": -5}
        adjusted = effective_photo(scan, photo)
        transform = cv2.getPerspectiveTransform(photo.outer, adjusted.outer)
        projected = cv2.perspectiveTransform(photo.inner[None], transform)[0]
        np.testing.assert_allclose(adjusted.inner, projected, atol=.001)
        self.assertTrue(cv2.isContourConvex(adjusted.outer))
        self.assertGreater(render_photo(scan, photo).images["image"].shape[1], 350)

    def test_invalid_numbers_and_limits_are_rejected(self):
        for name, value in [("angle", float("nan")), ("trim_top", float("inf")),
                            ("perspective_y", -16), ("angle", 11), ("trim_left", -1),
                            ("trim_right", True), ("angle", "bad")]:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                validate_geometry({name: value})
        self.assertEqual(validate_geometry({"occupancy": .6, "angle": "1.5"}), {"angle": 1.5})

    def test_out_of_scan_and_excessive_crop_are_rejected(self):
        scan, photo = fixture()
        photo.outer = order_quad([[0, 0], [799, 0], [799, 599], [0, 599]])
        photo.presentation = {"angle": 8}
        with self.assertRaisesRegex(ValueError, "扫描范围"):
            effective_photo(scan, photo)
        photo.inner = None
        photo.outer = order_quad([[200, 200], [250, 200], [250, 250], [200, 250]])
        photo.presentation = {"trim_left": 30, "trim_right": 30}
        with self.assertRaises(ValueError):
            effective_photo(scan, photo)


if __name__ == "__main__":
    unittest.main()
