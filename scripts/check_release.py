"""Verify the portable EXE using only the synthetic UI fixture's assets."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import uuid

root = Path(__file__).resolve().parents[1]
output = root / "outputs"
info = json.loads((output / "ui-pages/server.json").read_text(encoding="utf-8"))
fixture = output / ("native-fixture-" + uuid.uuid4().hex)
shutil.copytree(Path(info["fixture"]) / "workspace", fixture)
history = sorted((fixture / "history").glob("*.json"))
if history:
    shutil.copy2(history[-1], fixture / "manifest.json")
exe = root / "release/v1.0.4/InstaCrop/InstaCrop.exe"
reports = []
for mode in ("self-test", "smoke-test"):
    report = output / f"exe-v104-{mode}.json"
    result = subprocess.run([str(exe), "--" + mode, str(report)],
                            env={**os.environ, "POLASCAN_DATA_DIR": str(fixture)},
                            timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = json.loads(report.read_text(encoding="utf-8"))
    if result.returncode or data.get("status") != "ok":
        raise RuntimeError(data)
    reports.append(data)
print(json.dumps(reports, ensure_ascii=False, indent=2))
