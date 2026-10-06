"""Local, format-independent extraction of separated rectangular photo prints.

All quadrilaterals use source-image pixels. Film presets are advisory only;
neither detection nor warping assumes a brand, a size, or a photo count.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from io import BytesIO
import json
import math
from pathlib import Path
import re
from typing import Iterable

import cv2
import numpy as np
from PIL import Image, ImageFilter, ImageOps, ImageSequence

SUPPORTED = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
MAX_PIXELS = 200_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

# Width/height in millimetres; used only to describe results after extraction.
# Sources: https://www.instax.com/film/ and Polaroid Support photo dimensions.
FILM_FORMATS = {
    "Instax Mini": ((54.0, 86.0), (46.0, 62.0)),
    "Instax Square": ((72.0, 86.0), (62.0, 62.0)),
    "Instax Wide": ((108.0, 86.0), (99.0, 62.0)),
    "Polaroid SX-70 / 600 / i-Type": ((88.47, 107.52), (76.801, 78.94)),
    "Polaroid Go": ((53.9, 66.6), (46.0, 47.0)),
}


@dataclass
class Settings:
    detection_size: int = 2200
    min_area: float = 0.002
    sensitivity: float = 1.0

    def validate(self):
        if not 600 <= self.detection_size <= 4000:
            raise ValueError("识别长边应在 600–4000 像素之间")
        if not 0.0001 <= self.min_area <= 0.2:
            raise ValueError("最小面积占比应在 0.0001–0.2 之间")
        if not 0.3 <= self.sensitivity <= 3:
            raise ValueError("灵敏度应在 0.3–3 之间")


@dataclass
class Scan:
    image: np.ndarray  # RGB uint8
    name: str
    dpi: tuple[float, float] | None = None
    page: int = 1


@dataclass
class Photo:
    outer: np.ndarray
    inner: np.ndarray | None = None
    rotation: int = 0  # clockwise quarter turns; shared by all three exports
    warnings: list[str] = field(default_factory=list)
    method: str = "edges"
    enabled: bool = True
    restoration: dict = field(default_factory=dict)
    presentation: dict = field(default_factory=dict)

    def as_dict(self, dpi=None):
        result = {
            "outer": self.outer.tolist(),
            "inner": self.inner.tolist() if self.inner is not None else None,
            "rotation": self.rotation,
            "warnings": self.warnings,
            "method": self.method,
            "enabled": self.enabled,
            "restoration": self.restoration,
            "presentation": self.presentation,
            "format_hints": format_hints(self.outer, self.inner, dpi),
        }
        analysis = getattr(self, "crop_analysis", None)
        if analysis:
            result.update(analysis=analysis, needs_review=analysis["needs_review"],
                          review_reasons=analysis["review_reasons"])
        return result


def read_scans(source: str | Path | BytesIO, name: str | None = None) -> Iterable[Scan]:
    """Decode raster formats, honour EXIF orientation, iterate TIFF pages."""
    with Image.open(source) as opened:
        dpi = opened.info.get("dpi")
        try:
            dpi = tuple(float(v) for v in dpi[:2])
            if len(dpi) != 2 or not all(math.isfinite(v) and v > 0 for v in dpi):
                dpi = None
        except (TypeError, ValueError):
            dpi = None
        for index, frame in enumerate(ImageSequence.Iterator(opened)):
            if frame.width * frame.height > MAX_PIXELS:
                raise ValueError("单页超过 2 亿像素，请先降低扫描分辨率")
            oriented = ImageOps.exif_transpose(frame)
            if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                white = Image.new("RGBA", rgba.size, "white")
                white.alpha_composite(rgba)
                oriented = white
            yield Scan(np.array(oriented.convert("RGB")), name or Path(source).name, dpi, index + 1)


def order_quad(points) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32)
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise ValueError("裁剪区域需要四个有效角点")
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    points = points[np.argsort(angles)]
    points = np.roll(points, -int(np.argmin(points.sum(axis=1))), axis=0)
    if not cv2.isContourConvex(points) or cv2.contourArea(points) < 16:
        raise ValueError("四角必须组成没有交叉的凸四边形")
    return points


def quad_size(q):
    q = order_quad(q)
    w = max(np.linalg.norm(q[1] - q[0]), np.linalg.norm(q[2] - q[3]))
    h = max(np.linalg.norm(q[3] - q[0]), np.linalg.norm(q[2] - q[1]))
    return max(2, round(float(w)) + 1), max(2, round(float(h)) + 1)


def warp(image, quad, max_side=None):
    q = order_quad(quad)
    w, h = quad_size(q)
    if max_side and max(w, h) > max_side:
        factor = max_side / max(w, h)
        w, h = max(2, round(w * factor)), max(2, round(h * factor))
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32)
    matrix = cv2.getPerspectiveTransform(q, dst)
    return cv2.warpPerspective(image, matrix, (w, h), flags=cv2.INTER_CUBIC), matrix


def containment(a, b):
    """Intersection as fraction of the smaller polygon, plus its IoU."""
    aa, ab = cv2.contourArea(a), cv2.contourArea(b)
    intersection, _ = cv2.intersectConvexConvex(a.astype(np.float32), b.astype(np.float32))
    return float(intersection / max(1, min(aa, ab))), float(intersection / max(1, aa + ab - intersection))


def rectangular_candidates(image, settings):
    h, w = image.shape[:2]
    total = h * w
    gray = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    masks = []
    for low, high in [(8, 25), (18, 55), (35, 105)]:
        edge = cv2.Canny(gray, low / settings.sensitivity, high / settings.sensitivity)
        masks.append(cv2.morphologyEx(edge, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)))
    # Colour/dark regions supply seeds when picture edges are discontinuous.
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    neutral_background = np.percentile(gray, 80)
    foreground = ((gray < neutral_background - 28) | (hsv[:, :, 1] > 45)).astype(np.uint8) * 255
    masks.append(cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)))
    raw = []
    for mask in masks:
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = cv2.contourArea(contour)
            if not settings.min_area * total < area < 0.85 * total:
                continue
            rect = cv2.minAreaRect(contour)
            rw, rh = rect[1]
            if min(rw, rh) < 20 or min(rw, rh) / max(rw, rh) < 0.12:
                continue
            fill = area / max(1, rw * rh)
            if fill < 0.83:
                continue
            hull = cv2.convexHull(contour)
            approx = cv2.approxPolyDP(hull, 0.018 * cv2.arcLength(hull, True), True)
            if len(approx) != 4:
                continue
            q = order_quad(approx.reshape(4, 2))
            # Ignore scanner-bed frame and full-page borders.
            near = (q[:, 0] < 4) | (q[:, 0] > w - 5) | (q[:, 1] < 4) | (q[:, 1] > h - 5)
            if near.sum() >= 3:
                continue
            patch, _ = warp(image, q, 160)
            if np.std(patch.astype(np.float32), axis=(0, 1)).mean() < 7:
                continue
            raw.append((q, fill))
    # Collapse duplicate inner/outer contour traces without collapsing nesting.
    unique = []
    for q, fill in sorted(raw, key=lambda item: item[1], reverse=True):
        if any(containment(q, prior)[1] > 0.92 for prior in unique):
            continue
        unique.append(q)
    return sorted(unique, key=cv2.contourArea, reverse=True)


def refine_quad(image, q, radius=4, paper=False, gray=None):
    """Fit each long edge near its coarse estimate, independent of print size."""
    if gray is None:
        gray = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_RGB2GRAY), (3, 3), 0)
    lines = []
    for start, end in zip(q, np.roll(q, -1, axis=0)):
        vector = end - start
        length = np.linalg.norm(vector)
        tangent = vector / length
        normal = np.array([-tangent[1], tangent[0]])
        positions = start + np.linspace(0.08, 0.92, min(650, max(30, int(length / 3))))[:, None] * vector
        offsets = np.arange(-radius, radius + .5, .5, dtype=np.float32)
        coords = positions[None, :, :] + offsets[:, None, None] * normal
        values = cv2.remap(gray.astype(np.float32), coords[:, :, 0].astype(np.float32), coords[:, :, 1].astype(np.float32), cv2.INTER_LINEAR)
        signed = np.gradient(values, axis=0)
        derivative = np.abs(signed)
        # Select the paper-side edge of the scanner shadow for light/dark frames.
        if paper:
            inward = np.median(values[-max(2, len(offsets) // 5):])
            if inward > 180:
                derivative = np.maximum(signed, 0)
            elif inward < 75:
                derivative = np.maximum(-signed, 0)
        validity = (coords[:, :, 0] >= 1) & (coords[:, :, 0] < gray.shape[1] - 2) & (coords[:, :, 1] >= 1) & (coords[:, :, 1] < gray.shape[0] - 2)
        derivative[~validity] = 0
        index = np.argmax(derivative, axis=0)
        strength = derivative[index, np.arange(len(positions))]
        chosen = positions + offsets[index, None] * normal
        sample_offsets = offsets[index]
        median_offset = np.median(sample_offsets)
        mad = np.median(np.abs(sample_offsets - median_offset))
        keep = (strength > max(0.35, np.percentile(strength, 20))) & (np.abs(sample_offsets - median_offset) < max(2, 3 * mad))
        chosen = chosen[keep]
        if len(chosen) < 8:
            lines.append((start, tangent))
        else:
            vx, vy, x, y = cv2.fitLine(chosen.astype(np.float32), cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
            lines.append((np.array([x, y]), np.array([vx, vy])))
    corners = []
    for i in range(4):
        p, u = lines[i - 1]
        r, v = lines[i]
        mat = np.column_stack((u, -v))
        if abs(np.linalg.det(mat)) < 0.1:
            return q
        t = np.linalg.solve(mat, r - p)[0]
        corners.append(p + t * u)
    refined = order_quad(corners)
    return refined if np.linalg.norm(refined - q, axis=1).max() < radius * 3 else q


def grow_frame(image, inner):
    """Recover weak paper edges outside an image seed using long-line evidence.

    No film dimensions are used. Missing edges return no guess rather than
    silently adding a preset white border.
    """
    w, h = quad_size(inner)
    pad_x, pad_y = round(w * 0.40), round(h * 0.40)
    dst = np.array([[pad_x, pad_y], [pad_x + w - 1, pad_y],
                    [pad_x + w - 1, pad_y + h - 1], [pad_x, pad_y + h - 1]], np.float32)
    matrix = cv2.getPerspectiveTransform(inner, dst)
    pw, ph = w + 2 * pad_x, h + 2 * pad_y
    patch = cv2.warpPerspective(image, matrix, (pw, ph), flags=cv2.INTER_LINEAR)
    valid = cv2.warpPerspective(np.ones(image.shape[:2], np.uint8), matrix, (pw, ph), flags=cv2.INTER_NEAREST)
    gray = cv2.GaussianBlur(cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY).astype(np.float32), (5, 5), 0)
    dx = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)) / 8
    dy = np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)) / 8
    x0, y0 = pad_x, pad_y
    x1, y1 = x0 + w - 1, y0 + h - 1
    sx, sy = max(3, round(w * .08)), max(3, round(h * .08))
    profiles = [
        (dx[y0 + sy:y1 - sy, :], valid[y0 + sy:y1 - sy, :], max(2, x0 - int(.35 * w)), x0 - max(5, int(.015 * w))),
        (dx[y0 + sy:y1 - sy, :], valid[y0 + sy:y1 - sy, :], x1 + max(5, int(.015 * w)), min(pw - 2, x1 + int(.35 * w))),
        (dy[:, x0 + sx:x1 - sx].T, valid[:, x0 + sx:x1 - sx].T, max(2, y0 - int(.35 * h)), y0 - max(5, int(.015 * h))),
        (dy[:, x0 + sx:x1 - sx].T, valid[:, x0 + sx:x1 - sx].T, y1 + max(5, int(.015 * h)), min(ph - 2, y1 + int(.35 * h))),
    ]
    edges = []
    scores = []
    for data, mask, low, high in profiles:
        profile = np.percentile(data, 40, axis=0)
        # Exclude the scan boundary and padding introduced by the transform.
        validity = np.mean(mask, axis=0)
        for j in range(low, high):
            if np.min(validity[max(0, j - 4):j + 5]) < .98:
                profile[j] = 0
        search = profile[low:high]
        if not len(search):
            return None
        j = low + int(np.argmax(search))
        score = float(profile[j])
        if score < max(.6, float(np.median(search)) * 4):
            return None
        edges.append(j)
        scores.append(score)
    left, right, top, bottom = edges
    candidate_dst = np.array([[left, top], [right, top], [right, bottom], [left, bottom]], np.float32)
    outer = cv2.perspectiveTransform(candidate_dst[None], np.linalg.inv(matrix))[0]
    # A true frame should surround all sides and have calmer texture than the image.
    ring = np.zeros((ph, pw), np.uint8)
    cv2.fillConvexPoly(ring, candidate_dst.astype(np.int32), 1)
    ring[max(0, y0 - 3):y1 + 4, max(0, x0 - 3):x1 + 4] = 0
    ring = cv2.erode(ring, np.ones((5, 5), np.uint8))
    frame_gradient = (dx + dy)[ring.astype(bool)]
    interior_gradient = (dx + dy)[y0 + sy:y1 - sy, x0 + sx:x1 - sx]
    if len(frame_gradient) < 20 or np.median(frame_gradient) > max(1.2, np.median(interior_gradient) * .65):
        return None
    return order_quad(outer)


def find_inner(image, outer):
    patch, matrix = warp(image, outer, 1100)
    h, w = patch.shape[:2]
    candidates = rectangular_candidates(patch, Settings(1100, .12, 1.2))
    for q in candidates:
        area_ratio = cv2.contourArea(q) / (w * h)
        if not .25 < area_ratio < .92:
            continue
        # Content must sit inside the paper on all four sides.
        if q[:, 0].min() < .012 * w or q[:, 1].min() < .012 * h or q[:, 0].max() > .988 * w or q[:, 1].max() > .988 * h:
            continue
        q = refine_quad(patch, q)
        return order_quad(cv2.perspectiveTransform(q[None], np.linalg.inv(matrix))[0])
    return None


def default_rotation(outer, inner):
    """Only orient by an obvious thick margin; never guess the scene's subject."""
    if inner is None:
        return 0
    w, h = quad_size(outer)
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32)
    matrix = cv2.getPerspectiveTransform(outer, dst)
    iq = cv2.perspectiveTransform(inner[None], matrix)[0]
    margins = [float(iq[:, 1].min()), float(w - 1 - iq[:, 0].max()),
               float(h - 1 - iq[:, 1].max()), float(iq[:, 0].min())]
    sorted_margins = sorted(margins)
    if sorted_margins[-1] < sorted_margins[-2] * 1.4:
        return 0
    return [2, 1, 0, 3][int(np.argmax(margins))]


