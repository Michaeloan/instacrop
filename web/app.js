"use strict";
const $ = (id) => document.getElementById(id);
const canvas = $("scanCanvas"), ctx = canvas.getContext("2d");
let token = "", scan = null, scanImage = null, currentFile = null, selected = 0;
let editMode = "outer", zoom = 1, fitScale = 1, drawing = null, draft = [], drag = null, handle = null;
let previewTimer = null, previewVersion = 0, busy = false, previewRunning = false, lastPreview = null;
let cropFocus = null;
const controls = ["add", "redetect", "export", "findInner", "drawInner", "borderless", "rotate", "markReviewed", "remove", "editInner", "editOuter", "page", "sensitivity", "minArea", "occupancy", "trim"];
controls.push("saveProject", "stepRepair", "stepExport", "applyRepairAll");
function status(message, loading = false) { $("status").textContent = message; if($("editDialog")?.open&&$("editorMessage"))$("editorMessage").textContent=message; document.body.classList.toggle("busy", loading); }
function setBusy(value) { busy = value; controls.forEach(id => { $(id).disabled = value || !scan; }); document.querySelectorAll("#photoList button, #photoList input").forEach(node => { node.disabled = value; }); if (!window.Workspace) { $("openButton").disabled = value; $("emptyOpen").disabled = value; $("openProject").disabled = value; } if (window.RestUI) RestUI.setBusy(value); if (window.Workspace) Workspace.updateActions(); }
async function api(path, data, raw = false) {
  const response = await fetch(path, {method: "POST", headers: {"X-Session-Token": token, "Content-Type": raw ? "application/octet-stream" : "application/json"}, body: raw ? data : JSON.stringify(data)});
  if (!response.ok) { const error = await response.json(); throw new Error(error.error || "处理失败"); }
  return response;
}
function chooseFile() { if (!busy) $("file").click(); }
$("openButton").onclick = chooseFile; $("emptyOpen").onclick = chooseFile;
$("file").onchange = () => { if ($("file").files[0]) loadScan($("file").files[0]); };
$("viewport").addEventListener("dragover", e => { e.preventDefault(); if (!busy) $("viewport").classList.add("dragover"); });
$("viewport").addEventListener("dragleave", () => $("viewport").classList.remove("dragover"));
$("viewport").addEventListener("drop", e => { e.preventDefault(); $("viewport").classList.remove("dragover"); if (!busy && e.dataTransfer.files[0]) loadScan(e.dataTransfer.files[0]); });
async function loadScan(file, page = 1) {
  if (busy) return;
  if (file.size > 80 * 1024 * 1024) { status("文件超过 80 MB，请降低扫描分辨率。"); return; }
  setBusy(true); status("正在识别相纸与内部画面…", true);
  clearTimeout(previewTimer); ++previewVersion;
  try {
    const query = new URLSearchParams({name: file.name, page, sensitivity: $("sensitivity").value, min_area: $("minArea").value});
    const result = await (await api("/api/scan?" + query, file, true)).json();
    await installScan(result, file);
    status(scan.photos.length ? `已为你整理好 ${scan.photos.length} 张照片，可以直接导出。` : "没有找到完整照片，可以手动框选一张。");
  } catch (error) { status(error.message); }
  finally { setBusy(false); $("file").value=""; }
}
async function installScan(result, file = null, isCurrent = () => true) {
    const newImage = new Image(); newImage.src = "/api/scan-preview?job=" + encodeURIComponent(result.job);
    await newImage.decode();
    if (!isCurrent()) return false;
    scan = result; scanImage = newImage; currentFile = file; selected = 0; handle = null; drawing = null; draft = [];
    lastPreview = null;
    editMode = "outer"; zoom = 1;
    $("empty").hidden = true; canvas.hidden = false;
    $("scanName").textContent = `${scan.name} · ${scan.width} × ${scan.height}`;
    $("pageLabel").hidden = scan.pages < 2;
    $("page").replaceChildren(...Array.from({length: scan.pages}, (_, i) => { const option = document.createElement("option"); option.value = i + 1; option.textContent = `${i + 1} / ${scan.pages}`; return option; }));
    $("page").value = scan.page;
    ["zoomIn", "zoomOut", "fit"].forEach(id => { $(id).disabled = false; });
    $("occupancy").value = result.occupancy !== undefined?Math.round(result.occupancy*100):78;
    $("trim").value = result.trim !== undefined?result.trim:0;
    $("occupancyValue").textContent = $("occupancy").value + "%";
    if (window.RestUI) RestUI.onNewScan();
    fitCanvas(); renderList(); renderEditor();
    if (window.Workspace) { setBusy(false); } else if (window.Gallery) await Gallery.onScan(); else schedulePreview();
    return true;
}
async function redetect(page) {
  if (busy || !scan) return;
  setBusy(true); status("正在重新识别，当前页的编辑将重新开始…", true); ++previewVersion;
  try { const result = await (await api("/api/redetect", {job: scan.job, page, sensitivity: $("sensitivity").value, min_area: $("minArea").value})).json(); await installScan(result); status(`重新识别完成：${scan.photos.length} 张照片。`); }
  catch (error) { status(error.message); } finally { setBusy(false); }
}
$("page").onchange = () => redetect(Number($("page").value));
$("redetect").onclick = () => redetect(scan.page);
function fitCanvas() {
  if (!scanImage) return;
  const view = $("viewport"); fitScale = Math.min((view.clientWidth - 48) / scanImage.width, (view.clientHeight - 48) / scanImage.height);
  fitScale = Math.max(.1, fitScale); draw();
}
$("fit").onclick = () => { zoom = 1; fitCanvas(); };
$("zoomIn").onclick = () => { zoom = Math.min(6, zoom * 1.35); draw(); };
$("zoomOut").onclick = () => { zoom = Math.max(.4, zoom / 1.35); draw(); };
window.addEventListener("resize", fitCanvas);
function cropBounds() {
  const photo = currentPhoto();
  if (!photo || drawing === "outer" || !window.Gallery || !$("editDialog").open) return {x:0,y:0,w:scan.width,h:scan.height};
  if(cropFocus && cropFocus.owner===photo)return cropFocus;
  const minX=Math.min(...photo.outer.map(p=>p[0])),maxX=Math.max(...photo.outer.map(p=>p[0]));
  const minY=Math.min(...photo.outer.map(p=>p[1])),maxY=Math.max(...photo.outer.map(p=>p[1]));
  const pad=Math.max(maxX-minX,maxY-minY)*.075;
  const x=Math.max(0,minX-pad),y=Math.max(0,minY-pad);
  cropFocus={owner:photo,x,y,w:Math.min(scan.width,maxX+pad)-x,h:Math.min(scan.height,maxY+pad)-y};return cropFocus;
}
function cropRotation(){return window.Gallery&&$("editDialog").open&&drawing!=="outer"?(currentPhoto()?.rotation||0):0;}
function sourceToCanvas(point) { const b=cropBounds();let x=(point[0]-b.x)/b.w,y=(point[1]-b.y)/b.h;const r=cropRotation();if(r===1)[x,y]=[1-y,x];else if(r===2)[x,y]=[1-x,1-y];else if(r===3)[x,y]=[y,1-x];return [x*canvas.width,y*canvas.height]; }
function eventPoint(event) {
  const rect = canvas.getBoundingClientRect();
  const b=cropBounds();let x=(event.clientX-rect.left)/rect.width,y=(event.clientY-rect.top)/rect.height;const r=cropRotation();if(r===1)[x,y]=[y,1-x];else if(r===2)[x,y]=[1-x,1-y];else if(r===3)[x,y]=[1-y,x];return [Math.max(0,Math.min(scan.width-1,b.x+x*b.w)),Math.max(0,Math.min(scan.height-1,b.y+y*b.h))];
}
function polygon(points, color, active, dashed = false) {
  if (!points) return;
  ctx.beginPath(); points.map(sourceToCanvas).forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)); ctx.closePath();
  ctx.strokeStyle = color; ctx.lineWidth = active ? 2.5 : 1.2; ctx.setLineDash(dashed ? [5, 5] : []); ctx.stroke(); ctx.setLineDash([]);
  if (active) points.map(sourceToCanvas).forEach(([x, y], i) => { ctx.beginPath(); ctx.arc(x, y, handle === i ? 7 : 5, 0, Math.PI * 2); ctx.fillStyle = handle === i ? color : "white"; ctx.fill(); ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke(); });
}
function draw() {
  if (!scanImage || !scan) return;
  const dpr = window.devicePixelRatio || 1;
  const b=cropBounds(),view=$("viewport"),rotation=cropRotation();
  const viewW=rotation%2?b.h:b.w,viewH=rotation%2?b.w:b.h;
  const factor=Math.min((view.clientWidth-36)/viewW,(view.clientHeight-36)/viewH);
  const width=Math.max(60,Math.round(viewW*Math.max(.03,factor)*zoom));
  const height=Math.max(60,Math.round(viewH*Math.max(.03,factor)*zoom));
  canvas.style.width = width + "px"; canvas.style.height = height + "px";
  canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
  // Draw in physical canvas pixels; source transforms account for DPR.
  ctx.save();if(rotation===1){ctx.translate(canvas.width,0);ctx.rotate(Math.PI/2);}else if(rotation===2){ctx.translate(canvas.width,canvas.height);ctx.rotate(Math.PI);}else if(rotation===3){ctx.translate(0,canvas.height);ctx.rotate(-Math.PI/2);}ctx.drawImage(scanImage,b.x*scanImage.width/scan.width,b.y*scanImage.height/scan.height,b.w*scanImage.width/scan.width,b.h*scanImage.height/scan.height,0,0,rotation%2?canvas.height:canvas.width,rotation%2?canvas.width:canvas.height);ctx.restore();
  scan.photos.forEach((photo, index) => {
    if (window.Gallery && $("editDialog").open && index !== selected && drawing !== "outer") return;
    const active = selected === index && !drawing;
    polygon(photo.outer, photo.enabled ? "#236ca5" : "#7a858c", active && editMode === "outer", !photo.enabled);
    if (active) polygon(photo.inner, "#bd6118", editMode === "inner");
    if(window.Gallery&&$("editDialog").open&&drawing!=="outer")return;
    const [x, y] = sourceToCanvas(photo.outer[0]); ctx.font = `600 ${13 * dpr}px sans-serif`;
    const label = `${index + 1}`; ctx.fillStyle = active ? "#236ca5" : "#42596a"; ctx.fillRect(x, Math.max(0, y - 23 * dpr), 23 * dpr, 21 * dpr);
    ctx.fillStyle = "white"; ctx.fillText(label, x + 5 * dpr, Math.max(16 * dpr, y - 7 * dpr));
  });
  if (drawing) { ctx.strokeStyle = "#bd6118"; ctx.lineWidth = 2; ctx.beginPath(); draft.map(sourceToCanvas).forEach(([x, y], i) => { if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); }); ctx.stroke(); draft.map(sourceToCanvas).forEach(([x, y]) => { ctx.fillStyle = "#bd6118"; ctx.fillRect(x - 4, y - 4, 8, 8); }); }
  $("zoomValue").textContent = Math.round(zoom * 100) + "%";
  $("canvasHint").textContent = drawing ? `点选照片的四角（${draft.length}/4），Esc 取消` : "拖动四角，调整照片边缘";
}
function renderList() {
  if (!scan) return;
  $("count").textContent = scan.photos.length; const list = $("photoList"); list.replaceChildren();
  scan.photos.forEach((photo, index) => {
    const row = document.createElement("div"); row.className = "photo-row" + (index === selected ? " selected" : "");
    const check = document.createElement("input"); check.type = "checkbox"; check.checked = photo.enabled; check.setAttribute("aria-label", `导出照片 ${index + 1}`);
    check.disabled = busy;
    check.onchange = () => { if (busy) { check.checked = photo.enabled; return; } photo.enabled = check.checked; draw(); };
    const button = document.createElement("button"); button.type = "button";
    const title = document.createElement("span"); title.textContent = `照片 ${index + 1}`;
    const hint = document.createElement("span"); hint.className = "row-hint" + (photo.warnings.length ? " pending" : "");
    hint.textContent = !photo.inner ? "画面待框选" : photo.warnings.length ? "需检查" : photo.reviewed ? "已检查" : "相纸 + 画面";
    button.disabled = busy;
    button.append(title, hint); button.onclick = () => { if (busy) return; selected = index; handle = null; drawing = null; draft = []; renderList(); renderEditor(); draw(); schedulePreview(); };
    row.append(check, button); list.append(row);
  });
  if (!scan.photos.length) { const p = document.createElement("p"); p.className = "quiet"; p.textContent = "可调整识别参数，或手动补一张。"; list.append(p); }
}
function currentPhoto() { return scan && scan.photos[selected]; }
function renderEditor() {
  const photo = currentPhoto(); $("editor").hidden = !photo;
  if (window.RestUI) RestUI.renderOptions();
  if (!photo) { clearPreviews(); return; }
  if (window.Gallery) {
    $("trim").value = photo.presentation?.trim || 0;
    $("occupancy").value = Math.round((photo.presentation?.occupancy ?? .78) * 100);
    $("occupancyValue").textContent = $("occupancy").value + "%";
  }
  $("selectedName").textContent = `照片 ${selected + 1}`;
  $("formatHint").textContent = (photo.format_hints || ["其他 / 未知格式"]).join(" / ");
  $("editOuter").classList.toggle("active", editMode === "outer"); $("editInner").classList.toggle("active", editMode === "inner");
  $("warning").hidden = !photo.warnings.length; $("warning").textContent = photo.warnings.join("；");
  $("markReviewed").textContent = photo.reviewed ? "边缘已确认" : "确认边缘";
}
function clearPreviews() { ["paper", "image", "composition"].forEach(mode => { $(mode + "Preview").hidden = true; $(mode + "Preview").removeAttribute("src"); }); $("previewStatus").textContent = "选择一张照片查看"; }
function schedulePreview() { clearTimeout(previewTimer); const version = ++previewVersion; if (window.RestUI) RestUI.pending(); previewTimer = setTimeout(() => updatePreview(version), 220); }
async function updatePreview(version) {
  const photo = currentPhoto(); if (!photo || busy || drawing || previewRunning || (window.Gallery && !$("editDialog").open)) return;
  previewRunning = true;
  $("previewStatus").textContent = "正在更新预览…";
  status("正在更新照片预览…",true);
  try {
    const result = await (await api("/api/preview", {job: scan.job, photo, occupancy: Number($("occupancy").value) / 100, trim: Number($("trim").value)})).json();
    if (version !== previewVersion) return;
    lastPreview = result; showOutputPreview();
    if (window.RestUI) await RestUI.setPreview(result.repair, photo, version);
    if (version !== previewVersion) return;
    photo.format_hints = result.format_hints; renderEditor();
    $("previewStatus").textContent = photo.inner ? "预览经过缩小；导出使用原图" : "内部画面尚未确认";
    status(photo.inner?"预览已更新，原始照片保持不变。":"请补一下照片画面的四角。");
  } catch (error) { if (version === previewVersion) { clearPreviews(); $("previewStatus").textContent = error.message; if (window.RestUI) RestUI.failed(error.message);status(error.message); } }
  finally { previewRunning = false; if (version !== previewVersion) { clearTimeout(previewTimer); previewTimer = setTimeout(() => updatePreview(previewVersion), 80); } }
}
function showOutputPreview() { if (!lastPreview) return; const images = $("outputPreviewMode").value === "original" ? lastPreview.original_images : lastPreview.images; ["paper", "image", "composition"].forEach(mode => { const img = $(mode + "Preview"); img.hidden = !images[mode]; if (images[mode]) img.src = images[mode]; else img.removeAttribute("src"); }); }
$("outputPreviewMode").onchange = showOutputPreview;
function convex(points) {
  if (points.length !== 4) return false;
  let sign = 0;
  for (let i = 0; i < 4; i++) { const a = points[i], b = points[(i + 1) % 4], c = points[(i + 2) % 4]; const cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]); if (Math.abs(cross) < 1) return false; if (sign && Math.sign(cross) !== sign) return false; sign = Math.sign(cross); }
  return true;
}
function order(points) { const center = points.reduce((p, q) => [p[0] + q[0] / 4, p[1] + q[1] / 4], [0, 0]); const sorted = points.slice().sort((a, b) => Math.atan2(a[1] - center[1], a[0] - center[0]) - Math.atan2(b[1] - center[1], b[0] - center[0])); let start = 0; sorted.forEach((p, i) => { if (p[0] + p[1] < sorted[start][0] + sorted[start][1]) start = i; }); return sorted.slice(start).concat(sorted.slice(0, start)); }
function changed() { const photo = currentPhoto(); if (photo) { photo.reviewed = false; } $("projectState").textContent = "有编辑内容，可保存项目继续处理"; draw(); renderList(); renderEditor(); schedulePreview(); }
canvas.addEventListener("pointerdown", e => {
  if (!scan || busy) return;
  canvas.focus(); const point = eventPoint(e);
  if (drawing) {
    draft.push(point);
    if (draft.length === 4) {
      const q = order(draft); if (!convex(q)) { draft = []; status("四角不能重叠或在一条线上，请重新点选。"); draw(); return; }
      if (drawing === "outer") { scan.photos.push({outer: q, inner: null, rotation: 0, warnings: ["手动添加：请继续框选内部画面"], method: "manual", enabled: true, format_hints: ["其他 / 未知格式"]}); selected = scan.photos.length - 1; editMode = "outer"; }
      else { currentPhoto().inner = q; currentPhoto().warnings = ["手动框选画面，请检查四角是否位于相纸内"]; editMode = "inner"; }
      drawing = null; draft = []; status("四角已添加，可以继续拖动调整。"); changed();
    } else draw();
    return;
  }
  const photo = currentPhoto(); const points = photo && photo[editMode];
  if (points) {
    const rect = canvas.getBoundingClientRect(); const bounds=cropBounds();const radius = 14 * (cropRotation()%2?bounds.h:bounds.w) / rect.width;
    const index = points.findIndex(p => Math.hypot(p[0] - point[0], p[1] - point[1]) < radius);
    if (index >= 0) { handle = index; drag = {index, start: points[index].slice()}; canvas.setPointerCapture(e.pointerId); draw(); return; }
  }
  const index = scan.photos.findIndex(p => pointInPolygon(point, p.outer));
  if (index >= 0) { selected = index; handle = null; renderList(); renderEditor(); draw(); schedulePreview(); }
});
function pointInPolygon(point, points) { let inside = false; for (let i = 0, j = points.length - 1; i < points.length; j = i++) { const a = points[i], b = points[j]; if ((a[1] > point[1]) !== (b[1] > point[1]) && point[0] < (b[0] - a[0]) * (point[1] - a[1]) / (b[1] - a[1]) + a[0]) inside = !inside; } return inside; }
canvas.addEventListener("pointermove", e => { if (!drag) return; const photo = currentPhoto(), points = photo[editMode]; const prior = points[drag.index]; points[drag.index] = eventPoint(e); if (!convex(points)) points[drag.index] = prior; draw(); });
canvas.addEventListener("pointerup", () => { if (drag) { drag = null; changed(); } });
canvas.addEventListener("pointercancel", () => { if (drag) { currentPhoto()[editMode][drag.index] = drag.start; drag = null; draw(); } });
canvas.addEventListener("keydown", e => {
  if (e.key === "Escape") { drawing = null; draft = []; drag = null; draw(); return; }
  const directions = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]};
  const direction = directions[e.key]; const photo = currentPhoto(); if (!direction || handle === null || !photo || !photo[editMode] || busy) return;
  e.preventDefault(); const p = photo[editMode][handle], step = e.shiftKey ? 10 : 1;
  const r=cropRotation();if(r===1)[direction[0],direction[1]]=[direction[1],-direction[0]];else if(r===2)[direction[0],direction[1]]=[-direction[0],-direction[1]];else if(r===3)[direction[0],direction[1]]=[-direction[1],direction[0]];
  const next = [Math.max(0, Math.min(scan.width - 1, p[0] + direction[0] * step)), Math.max(0, Math.min(scan.height - 1, p[1] + direction[1] * step))];
  photo[editMode][handle] = next; if (!convex(photo[editMode])) photo[editMode][handle] = p; changed();
});
$("add").onclick = () => { if (window.RestUI) RestUI.showWorkspace("crop"); drawing = "outer"; draft = []; handle = null; status("在扫描图上依次点选这张相纸的四角，Esc 取消。"); draw(); canvas.focus(); };
$("drawInner").onclick = () => { drawing = "inner"; draft = []; handle = null; status("在扫描图上依次点选内部画面的四角，Esc 取消。"); draw(); canvas.focus(); };
$("editOuter").onclick = () => { editMode = "outer"; handle = null; renderEditor(); draw(); };
$("editInner").onclick = () => { const p = currentPhoto(); if (!p.inner) { $("drawInner").click(); return; } editMode = "inner"; handle = null; renderEditor(); draw(); };
$("rotate").onclick = () => { currentPhoto().rotation = (currentPhoto().rotation + 1) % 4; changed(); };
$("borderless").onclick = () => { const p = currentPhoto(); p.inner = p.outer.map(point => point.slice()); p.warnings = []; p.method = "borderless"; changed(); };
$("markReviewed").onclick = () => { const p = currentPhoto(); if (!p.inner) { status("请先框选内部画面，或标记为无边框照片。"); return; } p.warnings = []; p.reviewed = true; renderList(); renderEditor(); status("这张照片的四角已标记为检查完成。"); };
$("remove").onclick = () => { scan.photos.splice(selected, 1); selected = Math.max(0, Math.min(selected, scan.photos.length - 1)); handle = null; drawing = null; changed(); };
$("findInner").onclick = async () => {
  const p = currentPhoto(); setBusy(true); status("正在重新识别内部画面…", true);
  try { const result = await (await api("/api/inner", {job: scan.job, photo: p})).json(); Object.assign(p, result); status(p.inner ? "内部画面已更新，请检查橙色边框。" : "没有找到可靠的内部画面，请手动点选四角。"); }
  catch (error) { status(error.message); }
  finally { setBusy(false); changed(); }
};
$("occupancy").oninput = () => { $("occupancyValue").textContent = $("occupancy").value + "%"; schedulePreview(); };
$("trim").oninput = schedulePreview;
$("export").onclick = async () => {
  if (drawing) { status("请先完成四角框选，或按 Esc 取消。"); return; }
  setBusy(true); status("正在从原图导出三种结果…", true);
  try {
    const result = await saveArtifact("export");
    status(result.cancelled ? "已取消导出。" : "导出完成：三种成图，以及已启用修复照片的原始裁剪与修复区域。");
  } catch (error) { status(error.message); }
  finally { setBusy(false); }
};
function artifactPayload() { return {job: scan.job, photos: scan.photos, occupancy:.78,trim:0,friendly_names:true}; }
async function saveArtifact(kind) {
  const payload = artifactPayload();
  if (window.pywebview && window.pywebview.api && window.pywebview.api.save_artifact) {
    const result = await window.pywebview.api.save_artifact(kind, payload); if (result.error) throw new Error(result.error); return result;
  }
  const response = await api(kind === "project" ? "/api/project-save" : "/api/export", payload);
  const blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement("a");
  link.href = url; link.download = scan.name.replace(/\.[^.]+$/, "") + (kind === "project" ? ".polascan" : `_page${scan.page}.zip`); link.click(); setTimeout(() => URL.revokeObjectURL(url), 60000); return {cancelled:false};
}
$("saveProject").onclick = async () => { if (!scan || busy) return; setBusy(true); status("正在保存原图和编辑项目…", true); try { const result = await saveArtifact("project"); status(result.cancelled ? "已取消保存项目。" : "项目已保存，重新打开可继续调整四角和修复区域。"); if (!result.cancelled) $("projectState").textContent = "项目已保存"; } catch (error) { status(error.message); } finally { setBusy(false); } };
$("openProject").onclick = () => { if (!busy) $("projectFile").click(); };
$("projectFile").onchange = async () => { const file = $("projectFile").files[0]; if (!file || busy) return; setBusy(true); ++previewVersion; status("正在恢复项目…", true); try { const result = await (await api("/api/project-open", file, true)).json(); await installScan(result); status("项目已恢复，原图、四角和修复设置均已载入。"); $("projectState").textContent = "已打开保存的项目"; } catch (error) { status(error.message); } finally { setBusy(false); $("projectFile").value = ""; } };
$("openButton").disabled=true;$("emptyOpen").disabled=true;
(async () => { try { token = (await (await fetch("/api/session")).json()).token;$("openButton").disabled=false;$("emptyOpen").disabled=false; } catch { status("暂时无法连接，请关闭后重新打开软件。"); } })();
