"use strict";
window.Gallery=(()=>{
  let kind="paper",snapshot=null,opening=false,windowStart=0,virtualInfo=null,scrollFrame=null;
  const dialog=$("editDialog");
  const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(!entry.isIntersecting)return;const img=entry.target;if(img.dataset.src){img.src=img.dataset.src;delete img.dataset.src;}observer.unobserve(img);}),{rootMargin:"350px"});
  function needsReview(photo){return photo.needs_review ?? (!photo.inner || !!photo.warnings?.length || !!photo.review_reasons?.length);}
  function visible(){const source=$("sourceFilter").value,query=$("photoSearch").value.toLocaleLowerCase().trim();return Workspace.photos().filter(item=>(!source || item.page.source_id===source)&&(!$("onlyReview").checked || needsReview(item.photo))&&(!query || `${item.source?.name} ${item.page.page} ${item.index+1}`.toLocaleLowerCase().includes(query)));}
  function renderReview(){
    const pending=Workspace.photos().filter(item=>needsReview(item.photo));$("reviewCount").textContent=pending.length;$("reviewSummary").textContent=`待检查 ${pending.length} 张 · 点击编号调整`;$("reviewList").hidden=!pending.length;
    const parent=$("reviewItems");parent.replaceChildren();pending.forEach(({photo,page,source,index})=>{const row=document.createElement("button");row.className="review-item";const name=document.createElement("strong");name.textContent=`${source?.name || page.name} / 第 ${page.page} 页 / 照片 ${String(index+1).padStart(2,"0")}`;const reasons=document.createElement("span");reasons.textContent=(photo.review_reasons?.length?photo.review_reasons:!photo.inner?["缺少内部画面四角"]:photo.warnings?.length?photo.warnings:["边缘或朝向需要确认"]).join("；");row.append(name,reasons);row.onclick=()=>Workspace.run(()=>openPhoto(photo.id));parent.append(row);});
  }
  function render(){
    observer.disconnect();const grid=$("galleryGrid");grid.replaceChildren();const items=visible();renderReview();
    $("galleryTitle").textContent=`${Workspace.photos().length} 张照片 · ${Workspace.state.sources.length} 个来源`;
    $("gallerySubtitle").textContent="新导入会追加；按来源切换时，四角和修复都保留";
    $("nothingFound").hidden=items.length>0;$("galleryMore").hidden=items.length<=100;
    $("nothingFound").querySelector("p").textContent=$("onlyReview").checked?"当前筛选没有待检查照片。":Workspace.photos().length?"当前来源或搜索没有匹配的照片。":"没有找到完整照片。可以手动框选，也可以换一张扫描图。";$("emptyManual").hidden=!!Workspace.photos().length;
    $("galleryMore").textContent="回到照片列表顶部";
    const sourceId=$("sourceFilter").value;
    const pages=Workspace.state.pages.filter(page=>!sourceId||page.source_id===sourceId);
    const old=$("homePage").value;$("homePage").replaceChildren(...pages.map(page=>new Option(`${page.name} · 第 ${page.page} 页`,page.id)));$("homePage").value=pages.some(p=>p.id===old)?old:pages[0]?.id||"";$("homePageLabel").hidden=!pages.length;
    let start=0,end=items.length;
    grid.classList.toggle("virtualized",items.length>100);
    const spacer=height=>{if(height<=0)return;const node=document.createElement("div");node.className="gallery-spacer";node.style.height=height+"px";node.setAttribute("aria-hidden","true");grid.append(node);};
    if(items.length>100){
      const style=getComputedStyle(grid),columns=style.gridTemplateColumns.split(/\s+/).filter(Boolean).length || 3,gap=parseFloat(style.rowGap)||18,stride=420+gap;
      const totalRows=Math.ceil(items.length/columns),rows=Math.ceil(window.innerHeight/stride)+4;
      windowStart=Math.max(0,Math.min(windowStart,totalRows-rows));
      start=windowStart*columns;end=Math.min(items.length,(windowStart+rows)*columns);
      virtualInfo={top:grid.getBoundingClientRect().top+window.scrollY,stride,columns,rows,totalRows};
      spacer(windowStart*stride-gap);
    }else{virtualInfo=null;windowStart=0;}
    items.slice(start,end).forEach(item=>{
      const {photo,page,source,index}=item,card=document.createElement("article");card.className="photo-card"+(!photo.enabled?" unselected":"");card.dataset.photoId=photo.id;
      const stage=document.createElement("button");stage.className="photo-stage";stage.setAttribute("aria-label",`调整 ${source?.name} 第 ${page.page} 页照片 ${index+1}`);
      const img=document.createElement("img");img.dataset.src="/api/workspace/preview?"+new URLSearchParams({photo_id:photo.id,kind,rev:page.revision || 0});img.alt=`${source?.name || page.name} · 照片 ${index+1}`;img.loading="lazy";img.decoding="async";img.onerror=()=>{stage.classList.add("preview-error");img.remove();const text=document.createElement("span");text.textContent="预览暂不可用，点击检查边缘";stage.append(text);};stage.append(img);observer.observe(img);stage.onclick=()=>Workspace.run(()=>openPhoto(photo.id));
      const check=document.createElement("input");check.type="checkbox";check.checked=photo.enabled;check.className="photo-select";check.setAttribute("aria-label",`选择照片 ${index+1}`);check.onchange=()=>{check.disabled=true;Workspace.run(()=>Workspace.select([photo.id],check.checked));};
      const footer=document.createElement("div");footer.className="photo-footer";const title=document.createElement("span");title.textContent=`照片 ${index+1}`;const adjust=document.createElement("button");adjust.className="adjust-button";adjust.textContent="调整";adjust.onclick=stage.onclick;footer.append(title,adjust);
      const sourceName=document.createElement("p");sourceName.className="photo-origin";sourceName.textContent=`${source?.name || page.name} · 第 ${page.page} 页`;sourceName.title=sourceName.textContent;
      card.append(stage,check,footer,sourceName);
      if(needsReview(photo)){const hint=document.createElement("button");hint.className="review-badge";const reasons=photo.review_reasons || [];hint.textContent=!photo.inner?"补一下画面边缘":reasons[0] || "边缘或朝向待检查";hint.title=reasons.join("；");hint.onclick=stage.onclick;card.append(hint);}
      grid.append(card);
    });
    if(virtualInfo)spacer((virtualInfo.totalRows-Math.ceil(end/virtualInfo.columns))*virtualInfo.stride-(parseFloat(getComputedStyle(grid).rowGap)||18));
    Workspace.updateActions();
  }
  function tab(mode){
    document.body.dataset.editorTab=mode;
    document.querySelector(".adjustments").open=mode==="color";
    $("showRepairMask").checked=mode==="repair";
    RestUI.showWorkspace(mode==="crop"?"crop":"repair");
    ["crop","repair","color"].forEach((key,index)=>$( ["stepCrop","stepRepair","stepExport"][index]).classList.toggle("active",mode===key));
    if(mode==="crop")fitCanvas();
  }

  async function openPhoto(photoId){
    if(opening||busy)return;opening=true;
    try{
      const item=Workspace.photos().find(item=>item.photo.id===photoId);if(!item)throw new Error("照片已移除，请重新选择。");
      await Workspace.activate(item.page.id);const index=scan.photos.findIndex(photo=>photo.id===photoId);if(index<0)throw new Error("照片已更新，请重新选择。");
      open(index);
    }finally{opening=false;}
  }
  function open(index){
    if(!scan||busy)return;
    selected=index;snapshot=JSON.parse(JSON.stringify(scan.photos));cropFocus=null;zoom=1;
    handle=null;drawing=null;draft=[];$("dialogTitle").textContent=scan.photos[index]?`${scan.name} · 调整照片 ${index+1}`:"补一张照片";
    $("trim").value=scan.photos[index]?.presentation?.trim||0;$("occupancy").value=Math.round((scan.photos[index]?.presentation?.occupancy||.78)*100);
    dialog.showModal();document.body.classList.add("editing");renderList();renderEditor();tab("crop");schedulePreview();
  }
  async function done(){
    if(busy)return;drawing=null;draft=[];setBusy(true);$("editorDone").textContent="正在保存";
    try{await Workspace.savePage();++previewVersion;clearTimeout(previewTimer);dialog.close();document.body.classList.remove("editing");await Workspace.refresh();status("照片已更新，可以继续调整其他来源。");}
    catch(error){status(error.message);}
    finally{$("editorDone").textContent="完成";setBusy(false);}
  }
  function cancel(){if(busy)return;if(snapshot)scan.photos=snapshot;dialog.close();document.body.classList.remove("editing");++previewVersion;drawing=null;draft=[];render();status("已取消本次调整。");}
  $("editorDone").onclick=done;$("cancelEdit").onclick=cancel;dialog.addEventListener("cancel",event=>{event.preventDefault();cancel();});
  $("stepCrop").onclick=()=>tab("crop");$("stepRepair").onclick=()=>tab("repair");$("stepExport").onclick=()=>tab("color");
  document.querySelectorAll("[data-view]").forEach(button=>{button.onclick=()=>{kind=button.dataset.view;document.querySelectorAll("[data-view]").forEach(item=>{item.classList.toggle("active",item===button);item.setAttribute("aria-pressed",String(item===button));});render();};});
  $("manualAdd").onclick=()=>Workspace.run(async()=>{const pageId=$("homePage").value || Workspace.state.pages[0]?.id;if(!pageId)return;await Workspace.activate(pageId);open(Math.max(0,selected));drawing="outer";draft=[];tab("crop");draw();canvas.focus();status("在扫描图上依次点选四角。");});
  $("emptyManual").onclick=()=>$("manualAdd").click();
  $("homePage").onchange=()=>Workspace.run(async()=>{await Workspace.activate($("homePage").value);open(0);});
  $("page").onchange=()=>Workspace.run(async()=>{if(dialog.open)throw new Error("请先完成当前页调整，再从来源菜单切换页面。");});
  $("trim").oninput=()=>{const photo=currentPhoto();if(photo){photo.presentation={...photo.presentation,trim:Number($("trim").value)};changed();}};
  $("occupancy").oninput=()=>{const photo=currentPhoto();if(photo){photo.presentation={...photo.presentation,occupancy:Number($("occupancy").value)/100};$("occupancyValue").textContent=$("occupancy").value+"%";changed();}};
  $("sourceFilter").onchange=()=>{windowStart=0;$("removeSource").hidden=!$("sourceFilter").value;render();};
    $("photoSearch").oninput=()=>{windowStart=0;render();};$("onlyReview").onchange=()=>{windowStart=0;render();};$("galleryMore").onclick=()=>{windowStart=0;window.scrollTo({top:$("galleryGrid").getBoundingClientRect().top+window.scrollY-120,behavior:"smooth"});render();};
  window.addEventListener("scroll",()=>{if(!virtualInfo||document.querySelector("dialog[open]")||scrollFrame)return;scrollFrame=requestAnimationFrame(()=>{scrollFrame=null;const info=virtualInfo;if(!info)return;const row=Math.max(0,Math.min(Math.floor((window.scrollY-info.top)/info.stride)-2,info.totalRows-info.rows));if(row!==windowStart){windowStart=row;render();}});},{passive:true});
  window.addEventListener("resize",()=>{if(virtualInfo)render();});
  $("selectAll").onclick=()=>Workspace.run(()=>Workspace.select(visible().map(item=>item.photo.id),true));$("selectNone").onclick=()=>Workspace.run(()=>Workspace.select(visible().map(item=>item.photo.id),false));
  document.addEventListener("pointerdown",event=>{if(!$("moreMenu").contains(event.target))$("moreMenu").open=false;});
  const previousSetBusy=setBusy;setBusy=function(value){previousSetBusy(value);$("editorDone").disabled=value;$("cancelEdit").disabled=value;Workspace.updateActions();};
  return {onScan:render,open,openPhoto,refresh:Workspace.refresh,render};
})();