def detect(scan: Scan, settings: Settings | None = None) -> list[Photo]:
    settings = settings or Settings()
    settings.validate()
    height, width = scan.image.shape[:2]
    factor = min(1.0, settings.detection_size / max(height, width))
    small = cv2.resize(scan.image, (round(width * factor), round(height * factor)), interpolation=cv2.INTER_AREA)
    candidates = rectangular_candidates(small, settings)
    selected = []
    for q in candidates:
        if any(containment(q, old.outer)[0] > .94 for old in selected):
            continue
        # A candidate may already be the paper, or just its colourful image.
        inner = find_inner(small, q)
        method = "paper-edges"
        if inner is None:
            grown = grow_frame(small, q)
            if grown is not None:
                inner, q, method = q, grown, "image-and-frame-lines"
        q = refine_quad(small, q)
        if inner is not None:
            inner = refine_quad(small, inner)
        warnings = []
        if inner is None:
            warnings.append("内部画面未可靠识别；请手动框选四角，或标记为无边框照片")
        if any(containment(q, old.outer)[0] > .85 for old in selected):
            continue
        sw, sh = small.shape[1], small.shape[0]
        if np.any(q[:, 0] < 2) or np.any(q[:, 1] < 2) or np.any(q[:, 0] > sw - 3) or np.any(q[:, 1] > sh - 3):
            warnings.append("相纸接近扫描边缘，可能缺角；缺失部分无法从扫描图恢复")
        q[:, 0] = np.clip(q[:, 0], 0, sw - 1)
        q[:, 1] = np.clip(q[:, 1], 0, sh - 1)
        photo = Photo(order_quad(q), order_quad(inner) if inner is not None else None,
                      warnings=warnings, method=method)
        photo.rotation = default_rotation(photo.outer, photo.inner)
        selected.append(photo)
    radius = max(5, round(5 / factor))
    from crop_detection import refine_edges, analyze_photo
    for photo in selected:
        photo.outer /= factor
        photo.outer, _ = refine_edges(scan.image, photo.outer, radius=min(40, radius), paper=True)
        photo.outer[:, 0] = np.clip(photo.outer[:, 0], 0, width - 1)
        photo.outer[:, 1] = np.clip(photo.outer[:, 1], 0, height - 1)
        if photo.inner is not None:
            photo.inner /= factor
            photo.inner, _ = refine_edges(scan.image, photo.inner, radius=min(40, radius))
        photo.rotation = default_rotation(photo.outer, photo.inner)
        analysis = analyze_photo(scan.image, photo)
        orientation = analysis["orientation"]
        suggestion = orientation.get("suggested_rotation")
        if orientation.get("confidence") == "high" and isinstance(suggestion, int) and suggestion in range(4):
            photo.rotation = suggestion
            analysis = analyze_photo(scan.image, photo)
        photo.crop_analysis = analysis
    # Cluster nearby vertical centres, avoiding arbitrary row-bucket boundaries.
    if selected:
        tolerance = np.median([quad_size(p.outer)[1] for p in selected]) * .45
        rows = []
        for photo in sorted(selected, key=lambda p: float(p.outer[:, 1].mean())):
            center_y = float(photo.outer[:, 1].mean())
            if rows and abs(center_y - np.mean([p.outer[:, 1].mean() for p in rows[-1]])) < tolerance:
                rows[-1].append(photo)
            else:
                rows.append([photo])
        selected = [p for row in rows for p in sorted(row, key=lambda p: float(p.outer[:, 0].mean()))]
    return selected


