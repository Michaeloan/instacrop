"""Non-destructive crop adjustments expressed entirely in scan coordinates.

The render pipeline samples each crop from the original scan once. Fine angle
and perspective move the sampling quadrilateral in upright paper space; edge
trims are measured in source pixels and named after the upright output edges.
"""
from __future__ import annotations

from dataclasses import replace
import math

import cv2
import numpy as np

from scanner import inset_quad, quad_size


GEOMETRY_LIMITS = {
    "angle": (-10, 10), "perspective_x": (-15, 15), "perspective_y": (-15, 15),
    "trim_top": (0, 50), "trim_right": (0, 50),
    "trim_bottom": (0, 50), "trim_left": (0, 50),
}
SIDE_FIELDS = ("trim_top", "trim_right", "trim_bottom", "trim_left")


def sampling_size(quad):
    """Native edge lengths preserving semantic corner order after fine rotation."""
    width = max(np.linalg.norm(quad[1]-quad[0]), np.linalg.norm(quad[2]-quad[3]))
    height = max(np.linalg.norm(quad[3]-quad[0]), np.linalg.norm(quad[2]-quad[1]))
    return max(2, round(float(width))+1), max(2, round(float(height))+1)


def source_crops(scan, photo):
    """Sample original pixels once without reordering adjusted corner labels."""
    def sample(quad):
        width, height = sampling_size(quad)
        destination = np.array([[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]], np.float32)
        transform = cv2.getPerspectiveTransform(quad, destination)
        image = cv2.warpPerspective(scan.image, transform, (width, height), flags=cv2.INTER_CUBIC)
        return np.ascontiguousarray(np.rot90(image, -photo.rotation))
    return sample(photo.outer), sample(photo.inner) if photo.inner is not None else None


def validate_geometry(presentation):
    """Validate supplied fields without changing legacy/default presentation."""
    if not isinstance(presentation, dict):
        raise ValueError("照片几何设置格式不正确")
    result = {}
    for name, (low, high) in GEOMETRY_LIMITS.items():
        if name not in presentation:
            continue
        try:
            if isinstance(presentation[name], bool):
                raise ValueError()
            number = float(presentation[name])
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} 必须是有效数值") from None
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"{name} 超出允许范围 {low}–{high}")
        result[name] = number
    return result


def _convex_quad(quad):
    quad = np.asarray(quad, np.float32)
    # Keep corner correspondence: reordering a crossing polygon could hide an
    # excessive inset and misalign repairs or upright edge labels.
    if quad.shape != (4, 2) or not np.isfinite(quad).all():
        raise ValueError("几何调整后的四角无效")
    edges = np.roll(quad, -1, axis=0) - quad
    cross = edges[:, 0] * np.roll(edges[:, 1], -1) - edges[:, 1] * np.roll(edges[:, 0], -1)
    if np.any(cross <= 0) or cv2.contourArea(quad) < 16 or min(quad_size(quad)) < 3:
        raise ValueError("几何调整或收边过大，请减小数值")
    return quad


def _inset_sides(quad, amounts):
    if not any(amounts):
        return quad.copy()
    if len(set(amounts)) == 1:
        # Match the old trim path exactly, including its original pixel rounding.
        return inset_quad(quad, amounts[0]).copy()
    lines = []
    for index, (start, end) in enumerate(zip(quad, np.roll(quad, -1, axis=0))):
        direction = end - start
        direction = direction / np.linalg.norm(direction)
        inward = np.array([-direction[1], direction[0]])
        lines.append((start + inward * amounts[index], direction))
    corners = []
    try:
        for index in range(4):
            start, first = lines[index - 1]
            other, second = lines[index]
            distance = np.linalg.solve(np.column_stack((first, -second)), other - start)[0]
            corners.append(start + distance * first)
    except np.linalg.LinAlgError:
        raise ValueError("收边后的边缘不能相交，请减小数值") from None
    result = _convex_quad(corners)
    if cv2.contourArea(result) >= cv2.contourArea(quad) or any(
        cv2.pointPolygonTest(quad, tuple(map(float, point)), True) < -.01 for point in result
    ):
        raise ValueError("收边像素过大，请减小数值")
    return result


