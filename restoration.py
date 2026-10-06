"""Optional local dust inpainting and image adjustments, with explicit masks.

Brush strokes are anchored to source-image pixels, so rotating a print or
adjusting its crop does not move the user's repairs to a different subject.
"""
from __future__ import annotations

import math
import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter


def defaults():
    return {"enabled": False, "auto_border": True, "auto_image": False,
            "sensitivity": 40, "max_diameter": 14, "radius": 3,
            "adjustments": False, "brightness": 0, "contrast": 0,
            "saturation": 0, "sharpen": 0, "denoise": 0, "strokes": []}


def validate_restoration(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("修复设置格式不正确")
    result = defaults()
    for name in ("enabled", "auto_border", "auto_image", "adjustments"):
        if name in value:
            if not isinstance(value[name], bool):
                raise ValueError(f"{name} 必须是开关")
            result[name] = value[name]
    for name, (low, high) in {"sensitivity": (1, 100), "max_diameter": (3, 80), "radius": (1, 12),
                              "brightness": (-40, 40), "contrast": (-40, 40), "saturation": (-100, 100),
                              "sharpen": (0, 100), "denoise": (0, 10)}.items():
        if name in value:
            number = float(value[name])
            if not math.isfinite(number) or not low <= number <= high:
                raise ValueError(f"{name} 超出允许范围 {low}–{high}")
            result[name] = number
    strokes = value.get("strokes", [])
    if not isinstance(strokes, list) or len(strokes) > 500:
        raise ValueError("每张照片最多保存 500 笔修复")
    total = 0
    for stroke in strokes:
        if not isinstance(stroke, dict) or stroke.get("mode", "paint") not in ("paint", "erase"):
            raise ValueError("画笔只能修复或排除区域")
        points = np.asarray(stroke.get("points", []), dtype=np.float32)
        total += len(points)
        radius = float(stroke.get("radius", 6))
        if points.ndim != 2 or points.shape[1:] != (2,) or not 1 <= len(points) <= 4000 or not np.isfinite(points).all():
            raise ValueError("画笔坐标不正确")
        if not math.isfinite(radius) or not 1 <= radius <= 120:
            raise ValueError("画笔半径应在 1–120 个原图像素之间")
        if total > 15000:
            raise ValueError("手动修复点过多，请分段处理")
        result["strokes"].append({"mode": stroke.get("mode", "paint"), "radius": radius, "points": points.tolist()})
    return result


def auto_dust(image, allowed, sensitivity, max_diameter, texture_guard=None):
    """Small light/dark isolated defects; never claims to recognise real content."""
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    size = max(5, int(max_diameter) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    dark = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)
    light = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    residual = np.maximum(dark, light)
    threshold = max(12, 50 - float(sensitivity) * .38)
    candidate = ((residual > threshold) & (allowed > 0)).astype(np.uint8)
    if texture_guard is not None:
        # Scanner dust is usually near-neutral light/dark specks. Coloured
        # foliage, flowers and fabric details are left for the manual brush.
        chroma = image.max(axis=2).astype(np.int16) - image.min(axis=2).astype(np.int16)
        candidate[(texture_guard > 0) & (chroma > max(16, sensitivity * .25))] = 0
    count, labels, stats, centers = cv2.connectedComponentsWithStats(candidate, 8)
    texture = None
    if texture_guard is not None and np.any(texture_guard):
        # Estimate surrounding texture excluding proposed defects themselves.
        # High texture (hair, foliage, patterned clothes) is deliberately protected.
        clean = (candidate == 0).astype(np.float32)
        values = gray.astype(np.float32)
        window = max(17, int(max_diameter * 3) | 1)
        weight = np.maximum(.02, cv2.boxFilter(clean, -1, (window, window)))
        mean = cv2.boxFilter(values * clean, -1, (window, window)) / weight
        second = cv2.boxFilter(values * values * clean, -1, (window, window)) / weight
        texture = np.sqrt(np.maximum(0, second - mean * mean))
    lookup = np.zeros(count, np.uint8)
    accepted = 0
    max_area = math.pi * (max_diameter / 2) ** 2
    # Keep elongated edges and larger subject features out of the dust mask.
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        if not 2 <= area <= max_area or max(w, h) > max_diameter * 1.35 or max(w, h) / max(1, min(w, h)) > 5:
            continue
        cx, cy = np.rint(centers[i]).astype(int)
        if texture is not None and texture_guard[cy, cx] and texture[cy, cx] > 12 + sensitivity * .12:
            continue
        lookup[i] = 255
        accepted += 1
    mask = cv2.dilate(lookup[labels], np.ones((3, 3), np.uint8))
    mask[allowed == 0] = 0
    if texture_guard is not None:
        mask[(texture_guard > 0) & (chroma > max(16, sensitivity * .25))] = 0
    return mask, accepted


def brush_mask(shape, initial, strokes, source_to_paper):
    mask = initial.copy()
    for stroke in strokes:
        source = np.asarray(stroke["points"], np.float32)
        mapped = cv2.perspectiveTransform(source[None], source_to_paper)[0]
        # Transform brush radius to crop pixels, supporting mild perspective.
        shifted = cv2.perspectiveTransform((source + [stroke["radius"], 0]).astype(np.float32)[None], source_to_paper)[0]
        radii = np.clip(np.linalg.norm(shifted - mapped, axis=1), 1, 200)
        value = 255 if stroke["mode"] == "paint" else 0
        previous = None
        for point, radius in zip(mapped, radii):
            xy = tuple(np.rint(point).astype(int))
            if previous is not None:
                cv2.line(mask, previous[0], xy, value, max(1, round(float(previous[1] + radius))), cv2.LINE_8)
            cv2.circle(mask, xy, max(1, round(float(radius))), value, -1)
            previous = (xy, radius)
    return mask


def process(paper, inner_quad, source_to_paper, settings):
    options = validate_restoration(settings)
    h, w = paper.shape[:2]
    inside = np.zeros((h, w), np.uint8)
    if inner_quad is not None:
        cv2.fillConvexPoly(inside, np.rint(inner_quad).astype(np.int32), 255)
    auto_mask = np.zeros((h, w), np.uint8)
    mask = auto_mask.copy()
    stats = {"auto_spots": 0, "masked_pixels": 0, "manual_strokes": len(options["strokes"]), "warnings": []}
    if not options["enabled"]:
        return paper.copy(), mask, stats
    if options["auto_border"] or options["auto_image"]:
        if inner_quad is None:
            stats["warnings"].append("内部画面未确认，自动修复暂不可用；仍可手动修复")
        else:
            allowed = np.zeros((h, w), np.uint8)
            safe_inside = cv2.erode(inside, np.ones((7, 7), np.uint8))
            if options["auto_border"]:
                allowed = cv2.bitwise_not(cv2.dilate(inside, np.ones((9, 9), np.uint8)))
                allowed[:4] = 0; allowed[-4:] = 0; allowed[:, :4] = 0; allowed[:, -4:] = 0
            if options["auto_image"]:
                allowed = cv2.bitwise_or(allowed, safe_inside)
                stats["warnings"].append("内部自动修复可能选中高光或画面细节，请检查紫色区域并用排除画笔保护")
            auto_mask, stats["auto_spots"] = auto_dust(paper, allowed, options["sensitivity"], options["max_diameter"], inside)
    mask = brush_mask((h, w), auto_mask, options["strokes"], source_to_paper)
    stats["masked_pixels"] = int(np.count_nonzero(mask))
    if stats["masked_pixels"] > h * w * .2:
        raise ValueError("修复区域超过相纸面积的 20%，请降低灵敏度或减少涂抹范围")
    repaired = paper.copy()
    if stats["masked_pixels"]:
        # All operations are local interpolation, no external model or uploads.
        repaired = cv2.inpaint(repaired, mask, options["radius"], cv2.INPAINT_TELEA)
    if options["adjustments"]:
        if inner_quad is None:
            stats["warnings"].append("内部画面未确认，画面微调暂不应用")
        else:
            adjusted = repaired.copy()
            if options["denoise"]:
                adjusted = cv2.fastNlMeansDenoisingColored(adjusted, None, options["denoise"], options["denoise"], 7, 11)
            pil = Image.fromarray(adjusted)
            pil = ImageEnhance.Brightness(pil).enhance(1 + options["brightness"] / 100)
            pil = ImageEnhance.Contrast(pil).enhance(1 + options["contrast"] / 100)
            pil = ImageEnhance.Color(pil).enhance(1 + options["saturation"] / 100)
            if options["sharpen"]:
                pil = pil.filter(ImageFilter.UnsharpMask(radius=1.3, percent=round(options["sharpen"] * 1.5), threshold=3))
            adjusted = np.array(pil)
            # Affect the image only, keeping the real paper's colour and texture.
            repaired[inside > 0] = adjusted[inside > 0]
    return repaired, mask, stats
