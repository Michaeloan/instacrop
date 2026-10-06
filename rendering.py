"""One full-resolution pipeline shared by preview, export and saved projects."""
from dataclasses import dataclass
import cv2
import numpy as np

from scanner import compose
from restoration import process, validate_restoration
from crop_geometry import effective_photo, sampling_size, source_crops


@dataclass
class Rendered:
    original: dict
    images: dict
    mask: np.ndarray
    source_to_paper: np.ndarray
    stats: dict
    active: bool


def render_photo(scan, photo, trim=0, occupancy=.78):
    occupancy=photo.presentation.get("occupancy",occupancy)
    photo = effective_photo(scan, photo, legacy_trim=trim)
    q = photo.outer
    w, h = sampling_size(q)
    dst = np.array([[0, 0], [w-1, 0], [w-1, h-1], [0, h-1]], np.float32)
    matrix = cv2.getPerspectiveTransform(q, dst)
    rotations = [np.eye(3), np.array([[0, -1, h-1], [1, 0, 0], [0, 0, 1]]),
                 np.array([[-1, 0, w-1], [0, -1, h-1], [0, 0, 1]]),
                 np.array([[0, 1, 0], [-1, 0, w-1], [0, 0, 1]])]
    matrix = rotations[photo.rotation] @ matrix
    paper, picture = source_crops(scan, photo)
    restoration_inner = photo._geometry_inner
    inner = cv2.perspectiveTransform(restoration_inner[None], matrix.astype(np.float64))[0] if restoration_inner is not None else None
    options = validate_restoration(photo.restoration)
    repaired, mask, stats = process(paper, inner, matrix, options)
    original = {"paper": paper}
    images = {"paper": repaired}
    if picture is not None:
        original.update(image=picture, composition=compose(paper, picture, occupancy))
        if options["enabled"]:
            # Match the existing upright image crop dimensions and corner order,
            # avoiding double interpolation of the actual source pixels outside masks.
            target_h, target_w = picture.shape[:2]
            corner_order = np.roll(photo.inner, photo.rotation, axis=0)
            iq = cv2.perspectiveTransform(corner_order[None], matrix)[0].astype(np.float32)
            destination = np.array([[0, 0], [target_w-1, 0], [target_w-1, target_h-1], [0, target_h-1]], np.float32)
            image_matrix = cv2.getPerspectiveTransform(iq, destination)
            # Warp the repair/adjustment delta only. Preserve original image pixels
            # when no change is requested, and avoid another full image resampling.
            delta = repaired.astype(np.float32) - paper.astype(np.float32)
            image_delta = cv2.warpPerspective(delta, image_matrix, (target_w, target_h), flags=cv2.INTER_LINEAR)
            final_picture = np.clip(picture.astype(np.float32) + image_delta, 0, 255).astype(np.uint8)
        else:
            final_picture = picture
        images.update(image=final_picture, composition=compose(repaired, final_picture, occupancy))
    return Rendered(original, images, mask, matrix, stats, options["enabled"])
