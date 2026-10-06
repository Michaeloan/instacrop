"""Persistent, bounded background batches. AI retries are always user initiated."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor


class TaskQueue:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tasks.json"
        self.lock = threading.RLock()
        self.tasks = {}
        self.controls, self.workers, self.threads = {}, {}, []
        self.slots = {"local": threading.Semaphore(1), "ai": threading.Semaphore(1)}
        self.executors = {lane: ThreadPoolExecutor(max_workers=1, thread_name_prefix="PolaScan-" + lane) for lane in self.slots}
        if self.path.exists():
            self.tasks = json.loads(self.path.read_text(encoding="utf-8"))
            for record in self.tasks.values():
                if record["state"] in ("running", "queued", "paused", "cancelling"):
                    record.update(state="interrupted", message="上次运行已中断；点击重试继续", interrupted=True)
        self._save()

    def _save(self):
        temporary = self.path.with_suffix(".writing")
        temporary.write_text(json.dumps(self.tasks, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(list(self.tasks.values()))

    def start(self, kind, items, worker, metadata=None, lane="local"):
        task_id = secrets.token_hex(12)
        with self.lock:
            self.tasks[task_id] = {"id": task_id, "kind": kind, "state": "queued", "progress": 0,
                "done": 0, "total": len(items), "message": "已加入队列", "errors": [], "results": [],
                "items": copy.deepcopy(items), "metadata": metadata or {}, "created": time.time()}
            self.controls[task_id] = (threading.Event(), threading.Event())
            self.workers[task_id] = (worker, lane)
            self._save()
        self.threads.append(self.executors[lane].submit(self._run, task_id))
        return {"task_id": task_id}

    def _run(self, task_id):
        worker, lane = self.workers[task_id]
        cancelled, paused = self.controls[task_id]
        task = self.tasks[task_id]
        try:
            while not self.slots[lane].acquire(timeout=.2):
                if cancelled.is_set():
                    break
            else:
                try:
                    for index, item in enumerate(task["items"]):
                        while paused.is_set() and not cancelled.wait(.2):
                            pass
                        if cancelled.is_set():
                            break
                        with self.lock:
                            task.update(state="running", message=f"正在处理 {index + 1}/{task['total']}")
                            self._save()
                        def progress(*args):
                            with self.lock:
                                task["message"] = str(args[-1])[:1000] if args else "处理中"
                                task["progress"] = round((index / max(1, task["total"])) * 100)
                        try:
                            result = worker(item, cancelled, progress)
                            if isinstance(result, dict) and result.get("cancelled"):
                                break
                            with self.lock:
                                entry = {"item": item, "result": result}
                                if isinstance(result, dict):
                                    entry.update(result)
                                task["results"].append(entry)
                        except Exception as exc:
                            if cancelled.is_set():
                                break
                            with self.lock:
                                error = {"item": item, "error": str(exc)[:2000]}
                                if getattr(exc, "source_id", None):
                                    error["source_id"] = exc.source_id
                                task["errors"].append(error)
                        with self.lock:
                            task["done"] = index + 1
                            task["progress"] = round(task["done"] / max(1, task["total"]) * 100)
                            self._save()
                finally:
                    self.slots[lane].release()
            with self.lock:
                has_partial = any(entry.get("partial") for entry in task["results"])
                state = "cancelled" if cancelled.is_set() else "partial" if has_partial or (task["errors"] and task["results"]) else "failed" if task["errors"] else "completed"
                task.update(state=state, message={"completed":"全部完成", "partial":"部分完成，请检查失败项", "failed":"处理失败，可单独重试", "cancelled":"已取消，完成的成果已保留"}[state])
                self._save()
        except Exception as exc:
            with self.lock:
                task.update(state="failed", message=str(exc))
                self._save()

    def control(self, task_id, action):
        with self.lock:
            if task_id not in self.tasks:
                raise ValueError("没有这个任务")
            record = self.tasks[task_id]
            if action == "retry":
                if record["state"] not in ("partial", "failed", "cancelled", "interrupted"):
                    raise ValueError("任务尚未结束")
                worker_lane = self.workers.get(task_id)
                if not worker_lane:
                    raise ValueError("请重新选择该操作；上次完成成果仍保留")
                completed = [entry["item"] for entry in record["results"] if not entry.get("partial")]
                remaining = [item for item in record["items"] if item not in completed]
                if not remaining:
                    raise ValueError("没有需要重试的项目")
                return self.start(record["kind"], remaining, worker_lane[0], record["metadata"], worker_lane[1])
            if task_id not in self.controls or record["state"] in ("completed", "failed", "partial", "cancelled", "interrupted"):
                raise ValueError("任务已结束")
            cancel, pause = self.controls[task_id]
            if action == "pause":
                pause.set(); record.update(state="paused", message="暂停后续项目；当前项目会完成")
            elif action == "resume":
                pause.clear(); record.update(state="queued", message="继续处理")
            elif action == "cancel":
                cancel.set(); pause.clear(); record.update(state="cancelling", message="正在取消")
            else:
                raise ValueError("未知任务操作")
            self._save()
            return copy.deepcopy(record)

    def close(self):
        for cancel, pause in self.controls.values():
            cancel.set(); pause.clear()
        for executor in self.executors.values():
            executor.shutdown(wait=False, cancel_futures=True)