def format_hints(outer, inner, dpi=None):
    w, h = quad_size(outer)
    measured = np.sort([w, h])
    ratio = measured[0] / measured[1]
    hints = []
    for name, (paper, content) in FILM_FORMATS.items():
        expected = np.sort(paper)
        ratio_error = abs(ratio - expected[0] / expected[1])
        if ratio_error > .055:
            continue
        label = name + "（比例相近）"
        # DPI is useful only when both axes have the same physical scale.
        if dpi and abs(dpi[0] / dpi[1] - 1) < .01 and min(dpi) >= 72:
            mm = measured * 25.4 / dpi[0]
            if np.max(np.abs(mm - expected) / expected) > .08:
                continue
            label = name + "（尺寸相近，需确认 DPI）"
        hints.append(label)
    return hints or ["其他 / 未知格式"]


def validate_photo(data, shape):
    from restoration import validate_restoration
    h, w = shape[:2]
    def stable_quad(value):
        normalized = order_quad(value)
        supplied = np.asarray(value, dtype=np.float32)
        # Preserve semantic corner labels for already clockwise ordered edits.
        # A min(x+y) tie near 45 degrees must not silently rotate the crop.
        if cv2.isContourConvex(supplied) and cv2.contourArea(supplied, oriented=True) > 0:
            return supplied.copy()
        return normalized
    outer = stable_quad(data["outer"])
    inner = stable_quad(data["inner"]) if data.get("inner") is not None else None
    for q in [outer] + ([inner] if inner is not None else []):
        if q[:, 0].min() < 0 or q[:, 1].min() < 0 or q[:, 0].max() > w - 1 or q[:, 1].max() > h - 1:
            raise ValueError("角点超出了扫描图范围")
    if inner is not None and any(cv2.pointPolygonTest(outer, tuple(map(float, point)), True) < -.5 for point in inner):
        raise ValueError("内部画面四角应位于相纸四角以内")
    rotation = int(data.get("rotation", 0))
    if rotation not in range(4):
        raise ValueError("旋转只能是 0、1、2、3 个顺时针 90 度")
    restoration = validate_restoration(data.get("restoration"))
    for stroke in restoration["strokes"]:
        points = np.asarray(stroke["points"])
        if points[:, 0].min() < 0 or points[:, 1].min() < 0 or points[:, 0].max() > w - 1 or points[:, 1].max() > h - 1:
            raise ValueError("手动修复坐标超出了原图范围")
    presentation=data.get("presentation",{})
    if not isinstance(presentation,dict):raise ValueError("照片布局设置不正确")
    for key,low,high in [("trim",0,50),("occupancy",.2,.95)]:
        if key in presentation and not low<=float(presentation[key])<=high:raise ValueError("照片边缘或背景大小超出范围")
    from crop_geometry import validate_geometry
    validated_presentation={key:float(presentation[key]) for key in ("trim","occupancy") if key in presentation}
    validated_presentation.update(validate_geometry(presentation))
    for key in ("orientation_confirmed", "review_confirmed"):
        if key in presentation:
            if not isinstance(presentation[key], bool):
                raise ValueError("方向和边缘确认状态必须是开关")
            validated_presentation[key] = presentation[key]
    presentation = validated_presentation
    return Photo(outer, inner, rotation, list(data.get("warnings", [])),
                 str(data.get("method", "manual")), bool(data.get("enabled", True)), restoration, presentation)


