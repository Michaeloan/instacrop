"""Persistent, bounded background batches. AI retries are always user initiated."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import secrets
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor


ACTIVE_STATES = {"running", "queued", "paused", "cancelling"}
TASK_NAMES = {"import": "导入图片", "retry_import": "重试导入", "redetect": "重新识别",
              "save_project": "保存项目", "save_export": "导出照片", "save_folder": "导出照片",
              "save_live": "导出手帐", "save_live_folder": "导出手帐", "live_generate": "生成手帐",
              "live_render": "渲染手帐", "live_export": "导出手帐"}


class TasksActiveError(ValueError):
    def __init__(self, tasks):
        self.tasks = tasks
        names = "、".join(TASK_NAMES.get(task.get("kind"), "后台处理") for task in tasks)
        super().__init__(f"仍有 {len(tasks)} 项任务尚未停止：{names}。请等待当前任务安全结束。")


class TaskQueue:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tasks.json"
        self.lock = threading.RLock()
        self.submission_lock = threading.RLock()
        self.tasks = {}
        self.futures = {}
        self.closed = False
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
            self._reconcile()
            return copy.deepcopy(list(self.tasks.values()))

    def _reconcile(self):
        """An active label without a live executor future cannot block a batch."""
        changed = False
        for task_id, task in self.tasks.items():
            if task.get("state") not in ACTIVE_STATES:
                continue
            future = self.futures.get(task_id)
            if future is None:
                task.update(state="interrupted", message="任务执行已中断，已完成成果保留；可重试未完成项", interrupted=True)
            elif future.cancelled():
                task.update(state="cancelled", message="已取消，完成的成果已保留")
            elif future.done():
                try:
                    error = future.exception()
                except CancelledError:
                    task.update(state="cancelled", message="已取消，完成的成果已保留")
                else:
                    task.update(state="failed" if error else "interrupted",
                                message=str(error)[:2000] if error else "任务执行已结束，旧状态已恢复；已完成成果保留")
            else:
                continue
            changed = True
        if changed:
            self._save()

    def active_tasks(self):
        return [task for task in self.snapshot() if task.get("state") in ACTIVE_STATES]

    def run_when_idle(self, operation, cancel_active=False):
        """Keep new submissions out while checking and changing the workspace."""
        with self.submission_lock:
            if self.closed:
                raise ValueError("任务队列已关闭，请重新打开软件")
            if cancel_active:
                for task in self.active_tasks():
                    if task["state"] != "cancelling":
                        try:
                            self.control(task["id"], "cancel")
                        except ValueError:
                            # The worker may finish after the snapshot, before
                            # cancellation acquires the queue lock.
                            if any(active["id"] == task["id"] for active in self.active_tasks()):
                                raise
            active = self.active_tasks()
            if active:
                raise TasksActiveError(active)
            return operation()

    def start(self, kind, items, worker, metadata=None, lane="local"):
        if lane not in self.executors:
            raise ValueError("未知任务队列")
        task_id = secrets.token_hex(12)
        with self.submission_lock, self.lock:
            if self.closed:
                raise ValueError("任务队列已关闭，请重新打开软件")
            self.tasks[task_id] = {"id": task_id, "kind": kind, "state": "queued", "progress": 0,
                "done": 0, "total": len(items), "message": "已加入队列", "errors": [], "results": [],
                "items": copy.deepcopy(items), "metadata": metadata or {}, "created": time.time()}
            self.controls[task_id] = (threading.Event(), threading.Event())
            self.workers[task_id] = (worker, lane)
            try:
                self._save()
                future = self.executors[lane].submit(self._run, task_id)
            except Exception:
                self.tasks.pop(task_id, None)
                self.controls.pop(task_id, None)
                self.workers.pop(task_id, None)
                try:
                    self._save()
                except Exception:
                    pass
                raise
            self.futures[task_id] = future
            self.threads.append(future)
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
        with self.submission_lock, self.lock:
            self._reconcile()
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
                future = self.futures.get(task_id)
                if future is not None and future.cancel():
                    record.update(state="cancelled", message="已取消排队任务，完成的成果已保留")
            else:
                raise ValueError("未知任务操作")
            self._save()
            return copy.deepcopy(record)

    def close(self):
        with self.submission_lock:
            self.closed = True
            with self.lock:
                for cancel, pause in self.controls.values():
                    cancel.set(); pause.clear()
            for executor in self.executors.values():
                executor.shutdown(wait=False, cancel_futures=True)
