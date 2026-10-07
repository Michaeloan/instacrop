"""Isolated synthetic scan and photo sources for the crop edition UI test."""
from pathlib import Path
import json
import sys
import uuid

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "tests"))
from PIL import Image, ImageDraw
from app import LocalServer
from test_scanner import mixed_scan

output = root / "outputs/ui-pages"
fixture = output / uuid.uuid4().hex
fixture.mkdir(parents=True)
server = LocalServer(("127.0.0.1", 0), fixture / "workspace")
ws = server.service().workspace
scan, _ = mixed_scan()
path = fixture / "Mixed scan.png"
Image.fromarray(scan.image).save(path)
scanned = ws.import_file(path, "scan")
path = fixture / "Empty scan.png"
Image.new("RGB", (500, 600), "#f1f1ef").save(path)
empty = ws.import_file(path, "scan")
for name, color in [("Lake.png", "#477f9a"), ("Mountain.png", "#546a7e")]:
    path = fixture / name
    image = Image.new("RGB", (480, 360), color)
    drawing = ImageDraw.Draw(image)
    drawing.polygon([(0, 360), (175, 90), (300, 235), (480, 360)], fill="#8ab4a6")
    drawing.ellipse((350, 45, 400, 95), fill="#f3e4a3")
    image.save(path)
    ws.import_file(path, "photo")
info = {"url": f"http://127.0.0.1:{server.server_port}", "fixture": str(fixture),
        "scan_id": scanned["id"], "empty_id": empty["id"]}
(output / "server.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
print(json.dumps(info), flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