def _sampling_transform(quad, rotation, settings):
    w, h = quad_size(quad)
    if rotation % 2:
        w, h = h, w
    upright = np.array([[0, 0], [w-1, 0], [w-1, h-1], [0, h-1]], np.float32)
    to_upright = cv2.getPerspectiveTransform(np.roll(quad, rotation, axis=0), upright)
    sampling = upright.astype(np.float64)
    signs = np.array([1, -1, 1, -1])
    # Positive horizontal perspective pinches the upper edge and expands the
    # lower; vertical perspective pinches the left and expands the right.
    sampling[:, 0] += signs * settings.get("perspective_x", 0) * (w-1) / 200
    sampling[:, 1] += signs * settings.get("perspective_y", 0) * (h-1) / 200
    angle = math.radians(-settings.get("angle", 0))
    rotation_matrix = np.array([[math.cos(angle), -math.sin(angle)],
                                [math.sin(angle), math.cos(angle)]])
    center = np.array([(w-1)/2, (h-1)/2])
    sampling = (sampling-center) @ rotation_matrix.T + center
    change = cv2.getPerspectiveTransform(upright, _convex_quad(sampling))
    return np.linalg.inv(to_upright) @ change @ to_upright


def effective_photo(scan, photo, legacy_trim=0):
    """Return adjusted geometry on a copy; never alter stored quads or strokes."""
    settings = validate_geometry(photo.presentation)
    try:
        uniform = float(photo.presentation.get("trim", legacy_trim))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("向内收边应在 0–50 个原图像素之间") from None
    if not math.isfinite(uniform) or not 0 <= uniform <= 50:
        raise ValueError("向内收边应在 0–50 个原图像素之间")
    if photo.rotation not in range(4):
        raise ValueError("旋转只能是 0、1、2、3 个顺时针 90 度")
    amounts_upright = [settings.get(name, uniform) for name in SIDE_FIELDS]
    amounts = [amounts_upright[(index + photo.rotation) % 4] for index in range(4)]
    outer = np.asarray(photo.outer, np.float32).copy()
    inner = np.asarray(photo.inner, np.float32).copy() if photo.inner is not None else None
    if any(settings.get(name, 0) for name in ("angle", "perspective_x", "perspective_y")):
        transform = _sampling_transform(outer, photo.rotation, settings)
        outer = _convex_quad(cv2.perspectiveTransform(outer[None], transform)[0])
        if inner is not None:
            inner = _convex_quad(cv2.perspectiveTransform(inner[None], transform)[0])
    # The original picture boundary defines restoration/protection regions.
    # A display trim must not move this boundary or turn picture pixels into
    # the automatic white-border repair region.
    # Fine geometry moves the sampling window, not the physical picture on the
    # source scan. Keep colour adjustments and border repair anchored to that
    # real source boundary just like the user's source-coordinate brush strokes.
    restoration_inner = np.asarray(photo.inner, np.float32).copy() if photo.inner is not None else None
    outer = _inset_sides(outer, amounts)
    if inner is not None:
        inner = _inset_sides(inner, amounts)
    height, width = scan.image.shape[:2]
    for quad in (outer, inner):
        if quad is None:
            continue
        if (quad[:, 0].min() < -.01 or quad[:, 1].min() < -.01 or
                quad[:, 0].max() > width-1+.01 or quad[:, 1].max() > height-1+.01):
            raise ValueError("几何调整超出了原扫描范围，请减小旋转／透视，或先向内收边")
    result = replace(photo, outer=outer, inner=inner, warnings=list(photo.warnings))
    result._geometry_inner = restoration_inner
    return result
