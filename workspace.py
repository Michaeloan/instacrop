"""Persistent, disk-backed batch workspace shared by desktop and HTTP clients.

Source files are immutable snapshots. Only two decoded pages are cached; editing
records and completed assets never depend on that cache's lifetime.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile

import numpy as np
from PIL import Image, ImageCms, ImageOps, ImageSequence

from rendering import render_photo
from restoration import defaults
from scanner import MAX_PIXELS, Photo, Scan, Settings, SUPPORTED, detect, read_scans, safe_stem, validate_photo

IMAGE_EXTENSIONS = SUPPORTED | {".heic", ".heif"}
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_PROJECT_BYTES = 16 * 1024 * 1024 * 1024
MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 20000
CACHE_BYTES = 192 * 1024 * 1024
RESTORATION_FIELDS = set(defaults()) - {"strokes"}
PRESENTATION_FIELDS = {"trim", "occupancy", "trim_top", "trim_right", "trim_bottom", "trim_left"}


def _id():
    return uuid.uuid4().hex


def _pil(value):
    return value.copy() if isinstance(value, Image.Image) else Image.fromarray(value)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("素材路径不正确")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("..", ".", "") or ":" in part for part in path.parts):
        raise ValueError("素材路径超出工作区")
    if path.as_posix() != value:
        raise ValueError("素材路径不正确")
    reserved = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}
    if any(part.rstrip(" .") != part or any(character in part for character in '<>"|?*')
           or part.upper().split(".")[0] in reserved for part in path.parts):
        raise ValueError("素材路径包含无效文件名")
    return path






def _photo_scans(path, name):
    """Color-manage direct photos; the existing scan detection path is unchanged."""
    destination = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
    with Image.open(path) as opened:
        try:
            dpi = tuple(float(value) for value in opened.info.get("dpi", ())[:2])
            if len(dpi) != 2 or any(not math.isfinite(value) or value <= 0 for value in dpi):
                dpi = None
        except (ValueError, TypeError):
            dpi = None
        for index, frame in enumerate(ImageSequence.Iterator(opened)):
            if frame.width * frame.height > MAX_PIXELS:
                raise ValueError("单页超过 2 亿像素，请先降低照片分辨率")
            info = frame.info
            oriented = ImageOps.exif_transpose(frame)
            color = {"profile": "assumed-sRGB", "converted": False, "hdr_decoded_8bit": False, "warnings": []}
            alpha = oriented.convert("RGBA").getchannel("A") if oriented.mode in ("RGBA", "LA", "PA") or "transparency" in info else None
            nclx = info.get("nclx_profile", {})
            high_depth = int(info.get("bit_depth", 8)) > 8 or oriented.mode.startswith("I;16")
            hdr = isinstance(nclx, dict) and nclx.get("transfer_characteristics") in (16, 18)
            if high_depth or hdr:
                color["hdr_decoded_8bit"] = True
                color["warnings"].append("高位深／HDR 素材已解码为 8 位 SDR 预览，动态范围不能完整保留；原始文件仍完整保留")
            if oriented.mode.startswith("I;16"):
                values = np.asarray(oriented).astype(np.float32)
                oriented = Image.fromarray(np.clip(np.rint(values / 257), 0, 255).astype(np.uint8), "L")
            profile = info.get("icc_profile")
            if profile:
                try:
                    source_profile = ImageCms.ImageCmsProfile(BytesIO(profile))
                    image = oriented if oriented.mode in ("RGB", "CMYK", "L", "LAB") else oriented.convert("RGB")
                    image = ImageCms.profileToProfile(image, source_profile, destination, outputMode="RGB")
                    color.update(profile="sRGB", converted=True)
                except (OSError, ValueError, ImageCms.PyCMSError):
                    image = oriented.convert("RGB")
                    color["warnings"].append("ICC 配置无效，无法准确转换色彩；当前预览按 sRGB 显示")
            else:
                image = oriented.convert("RGB")
                if isinstance(nclx, dict) and nclx.get("color_primaries") not in (None, 1):
                    color["warnings"].append("素材包含广色域标记但缺少可转换的 ICC 配置，当前 SDR 预览可能存在色差")
            if alpha is not None:
                white = Image.new("RGB", image.size, "white")
                white.paste(image, mask=alpha)
                image = white
            yield Scan(np.array(image), name, dpi, index + 1), color


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._cache = OrderedDict()
        self._undo = {}
        self.path = self.root / "manifest.json"
        if self.path.is_file():
            self.manifest = json.loads(self.path.read_text(encoding="utf-8"))
            self._validate_manifest(self.manifest)
            changed = False
            for source in self.manifest["sources"]:
                if source["status"] == "importing":
                    source.update(status="interrupted", error="上次导入已中断，可重新导入")
                    changed = True
            for task in self.manifest.get("tasks", []):
                if task.get("status") in ("running", "queued"):
                    task["status"] = "interrupted"
                    changed = True
            if changed:
                self._persist()
        else:
            self.manifest = {"version": 2, "id": _id(), "sources": [], "pages": [], "tasks": []}
            self._persist()

    def asset(self, relative_path):
        relative = _safe_relative(relative_path)
        resolved = (self.root / Path(*relative.parts)).resolve()
        if resolved == self.root or self.root not in resolved.parents:
            raise ValueError("素材路径超出工作区")
        return resolved

    def _persist(self):
        _atomic_json(self.path, self.manifest)

    def save(self):
        with self.lock:
            self._persist()

    def snapshot(self):
        with self.lock:
            snapshot = deepcopy(self.manifest)
            for page in snapshot["pages"]:
                for photo in page["photos"]:
                    if "analysis" not in photo:
                        photo["needs_review"] = True
                        photo["review_reasons"] = list(photo.get("warnings", [])) + ["旧项目尚未完成新版边缘与朝向检查；打开调整后检查"]
            return snapshot

    def new(self):
        """Start a fresh batch; existing disk assets remain recoverable."""
        with self.lock:
            if self.manifest["sources"]:
                history = self.asset(f"history/{self.manifest['id']}.json")
                history.parent.mkdir(parents=True, exist_ok=True)
                _atomic_json(history, self.manifest)
            self.manifest = {"version": 2, "id": _id(), "sources": [], "pages": [], "tasks": []}
            self._cache.clear()
            self._undo.clear()
            self._persist()
        return self.snapshot()

    def _source(self, source_id):
        for source in self.manifest["sources"]:
            if source["id"] == source_id:
                return source
        raise ValueError("没有这个来源文件")

    def _page(self, page_id):
        for page in self.manifest["pages"]:
            if page["id"] == page_id:
                return page
        raise ValueError("没有这个扫描页")


    def locate_photo(self, photo_id):
        with self.lock:
            for page in self.manifest["pages"]:
                for photo in page["photos"]:
                    if photo["id"] == photo_id:
                        return deepcopy(page), deepcopy(photo)
        raise ValueError("没有这张照片")

    def import_file(self, path, kind="scan", name=None):
        if kind not in ("scan", "photo"):
            raise ValueError("导入类型只能是扫描图或照片")
        path = Path(path)
        display_name = Path(name).name if name else path.name
        suffix = path.suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            raise ValueError("不支持这个文件格式")
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
            raise ValueError("单个素材文件必须为 1 字节至 512 MB")
        if suffix in {".heic", ".heif"}:
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError as exc:
                raise ValueError("HEIC／HEIF 需要安装 pillow-heif 解码组件") from exc
        source_id = _id()
        relative = f"sources/{source_id}{suffix}"
        target = self.asset(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(path, temporary)
        os.replace(temporary, target)
        metadata = {}
        source = {"id": source_id, "name": display_name, "kind": kind,
                  "status": "importing", "error": None, "pages": [], "asset": relative,
                  "sha256": _hash(target), "content_identifier": metadata.get("ContentIdentifier")}
        with self.lock:
            self.manifest["sources"].append(source)
            self._persist()
        return self._process_source(source, metadata)

    def retry_source(self, source_id):
        """Resume missing pages without replacing already reviewed photos."""
        with self.lock:
            source = self._source(source_id)
            if source["status"] not in ("failed", "partial", "interrupted"):
                raise ValueError("这个来源没有需要重试的页面")
            original = self.asset(source["asset"])
            if not original.is_file() or _hash(original) != source["sha256"]:
                raise ValueError("原始素材缺失或校验失败，无法重试")
            source.update(status="importing", error=None)
            self._persist()
        return self._process_source(source)

    def _process_source(self, source, metadata=None):
        source_id, kind, display_name = source["id"], source["kind"], source["name"]
        target = self.asset(source["asset"])
        metadata = {} if metadata is None else metadata
        reader, decoded = None, None
        try:
            if target.suffix.lower() in {".heic", ".heif"}:
                try:
                    import pillow_heif
                    pillow_heif.register_heif_opener()
                except ImportError as exc:
                    raise ValueError("HEIC／HEIF 需要安装 pillow-heif 解码组件") from exc
            with self.lock:
                if metadata.get("ContentIdentifier"):
                    source["content_identifier"] = metadata["ContentIdentifier"]
            with Image.open(target) as opened:
                if opened.format not in {"JPEG", "PNG", "TIFF", "BMP", "WEBP", "HEIF", "HEIC"}:
                    raise ValueError("文件内容不是受支持的照片格式")
                page_count = getattr(opened, "n_frames", 1) if opened.format == "TIFF" else 1
            with self.lock:
                if self._source(source_id) is not source:
                    raise ValueError("来源已切换或移除，停止导入")
                source["page_count"] = page_count
                complete = {self._page(page_id)["page"] for page_id in source["pages"]}
            if kind == "scan":
                reader = read_scans(target, display_name)
                decoded = ((scan, None) for scan in reader)
            else:
                decoded = _photo_scans(target, display_name)
            for scan, color in decoded:
                if scan.page > page_count:
                    break
                if scan.page in complete:
                    continue
                page_id = _id()
                page_asset = f"pages/{page_id}.png"
                page_target = self.asset(page_asset)
                page_target.parent.mkdir(parents=True, exist_ok=True)
                Image.fromarray(scan.image).save(page_target, format="PNG", **({"dpi": scan.dpi} if scan.dpi else {}))
                if kind == "scan":
                    photos = detect(scan)
                    for photo in photos:
                        photo.restoration = {**defaults(), "enabled": True, "sensitivity": 25, "max_diameter": 8}
                else:
                    height, width = scan.image.shape[:2]
                    quad = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], np.float32)
                    photos = [Photo(quad, quad.copy(), method="imported-photo", restoration=defaults(), warnings=color["warnings"])]
                    for photo in photos:
                        self._analyze_crop(scan, photo, apply_orientation=True)
                records = [{**photo.as_dict(scan.dpi), "id": _id()} for photo in photos]
                page = {"id": page_id, "source_id": source_id, "name": display_name,
                        "page": scan.page, "pages": page_count, "width": scan.image.shape[1],
                        "height": scan.image.shape[0], "dpi": list(scan.dpi) if scan.dpi else None,
                        "photos": records, "revision": 0, "asset": page_asset,
                        "status": "ready" if records else "empty"}
                if color is not None:
                    page["color"] = color
                with self.lock:
                    if self._source(source_id) is not source:
                        raise ValueError("来源已切换或移除，停止导入")
                    if color is not None:
                        source["warnings"] = list(dict.fromkeys(source.get("warnings", []) + color["warnings"]))
                    self.manifest["pages"].append(page)
                    source["pages"].append(page_id)
                    complete.add(scan.page)
                    self._persist()
            with self.lock:
                if self._source(source_id) is not source:
                    raise ValueError("来源已切换或移除，停止导入")
                source.update(status="ready", error=None, failed_pages=[])

                self._persist()
        except Exception as exc:
            with self.lock:
                source.update(status="partial" if source["pages"] else "failed", error=str(exc))
                completed = {page["page"] for page in self.manifest["pages"] if page["source_id"] == source_id}
                source["failed_pages"] = [] if kind == "video" else sorted(set(range(1, source.get("page_count", 1) + 1)) - completed)
                self._persist()
        finally:
            # Retained exception tracebacks can keep suspended generators alive.
            # Close both the wrapper and its source so TIFF handles release now,
            # including on detection/save failure and early loop termination.
            try:
                if decoded is not None:
                    decoded.close()
            finally:
                if reader is not None:
                    reader.close()
        with self.lock:
            return deepcopy(source)



    def _read_scan(self, page_id):
        with self.lock:
            page = self._page(page_id)
            if page_id in self._cache:
                scan = self._cache.pop(page_id)
                self._cache[page_id] = scan
            else:
                with Image.open(self.asset(page["asset"])) as image:
                    array = np.array(image.convert("RGB"))
                array.flags.writeable = False
                scan = Scan(array, page["name"], tuple(page["dpi"]) if page["dpi"] else None, page["page"])
                if array.nbytes <= CACHE_BYTES:
                    self._cache[page_id] = scan
            while len(self._cache) > 2 or sum(s.image.nbytes for s in self._cache.values()) > CACHE_BYTES:
                self._cache.popitem(last=False)
            return scan

    def read_page(self, page_id):
        with self.lock:
            source = self._source(self._page(page_id)["source_id"])
            return self._read_scan(page_id), self.asset(source["asset"]).read_bytes()

    def page_response(self, page_id):
        with self.lock:
            page = self._page(page_id)
            missing = [record for record in page["photos"] if "analysis" not in record]
            if missing:
                scan = self._read_scan(page_id)
                for record in missing:
                    photo = validate_photo(record, scan.image.shape)
                    self._analyze_crop(scan, photo)
                    record.update(photo.as_dict(page["dpi"]))
                self._persist()
            return {**deepcopy(page), "job": page_id}

    @staticmethod
    def _analyze_crop(scan, photo, apply_orientation=False):
        from crop_detection import analyze_photo
        analysis = analyze_photo(scan.image, photo)
        orientation = analysis.get("orientation", {})
        suggestion = orientation.get("suggested_rotation")
        if (apply_orientation and not photo.presentation.get("orientation_confirmed")
                and orientation.get("confidence") == "high" and isinstance(suggestion, int) and suggestion in range(4)):
            photo.rotation = suggestion
            analysis = analyze_photo(scan.image, photo)
        photo.crop_analysis = analysis
        return photo

    def update_page(self, page_id, photos, expected_revision=None):
        with self.lock:
            page = self._page(page_id)
            if expected_revision is not None and expected_revision != page["revision"]:
                raise ValueError("页面已更新，请刷新后重试，避免覆盖新编辑")
            if not isinstance(photos, list) or len(photos) > 100:
                raise ValueError("每页最多 100 张照片")
            existing = {p["id"]: p for p in page["photos"]}
            scan = self._read_scan(page_id)
            records, used = [], set()
            for data in photos:
                photo = validate_photo(data, (page["height"], page["width"], 3))
                from crop_geometry import effective_photo
                effective_photo(scan, photo)
                self._analyze_crop(scan, photo)
                key = data.get("id") or _id()
                if key in used or (data.get("id") and key not in existing):
                    raise ValueError("照片 ID 重复或不属于当前页")
                used.add(key)
                records.append({**photo.as_dict(page["dpi"]), "id": key})
            page["photos"] = records
            page["revision"] += 1
            page["status"] = "ready" if records else "empty"
            self._persist()
            return self.page_response(page_id)

    def _rendered(self, photo_id, snapshot=None):
        page, data = snapshot or self.locate_photo(photo_id)
        scan = self._read_scan(page["id"])
        photo = validate_photo(data, scan.image.shape)
        return page, data, render_photo(scan, photo)

    def render(self, photo_id, original=False):
        _, _, rendered = self._rendered(photo_id)
        images = rendered.original if original else rendered.images
        if "image" not in images:
            raise ValueError("请先确认内部画面四角，再导出照片画面")
        return _pil(images["image"])

    def preview(self, photo_id, kind="paper", size=440):
        if kind not in ("paper", "image", "composition", "original_paper", "original_image", "original_composition", "mask"):
            raise ValueError("没有这种预览")
        size = int(size)
        if not 64 <= size <= 1800:
            raise ValueError("预览尺寸应为 64–1800 像素")
        page, record = self.locate_photo(photo_id)
        target = self.asset(f"cache/{photo_id}-{page['revision']}-{kind}-{size}.jpg")
        if target.is_file():
            return target
        _, _, rendered = self._rendered(photo_id, (page, record))
        if kind == "mask":
            data = rendered.mask
        elif kind.startswith("original_"):
            data = rendered.original.get(kind[9:])
        else:
            data = rendered.images.get(kind)
        if data is None:
            raise ValueError("该照片的内部画面尚未确认")
        image = _pil(data)
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + f".{_id()}.tmp")
        image.convert("RGB").save(temporary, "JPEG", quality=88)
        os.replace(temporary, target)
        return target

    def select(self, photo_ids, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("勾选状态必须是开关")
        with self.lock:
            targets = [self.locate_photo(key) for key in dict.fromkeys(photo_ids)]
            for page, photo in targets:
                target = next(p for p in self._page(page["id"])["photos"] if p["id"] == photo["id"])
                target["enabled"] = enabled
            self._persist()
        return self.snapshot()

    def batch_apply(self, photo_ids, fields):
        if not isinstance(fields, dict) or not fields or set(fields) - {"restoration", "presentation"}:
            raise ValueError("只允许批量复制修复和布局参数")
        for group, allowed in (("restoration", RESTORATION_FIELDS), ("presentation", PRESENTATION_FIELDS)):
            if group in fields and (not isinstance(fields[group], dict) or set(fields[group]) - allowed):
                raise ValueError("不能批量复制四角或手动笔迹")
        with self.lock:
            prepared, previous, touched = [], [], set()
            for key in dict.fromkeys(photo_ids):
                page, photo = self.locate_photo(key)
                record = deepcopy(photo)
                changes = {}
                for group, values in fields.items():
                    changes[group] = {k: (k in record.get(group, {}), record.get(group, {}).get(k)) for k in values}
                    record[group] = {**record.get(group, {}), **values}
                if set(fields.get("presentation", {})) - {"occupancy"}:
                    record["presentation"]["review_confirmed"] = False
                validated = validate_photo(record, (page["height"], page["width"], 3))
                from crop_geometry import effective_photo
                scan = self._read_scan(page["id"])
                effective_photo(scan, validated)
                self._analyze_crop(scan, validated)
                validated_record = validated.as_dict(page["dpi"])
                prepared.append((page["id"], key, validated_record))
                applied = {group: {field: validated_record[group][field] for field in values}
                           for group, values in fields.items()}
                previous.append((page["id"], key, changes, applied))
                touched.add(page["id"])
            if not prepared:
                raise ValueError("请选择要批量调整的照片")
            for page_id, key, validated in prepared:
                target = next(p for p in self._page(page_id)["photos"] if p["id"] == key)
                target.update(validated)
            for page_id in touched:
                self._page(page_id)["revision"] += 1
            token = _id()
            self._undo[token] = previous
            self._persist()
            return token

    def undo(self, token):
        with self.lock:
            if token not in self._undo:
                raise ValueError("没有可撤销的批量操作")
            for page_id, key, groups, applied in self._undo.pop(token):
                try:
                    page = self._page(page_id)
                    target = next(p for p in page["photos"] if p["id"] == key)
                except (ValueError, StopIteration):
                    continue
                for group, values in groups.items():
                    for field, (exists, value) in values.items():
                        # A later single-photo edit wins over an older batch undo.
                        if target[group].get(field) != applied[group][field]:
                            continue
                        if exists:
                            target[group][field] = value
                        else:
                            target[group].pop(field, None)
                reviewed = validate_photo(target, (page["height"], page["width"], 3))
                self._analyze_crop(self._read_scan(page_id), reviewed)
                target.update(reviewed.as_dict(page["dpi"]))
                page["revision"] += 1
            self._persist()
        return self.snapshot()

    def redetect(self, page_id, sensitivity=1.0, min_area=.002):
        settings = Settings(sensitivity=float(sensitivity), min_area=float(min_area))
        settings.validate()
        page = self.page_response(page_id)
        scan = self._read_scan(page_id)
        photos = detect(scan, settings)
        for photo in photos:
            photo.restoration = {**defaults(), "enabled": True, "sensitivity": 25, "max_diameter": 8}
        return self.update_page(page_id, [photo.as_dict(scan.dpi) for photo in photos], page["revision"])

    def remove_source(self, source_id):
        with self.lock:
            source = self._source(source_id)
            page_ids = set(source["pages"])
            self.manifest["pages"] = [p for p in self.manifest["pages"] if p["id"] not in page_ids]
            self.manifest["sources"] = [s for s in self.manifest["sources"] if s["id"] != source_id]
            for page_id in page_ids:
                self._cache.pop(page_id, None)

            self._persist()
        return self.snapshot()


    @staticmethod
    def _validate_manifest(meta):
        if meta.get("live") or any(src.get("kind") == "video" for src in meta.get("sources", [])):
            raise ValueError("裁剪版只支持静态照片项目，请从原版导出照片后导入")
        if not isinstance(meta, dict) or meta.get("version") != 2:
            raise ValueError("不支持这个版本的批次项目")
        if not isinstance(meta.get("sources"), list) or not isinstance(meta.get("pages"), list):
            raise ValueError("项目来源或页面格式不正确")
        if not isinstance(meta.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", meta["id"]):
            raise ValueError("项目 ID 不正确")
        if len(meta["sources"]) > MAX_MEMBERS or len(meta["pages"]) > MAX_MEMBERS:
            raise ValueError("项目条目过多")
        used, sources, pages = set(), {}, {}
        for source in meta["sources"]:
            key = source["id"]
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key) or key in used or source["kind"] not in ("scan", "photo", "video"):
                raise ValueError("项目来源 ID 或类型不正确")
            used.add(key)
            _safe_relative(source["asset"])
            if not isinstance(source.get("pages"), list):
                raise ValueError("项目页列表不正确")
            sources[key] = source
        for page in meta["pages"]:
            if not isinstance(page["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", page["id"]) or page["id"] in used or page["source_id"] not in sources:
                raise ValueError("项目页面 ID 或来源不正确")
            used.add(page["id"])
            pages[page["id"]] = page
            _safe_relative(page["asset"])
            width, height = page["width"], page["height"]
            if not isinstance(width, int) or not isinstance(height, int) or min(width, height) < 2 or width * height > 100000000:
                raise ValueError("项目页面尺寸不正确")
            if not isinstance(page["revision"], int) or page["revision"] < 0 or len(page["photos"]) > 100:
                raise ValueError("项目照片或版本不正确")
            dpi = page.get("dpi")
            if dpi is not None and (len(dpi) != 2 or any(not math.isfinite(float(n)) or float(n) <= 0 for n in dpi)):
                raise ValueError("项目分辨率不正确")
            for photo in page["photos"]:
                if not isinstance(photo["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", photo["id"]) or photo["id"] in used:
                    raise ValueError("项目照片 ID 重复")
                used.add(photo["id"])
                validate_photo(photo, (height, width, 3))
                if any(key.startswith("live_") for key in photo):
                    raise ValueError("裁剪版不支持含 Live 配对的照片项目")
        for source in sources.values():
            if len(set(source["pages"])) != len(source["pages"]) or any(key not in pages or pages[key]["source_id"] != source["id"] for key in source["pages"]):
                raise ValueError("项目页面与来源不匹配")
        if any(page["id"] not in sources[page["source_id"]]["pages"] for page in pages.values()):
            raise ValueError("项目来源缺少页面索引")
        if not isinstance(meta.get("tasks", []), list):
            raise ValueError("项目任务格式不正确")
        meta.pop("live", None)
        return meta

    def _project_assets(self, manifest):
        names = {s["asset"] for s in manifest["sources"]} | {p["asset"] for p in manifest["pages"]}
        return sorted(names)

    def write_project(self, dest):
        dest = Path(dest)
        if dest.resolve() == self.path:
            raise ValueError("不能覆盖工作区清单")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            manifest = self.snapshot()
            assets = self._project_assets(manifest)
            hashes = {name: _hash(self.asset(name)) for name in assets}
            metadata = {**manifest, "asset_sha256": hashes}
            temporary = dest.with_name(dest.name + f".{_id()}.tmp")
            try:
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                    archive.writestr("project.json", json.dumps(metadata, ensure_ascii=False, allow_nan=False))
                    for name in assets:
                        archive.write(self.asset(name), name)
                os.replace(temporary, dest)
            finally:
                temporary.unlink(missing_ok=True)
        return dest

    def open_project(self, path):
        # Validate all entries before any current workspace records are replaced.
        with tempfile.TemporaryDirectory(dir=self.root, prefix="project-import-") as temporary:
            staging = Path(temporary)
            try:
                with zipfile.ZipFile(path) as archive:
                    infos = archive.infolist()
                    names = [i.filename for i in infos]
                    if len(infos) > MAX_MEMBERS or len({name.casefold() for name in names}) != len(names):
                        raise ValueError("项目文件条目重复或过多")
                    if sum(i.file_size for i in infos) > MAX_PROJECT_BYTES or any(i.flag_bits & 1 for i in infos):
                        raise ValueError("项目文件过大或已加密")
                    for info in infos:
                        _safe_relative(info.filename)
                        if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError("项目不能包含目录或符号链接条目")
                    if archive.getinfo("project.json").file_size > MAX_METADATA_BYTES:
                        raise ValueError("项目设置过大")
                    meta = json.loads(archive.read("project.json"))
                    if meta.get("version") == 1:
                        meta = self._legacy_project(archive, meta, staging)
                    else:
                        self._validate_manifest(meta)
                        hashes = meta.get("asset_sha256")
                        if not isinstance(hashes, dict) or set(names) != {"project.json", *hashes}:
                            raise ValueError("项目素材清单不一致")
                        for name, expected in hashes.items():
                            if not name.startswith(("sources/", "pages/")):
                                raise ValueError("项目素材路径不正确")
                            target = staging / Path(*_safe_relative(name).parts)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(name) as source, target.open("wb") as output:
                                shutil.copyfileobj(source, output, 1024 * 1024)
                            if _hash(target) != expected:
                                raise ValueError("项目中的素材校验失败")
                        for source in meta["sources"]:
                            asset = staging / source["asset"]
                            if not asset.is_file() or asset.stat().st_size > MAX_SOURCE_BYTES or _hash(asset) != source["sha256"]:
                                raise ValueError("项目中的原始素材缺失或校验失败")
                        for page in meta["pages"]:
                            with Image.open(staging / page["asset"]) as image:
                                if image.size != (page["width"], page["height"]):
                                    raise ValueError("项目页面尺寸与图片不符")
                        meta.pop("asset_sha256", None)
            except (zipfile.BadZipFile, KeyError, TypeError, OSError, RuntimeError, json.JSONDecodeError) as exc:
                raise ValueError("无法读取完整的批次项目") from exc
            self._validate_manifest(meta)
            with self.lock:
                # Roll back asset collisions as well as metadata if storage fails
                # during commit; the previous batch must remain openable.
                files = [file for file in staging.rglob("*") if file.is_file()]
                backups, installed, previous = [], [], self.manifest
                try:
                    for file in files:
                        relative = file.relative_to(staging)
                        target = self.asset(relative.as_posix())
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists():
                            backup = staging / "rollback" / relative
                            backup.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(target, backup)
                            backups.append((target, backup))
                        os.replace(file, target)
                        installed.append(target)
                    self.manifest = meta
                    self._persist()
                except Exception:
                    self.manifest = previous
                    for target in reversed(installed):
                        target.unlink(missing_ok=True)
                    for target, backup in reversed(backups):
                        os.replace(backup, target)
                    raise
                self._cache.clear()
                self._undo.clear()
        return self.snapshot()

    def _legacy_project(self, archive, meta, staging):
        if sorted(archive.namelist()) != ["project.json", "source.bin"] or archive.getinfo("source.bin").file_size > MAX_SOURCE_BYTES:
            raise ValueError("旧项目文件内容不正确")
        data = archive.read("source.bin")
        if hashlib.sha256(data).hexdigest() != meta.get("source_sha256"):
            raise ValueError("旧项目中的原图校验失败")
        source_id, page_id = _id(), _id()
        source_asset = f"sources/{source_id}.bin"
        source_path = staging / source_asset
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(data)
        requested = int(meta.get("page", 1))
        scans = read_scans(BytesIO(data), Path(meta["source_name"]).name)
        scan = next((s for s in scans if s.page == requested), None)
        if scan is None:
            raise ValueError("旧项目页码不正确")
        with Image.open(BytesIO(data)) as image:
            count = getattr(image, "n_frames", 1) if image.format == "TIFF" else 1
        records = []
        for item in meta["photos"]:
            record = deepcopy(item)
            record["presentation"] = {"trim": meta.get("trim", 0), "occupancy": meta.get("occupancy", .78),
                                      **record.get("presentation", {})}
            records.append({**validate_photo(record, scan.image.shape).as_dict(scan.dpi), "id": _id()})
        page_asset = f"pages/{page_id}.png"
        page_path = staging / page_asset
        page_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(scan.image).save(page_path, "PNG")
        return {"version": 2, "id": _id(), "sources": [{"id": source_id, "name": scan.name,
                "kind": "scan", "status": "ready", "error": None, "pages": [page_id], "asset": source_asset,
                "sha256": _hash(source_path), "content_identifier": None}],
                "pages": [{"id": page_id, "source_id": source_id, "name": scan.name, "page": requested,
                "pages": count, "width": scan.image.shape[1], "height": scan.image.shape[0],
                "dpi": list(scan.dpi) if scan.dpi else None, "photos": records, "revision": 0,
                "asset": page_asset, "status": "ready" if records else "empty"}], "tasks": []}

    def export(self, dest, photo_ids=None, zip_output=True, cancel_event=None, progress=None):
        snapshot = self.snapshot()
        selected = set(photo_ids) if photo_ids is not None else None
        targets = [(page, photo, index) for page in snapshot["pages"] for index, photo in enumerate(page["photos"], 1)
                   if (photo["id"] in selected if selected is not None else photo.get("enabled", True))]
        if not targets:
            raise ValueError("没有勾选要导出的照片")
        if selected is not None and selected - {photo["id"] for _, photo, _ in targets}:
            raise ValueError("导出选择包含不存在的照片")
        destination = Path(dest)
        number, base = 2, destination
        while destination.exists():
            destination = base.with_name(f"{base.stem}_{number}{base.suffix}" if zip_output else f"{base.name}_{number}")
            number += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        report = {"path": str(destination), "success": 0, "failed": 0, "errors": [], "photos": [],
                  "cancelled": False, "total": len(targets)}
        groups, seen = {}, set()
        for source in snapshot["sources"]:
            stem = safe_stem(source["name"])
            candidate, counter = stem, 2
            while candidate.casefold() in seen:
                candidate = f"{stem}_{counter}"
                counter += 1
            seen.add(candidate.casefold())
            groups[source["id"]] = candidate
        working = destination if not zip_output else Path(tempfile.mkdtemp(dir=destination.parent, prefix="export-"))
        working.mkdir(parents=True, exist_ok=True)
        try:
            for page, data, index in targets:
                if cancel_event and cancel_event.is_set():
                    report["cancelled"] = True
                    break
                staging_photo = None
                try:
                    scan = self._read_scan(page["id"])
                    rendered = render_photo(scan, validate_photo(data, scan.image.shape))
                    if cancel_event and cancel_event.is_set():
                        report["cancelled"] = True
                        break
                    prefix = Path(groups[page["source_id"]]) / f"第{page['page']:03d}页" / f"照片{index:03d}"
                    photo_output = working / prefix
                    staging_photo = working / f"_partial_{_id()}"
                    staging_photo.mkdir()
                    record = {"id": data["id"], "source": page["name"], "page": page["page"], "files": {},
                              "restoration_stats": rendered.stats, "warnings": list(data.get("warnings", []))}
                    modes = {"paper": "带白边", "image": "照片画面", "composition": "背景成图"}
                    for mode, array in rendered.images.items():
                        relative = prefix / f"{modes[mode]}.png"
                        target = staging_photo / relative.relative_to(prefix)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        _pil(array).save(target, "PNG", **({"dpi": scan.dpi} if scan.dpi and mode != "composition" else {}))
                        record["files"][mode] = relative.as_posix()
                    if "image" not in rendered.images:
                        record["warnings"].append("仅导出相纸：内部画面未确认")
                    if rendered.active:
                        for mode, array in rendered.original.items():
                            relative = prefix / "原始版本" / f"{modes[mode]}.png"
                            target = staging_photo / relative.relative_to(prefix)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            _pil(array).save(target, "PNG")
                            record["files"]["original_" + mode] = relative.as_posix()
                        relative = prefix / "修复记录" / "蒙版.png"
                        target = staging_photo / relative.relative_to(prefix)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        Image.fromarray(rendered.mask).save(target, "PNG")
                        record["files"]["mask"] = relative.as_posix()
                    photo_output.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staging_photo, photo_output)
                    report["photos"].append(record)
                    report["success"] += 1
                except Exception as exc:
                    report["failed"] += 1
                    report["errors"].append({"id": data["id"], "photo_id": data["id"], "source": page["name"], "page": page["page"], "error": str(exc)})
                finally:
                    if staging_photo is not None and staging_photo.exists():
                        shutil.rmtree(staging_photo)
                if progress:
                    progress(deepcopy(report))
            report["status"] = ("cancelled" if report["cancelled"] else "failed" if report["failed"] and not report["success"]
                                else "partial" if report["failed"] else "completed")
            _atomic_json(working / "导出清单.json", report)
            if zip_output and not report["cancelled"]:
                temporary = destination.with_name(destination.name + ".tmp")
                try:
                    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                        for file in working.rglob("*"):
                            if file.is_file():
                                if cancel_event and cancel_event.is_set():
                                    report.update(cancelled=True, status="cancelled")
                                    break
                                archive.write(file, file.relative_to(working).as_posix())
                    if not report["cancelled"]:
                        os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            if report["cancelled"] and zip_output:
                report["path"] = None
            return report
        finally:
            if zip_output:
                shutil.rmtree(working)
