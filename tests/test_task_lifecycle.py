from concurrent.futures import CancelledError, Future
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from task_queue import TaskQueue, TasksActiveError
from work_service import WorkService


def settled(queue, task_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = next(t for t in queue.snapshot() if t["id"] == task_id)
        if task["state"] not in {"queued", "running", "paused", "cancelling"}:
            return task
        time.sleep(.01)
    raise AssertionError("Task did not settle")


class TaskLifecycleTests(unittest.TestCase):
    def test_closed_queue_rejects_submission_without_recording_ghost_task(self):
        with TemporaryDirectory() as folder:
            queue = TaskQueue(folder)
            queue.close()
            with self.assertRaisesRegex(ValueError, "已关闭"):
                queue.start("import", [1], lambda *args: {})
            self.assertEqual(queue.snapshot(), [])
            self.assertEqual(json.loads(queue.path.read_text(encoding="utf-8")), {})

    def test_failed_submission_rolls_back_queued_record(self):
        with TemporaryDirectory() as folder:
            queue = TaskQueue(folder)
            try:
                with patch.object(queue.executors["local"], "submit", side_effect=RuntimeError("cannot start thread")):
                    with self.assertRaisesRegex(RuntimeError, "cannot start"):
                        queue.start("import", [1], lambda *args: {})
                self.assertEqual(queue.tasks, {})
                self.assertEqual(queue.controls, {})
                self.assertEqual(queue.workers, {})
                self.assertEqual(json.loads(queue.path.read_text(encoding="utf-8")), {})
            finally:
                queue.close()

    def test_cancel_race_during_snapshot_is_reported_as_cancelled(self):
        with TemporaryDirectory() as folder:
            queue = TaskQueue(folder)
            future = Mock()
            future.cancelled.return_value = False
            future.done.return_value = True
            future.exception.side_effect = CancelledError()
            queue.tasks["race"] = {"id": "race", "kind": "import", "state": "queued"}
            queue.futures["race"] = future
            try:
                self.assertEqual(queue.snapshot()[0]["state"], "cancelled")
                self.assertEqual(queue.active_tasks(), [])
            finally:
                queue.close()

    def test_finishing_between_snapshot_and_cancel_does_not_abort_new_batch(self):
        with TemporaryDirectory() as folder:
            queue = TaskQueue(folder)
            entered, release = threading.Event(), threading.Event()
            def blocking(item, cancel, progress):
                entered.set()
                release.wait(5)
                return {}
            try:
                task = queue.start("import", [1], blocking)["task_id"]
                self.assertTrue(entered.wait(2))
                original_control = queue.control
                def finish_before_control(task_id, action):
                    release.set()
                    queue.futures[task].result(timeout=3)
                    return original_control(task_id, action)
                with patch.object(queue, "control", side_effect=finish_before_control):
                    self.assertEqual(queue.run_when_idle(lambda: "created", cancel_active=True), "created")
                self.assertEqual(queue.active_tasks(), [])
            finally:
                release.set()
                queue.close()

    def test_orphan_active_state_is_recovered_without_discarding_results(self):
        with TemporaryDirectory() as folder:
            queue = TaskQueue(folder)
            try:
                queue.tasks["orphan"] = {"id": "orphan", "kind": "import", "state": "running",
                                         "results": [{"item": 1}], "done": 1, "total": 2}
                self.assertEqual(queue.active_tasks(), [])
                task = queue.snapshot()[0]
                self.assertEqual(task["state"], "interrupted")
                self.assertEqual(task["results"], [{"item": 1}])
                self.assertEqual(queue.run_when_idle(lambda: "new batch"), "new batch")
                self.assertEqual(json.loads(queue.path.read_text(encoding="utf-8"))["orphan"]["state"], "interrupted")
            finally:
                queue.close()

    def test_cancelling_queued_task_is_immediate_even_behind_a_paused_worker(self):
        with TemporaryDirectory() as folder:
            queue = TaskQueue(folder)
            entered, release = threading.Event(), threading.Event()
            second = Mock()
            def blocking(item, cancel, progress):
                entered.set()
                release.wait(5)
                return {"value": item}
            try:
                first = queue.start("import", [1, 2], blocking)["task_id"]
                self.assertTrue(entered.wait(2))
                queue.control(first, "pause")
                pending = queue.start("retry_import", [3], second)["task_id"]
                queue.control(pending, "cancel")
                self.assertEqual(settled(queue, pending)["state"], "cancelled")
                self.assertTrue(queue.futures[pending].cancelled())
                self.assertEqual([task["id"] for task in queue.active_tasks()], [first])
                second.assert_not_called()
                queue.control(first, "cancel")
                release.set()
                self.assertEqual(settled(queue, first)["state"], "cancelled")
                second.assert_not_called()
            finally:
                release.set()
                queue.close()

    def test_done_future_cannot_leave_an_active_label(self):
        with TemporaryDirectory() as folder:
            queue = TaskQueue(folder)
            try:
                future = Future()
                future.set_exception(RuntimeError("worker exited"))
                queue.futures["failed"] = future
                queue.tasks["failed"] = {"id": "failed", "kind": "import", "state": "queued"}
                self.assertEqual(queue.active_tasks(), [])
                self.assertEqual(queue.snapshot()[0]["state"], "failed")
            finally:
                queue.close()

    def test_real_worker_blocks_switch_until_cancelled_and_finished(self):
        with TemporaryDirectory() as folder:
            queue = TaskQueue(folder)
            entered, release = threading.Event(), threading.Event()
            operation = Mock(return_value="created")
            def blocking(item, cancel, progress):
                entered.set()
                release.wait(5)
                return {"value": item}
            try:
                task = queue.start("import", [1], blocking)["task_id"]
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(TasksActiveError, "导入图片"):
                    queue.run_when_idle(operation, cancel_active=True)
                operation.assert_not_called()
                self.assertEqual(queue.active_tasks()[0]["state"], "cancelling")
                release.set()
                settled(queue, task)
                self.assertEqual(queue.run_when_idle(operation, cancel_active=True), "created")
                operation.assert_called_once()
            finally:
                release.set()
                queue.close()

    def test_new_batch_stops_tasks_then_archives_the_previous_workspace(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            service = WorkService(root / "workspace")
            entered, release = threading.Event(), threading.Event()
            def blocking(item, cancel, progress):
                entered.set()
                release.wait(5)
                return {"value": item}
            try:
                photo = root / "photo.png"
                Image.new("RGB", (80, 90), "blue").save(photo)
                source = service.workspace.import_file(photo, "photo")
                before = service.workspace.snapshot()
                original = service.workspace.asset(source["asset"])
                original_bytes = original.read_bytes()
                task = service.queue.start("import", [1], blocking)["task_id"]
                self.assertTrue(entered.wait(2))
                pending = service.post("/api/workspace/new", {"cancel_tasks": True})
                self.assertTrue(pending["pending"])
                self.assertEqual(pending["active_tasks"][0]["id"], task)
                self.assertEqual(service.workspace.snapshot(), before)
                release.set()
                settled(service.queue, task)
                result = service.post("/api/workspace/new", {"cancel_tasks": True})
                self.assertNotEqual(result["id"], before["id"])
                self.assertEqual(result["sources"], [])
                history = service.workspace.asset(f"history/{before['id']}.json")
                self.assertEqual(json.loads(history.read_text(encoding="utf-8")), before)
                self.assertEqual(original.read_bytes(), original_bytes)
            finally:
                release.set()
                service.close()

    def test_submission_waits_for_workspace_transition(self):
        with TemporaryDirectory() as folder:
            queue = TaskQueue(folder)
            entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
            def transition():
                entered.set()
                release.wait(5)
            switch = threading.Thread(target=lambda: queue.run_when_idle(transition))
            def submit():
                queue.start("import", [1], lambda *args: {})
                submitted.set()
            addition = threading.Thread(target=submit)
            try:
                switch.start()
                self.assertTrue(entered.wait(2))
                addition.start()
                self.assertFalse(submitted.wait(.1))
                release.set()
                self.assertTrue(submitted.wait(2))
            finally:
                release.set()
                switch.join(5)
                if addition.ident:
                    addition.join(5)
                queue.close()
                for future in queue.futures.values():
                    if not future.cancelled():
                        future.result(timeout=5)


if __name__ == "__main__":
    unittest.main()
