"""HTTP/native adapters for the disk-backed batch workspace."""
from __future__ import annotations
import copy
import json
import mimetypes
from pathlib import Path
import secrets
import shutil
import threading
import urllib.parse
import zipfile

from task_queue import TaskQueue


class ImportFailure(ValueError):
    def __init__(self, message, source_id):
        super().__init__(message)
        self.source_id = source_id


class WorkService:
    def __init__(self, root):
        from workspace import Workspace
        self.root = Path(root)
        self.workspace = Workspace(self.root)
        self.queue = TaskQueue(self.root / "queue")
        self.artifacts = {}
        self.lock = threading.RLock()
        self.preview_lock = threading.RLock()
        self.uploads = self.root / "incoming"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.outputs = self.root / "exports"
        self.outputs.mkdir(parents=True, exist_ok=True)
        for task in self.queue.snapshot():
            for result in task.get("results", []):
                filename = result.get("filename", "")
                artifact_id = result.get("artifact_id")
                if artifact_id and filename and Path(filename).name == filename:
                    path = self.outputs / filename
                    if path.is_file():
                        self.artifacts[artifact_id] = path

    def snapshot(self):
        result = self.workspace.snapshot()
        result["tasks"] = self.queue.snapshot()
        return result

    def preview(self, photo_id, kind):
        # A gallery can request many thumbnails at once. Decode/render one at a
        # time instead of retaining a full-resolution array per HTTP thread.
        with self.preview_lock:
            return self.workspace.preview(photo_id, kind)

    def control(self, data):
        task_id, action = data["task_id"], data["action"]
        if action == "retry":
            task = next((t for t in self.queue.snapshot() if t["id"] == task_id), None)
            if not task:
                raise ValueError("没有这个任务")
            if task["state"] not in ("partial", "failed", "cancelled", "interrupted"):
                raise ValueError("任务尚未结束")
            if task["kind"] in ("import", "retry_import"):
                sources = [entry["source_id"] for entry in task["errors"] if entry.get("source_id")]
                sources += [entry["id"] for entry in task["results"] if entry.get("partial") and entry.get("id")]
                recorded = [entry["item"] for entry in task["results"]]
                recorded += [entry["item"] for entry in task["errors"] if entry.get("source_id")]
                remaining = [item for item in task["items"] if item not in recorded]
                operations = [{"action": "source", "source_id": source_id} for source_id in dict.fromkeys(sources)]
                for item in remaining:
                    if isinstance(item, str):
                        operations.append({"action": "source", "source_id": item})
                    elif item.get("action"):
                        operations.append(item)
                    else:
                        operations.append({"action": "file", **item})
                if operations:
                    kind = task["metadata"].get("kind", "scan")
                    def retry_import(item, cancel, progress):
                        if item["action"] == "source":
                            return self._import_result(self.workspace.retry_source(item["source_id"]))
                        return self._import_result(self.workspace.import_file(item["path"], kind, item["name"]))
                    return self.queue.start("retry_import", operations, retry_import, {"kind": kind})
            if task["kind"].startswith("save_") and task["state"] == "partial":
                reports = [entry.get("report", {}) for entry in task["results"]]
                failed = [err.get("photo_id") for report in reports for err in report.get("errors", []) if err.get("photo_id")]
                if failed:
                    return self.save(task["kind"][5:], {"photo_ids": failed})
            if task_id not in self.queue.workers:
                done = [entry["item"] for entry in task["results"] if not entry.get("partial")]
                items = [item for item in task["items"] if item not in done]
                if task["kind"] == "import":
                    return self.import_paths([item["path"] for item in items], task["metadata"]["kind"], [item["name"] for item in items])
                if task["kind"] == "redetect":
                    return self.queue.start("redetect", items, lambda item, cancel, progress: self.workspace.redetect(item, task["metadata"].get("sensitivity", 1.0), task["metadata"].get("min_area", .002)), task["metadata"])
                if task["kind"].startswith("save_"):
                    return self.save(task["kind"][5:], task["metadata"])
        return self.queue.control(task_id, action)

    def import_paths(self, paths, kind, names=None):
        if kind not in ("scan", "photo"):
            raise ValueError("请选择扫描图或照片导入")
        approved = [{"path": str(Path(path).resolve()), "name": names[index] if names else Path(path).name} for index, path in enumerate(paths)]
        if not approved:
            return {"cancelled": True}
        def worker(item, cancel, progress):
            progress(item["name"])
            source = self.workspace.import_file(item["path"], kind, item["name"])
            return self._import_result(source)
        return self.queue.start("import", approved, worker, {"kind": kind})

    @staticmethod
    def _import_result(source):
        if source.get("status") in ("failed", "error"):
            raise ImportFailure(source.get("error") or "导入失败", source["id"])
        return {**source, "partial": source.get("status") in ("partial", "interrupted")}

    def artifact(self, path, report=None):
        artifact_id = secrets.token_hex(16)
        with self.lock:
            self.artifacts[artifact_id] = Path(path)
        return {"artifact_id": artifact_id, "filename": Path(path).name, "report": report or {}}

    def save(self, kind, data, destination=None):
        ids = data.get("photo_ids") or None
        if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)):
            raise ValueError("照片选择格式错误")
        if kind not in ("project", "export", "folder"):
            raise ValueError("未知保存类型")
        internal = destination is None
        if internal:
            destination = self.outputs / (secrets.token_hex(8) + (".polascan" if kind == "project" else ".zip" if kind == "export" else ""))
        destination = Path(destination)
        def worker(_, cancel, progress):
            if kind == "project":
                progress("保存整个批次及原素材")
                self.workspace.write_project(destination)
                report = {"status": "completed"}
            else:
                def export_progress(report):
                    if isinstance(report, dict):
                        progress(f"导出完成 {report.get('success', 0)} 张，失败 {report.get('failed', 0)} 张")
                    else:
                        progress(str(report))
                report = self.workspace.export(destination, ids, kind != "folder", cancel, export_progress)
            if cancel.is_set():
                return {"cancelled": True}
            actual_path = Path(report.get("path", destination))
            result = self.artifact(actual_path, report) if internal and kind != "folder" else {"filename": actual_path.name, "path": str(actual_path.resolve()), "report": report}
            if report.get("failed"):
                # Preserve completed results and show partial status at task level.
                result["partial"] = True
            return result
        return self.queue.start("save_" + kind, [kind], worker, {"photo_ids": ids})


    def post(self, path, data):
        ws = self.workspace
        if path == "/api/workspace/page":
            return ws.page_response(data["page_id"])
        if path == "/api/workspace/update":
            return ws.update_page(data["page_id"], data["photos"], data.get("revision"))
        if path == "/api/workspace/select":
            ws.select(data["photo_ids"], data["enabled"]); return self.snapshot()
        if path == "/api/workspace/apply":
            return {"undo_token": ws.batch_apply(data["photo_ids"], data["fields"])}
        if path == "/api/workspace/undo":
            ws.undo(data["undo_token"]); return self.snapshot()
        if path == "/api/workspace/redetect":
            sensitivity, min_area = float(data.get("sensitivity", 1.0)), float(data.get("min_area", .002))
            from scanner import Settings
            Settings(sensitivity=sensitivity, min_area=min_area).validate()
            return self.queue.start("redetect", [data["page_id"]], lambda item, cancel, progress: ws.redetect(item, sensitivity, min_area), {"sensitivity": sensitivity, "min_area": min_area})
        if path == "/api/workspace/remove":
            ws.remove_source(data["source_id"]); return self.snapshot()
        if path == "/api/workspace/new":
            from task_queue import TasksActiveError
            def create_batch():
                ws.new()
                return self.snapshot()
            try:
                return self.queue.run_when_idle(create_batch, cancel_active=data.get("cancel_tasks") is True)
            except TasksActiveError as exc:
                if data.get("cancel_tasks") is not True:
                    raise
                return {"pending": True, "active_tasks": [{key: task.get(key) for key in
                    ("id", "kind", "state", "done", "total", "message")} for task in exc.tasks]}
        if path == "/api/workspace/export":
            return self.save("folder" if data.get("zip_output") is False else "export", data)
        if path == "/api/workspace/project-save":
            return self.save("project", data)
        if path == "/api/tasks/control":
            return self.control(data)
        raise ValueError("未知工作区操作")

    def close(self):
        self.queue.close()


def send_file(handler, path, download=False):
    """Stream a known asset and support a single byte range for HTML video."""
    path = Path(path)
    size = path.stat().st_size
    start, end = 0, size - 1
    partial = False
    requested = handler.headers.get("Range", "")
    if requested:
        try:
            if not requested.startswith("bytes=") or "," in requested:
                raise ValueError()
            left, right = requested[6:].split("-", 1)
            if left:
                start = int(left); end = min(size - 1, int(right)) if right else size - 1
            else:
                start = max(0, size - int(right))
            if start < 0 or start >= size or end < start:
                raise ValueError()
            partial = True
        except ValueError:
            handler.send({}, status=416, headers={"Content-Range": f"bytes */{size}"}); return
    handler.send_response(206 if partial else 200)
    handler.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    handler.send_header("Content-Length", str(end - start + 1))
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    if partial:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    if download:
        handler.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(path.name))
    handler.end_headers()
    try:
        with path.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = source.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass
