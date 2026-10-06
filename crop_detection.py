"""Conservative local edge refinement and evidence-based crop review.

Coordinates always refer to the immutable original RGB scan. A weak or missing
edge stays where the user put it. Neither empty pictures nor faces identify the
back of a print; orientation is only a suggestion and never changes the input.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import threading

import cv2
import numpy as np


EDGE_NAMES = ('上', '右', '下', '左')
_FACE_LOCK = threading.Lock()


class _YuNetEvidence:
    """Adapt the tiny local OpenCV face detector to conservative upright evidence."""
    rgb = True

    def __init__(self, model):
        self.detector = cv2.FaceDetectorYN_create('onnx', np.frombuffer(model, dtype=np.uint8),
                                                 np.empty(0, np.uint8), (320, 320), .85, .3, 200)

    def detectMultiScale3(self, image, **kwargs):
        h, w = image.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        accepted, weights = [], []
        for face in faces if faces is not None else []:
            if min(float(face[2]), float(face[3])) < 28:
                continue
            right_eye, left_eye = face[4:6], face[6:8]
            nose = face[8:10]
            mouth = (face[10:12] + face[12:14]) / 2
            eyes = (right_eye + left_eye) / 2
            delta = right_eye - left_eye
            # Detection confidence by itself cannot establish uprightness. Both
            # eyes should be level, and the mouth below eyes with the nose between.
            if (abs(float(delta[0])) < abs(float(delta[1])) * 2
                    or mouth[1] - eyes[1] < max(2., float(face[3]) * .12)
                    or not eyes[1] <= nose[1] <= mouth[1] + float(face[3]) * .05):
                continue
            accepted.append(face[:4])
            # ≥.9125 is usable directional evidence; ≥.95 with a clear winner is
            # high confidence. Faded paper scans often fall short of .99.
            weights.append((float(face[14]) - .8) * 40)
        return accepted, [], np.asarray(weights, dtype=np.float32)


def _inputs(image, quad, radius):
    image = np.asarray(image)
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8 or min(image.shape[:2]) < 4:
        raise ValueError('边缘分析需要 RGB uint8 原图')
    q = np.asarray(quad, dtype=np.float32)
    if q.shape != (4, 2) or not np.isfinite(q).all() or not cv2.isContourConvex(q) or cv2.contourArea(q) < 16:
        raise ValueError('需要按顺序排列的四个有效角点')
    # A clockwise traversal in image coordinates has inward-facing left normals.
    if cv2.contourArea(q, oriented=True) <= 0:
        raise ValueError('角点顺序应为左上、右上、右下、左下')
    if not np.isfinite(radius) or not 1 <= float(radius) <= 128:
        raise ValueError('边缘搜索半径应在 1–128 原图像素之间')
    h, w = image.shape[:2]
    if np.any(q < 0) or np.any(q[:, 0] > w - 1) or np.any(q[:, 1] > h - 1):
        raise ValueError('角点超出原图边界')
    return image, q.copy(), float(radius)


def _edge(gray, start, end, radius, paper):
    vector = end - start
    length = float(np.linalg.norm(vector))
    tangent = vector / length
    normal = np.array([-tangent[1], tangent[0]], np.float32)
    count = min(360, max(32, int(length / 3)))
    t = np.linspace(.08, .92, count, dtype=np.float32)
    positions = start + t[:, None] * vector
    # Extra samples let us compare both sides of each candidate without zero
    # padding appearing as a scan edge.
    offsets = np.arange(-radius - 3, radius + 3.25, .5, dtype=np.float32)
    coords = positions[None] + offsets[:, None, None] * normal
    values = cv2.remap(gray, coords[:, :, 0], coords[:, :, 1], cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
    valid = ((coords[:, :, 0] >= 1) & (coords[:, :, 0] <= gray.shape[1] - 2)
             & (coords[:, :, 1] >= 1) & (coords[:, :, 1] <= gray.shape[0] - 2))
    signed = np.zeros_like(values)
    signed[4:-4] = values[8:] - values[:-8]
    valid[4:-4] &= valid[:-8] & valid[8:]
    valid[:4] = valid[-4:] = False
    polarity = 'either'
    inward_band = (offsets >= 3) & (offsets <= radius + 1)
    outward_band = (offsets <= -3) & (offsets >= -radius - 1)
    inward_values = values[inward_band][valid[inward_band]]
    outward_values = values[outward_band][valid[outward_band]]
    if paper and inward_values.size and outward_values.size:
        inside = float(np.median(inward_values))
        outside = float(np.median(outward_values))
        # White paper can be darker than a white scanner lid; absolute brightness
        # alone would reject its real edge. Compare both sides of this local line.
        if inside - outside >= 3:
            polarity = 'light_inside'
        elif outside - inside >= 3:
            polarity = 'dark_inside'
        elif inside >= 185:
            polarity = 'light_inside'
        elif inside <= 75:
            polarity = 'dark_inside'
    response = np.maximum(signed, 0) if polarity == 'light_inside' else (
        np.maximum(-signed, 0) if polarity == 'dark_inside' else np.abs(signed))
    response[~valid] = 0
    # Search whole lines rather than allowing every sample to chase the strongest
    # neighbouring object. Saturation makes a shadow or neighbour no more valuable
    # than a supported print edge; locality breaks ties.
    grid_step = max(1., radius / 14)
    grid = np.arange(-radius, radius + grid_step / 2, grid_step, dtype=np.float32)
    aa, bb = np.meshgrid(grid, grid, indexing='ij')
    candidates = aa.ravel()[:, None] * (1 - t) + bb.ravel()[:, None] * t
    indices = np.clip(np.rint((candidates - offsets[0]) * 2).astype(np.int32), 0, len(offsets) - 1)
    sampled = response[indices, np.arange(count)]
    support = np.mean(sampled >= 3.5, axis=1)
    strength_score = np.mean(np.minimum(sampled / 12, 1), axis=1)
    locality = np.mean(np.abs(candidates), axis=1) / radius
    scores = .65 * support + .35 * strength_score - .12 * locality
    best = int(np.argmax(scores))
    predicted = candidates[best]
    # Refine within two pixels of the selected coherent line, using a robust fit.
    nearby_offsets = np.arange(-2, 2.25, .5, dtype=np.float32)
    nearby_indices = np.clip(np.rint((predicted[None] + nearby_offsets[:, None] - offsets[0]) * 2).astype(np.int32), 0, len(offsets) - 1)
    nearby_response = response[nearby_indices, np.arange(count)]
    maximum = np.max(nearby_response, axis=0)
    # Average ties: a hard step's symmetric derivative plateau must not bias a
    # fitted edge toward the first pixel in that plateau.
    near_max = nearby_response >= np.maximum(3.5, maximum[None] * .94)
    selected_offsets = offsets[nearby_indices]
    chosen_offset = np.sum(selected_offsets * near_max, axis=0) / np.maximum(1, np.sum(near_max, axis=0))
    keep = maximum >= 3.5
    support_value = float(np.mean(keep))
    bins = np.array_split(keep, 8)
    continuity = float(np.mean([float(np.mean(part)) >= .5 for part in bins]))
    median_strength = float(np.median(maximum[keep])) if np.any(keep) else 0.
    line = (start, tangent)
    residual = None
    angle = None
    if np.count_nonzero(keep) >= 12:
        points = positions[keep] + chosen_offset[keep, None] * normal
        vx, vy, x, y = cv2.fitLine(points.astype(np.float32), cv2.DIST_HUBER, 0, .01, .01).ravel()
        fitted_tangent = np.array([vx, vy], np.float32)
        if np.dot(fitted_tangent, tangent) < 0:
            fitted_tangent *= -1
        fitted_normal = np.array([-fitted_tangent[1], fitted_tangent[0]], np.float32)
        residual = float(np.percentile(np.abs((points - [x, y]) @ fitted_normal), 85))
        angle = float(np.degrees(np.arccos(np.clip(np.dot(fitted_tangent, tangent), -1, 1))))
        line = (np.array([x, y], np.float32), fitted_tangent)
    reliable = (support_value >= .58 and continuity >= .625 and median_strength >= 3.5
                and residual is not None and residual <= max(1.5, radius * .12)
                and angle is not None and angle <= 8)
    border = bool(np.mean(np.any(~valid[4:-4], axis=0)) > .25 and np.mean(valid[len(offsets) // 2]) < .75)
    diagnostic = {
        'support': round(support_value, 4), 'continuity': round(continuity, 4),
        'strength': round(median_strength, 3), 'residual_px': round(residual, 3) if residual is not None else None,
        'straightness': round(max(0., 1 - residual / max(2., radius)), 4) if residual is not None else 0.,
        'score': round(max(0., min(1., float(scores[best]))), 4), 'weak': not bool(reliable),
        'border': border, 'polarity': polarity, 'angle_degrees': round(angle, 3) if angle is not None else None,
    }
    return line if reliable else (start, tangent), diagnostic


def refine_edges(image, quad, paper=False, radius=8):
    """Return refined source-pixel quad and JSON-safe edge diagnostics.

    Only intersections of two adequately supported local lines move a corner.
    The caller decides whether to accept suggestions; neither argument mutates.
    """
    image, q, radius = _inputs(image, quad, radius)
    # Full-resolution evidence only needs this print and its small search halo;
    # avoid allocating a full float32 scanner page for every gallery card.
    h, w = image.shape[:2]
    origin = np.maximum(0, np.floor(q.min(axis=0) - radius - 5)).astype(np.int32)
    end = np.minimum([w, h], np.ceil(q.max(axis=0) + radius + 6)).astype(np.int32)
    region = image[origin[1]:end[1], origin[0]:end[0]]
    gray = cv2.GaussianBlur(cv2.cvtColor(region, cv2.COLOR_RGB2GRAY).astype(np.float32), (3, 3), 0)
    local_quad = (q - origin).astype(np.float32)
    lines, edges = [], []
    for i in range(4):
        line, diagnostic = _edge(gray, local_quad[i], local_quad[(i + 1) % 4], radius, bool(paper))
        diagnostic['name'] = EDGE_NAMES[i]
        lines.append(line)
        edges.append(diagnostic)
    refined = q.copy()
    for i in range(4):
        if edges[i - 1]['weak'] or edges[i]['weak']:
            continue
        p, u = lines[i - 1]
        r, v = lines[i]
        matrix = np.column_stack((u, -v))
        if abs(float(np.linalg.det(matrix))) < .15:
            continue
        point = p + float(np.linalg.solve(matrix, r - p)[0]) * u + origin
        if (0 <= point[0] <= w - 1 and 0 <= point[1] <= h - 1
                and np.linalg.norm(point - q[i]) <= radius * 1.8):
            refined[i] = point
    accepted = bool(cv2.isContourConvex(refined) and cv2.contourArea(refined) >= 16)
    if not accepted:
        refined = q
    return refined.astype(np.float32), {
        'edges': edges, 'uncertain': bool(any(edge['weak'] or edge['border'] for edge in edges)),
        'refined': bool(accepted and np.max(np.abs(refined - q)) > .05),
        'max_movement_px': round(float(np.max(np.linalg.norm(refined - q, axis=1))), 3),
    }


def snap_point(image, quad, index, point, radius=12, paper=False):
    """Snap one release point to two local supported edges; fail unchanged."""
    image, q, radius = _inputs(image, quad, radius)
    if isinstance(index, bool) or not isinstance(index, (int, np.integer)) or index not in range(4):
        raise ValueError('角点编号应为 0–3')
    point = np.asarray(point, dtype=np.float32)
    if point.shape != (2,) or not np.isfinite(point).all():
        raise ValueError('吸附位置需要两个有效坐标')
    h, w = image.shape[:2]
    if not (0 <= point[0] <= w - 1 and 0 <= point[1] <= h - 1):
        raise ValueError('吸附位置超出原图边界')
    if np.linalg.norm(point - q[index]) > radius * 1.8:
        return point.copy()
    trial = q.copy()
    trial[index] = point
    if not cv2.isContourConvex(trial):
        return point.copy()
    refined, diagnostic = refine_edges(image, trial, paper=paper, radius=radius)
    adjacent = (diagnostic['edges'][index - 1], diagnostic['edges'][index])
    if any(edge['weak'] or edge['border'] for edge in adjacent) or np.linalg.norm(refined[index] - point) > radius:
        return point.copy()
    return refined[index].copy()


@lru_cache(maxsize=1)
def _face_cascade():
    # cv2 wheels normally ship this small local detector. A packaged application
    # can also include the cascade beside this module; absence is graceful.
    model = Path(__file__).parent / 'tools' / 'haarcascades' / 'face_detection_yunet_2023mar.onnx'
    if hasattr(cv2, 'FaceDetectorYN_create') and model.is_file():
        try:
            return _YuNetEvidence(model.read_bytes())
        except (cv2.error, OSError):
            pass
    if not hasattr(cv2, 'CascadeClassifier'):
        return None
    data_dir = getattr(getattr(cv2, 'data', None), 'haarcascades', '')
    candidates = [Path(data_dir) / 'haarcascade_frontalface_default.xml',
                  Path(__file__).parent / 'tools' / 'haarcascades' / 'haarcascade_frontalface_default.xml']
    for path in candidates:
        if path.is_file():
            try:
                # Read UTF-8 XML through Python to support Chinese Windows paths
                # with native OpenCV builds whose filename API is ANSI-only.
                storage = cv2.FileStorage(path.read_text(encoding='utf-8'),
                                          cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
                cascade = cv2.CascadeClassifier()
                loaded = cascade.read(storage.getFirstTopLevelNode())
                storage.release()
                if loaded and not cascade.empty():
                    return cascade
            except (cv2.error, OSError, UnicodeError):
                continue
    return None


def _face_orientation(image, quad):
    cascade = _face_cascade()
    if cascade is None:
        return None
    widths = [np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3])]
    heights = [np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1])]
    width, height = max(widths), max(heights)
    factor = min(1., 640 / max(width, height))
    width, height = max(2, round(width * factor)), max(2, round(height * factor))
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], np.float32)
    matrix = cv2.getPerspectiveTransform(quad, dst)
    patch = cv2.warpPerspective(image, matrix, (width, height), flags=cv2.INTER_AREA)
    gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
    scores = []
    with _FACE_LOCK:
        for rotation in range(4):
            rotated = np.ascontiguousarray(np.rot90(patch if getattr(cascade, 'rgb', False) else gray, -rotation))
            # Haar evidence is deliberately strict. A weak pattern resembling a
            # face does not justify rotating a building, text, or empty picture.
            try:
                _, _, weights = cascade.detectMultiScale3(rotated, scaleFactor=1.15, minNeighbors=7,
                                                           minSize=(28, 28), outputRejectLevels=True)
            except cv2.error:
                return None
            weights = np.asarray(weights).ravel()
            valid = weights[weights >= 4.5]
            scores.append(float(np.max(valid) + min(2, len(valid) - 1)) if len(valid) else 0.)
    order = np.argsort(scores)
    winner, runner = int(order[-1]), float(scores[int(order[-2])])
    if scores[winner] < 4.5 or scores[winner] < runner * 1.35 or scores[winner] - runner < 2:
        return None
    return {'suggested_rotation': winner, 'rotation': winner,
            'confidence': 'high' if scores[winner] >= 6 and runner == 0 else 'medium',
            'reason': '该方向检测到较清晰的正立人脸；方向建议可手动更改',
            'evidence': 'upright_faces', 'face_scores': [round(score, 3) for score in scores]}


def _margin_orientation(outer, inner, reliable):
    if inner is None or not reliable:
        return None
    width = max(np.linalg.norm(outer[1] - outer[0]), np.linalg.norm(outer[2] - outer[3]))
    height = max(np.linalg.norm(outer[3] - outer[0]), np.linalg.norm(outer[2] - outer[1]))
    dst = np.array([[0, 0], [width, 0], [width, height], [0, height]], np.float32)
    matrix = cv2.getPerspectiveTransform(outer, dst)
    iq = cv2.perspectiveTransform(inner[None].astype(np.float32), matrix)[0]
    margins = np.array([iq[:, 1].min(), width - iq[:, 0].max(), height - iq[:, 1].max(), iq[:, 0].min()])
    if np.min(margins) < 1:
        return None
    side = int(np.argmax(margins))
    sorted_margins = np.sort(margins)
    if sorted_margins[-1] < sorted_margins[-2] * 1.65 or sorted_margins[-1] - sorted_margins[-2] < min(width, height) * .06:
        return None
    rotation = [2, 1, 0, 3][side]
    return {'suggested_rotation': rotation, 'rotation': rotation, 'confidence': 'high',
            'reason': '相纸与内部画面边缘清晰，较宽白边建议置于下方；不据此猜测画面内容',
            'evidence': 'thick_paper_margin', 'margins_px': [round(float(value), 3) for value in margins]}


def analyze_photo(image, photo):
    """Review a Photo or serialized photo without changing crop or rotation."""
    data = photo if isinstance(photo, dict) else {
        'outer': photo.outer, 'inner': photo.inner, 'rotation': photo.rotation,
            'warnings': getattr(photo, 'warnings', []), 'presentation': getattr(photo, 'presentation', {}),
            'method': getattr(photo, 'method', '')}
    image, outer, _ = _inputs(image, data.get('outer'), 8)
    inner_value = data.get('inner')
    inner = _inputs(image, inner_value, 8)[1] if inner_value is not None else None
    # Search is local and scale-aware, never a replacement full-scene detector.
    radius = min(24, max(5, round(min(image.shape[:2]) / 550)))
    borderless = data.get('method') in ('imported-photo', 'manual-borderless', 'borderless')
    if borderless:
        outer_diagnostic = {'edges': [], 'uncertain': False, 'refined': False, 'max_movement_px': 0.}
        inner_diagnostic = {'edges': [], 'uncertain': False, 'refined': False, 'max_movement_px': 0.} if inner is not None else None
    else:
        _, outer_diagnostic = refine_edges(image, outer, paper=True, radius=radius)
        inner_diagnostic = refine_edges(image, inner, paper=False, radius=radius)[1] if inner is not None else None
    reasons = []
    h, w = image.shape[:2]
    boundary = bool(not borderless and (np.any(outer[:, 0] <= 2) or np.any(outer[:, 1] <= 2)
                    or np.any(outer[:, 0] >= w - 3) or np.any(outer[:, 1] >= h - 3)))
    if boundary:
        reasons.append('相纸接近扫描边缘，可能缺角或误含扫描背景；请核对四角')
    weak = [EDGE_NAMES[i] for i, edge in enumerate(outer_diagnostic['edges']) if edge['weak']]
    reviewed = bool(data.get('presentation', {}).get('review_confirmed', False))
    if weak and not reviewed:
        reasons.append('相纸' + '、'.join(weak) + '边缘证据不足，可能存在阴影、遮挡或多余背景')
    if inner is None and not borderless:
        reasons.append('内部画面未可靠识别；请补四角或确认无白边照片')
    elif inner is not None and not borderless:
        if any(cv2.pointPolygonTest(outer, (float(point[0]), float(point[1])), True) < -1.5 for point in inner):
            reasons.append('内部画面超出相纸四角，请核对两组边缘')
        if inner_diagnostic['uncertain'] and not reviewed:
            reasons.append('内部画面边缘不完整或较弱，请核对是否裁入白边或丢失画面')
    for warning in data.get('warnings', []):
        if isinstance(warning, str) and warning and warning not in reasons:
            reasons.append(warning)
    orientation = _face_orientation(image, inner if inner is not None else outer) if inner is not None or borderless else None
    if orientation is None and not borderless:
        orientation = _margin_orientation(outer, inner, not outer_diagnostic['uncertain']
                                          and inner_diagnostic is not None and not inner_diagnostic['uncertain'])
    if orientation is None:
        orientation = {'suggested_rotation': None, 'rotation': None, 'confidence': 'low',
                       'reason': '缺少可靠的正立人脸或明显宽白边；请人工确认画面方向', 'evidence': 'none'}
    current = int(data.get('rotation', 0)) % 4
    orientation['current_rotation'] = current
    confirmed = bool(data.get('presentation', {}).get('orientation_confirmed', False))
    orientation['confirmed'] = confirmed
    if not confirmed:
        if orientation['suggested_rotation'] is None:
            reasons.append('画面方向待确认；空白或弱纹理不能可靠区分正立、倒置或侧转')
        elif orientation['suggested_rotation'] != current:
            reasons.append('画面方向与自动建议不同，请确认是否倒置或侧转')
    return {'review_reasons': reasons, 'needs_review': bool(reasons), 'orientation': orientation,
            'edges': outer_diagnostic['edges'], 'outer': outer_diagnostic, 'inner': inner_diagnostic,
            'scan_boundary': boundary}
