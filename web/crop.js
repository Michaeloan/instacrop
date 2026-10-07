"use strict";
window.CropUI = (() => {
  const fields = {angle:"fineAngle",perspective_x:"perspectiveX",perspective_y:"perspectiveY",trim_top:"trimTop",trim_right:"trimRight",trim_bottom:"trimBottom",trim_left:"trimLeft"};
  let loupePoint=null,tile=null,tileKey="",tileTimer=null,tileVersion=0,operation=0;
  const loupe=$("cornerLoupe"), lens=$("loupeCanvas"), lensContext=lens.getContext("2d");
  function presentation(photo=currentPhoto()){return photo?(photo.presentation ||= {}):{};}
  function render(){
    const photo=currentPhoto(),p=presentation(photo);
    Object.entries(fields).forEach(([key,id])=>{$(id).value=p[key] ?? (key.startsWith("trim_")?p.trim||0:0);});
    $("orientationConfirmed").checked=!!p.orientation_confirmed;
    const reasons=photo?.review_reasons || photo?.crop_analysis?.review_reasons || [];
    const orientation=photo?.crop_analysis?.orientation || photo?.analysis?.orientation;
    const suggested=orientation?.suggested_rotation;
    $("applyOrientation").hidden=!!p.orientation_confirmed||!Number.isInteger(suggested)||suggested===(photo?.rotation||0);$("applyOrientation").dataset.rotation=suggested;
    $("orientationStatus").textContent=p.orientation_confirmed?"朝向已确认":orientation?.reason || "相纸方向可自动判断；画面上下方向不确定时，请人工确认。";
    $("cropReviewReasons").textContent=reasons.join("；");$("cropReviewReasons").hidden=!reasons.length;
    $("sourceResolution").textContent=scan?`原扫描 ${scan.width} × ${scan.height} px · 导出从原图计算`:"";
    if(!$("editDialog").open||handle===null){loupe.hidden=true;loupePoint=null;}
  }
  function drawLoupe(){
    if(!loupePoint||!scanImage||!$("cornerMagnifier").checked||!$("editDialog").open||drawing){loupe.hidden=true;return;}
    loupe.hidden=false;const size=lens.width,span=96,point=loupePoint;
    lensContext.fillStyle="#e3e9ed";lensContext.fillRect(0,0,size,size);lensContext.imageSmoothingEnabled=false;
    lensContext.save();lensContext.translate(size/2,size/2);lensContext.rotate(cropRotation()*Math.PI/2);
    const currentKey=`${scan.job}:${Math.round(point[0])}:${Math.round(point[1])}`;
    if(tile&&tileKey===currentKey)lensContext.drawImage(tile,0,0,tile.width,tile.height,-size/2,-size/2,size,size);
    else{const scaleX=scanImage.width/scan.width,scaleY=scanImage.height/scan.height;lensContext.drawImage(scanImage,(point[0]-span/2)*scaleX,(point[1]-span/2)*scaleY,span*scaleX,span*scaleY,-size/2,-size/2,size,size);}
    lensContext.restore();lensContext.strokeStyle="#ffb448";lensContext.lineWidth=1;lensContext.beginPath();lensContext.moveTo(size/2,0);lensContext.lineTo(size/2,size);lensContext.moveTo(0,size/2);lensContext.lineTo(size,size/2);lensContext.stroke();
    $("loupeCoordinates").textContent=`${Math.round(point[0])}, ${Math.round(point[1])} px · ${tile&&tileKey===currentKey?"原图细节":"载入原图细节…"}`;
  }
  function showLoupe(point){
    loupePoint=point?.slice() || null;drawLoupe();clearTimeout(tileTimer);const version=++tileVersion;
    if(!point||!$("cornerMagnifier").checked)return;
    const job=scan.job,x=Math.round(point[0]),y=Math.round(point[1]);
    tileTimer=setTimeout(async()=>{
      try{const image=new Image();image.src="/api/crop-tile?"+new URLSearchParams({job,x,y,size:96,token});await image.decode();if(version!==tileVersion||scan?.job!==job)return;tile=image;tileKey=`${job}:${x}:${y}`;drawLoupe();}catch{if(version===tileVersion)$("loupeCoordinates").textContent=`${x}, ${y} px · 预览细节（原图加载失败）`;}
    },90);
  }
  async function snap(photo,target,index,point){
    if(busy||!$("edgeSnap").checked)return;
    const job=scan.job,id=++operation,pointsBefore=JSON.stringify(photo[target]);
    try{const result=await(await api("/api/crop-snap",{job,photo,target,index,point,radius:12})).json();
      if(busy||id!==operation||scan?.job!==job||currentPhoto()!==photo||!$("editDialog").open||JSON.stringify(photo[target])!==pointsBefore)return;
      if(result.snapped&&Array.isArray(result.point)){
        const prior=photo[target][index];photo[target][index]=result.point;if(!convex(photo[target])){photo[target][index]=prior;return;}
        changed();showLoupe(result.point);status("角点已吸附到附近边缘；方向键可继续逐像素微调。");
      }
    }catch(error){if(id===operation&&!busy)status("边缘吸附暂不可用："+error.message);}
  }
  function staleKey(){return JSON.stringify(currentPhoto());}
  async function analyze(refine=false){
    if(busy||!currentPhoto())return;const photo=currentPhoto(),before=staleKey(),job=scan.job,id=++operation;
    setBusy(true);status(refine?"正在沿四条边重新拟合…":"正在检查边缘与朝向…",true);
    try{const result=await(await api(refine?"/api/crop-refine":"/api/crop-analysis",{job,photo})).json();
      if(id!==operation||scan?.job!==job||currentPhoto()!==photo||staleKey()!==before||!$("editDialog").open)return;
      if(refine&&result.photo)Object.assign(photo,result.photo);
      const analysis=result.analysis || result;photo.crop_analysis=analysis;
      if(analysis.review_reasons)photo.review_reasons=analysis.review_reasons;
      if(analysis.needs_review!==undefined)photo.needs_review=analysis.needs_review;
      const orientation=analysis.orientation || analysis;
      const suggested=orientation.suggested_rotation;
      $("applyOrientation").hidden=!Number.isInteger(suggested);$("applyOrientation").dataset.rotation=suggested;
      changed();status(refine?"本张边缘已重新拟合，请检查四角；取消可恢复原来的编辑。":orientation.reason || "检查完成。请确认画面上下方向。 ");
    }catch(error){status(error.message);}finally{setBusy(false);}
  }
  const previousRender=renderEditor;renderEditor=function(){previousRender();render();};
  const previousDraw=draw;draw=function(){previousDraw();
    if($("geometryGrid").checked&&$("editDialog").open&&scanImage){ctx.save();ctx.strokeStyle="rgba(255,255,255,.55)";ctx.lineWidth=1;for(let i=1;i<4;i++){ctx.beginPath();ctx.moveTo(canvas.width*i/4,0);ctx.lineTo(canvas.width*i/4,canvas.height);ctx.moveTo(0,canvas.height*i/4);ctx.lineTo(canvas.width,canvas.height*i/4);ctx.stroke();}ctx.restore();}drawLoupe();};
  const previousChanged=changed;changed=function(){++operation;if(currentPhoto())presentation().review_confirmed=false;previousChanged();};
  canvas.addEventListener("pointerdown",event=>{if(drag)showLoupe(currentPhoto()[editMode][drag.index]);});
  canvas.addEventListener("pointermove",()=>{if(drag)showLoupe(currentPhoto()[editMode][drag.index]);});
  // Capture the active corner before app.js's pointerup handler clears drag.
  canvas.addEventListener("pointerup",()=>{if(!drag)return;const photo=currentPhoto(),target=editMode,index=drag.index,point=photo[target][index].slice();queueMicrotask(()=>snap(photo,target,index,point));},{capture:true});
  canvas.addEventListener("pointercancel",()=>showLoupe(null));
  canvas.addEventListener("keydown",event=>{if(event.key==="Escape")showLoupe(null);else if(event.key.startsWith("Arrow")&&handle!==null)showLoupe(currentPhoto()?.[editMode]?.[handle]);});
  $("cornerMagnifier").onchange=()=>showLoupe($("cornerMagnifier").checked&&handle!==null?currentPhoto()?.[editMode]?.[handle]:null);
  $("geometryGrid").onchange=draw;
  const previousOutput=showOutputPreview;showOutputPreview=function(){previousOutput();const image=lastPreview?.images?.image || lastPreview?.images?.paper;$("geometryPreviewFrame").hidden=!image;if(image)$("geometryPreview").src=image;};
  const previousClear=clearPreviews;clearPreviews=function(){previousClear();$("geometryPreviewFrame").hidden=true;$("geometryPreview").removeAttribute("src");};
  Object.entries(fields).forEach(([key,id])=>{$(id).oninput=()=>{const value=Number($(id).value),min=Number($(id).min),max=Number($(id).max);if(busy||!currentPhoto()||!Number.isFinite(value)||value<min||value>max)return;presentation()[key]=value;changed();};});
  function rotateBy(amount){const photo=currentPhoto();if(busy||!photo)return;photo.rotation=((photo.rotation||0)+amount+4)%4;presentation().orientation_confirmed=true;showLoupe(null);changed();}
  $("rotate").onclick=()=>rotateBy(1);$("rotateLeft").onclick=()=>rotateBy(-1);$("rotateHalf").onclick=()=>rotateBy(2);
  $("orientationConfirmed").onchange=()=>{if(busy||!currentPhoto())return;presentation().orientation_confirmed=$("orientationConfirmed").checked;changed();};
  $("checkOrientation").onclick=()=>analyze();$("refineEdges").onclick=()=>analyze(true);
  $("applyOrientation").onclick=()=>{const rotation=Number($("applyOrientation").dataset.rotation);if(busy||!Number.isInteger(rotation)||!currentPhoto())return;currentPhoto().rotation=rotation;presentation().orientation_confirmed=true;changed();};
  $("resetGeometry").onclick=()=>{if(busy||!currentPhoto())return;const p=presentation();p.angle=0;p.perspective_x=0;p.perspective_y=0;["top","right","bottom","left"].forEach(side=>delete p["trim_"+side]);p.trim=0;$("trim").value=0;changed();status("精细几何与收边已归零，四角保持当前编辑；取消可撤回。");};
  const previousTrim=$("trim").oninput;$("trim").oninput=()=>{if(busy||!currentPhoto())return;const p=presentation();["top","right","bottom","left"].forEach(side=>delete p["trim_"+side]);previousTrim();render();};
  const previousReviewed=$("markReviewed").onclick;$("markReviewed").onclick=()=>{if(busy||!currentPhoto())return;previousReviewed();if(currentPhoto()?.reviewed){presentation().review_confirmed=true;render();}};
  ["refineEdges","checkOrientation","rotateLeft","rotateHalf","applyOrientation","resetGeometry",...Object.values(fields),"orientationConfirmed"].forEach(id=>controls.push(id));
  return {render,showLoupe,analyze};
})();
