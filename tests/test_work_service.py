from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from PIL import Image

from app import LocalServer
from task_queue import TaskQueue


def wait(queue, task_id):
    until = time.monotonic() + 15
    while time.monotonic() < until:
        task = next(t for t in queue.snapshot() if t["id"] == task_id)
        if task["state"] in ("completed", "partial", "failed", "cancelled"):
            return task
        time.sleep(.02)
    raise AssertionError("Task did not complete")


class WorkServiceTests(unittest.TestCase):
    def test_import_edit_append_save_and_download_v2_over_http(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            server = LocalServer(("127.0.0.1", 0), root / "data")
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            def post(path, data):
                body = data if isinstance(data, bytes) else json.dumps(data).encode()
                return urlopen(Request(base + path, body, {"X-Session-Token":server.token,"Content-Type":"application/json"}), timeout=20)
            try:
                for index in range(5):
                    file = root / f"photo{index}.png"; Image.new("RGB", (80, 90), (index*20, 80, 90)).save(file)
                    task_id = json.load(post(f"/api/workspace/import?kind=photo&name=photo{index}.png", file.read_bytes()))["task_id"]
                    self.assertEqual(wait(server.service().queue, task_id)["state"], "completed")
                snap = json.load(urlopen(base + "/api/workspace"))
                self.assertEqual(len(snap["pages"]), 5)
                self.assertEqual(snap["sources"][0]["name"], "photo0.png")
                page = json.load(post("/api/workspace/page", {"page_id":snap["pages"][0]["id"]}))
                page["photos"][0]["rotation"] = 1
                updated = json.load(post("/api/workspace/update", {"page_id":page["id"],"photos":page["photos"],"revision":page["revision"]}))
                self.assertEqual(updated["photos"][0]["rotation"], 1)
                with self.assertRaises(HTTPError) as caught:
                    post("/api/workspace/update", {"page_id":page["id"],"photos":page["photos"],"revision":page["revision"]})
                self.assertEqual(caught.exception.code, 400)
                task_id = json.load(post("/api/workspace/project-save", {}))["task_id"]
                task = wait(server.service().queue, task_id)
                self.assertEqual(task["state"], "completed")
                artifact = task["results"][0]["artifact_id"]
                with self.assertRaises(HTTPError):
                    urlopen(base + "/api/artifacts/" + artifact)
                content = urlopen(base + "/api/artifacts/" + artifact + "?token=" + server.token).read()
                self.assertGreater(len(content), 1000)
                project = root / "restored.polascan"; project.write_bytes(content)
                server.service().workspace.open_project(project)
                self.assertEqual(len(server.service().workspace.snapshot()["pages"]), 5)
                self.assertEqual(server.service().workspace.snapshot()["pages"][0]["photos"][0]["rotation"], 1)
                self.assertEqual(json.load(urlopen(base + "/api/tasks"))["tasks"][-1]["id"], task_id)
            finally:
                server.shutdown(); server.server_close(); thread.join(3)

    def test_range_and_path_access_are_restricted(self):
        from work_service import send_file
        with TemporaryDirectory() as folder:
            server = LocalServer(("127.0.0.1", 0), Path(folder) / "data")
            thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                asset = Path(folder) / "preview.mp4"; asset.write_bytes(bytes(range(100)))
                art = server.service().artifact(asset)["artifact_id"]
                request = Request(base + "/api/artifacts/" + art + "?token=" + server.token, headers={"Range":"bytes=10-19"})
                response = urlopen(request)
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), bytes(range(10,20)))
                self.assertEqual(response.headers["Content-Range"], "bytes 10-19/100")
                with self.assertRaises(HTTPError):
                    urlopen(Request(base + "/api/artifacts/" + art + "?token=" + server.token, headers={"Range":"bytes=100-101"}))
            finally:
                server.shutdown(); server.server_close(); thread.join(3)

    def test_queue_failure_retry_and_interrupted_restart(self):
        with TemporaryDirectory() as folder:
            queue = TaskQueue(Path(folder)); failed = {2}
            def worker(item, cancel, progress):
                if item in failed:
                    raise ValueError("fixture failure")
                return {"value":item}
            task = queue.start("fixture", [1,2,3],worker)["task_id"]
            result = wait(queue, task)
            self.assertEqual(result["state"], "partial")
            self.assertEqual([entry["item"] for entry in result["results"]], [1,3])
            failed.clear()
            retried = queue.control(task,"retry")["task_id"]
            self.assertEqual(wait(queue,retried)["items"], [2])
            with queue.lock:
                queue.tasks["interrupted"] = {"state":"running"}
                queue._save()
            self.assertEqual(next(t for t in TaskQueue(Path(folder)).snapshot() if t.get("state")=="interrupted")["interrupted"], True)
            queue.close()

    def test_cancel_after_completed_item_does_not_duplicate_it_on_retry(self):
        with TemporaryDirectory() as folder:
            queue = TaskQueue(Path(folder))
            def worker(item, cancel, progress):
                if item == 1:
                    cancel.set()
                return {"created_source": item}
            task = queue.start("fixture", [1,2], worker)["task_id"]
            result = wait(queue, task)
            self.assertEqual(result["state"], "cancelled")
            self.assertEqual([entry["item"] for entry in result["results"]], [1])
            resumed = queue.control(task,"retry")["task_id"]
            self.assertEqual(wait(queue,resumed)["items"], [2])
            queue.close()

    def test_pause_holds_following_items_and_resume_finishes(self):
        with TemporaryDirectory() as folder:
            queue=TaskQueue(Path(folder)); entered=threading.Event(); release=threading.Event(); seen=[]
            def worker(item,cancel,progress):
                seen.append(item)
                if item==1:
                    entered.set();release.wait(5)
                return {"item_value":item}
            tid=queue.start("fixture",[1,2],worker)["task_id"]
            self.assertTrue(entered.wait(5))
            queue.control(tid,"pause");release.set();time.sleep(.25)
            self.assertEqual(seen,[1])
            queue.control(tid,"resume")
            self.assertEqual(wait(queue,tid)["state"],"completed")
            self.assertEqual(seen,[1,2])
            queue.close()

    def test_partial_import_retry_keeps_source_instead_of_importing_again(self):
        from work_service import WorkService
        from unittest.mock import patch
        with TemporaryDirectory() as folder:
            service=WorkService(Path(folder)/"data")
            image=Image.new("RGB",(100,120),"white")
            file=Path(folder)/"scan.tiff";image.save(file,save_all=True,append_images=[image])
            with patch("workspace.detect",side_effect=[[],RuntimeError("transient detector failure")]):
                tid=service.import_paths([file],"scan")["task_id"]
                result=wait(service.queue,tid)
            self.assertEqual(result["state"],"partial")
            sourceid=service.workspace.snapshot()["sources"][0]["id"]
            firstpage=service.workspace.snapshot()["pages"][0]
            with patch("workspace.detect",return_value=[]):
                retry=service.control({"task_id":tid,"action":"retry"})["task_id"]
                self.assertEqual(wait(service.queue,retry)["state"],"completed")
            snap=service.workspace.snapshot()
            self.assertEqual(len(snap["sources"]),1)
            self.assertEqual(snap["sources"][0]["id"],sourceid)
            self.assertEqual(snap["pages"][0],firstpage)
            self.assertEqual(len(snap["pages"]),2)
            service.close()

    def test_workspace_actions_have_consistent_results(self):
        from work_service import WorkService
        with TemporaryDirectory() as folder:
            service=WorkService(Path(folder)/"data")
            file=Path(folder)/"ordinary.png";Image.new("RGB",(80,100)).save(file)
            source=service.workspace.import_file(file,"photo")
            page=service.workspace.snapshot()["pages"][0];pid=page["photos"][0]["id"]
            selected=service.post("/api/workspace/select",{"photo_ids":[pid],"enabled":False})
            self.assertFalse(selected["pages"][0]["photos"][0]["enabled"])
            applied=service.post("/api/workspace/apply",{"photo_ids":[pid],"fields":{"presentation":{"trim":1}}})
            service.post("/api/workspace/undo",{"undo_token":applied["undo_token"]})
            self.assertEqual(service.workspace.snapshot()["pages"][0]["photos"][0]["presentation"].get("trim",0),0)
            service.post("/api/workspace/remove",{"source_id":source["id"]})
            self.assertFalse(service.workspace.snapshot()["pages"])
            service.post("/api/workspace/new",{})
            self.assertFalse(service.workspace.snapshot()["sources"])
            service.close()




if __name__ == "__main__":
    unittest.main()
