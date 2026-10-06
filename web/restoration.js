"use strict";
window.RestUI = (() => {
  const repairCanvas = $("repairCanvas"), rctx = repairCanvas.getContext("2d");
  let owner = null, data = null, originals = null, repaired = null, maskImage = null, ready = false;
  let repairZoom = 1, brush = null, pointer = null, draftStroke = null;
  const redo = new WeakMap();
  const defaults = () => ({enabled:false,auto_border:true,auto_image:false,sensitivity:40,max_diameter:14,radius:3,adjustments:false,brightness:0,contrast:0,saturation:0,sharpen:0,denoise:0,strokes:[]});
  const fields = {repairEnabled:"enabled",autoBorder:"auto_border",autoImage:"auto_image",dustSensitivity:"sensitivity",dustDiameter:"max_diameter",inpaintRadius:"radius",adjustmentsEnabled:"adjustments",brightness:"brightness",contrast:"contrast",saturation:"saturation",sharpen:"sharpen",denoise:"denoise"};
  function options(photo = currentPhoto()) { if (!photo) return null; if (!photo.restoration || !Array.isArray(photo.restoration.strokes)) photo.restoration = {...defaults(),...(photo.restoration || {}),strokes:photo.restoration?.strokes || []}; return photo.restoration; }
  function workspace(mode) { if (mode !== "crop" && !currentPhoto()) { status("先打开扫描图并选择一张照片。"); return; } document.body.dataset.workspace = mode; ["crop","repair","export"].forEach(key => $("step" + key[0].toUpperCase() + key.slice(1)).classList.toggle("active", key === mode)); draw(); drawRepair(); }
  $("stepCrop").onclick = () => workspace("crop"); $("stepRepair").onclick = () => workspace("repair"); $("stepExport").onclick = () => workspace("export");
  function renderOptions() {
    const photo = currentPhoto(), value = options(photo); $("repairOptions").hidden = !photo;
    if (owner !== photo) { owner = photo; ready = false; data = null; repaired = null; originals = null; maskImage = null; repairZoom = 1; repairCanvas.hidden = true; $("repairEmpty").hidden = false; $("repairEmpty").textContent = photo ? "正在生成原图预览…" : "选择一张照片，等待裁剪预览。"; $("repairStats").textContent = photo ? "正在生成本张预览…" : "先选择一张照片"; $("repairHint").textContent = "预览更新中，暂不涂抹"; $("repairWarning").hidden = true; lastPreview = null; clearPreviews(); }
    if (!value) return;
    $("repairPhotoName").textContent = `照片 ${selected + 1} · 污点修复`;
    Object.entries(fields).forEach(([id,key]) => { if ($(id).type === "checkbox") $(id).checked = ["auto_border","auto_image"].includes(key)?value.enabled&&value[key]:value[key]; else $(id).value = value[key]; updateOutput(id); });
    $("undoStroke").disabled = busy || !value.strokes.length; $("redoStroke").disabled = busy || !(redo.get(photo) || []).length;
    $("clearStrokes").disabled = busy || !value.strokes.length;
  }
  function updateOutput(id) { const output = $(id + "Value"); if (output) output.textContent = $(id).value + (["dustDiameter","inpaintRadius","brushDiameter"].includes(id) ? " px" : ""); }
  Object.entries(fields).forEach(([id,key]) => { $(id).addEventListener("input", () => { const value = options(); if (!value || busy) return; value[key] = $(id).type === "checkbox" ? $(id).checked : Number($(id).value); if(["brightness","contrast","saturation","sharpen","denoise"].includes(key))value.adjustments=true;if (key !== "enabled" && (key !== "adjustments" || value.adjustments)) value.enabled = true; changed(); }); });
  $("brushDiameter").oninput = () => { updateOutput("brushDiameter"); drawRepair(); };
  $("brushMode").onchange = drawRepair;
  $("applyRepairAll").onclick = () => { if (!scan || busy) return; const copy = {...options()}; delete copy.strokes; scan.photos.forEach(photo => { photo.restoration = {...options(photo),...copy,strokes:options(photo).strokes}; }); changed(); status("修复开关与参数已应用到全部照片，手动区域仍保留在各自照片。"); };
  $("resetRepair").onclick = () => { const photo = currentPhoto(); if (!photo || busy) return; photo.restoration = defaults(); redo.delete(photo); changed(); status("本张修复已重置，原始裁剪保持不变。"); };
  function undoStroke() { const photo = currentPhoto(), value = options(); if (!value || busy || !value.strokes.length) return; const stack = redo.get(photo) || []; stack.push(value.strokes.pop()); redo.set(photo,stack); changed(); }
  function redoStroke() { const photo = currentPhoto(), stack = redo.get(photo) || []; if (!photo || busy || !stack.length) return; options().strokes.push(stack.pop()); changed(); }
  $("undoStroke").onclick = undoStroke; $("redoStroke").onclick = redoStroke;
  $("clearStrokes").onclick = () => { const value = options(); if (!value || busy) return; value.strokes = []; redo.delete(currentPhoto()); changed(); };
  function pending() { ready = false; if (data) $("repairStats").textContent = "正在更新全分辨率修复预览…"; }
  function failed(message) { ready = false; $("repairStats").textContent = message; }
  async function setPreview(result, photo, version) {
    if (currentPhoto() !== photo || version !== previewVersion) return;
    const load = async src => { const img = new Image(); img.src = src; await img.decode(); return img; };
    const images = await Promise.all([load(result.original),load(result.result),load(result.mask)]);
    if (currentPhoto() !== photo || version !== previewVersion) return;
    owner = photo; data = result; [originals,repaired,maskImage] = images; ready = true; draftStroke = null;
    repairCanvas.hidden = false; $("repairEmpty").hidden = true;
    const stats = result.stats;
    $("repairStats").textContent = result.active ? `自动候选 ${stats.auto_spots} 处 · 手动 ${stats.manual_strokes} 笔 · 修复 ${stats.masked_pixels.toLocaleString()} 像素` : "修复未启用；当前显示原始裁剪";
    $("repairWarning").hidden = !stats.warnings.length; $("repairWarning").textContent = stats.warnings.join("；");
    drawRepair(); renderOptions();
  }
  function drawRepair() {
    if (!originals || !data || document.body.dataset.workspace !== "repair") return;
    const view = $("repairViewport"), dpr = window.devicePixelRatio || 1;
    const fit = Math.max(.1, Math.min((view.clientWidth-44)/originals.width,(view.clientHeight-44)/originals.height));
    const w = Math.max(40, Math.round(originals.width*fit*repairZoom)), h = Math.max(40,Math.round(originals.height*fit*repairZoom));
    repairCanvas.style.width=w+"px"; repairCanvas.style.height=h+"px"; repairCanvas.width=Math.round(w*dpr); repairCanvas.height=Math.round(h*dpr);
    const mode = $("repairView").value, split = Number($("compareSplit").value)/100;
    rctx.drawImage(mode === "result" ? repaired : originals,0,0,repairCanvas.width,repairCanvas.height);
    if (mode === "split") { const x=repairCanvas.width*split; rctx.save();rctx.beginPath();rctx.rect(x,0,repairCanvas.width-x,repairCanvas.height);rctx.clip();rctx.drawImage(repaired,0,0,repairCanvas.width,repairCanvas.height);rctx.restore();rctx.strokeStyle="white";rctx.lineWidth=2*dpr;rctx.beginPath();rctx.moveTo(x,0);rctx.lineTo(x,repairCanvas.height);rctx.stroke(); }
    if ($("showRepairMask").checked) rctx.drawImage(maskImage,0,0,repairCanvas.width,repairCanvas.height);
    if (draftStroke) { rctx.strokeStyle=draftStroke.mode === "erase" ? "#42bca3" : "#aa38dc"; rctx.globalAlpha=.55; rctx.lineCap="round";rctx.lineJoin="round";rctx.lineWidth=Number($("brushDiameter").value)*repairCanvas.width/data.paper_size[0];rctx.beginPath();draftStroke.paperPoints.forEach((p,i)=> i?rctx.lineTo(p[0]*repairCanvas.width/data.paper_size[0],p[1]*repairCanvas.height/data.paper_size[1]):rctx.moveTo(p[0]*repairCanvas.width/data.paper_size[0],p[1]*repairCanvas.height/data.paper_size[1]));rctx.stroke();rctx.globalAlpha=1; }
    if(pointer) { rctx.beginPath(); const radius=Math.max(3,Number($("brushDiameter").value)/2*repairCanvas.width/data.paper_size[0]);rctx.arc(pointer[0]*repairCanvas.width/data.paper_size[0],pointer[1]*repairCanvas.height/data.paper_size[1],radius,0,Math.PI*2);rctx.strokeStyle=$("brushMode").value === "erase"?"#42bca3":"#b541e7";rctx.lineWidth=1.5*dpr;rctx.stroke(); }
    $("repairZoomValue").textContent=Math.round(repairZoom*100)+"%";
    $("repairHint").textContent=ready?"在污点上点选或涂抹，Ctrl+Z 撤销":"预览更新中，暂不涂抹";
  }
  $("repairView").onchange=drawRepair; $("compareSplit").oninput=drawRepair; $("showRepairMask").onchange=drawRepair;
  $("repairZoomIn").onclick=()=>{repairZoom=Math.min(6,repairZoom*1.4);drawRepair();}; $("repairZoomOut").onclick=()=>{repairZoom=Math.max(.5,repairZoom/1.4);drawRepair();}; $("repairFit").onclick=()=>{repairZoom=1;drawRepair();};
  window.addEventListener("resize",drawRepair);
  function paperPoint(e) { const rect=repairCanvas.getBoundingClientRect();return [Math.max(0,Math.min(data.paper_size[0]-1,(e.clientX-rect.left)*data.paper_size[0]/rect.width)),Math.max(0,Math.min(data.paper_size[1]-1,(e.clientY-rect.top)*data.paper_size[1]/rect.height))]; }
  function project(point,m) { const denominator=m[2][0]*point[0]+m[2][1]*point[1]+m[2][2];return [(m[0][0]*point[0]+m[0][1]*point[1]+m[0][2])/denominator,(m[1][0]*point[0]+m[1][1]*point[1]+m[1][2])/denominator]; }
  function inverse(m) { const [a,b,c]=m[0],[d,e,f]=m[1],[g,h,i]=m[2];const determinant=a*(e*i-f*h)-b*(d*i-f*g)+c*(d*h-e*g);return [[e*i-f*h,c*h-b*i,b*f-c*e],[f*g-d*i,a*i-c*g,c*d-a*f],[d*h-e*g,b*g-a*h,a*e-b*d]].map(row=>row.map(v=>v/determinant)); }
  function addPoint(point) {
    const last=draftStroke.paperPoints.at(-1); if(last && Math.hypot(point[0]-last[0],point[1]-last[1])<Math.max(1,Number($("brushDiameter").value)/8))return;
    if(draftStroke.points.length>=4000)return;
    const source=project(point,inverse(data.source_to_paper));source[0]=Math.max(0,Math.min(scan.width-1,source[0]));source[1]=Math.max(0,Math.min(scan.height-1,source[1]));draftStroke.points.push(source);draftStroke.paperPoints.push(point);
  }
  repairCanvas.addEventListener("pointerdown",e=>{if(!ready || busy || !data || !currentPhoto())return;repairCanvas.focus();const value=options();if(value.strokes.length>=500){status("本张已有 500 笔，请清理部分区域。");return;}brush={pointerId:e.pointerId};draftStroke={mode:$("brushMode").value,radius:Number($("brushDiameter").value)/2,points:[],paperPoints:[]};addPoint(paperPoint(e));repairCanvas.setPointerCapture(e.pointerId);drawRepair();});
  repairCanvas.addEventListener("pointermove",e=>{if(!data)return;pointer=paperPoint(e);if(brush)addPoint(pointer);drawRepair();});
  repairCanvas.addEventListener("pointerleave",()=>{pointer=null;if(!brush)drawRepair();});
  repairCanvas.addEventListener("pointerup",()=>{if(!brush || !draftStroke)return;const value=options();const {paperPoints,...stroke}=draftStroke;value.strokes.push(stroke);value.enabled=true;redo.delete(currentPhoto());brush=null;changed();status(stroke.mode==="erase"?"已排除该区域，正在更新修复。":"修复区域已加入，正在更新预览。");});
  repairCanvas.addEventListener("pointercancel",()=>{brush=null;draftStroke=null;drawRepair();});
  repairCanvas.addEventListener("keydown",e=>{if(!(e.ctrlKey||e.metaKey))return;if(e.key.toLowerCase()==="z"){e.preventDefault();e.shiftKey?redoStroke():undoStroke();}else if(e.key.toLowerCase()==="y"){e.preventDefault();redoStroke();}});
  function setBusy(value){Object.keys(fields).forEach(id=>{$(id).disabled=value||!currentPhoto();});["brushMode","brushDiameter","inpaintRadius","resetRepair"].forEach(id=>{$(id).disabled=value||!currentPhoto();});renderOptions();}
  function onNewScan(){owner=null;data=null;ready=false;repairZoom=1;workspace("crop");renderOptions();}
  return {renderOptions,setPreview,pending,failed,setBusy,onNewScan,showWorkspace:workspace};
})();
