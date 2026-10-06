import unittest
from unittest.mock import patch

import cv2
import numpy as np

from crop_detection import analyze_photo, refine_edges, snap_point


def perspective_print(background=180, paper=245):
    image = np.full((460, 540, 3), background, np.uint8)
    outer = np.array([[92, 66], [428, 83], [404, 395], [72, 371]], np.float32)
    cv2.fillConvexPoly(image, (outer + [5, 5]).astype(np.int32), (115, 115, 115))
    cv2.fillConvexPoly(image, outer.astype(np.int32), (paper,) * 3)
    src = np.array([[0, 0], [335, 0], [335, 319], [0, 319]], np.float32)
    inner_rect = np.array([[22, 20], [313, 20], [313, 239], [22, 239]], np.float32)
    matrix = cv2.getPerspectiveTransform(src, outer)
    inner = cv2.perspectiveTransform(inner_rect[None], matrix)[0]
    cv2.fillConvexPoly(image, inner.astype(np.int32), (40, 82, 140))
    return image, outer, inner


class CropDetectionTests(unittest.TestCase):
    def test_perspective_refinement_uses_paper_edge_not_shadow(self):
        image, outer, _ = perspective_print()
        initial = outer + [[3, -3], [-3, 3], [4, -3], [-3, 4]]
        refined, diagnostic = refine_edges(image, initial, paper=True, radius=9)
        self.assertLess(np.linalg.norm(refined - outer, axis=1).mean(), 1.6)
        self.assertEqual(len(diagnostic['edges']), 4)
        self.assertFalse(diagnostic['uncertain'])
        self.assertTrue(all(edge['support'] > .7 for edge in diagnostic['edges']))

    def test_dark_paper_polarity(self):
        image, outer, _ = perspective_print(background=215, paper=25)
        refined, diagnostic = refine_edges(image, outer + [2, -2], paper=True, radius=7)
        self.assertLess(np.linalg.norm(refined - outer, axis=1).mean(), 1.6)
        self.assertTrue(all(edge['polarity'] == 'dark_inside' for edge in diagnostic['edges']))

    def test_missing_and_weak_edges_do_not_invent_rectangles(self):
        image = np.full((240, 300, 3), 220, np.uint8)
        quad = np.array([[25, 25], [260, 25], [260, 200], [25, 200]], np.float32)
        refined, diagnostics = refine_edges(image, quad, radius=10, paper=True)
        np.testing.assert_array_equal(refined, quad)
        self.assertTrue(diagnostics['uncertain'])
        self.assertTrue(all(edge['weak'] for edge in diagnostics['edges']))
        # A very low contrast region remains a review item rather than certainty.
        cv2.fillConvexPoly(image, quad.astype(np.int32), (222, 222, 222))
        _, diagnostics = refine_edges(image, quad, radius=7)
        self.assertTrue(diagnostics['uncertain'])

    def test_border_not_recognized_as_print_edge(self):
        image, _, inner = perspective_print()
        quad = np.array([[0, 40], [435, 40], [435, 410], [0, 410]], np.float32)
        result = analyze_photo(image, {'outer': quad, 'inner': inner, 'rotation': 0})
        self.assertTrue(result['needs_review'])
        self.assertTrue(any('扫描边缘' in reason for reason in result['review_reasons']))
        self.assertTrue(any('边缘' in reason for reason in result['review_reasons']))

    def test_thick_margin_orientation_and_ambiguous_scene(self):
        image, outer, inner = perspective_print()
        result = analyze_photo(image, {'outer': outer, 'inner': inner, 'rotation': 0})
        self.assertEqual(result['orientation']['rotation'], 0)
        self.assertEqual(result['orientation']['confidence'], 'high')
        # A hand-rotated photo is never overwritten by the geometric suggestion.
        result = analyze_photo(image, {'outer': outer, 'inner': inner, 'rotation': 1})
        self.assertEqual(result['orientation']['current_rotation'], 1)
        self.assertEqual(result['orientation']['rotation'], 0)
        self.assertTrue(any('方向' in reason for reason in result['review_reasons']))
        result = analyze_photo(image, {'outer': outer, 'inner': None, 'rotation': 0})
        self.assertIsNone(result['orientation']['rotation'])
        self.assertEqual(result['orientation']['confidence'], 'low')
        self.assertNotIn('背面', ' '.join(result['review_reasons']))

    def test_corner_snapping_is_local_and_reversible_on_missing_evidence(self):
        image, outer, _ = perspective_print()
        point = outer[0] + [4, -4]
        snapped = snap_point(image, outer, 0, point, radius=10, paper=True)
        self.assertLess(np.linalg.norm(snapped - outer[0]), 1.8)
        blank = np.full_like(image, 240)
        np.testing.assert_array_equal(snap_point(blank, outer, 0, point, radius=10), point)
        # Far-away release points should never jump to a distant corner.
        outlier = np.array([22., 20.], np.float32)
        np.testing.assert_array_equal(snap_point(image, outer, 0, outlier, radius=10), outlier)

    def test_invalid_inputs_and_no_mutation(self):
        image, outer, _ = perspective_print()
        before = image.copy()
        quad_before = outer.copy()
        refine_edges(image, outer, paper=True)
        np.testing.assert_array_equal(image, before)
        np.testing.assert_array_equal(outer, quad_before)
        with self.assertRaises(ValueError):
            refine_edges(image, [[0, 0]] * 4)
        with self.assertRaises(ValueError):
            snap_point(image, outer, 4, [10, 10])
        with self.assertRaises(ValueError):
            refine_edges(image.astype(float), outer)

    def test_stronger_neighbour_does_not_pull_print_edge(self):
        image = np.full((340, 360, 3), 200, np.uint8)
        quad = np.array([[80, 45], [285, 45], [285, 290], [80, 290]], np.float32)
        cv2.fillConvexPoly(image, quad.astype(np.int32), (242, 242, 242))
        image[35:305, 66:73] = 255
        initial = quad + [3, 0]
        refined, _ = refine_edges(image, initial, paper=True, radius=16)
        self.assertLess(np.max(np.linalg.norm(refined - quad, axis=1)), 1.5)

    def test_face_orientation_is_absolute_and_manual_confirmation_is_respected(self):
        image, outer, inner = perspective_print()
        class FaceEvidence:
            def __init__(self):
                self.calls = 0
            def detectMultiScale3(self, *args, **kwargs):
                score = [0., 0., 7.3, 0.][self.calls % 4]
                self.calls += 1
                return [], [], np.array([[score]])
        with patch('crop_detection._face_cascade', return_value=FaceEvidence()):
            result = analyze_photo(image, {'outer': outer, 'inner': inner, 'rotation': 2})
        self.assertEqual(result['orientation']['suggested_rotation'], 2)
        self.assertEqual(result['orientation']['confidence'], 'high')
        self.assertFalse(any('方向' in reason for reason in result['review_reasons']))
        confirmed = {'outer': outer, 'inner': None, 'rotation': 1,
                     'presentation': {'orientation_confirmed': True}}
        result = analyze_photo(image, confirmed)
        self.assertFalse(any('方向' in reason for reason in result['review_reasons']))

    def test_ordinary_photo_does_not_receive_missing_paper_border_warnings(self):
        image = np.full((120, 150, 3), 90, np.uint8)
        quad = np.array([[0, 0], [149, 0], [149, 119], [0, 119]], np.float32)
        result = analyze_photo(image, {'outer': quad, 'inner': quad, 'method': 'imported-photo',
                                      'presentation': {'orientation_confirmed': True}})
        self.assertFalse(result['needs_review'])
        self.assertFalse(result['scan_boundary'])

    def test_review_confirmation_never_hides_missing_picture_or_scan_boundary(self):
        image = np.full((120, 150, 3), 230, np.uint8)
        quad = np.array([[0, 10], [139, 10], [139, 109], [0, 109]], np.float32)
        result = analyze_photo(image, {'outer': quad, 'inner': None, 'presentation': {
            'orientation_confirmed': True, 'review_confirmed': True}})
        self.assertTrue(result['needs_review'])
        self.assertTrue(any('扫描边缘' in reason for reason in result['review_reasons']))
        self.assertTrue(any('内部画面未可靠识别' in reason for reason in result['review_reasons']))
        self.assertFalse(any('边缘证据不足' in reason for reason in result['review_reasons']))

    def test_json_safe_diagnostics_and_conflicting_face_evidence_stay_uncertain(self):
        import json
        image, outer, inner = perspective_print()
        class ConflictingFaces:
            def detectMultiScale3(self, *args, **kwargs):
                return [], [], np.array([[6.5]])
        with patch('crop_detection._face_cascade', return_value=ConflictingFaces()):
            result = analyze_photo(image, {'outer': outer, 'inner': inner, 'rotation': 0})
        self.assertEqual(result['orientation']['evidence'], 'thick_paper_margin')
        self.assertTrue(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