def inset_quad(quad, pixels):
    """Inset fitted edges in source coordinates; resample the original only once."""
    if not math.isfinite(pixels) or not 0 <= pixels <= 50:
        raise ValueError("向内收边应在 0–50 个原图像素之间")
    if not pixels:
        return quad
    if min(quad_size(quad)) <= pixels * 2 + 2:
        raise ValueError("收边像素过大，请减小数值")
    lines = []
    for start, end in zip(quad, np.roll(quad, -1, axis=0)):
        direction = end - start
        direction /= np.linalg.norm(direction)
        inward = np.array([-direction[1], direction[0]])
        lines.append((start + inward * pixels, direction))
    corners = []
    for i in range(4):
        p, u = lines[i - 1]
        r, v = lines[i]
        t = np.linalg.solve(np.column_stack((u, -v)), r - p)[0]
        corners.append(p + t * u)
    result = order_quad(corners)
    if cv2.contourArea(result) >= cv2.contourArea(quad):
        raise ValueError("收边像素过大，请减小数值")
    return result


def crops(scan, photo, max_side=None, trim=0):
    paper, _ = warp(scan.image, inset_quad(photo.outer, trim), max_side)
    picture = warp(scan.image, inset_quad(photo.inner, trim), max_side)[0] if photo.inner is not None else None
    paper = np.ascontiguousarray(np.rot90(paper, -photo.rotation))
    if picture is not None:
        picture = np.ascontiguousarray(np.rot90(picture, -photo.rotation))
    return paper, picture


def compose(paper, picture, occupancy=.78, long_edge=None):
    """Clear image as background, full print centred; canvas follows image ratio."""
    if not .2 <= occupancy <= .95:
        raise ValueError("居中相纸占比应在 0.2–0.95 之间")
    background = Image.fromarray(picture)
    # Tiny edge-measurement differences should not make square compositions oblong.
    # Centre-crop the background only; preserve original paper and image crops.
    if abs(background.width / background.height - 1) < .025:
        side = min(background.size)
        background = ImageOps.fit(background, (side, side), Image.Resampling.LANCZOS)
    if long_edge:
        w, h = background.size
        scale = long_edge / max(w, h)
        background = background.resize((max(2, round(w * scale)), max(2, round(h * scale))), Image.Resampling.LANCZOS)
    w, h = background.size
    foreground = Image.fromarray(paper)
    scale = min(w * occupancy / foreground.width, h * occupancy / foreground.height)
    foreground = foreground.resize((max(2, round(foreground.width * scale)), max(2, round(foreground.height * scale))), Image.Resampling.LANCZOS)
    x, y = (w - foreground.width) // 2, (h - foreground.height) // 2
    # A small shadow separates the real paper from the identical clear background.
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    rectangle = Image.new("RGBA", foreground.size, (0, 0, 0, 95))
    shadow.paste(rectangle, (x + max(1, w // 200), y + max(1, h // 150)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, min(w, h) / 150)))
    background = Image.alpha_composite(background.convert("RGBA"), shadow)
    background.paste(foreground, (x, y))
    return background.convert("RGB")


def safe_stem(name):
    stem = Path(name).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "scan"
    if stem.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}:
        stem = "scan_" + stem
    return stem[:100]


def png_bytes(image, dpi=None):
    stream = BytesIO()
    if isinstance(image, np.ndarray):
        image = Image.fromarray(image)
    image.save(stream, format="PNG", **({"dpi": dpi} if dpi else {}))
    return stream.getvalue()


def preview_image(scan, photos, max_side=1800):
    h, w = scan.image.shape[:2]
    factor = min(1, max_side / max(h, w))
    canvas = cv2.resize(scan.image, (round(w * factor), round(h * factor)))
    for i, photo in enumerate(photos, 1):
        q = (photo.outer * factor).astype(np.int32)
        cv2.polylines(canvas, [q], True, (33, 122, 192), 3)
        if photo.inner is not None:
            cv2.polylines(canvas, [(photo.inner * factor).astype(np.int32)], True, (229, 131, 34), 2)
        pos = tuple(np.maximum(12, q[0]).astype(int))
        cv2.putText(canvas, str(i), pos, cv2.FONT_HERSHEY_SIMPLEX, .9, (33, 122, 192), 3)
    return canvas


def export_to_directory(scan, photos, output, occupancy=.78, trim=0):
    from rendering import render_photo
    # A new directory prevents accidental overwrites on repeated/batch runs.
    base = Path(output) / f"{safe_stem(scan.name)}_page{scan.page}"
    destination = base
    number = 2
    while destination.exists():
        destination = base.with_name(base.name + f"_{number}")
        number += 1
    destination.mkdir(parents=True)
    manifest = {"source": scan.name, "page": scan.page, "source_size": [scan.image.shape[1], scan.image.shape[0]],
                "dpi": scan.dpi, "trim_pixels": trim, "composition": {"background": "clear", "aspect": "inner-image; near-square snapped to 1:1", "occupancy": occupancy}, "photos": []}
    for i, photo in enumerate(photos, 1):
        if not photo.enabled:
            continue
        rendered = render_photo(scan, photo, trim, occupancy)
        record = photo.as_dict(scan.dpi)
        record["id"] = i
        record["files"] = {}
        images = rendered.images
        if "image" not in images:
            record["warnings"] = record["warnings"] + ["仅导出相纸：内部画面未确认"]
        record["restoration_stats"] = rendered.stats
        for mode, img in images.items():
            path = destination / mode / f"{i:03d}.png"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(png_bytes(img, scan.dpi if mode != "composition" else None))
            record["files"][mode] = str(path.relative_to(destination))
        if rendered.active:
            for mode, img in rendered.original.items():
                path = destination / "original" / mode / f"{i:03d}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(png_bytes(img, scan.dpi if mode != "composition" else None))
                record["files"]["original_" + mode] = str(path.relative_to(destination))
            path = destination / "masks" / f"{i:03d}.png"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(png_bytes(rendered.mask))
            record["files"]["mask"] = str(path.relative_to(destination))
        manifest["photos"].append(record)
    (destination / "preview.png").write_bytes(png_bytes(preview_image(scan, photos)))
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination, manifest


def main():
    parser = argparse.ArgumentParser(description="混合尺寸相纸识别裁剪，输出相纸、内部画面和清晰背景居中成图")
    parser.add_argument("input", type=Path, help="扫描图或包含扫描图的文件夹")
    parser.add_argument("--output", type=Path, default=Path("outputs"))
    parser.add_argument("--min-area", type=float, default=.002, help="候选最小面积占整页比例，默认 0.002")
    parser.add_argument("--sensitivity", type=float, default=1.0)
    parser.add_argument("--detection-size", type=int, default=2200)
    parser.add_argument("--occupancy", type=float, default=.78, help="中央相纸占画布最大宽/高的比例")
    parser.add_argument("--trim", type=float, default=0, help="向内收边的原图像素数，默认 0；建议 1–3")
    parser.add_argument("--review", type=Path, help="使用已导出的 manifest.json 四角重新导出单页")
    args = parser.parse_args()
    settings = Settings(args.detection_size, args.min_area, args.sensitivity)
    settings.validate()
    if not .2 <= args.occupancy <= .95:
        parser.error("occupancy 应在 0.2–0.95 之间")
    if not 0 <= args.trim <= 50:
        parser.error("trim 应在 0–50 之间")
    if not args.input.exists():
        parser.error("输入文件或文件夹不存在")
    files = sorted(p for p in args.input.iterdir() if p.suffix.lower() in SUPPORTED and p.is_file()) if args.input.is_dir() else [args.input]
    if args.review and (args.input.is_dir() or len(files) != 1):
        parser.error("--review 仅适用于一个扫描文件")
    reviewed = json.loads(args.review.read_text(encoding="utf-8")) if args.review else None
    failures = 0
    for path in files:
        try:
            for scan in read_scans(path):
                if reviewed:
                    if scan.page != reviewed["page"]:
                        continue
                    if [scan.image.shape[1], scan.image.shape[0]] != reviewed["source_size"] or scan.name != reviewed["source"]:
                        raise ValueError("复核记录与原图名称或尺寸不匹配")
                    photos = [validate_photo(item, scan.image.shape) for item in reviewed["photos"]]
                else:
                    photos = detect(scan, settings)
                destination, manifest = export_to_directory(scan, photos, args.output, args.occupancy, args.trim)
                pending = sum(bool(p["warnings"]) for p in manifest["photos"])
                print(f"{path.name} 第 {scan.page} 页：{len(photos)} 张，{pending} 张需检查；{destination}")
        except Exception as exc:
            failures += 1
            print(f"处理失败 {path}: {exc}")
    if not files:
        parser.error("文件夹中没有支持的扫描图")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
