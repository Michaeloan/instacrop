"use strict";
window.Gallery=(()=>{
  let kind="paper",snapshot=null,opening=false,windowStart=0,virtualInfo=null,scrollFrame=null;
  const dialog=$("editDialog");
  const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(!entry.isIntersecting)return;const img=entry.target;if(img.dataset.src){img.src=img.dataset.src;delete img.dataset.src;}observer.unobserve(img);}),{rootMargin:"350px"});
  const selections=new Set(),filters={library:{category:"all",source:"",query:"",review:false},crop:{category:"all",source:"",query:"",review:false},photo:{category:"all",source:"",query:"",review:false}};
  let mode="library",category="all",workspaceId=null,navigationVersion=0;
  const modeCopy={
    library:{title:"图片库",description:"按扫描原图和独立照片分类管理，再进入对应的裁剪工作区。",empty:"先把素材分类放好",help:"扫描图在「扫描裁剪」导入，独立照片在「照片调整」导入。"},
    crop:{title:"扫描裁剪",description:"导入整页扫描图，检查边缘、修复和调色，再导出成图。",empty:"把整页扫描图放进来",help:"支持多选扫描图和多页 TIFF；自动找边、分开裁剪与拉正。"},
    photo:{title:"照片调整",description:"导入已经分开的照片，单独裁剪、修复和调色。",empty:"导入独立照片",help:"一份照片直接作为一张素材加入；原始文件保留。"}
  };
  const categoryDefinitions={library:[["all","全部素材"],["scan","扫描原图"],["photo","独立照片"]],crop:[["all","全部成图"],["pending","待检查"],["confirmed","已确认"]],photo:[["all","全部照片"],["pending","待检查"],["confirmed","已确认"]]};
  function needsReview(photo){return photo.needs_review ?? (!photo.inner || !!photo.warnings?.length || !!photo.review_reasons?.length);}
  function sourcesForMode(){return Workspace.state.sources.filter(source=>mode==="library"||source.kind===(mode==="crop"?"scan":"photo"));}
  function inCategory(item,key=category){
    if(mode==="library")return key==="all"||item.source?.kind===key;
    return item.source?.kind===(mode==="crop"?"scan":"photo")&&(key==="all"||(key==="pending"?needsReview(item.photo):!needsReview(item.photo)));
  }
  function modePhotos(){return Workspace.photos().filter(item=>mode!=="library");}
  function visible(){const source=$("sourceFilter").value,query=$("photoSearch").value.toLocaleLowerCase().trim();return modePhotos().filter(item=>inCategory(item)&&(!source||item.page.source_id===source)&&(!$("onlyReview").checked||needsReview(item.photo))&&(!query||`${item.source?.name} ${item.page.page} ${item.index+1}`.toLocaleLowerCase().includes(query)));}
  function visibleSources(){const source=$("sourceFilter").value,query=$("photoSearch").value.toLocaleLowerCase().trim();return sourcesForMode().filter(item=>(mode!=="library"||category==="all"||item.kind===category)&&(!source||item.id===source)&&(!query||item.name.toLocaleLowerCase().includes(query)));}
  function remember(){filters[mode]={category,source:$("sourceFilter").value,query:$("photoSearch").value,review:$("onlyReview").checked};}
  function setMode(next){
    if(!modeCopy[next])return;
    if(next===mode){render();return;}
    if(dialog.open)return;
    remember();
    ++navigationVersion;mode=next;document.body.dataset.mode=mode;category=filters[mode].category;
    $("photoSearch").value=filters[mode].query;$("onlyReview").checked=filters[mode].review;
    windowStart=0;render(filters[mode].source);
    window.scrollTo({top:0,behavior:"instant"});
  }
  function showImports(kind){
    setMode(kind==="scan"?"crop":"photo");category="all";
    $("sourceFilter").value="";$("photoSearch").value="";$("onlyReview").checked=false;windowStart=0;render();
  }
  function categoryTabs(){
    $("categoryTabs").replaceChildren();
    for(const [key,label] of categoryDefinitions[mode]){
      const count=mode==="library"?Workspace.state.sources.filter(source=>key==="all"||source.kind===key).length:modePhotos().filter(item=>inCategory(item,key)).length;
      const button=document.createElement("button");button.textContent=`${label} ${count}`;button.classList.toggle("active",key===category);button.setAttribute("aria-pressed",String(key===category));button.dataset.category=key;
      button.onclick=()=>{category=key;windowStart=0;$("sourceFilter").value="";render();};$("categoryTabs").append(button);
    }

  }
  function renderNavigation(restoredSource=null){
    const copy=modeCopy[mode];$("modeTitle").textContent=copy.title;$("modeDescription").textContent=copy.description;
    $("libraryCount").textContent=Workspace.state.sources.length;$("cropCount").textContent=Workspace.photos().filter(item=>item.source?.kind==="scan").length;$("photoCount").textContent=Workspace.photos().filter(item=>item.source?.kind==="photo").length;
    document.querySelectorAll(".mode-nav [data-mode]").forEach(button=>{button.classList.toggle("active",button.dataset.mode===mode);if(button.dataset.mode===mode)button.setAttribute("aria-current","page");else button.removeAttribute("aria-current");});
    for(const id of ["libraryToCrop","libraryToPhoto"])$(id).hidden=mode!=="library";
    $("openButton").hidden=mode!=="crop";$("openPhotos").hidden=mode!=="photo";$("export").hidden=mode==="library";
    $("scanFolder").hidden=mode!=="crop";$("photoFolder").hidden=mode!=="photo";
    $("exportFolder").hidden=mode==="library";$("exportAll").hidden=mode==="library";
    for(const id of ["manualAdd","retryScan","gentleCleanup"])$(id).closest("label")?$(id).closest("label").hidden=mode!=="crop":$(id).hidden=mode!=="crop"||mode==="photo";
    $("importFolder").hidden=mode==="library";
    $("outputViews").hidden=mode==="library";
    $("batchEdit").hidden=mode==="library";$("batchUndo").hidden=mode==="library";$("onlyReview").closest("label").hidden=mode==="library";
    $("homePageLabel").hidden=mode!=="crop"||!Workspace.state.pages.some(page=>Workspace.state.sources.find(source=>source.id===page.source_id)?.kind==="scan");
    $("sourceNav").replaceChildren();
    const sources=sourcesForMode();const old=restoredSource ?? $("sourceFilter").value;
    $("sourceFilter").replaceChildren(new Option("全部来源",""),...sources.map(source=>new Option(source.name,source.id)));
    $("sourceFilter").value=sources.some(source=>source.id===old)?old:"";$("removeSource").hidden=mode!=="library"||!$("sourceFilter").value;
    const names={scan:"扫描图",photo:"照片"};
    if(!sources.length){const hint=document.createElement("p");hint.className="quiet";hint.textContent="导入后在这里按来源切换";$("sourceNav").append(hint);}
    sources.forEach(source=>{const button=document.createElement("button");button.className="source-nav-item";button.classList.toggle("active",source.id===$("sourceFilter").value);button.setAttribute("aria-pressed",String(source.id===$("sourceFilter").value));const name=document.createElement("span");name.textContent=source.name;name.title=source.name;const detail=document.createElement("small");detail.textContent=names[source.kind]||"素材";button.append(name,detail);button.onclick=()=>{$("sourceFilter").value=source.id;windowStart=0;render();};$("sourceNav").append(button);});
    categoryTabs();
  }
  function updateSelection(){
    const ids=new Set(Workspace.state.sources.map(source=>source.id));for(const id of selections)if(!ids.has(id))selections.delete(id);
    const shown=visibleSources(),selectedSources=shown.filter(source=>selections.has(source.id));
    $("removeSelectedSources").hidden=mode!=="library";$("removeSelectedSources").disabled=!selectedSources.length;
    $("removeSelectedSources").textContent=selectedSources.length?`移除选中 ${selectedSources.length} 个素材`:"移除选中素材";
    if(mode==="library")$("selectionSummary").textContent=`当前 ${shown.length} 个来源 · 已选 ${selectedSources.length} 个`;
  }
  function renderSources(sources){
    const grid=$("sourceGrid");grid.replaceChildren();grid.hidden=!sources.length;
    const names={scan:"扫描原图",photo:"照片素材"},states={ready:"已导入",empty:"未识别出照片",importing:"正在导入",failed:"导入失败",partial:"部分导入",interrupted:"导入中断"};
    sources.forEach(source=>{
      const card=document.createElement("article");card.className="source-card";card.dataset.sourceId=source.id;
      const stage=document.createElement("button");stage.className="source-stage";stage.setAttribute("aria-label",`打开 ${source.name}`);
      const page=Workspace.state.pages.find(page=>page.source_id===source.id);
      if(page&&(source.kind==="scan"||page.photos.length)){
        const img=document.createElement("img");img.alt=source.name;img.loading="lazy";img.dataset.src=source.kind==="scan"?"/api/scan-preview?job="+encodeURIComponent(page.id):"/api/workspace/preview?"+new URLSearchParams({photo_id:page.photos[0].id,kind:"image",rev:page.revision||0});stage.append(img);observer.observe(img);
      }else{const symbol=document.createElement("span");symbol.className="source-placeholder";symbol.textContent="▧";stage.append(symbol);}
      stage.onclick=()=>{setMode(source.kind==="scan"?"crop":"photo");$("sourceFilter").value=source.id;render();};
      const label=document.createElement("div");label.className="source-card-label";const title=document.createElement("strong");title.textContent=source.name;title.title=source.name;const detail=document.createElement("p");const count=Workspace.photos().filter(item=>item.page.source_id===source.id).length;detail.textContent=`${names[source.kind]||"素材"} · ${states[source.status]||source.status}${source.kind!=="video"?` · ${source.pages.length} 页 / ${count} 张成图`:""}`;label.append(title,detail);
      if(mode==="library"){const check=document.createElement("input");check.type="checkbox";check.className="photo-select";check.checked=selections.has(source.id);check.setAttribute("aria-label",`选择素材 ${source.name}`);check.onchange=()=>{if(check.checked)selections.add(source.id);else selections.delete(source.id);updateSelection();};card.append(check);}
      const actions=document.createElement("div");actions.className="source-card-actions";const open=document.createElement("button");open.textContent=source.kind==="scan"?"进入扫描裁剪":"进入照片调整";open.onclick=stage.onclick;actions.append(open);
      if(mode==="library"&&source.kind==="photo"&&page?.photos.length){const adjust=document.createElement("button");adjust.className="source-adjust";adjust.textContent="调整照片";adjust.onclick=()=>Workspace.run(()=>openPhoto(page.photos[0].id));actions.append(adjust);}
      if(mode==="library"){const remove=document.createElement("button");remove.textContent="移除";remove.className="text-danger";remove.onclick=()=>Workspace.run(()=>Workspace.removeSources([source.id]));actions.append(remove);}
      card.append(stage,label,actions);grid.append(card);
    });
  }
  function renderReview(){
    const pending=visible().filter(item=>needsReview(item.photo));$("reviewCount").textContent=pending.length;$("reviewSummary").textContent=`待检查 ${pending.length} 张 · 点击编号调整`;$("reviewList").hidden=mode==="library"||!pending.length;
    const parent=$("reviewItems");parent.replaceChildren();if(mode==="library")return;
    visible().filter(item=>needsReview(item.photo)).forEach(({photo,page,source,index})=>{const row=document.createElement("button");row.className="review-item";const name=document.createElement("strong");name.textContent=`${source?.name||page.name} / 第 ${page.page} 页 / 照片 ${String(index+1).padStart(2,"0")}`;const reasons=document.createElement("span");reasons.textContent=(photo.review_reasons?.length?photo.review_reasons:!photo.inner?["缺少内部画面四角"]:photo.warnings?.length?photo.warnings:["边缘或朝向需要确认"]).join("；");row.append(name,reasons);row.onclick=()=>Workspace.run(()=>openPhoto(photo.id));parent.append(row);});
  }
  function render(restoredSource=null){
    if(Workspace.state.id&&workspaceId!==Workspace.state.id){
      ++navigationVersion;workspaceId=Workspace.state.id;selections.clear();category="all";
      for(const key of Object.keys(filters))filters[key]={category:"all",source:"",query:"",review:false};
      restoredSource="";$("sourceFilter").value="";$("photoSearch").value="";$("onlyReview").checked=false;windowStart=0;
    }
    observer.disconnect();renderNavigation(restoredSource);const grid=$("galleryGrid");grid.replaceChildren();const items=visible(),sources=visibleSources();renderReview();
    const available=mode==="library"?Workspace.state.sources.length:sourcesForMode().length;
    $("welcome").hidden=available>0;$("galleryArea").hidden=!available;
    const copy=modeCopy[mode];$("welcomeTitle").textContent=copy.empty;$("welcomeDescription").textContent=copy.help;
    $("emptyOpen").hidden=mode==="library";$("emptyOpen").textContent=mode==="photo"?"导入照片":"导入扫描图";
    $("dropNote").textContent=mode==="library"?"使用上方入口或左侧导航，进入对应工作区":mode==="crop"?"拖入扫描图会自动识别；支持批量导入":"拖入独立照片，直接加入当前批次";
    $("galleryTitle").textContent=mode==="library"?`${sources.length} 个素材来源`:`${items.length} 张${mode==="crop"?"裁剪成图":"独立照片"}`;
    $("gallerySubtitle").textContent=mode==="library"?"这里管理原始素材；打开来源可进入对应工作区":mode==="crop"?"选择、批量参数与导出只针对当前结果":"打开一张调整裁剪、污点或颜色；各张四角分别保留";
    $("sourceIssues").replaceChildren();
    sources.filter(source=>source.error||["failed","interrupted"].includes(source.status)).forEach(source=>{const line=document.createElement("p");line.textContent=`${source.name}：${source.error||"导入未完成，请查看处理进度"}`;$("sourceIssues").append(line);});
    renderSources(mode==="library"?sources:[]);grid.hidden=mode==="library";
    $("nothingFound").hidden=mode==="library"?sources.length>0:items.length>0;$("galleryMore").hidden=mode==="library"||items.length<=100;
    $("nothingFound").querySelector("p").textContent=mode==="library"?"没有匹配的素材，试试其他分类、来源或文件名。":mode==="photo"?"没有匹配的照片，试试其他分类、来源或文件名。":modePhotos().length?"当前筛选没有匹配的照片。":"这份扫描图没有识别出完整照片，可以手动框选。";
    const currentSourcePhotos=modePhotos().filter(item=>inCategory(item)&&(!$("sourceFilter").value||item.page.source_id===$("sourceFilter").value));
    $("emptyManual").hidden=mode!=="crop"||currentSourcePhotos.length>0||category!=="all"||!!$("photoSearch").value;
    $("galleryMore").textContent="回到照片列表顶部";
    const sourceId=$("sourceFilter").value,pages=Workspace.state.pages.filter(page=>(!sourceId||page.source_id===sourceId)&&Workspace.state.sources.find(source=>source.id===page.source_id)?.kind==="scan");
    const old=$("homePage").value;$("homePage").replaceChildren(...pages.map(page=>new Option(`${page.name} · 第 ${page.page} 页`,page.id)));$("homePage").value=pages.some(p=>p.id===old)?old:pages[0]?.id||"";$("homePageLabel").hidden=mode!=="crop"||!pages.length;
    let start=0,end=items.length;
    grid.classList.toggle("virtualized",items.length>100);
    const spacer=height=>{if(height<=0)return;const node=document.createElement("div");node.className="gallery-spacer";node.style.height=height+"px";node.setAttribute("aria-hidden","true");grid.append(node);};
    if(items.length>100){
      const style=getComputedStyle(grid),columns=style.gridTemplateColumns.split(/\s+/).filter(Boolean).length||3,gap=parseFloat(style.rowGap)||18,stride=420+gap;
      const totalRows=Math.ceil(items.length/columns),rows=Math.ceil(window.innerHeight/stride)+4;
      windowStart=Math.max(0,Math.min(windowStart,totalRows-rows));start=windowStart*columns;end=Math.min(items.length,(windowStart+rows)*columns);
      virtualInfo={top:grid.getBoundingClientRect().top+window.scrollY,stride,columns,rows,totalRows};spacer(windowStart*stride-gap);
    }else{virtualInfo=null;windowStart=0;}
    items.slice(start,end).forEach(item=>{
      const {photo,page,source,index}=item,card=document.createElement("article");card.className="photo-card"+(!photo.enabled?" unselected":"");card.dataset.photoId=photo.id;
      const stage=document.createElement("button");stage.className="photo-stage";stage.setAttribute("aria-label",`调整 ${source?.name} 照片 ${index+1}`);
      const img=document.createElement("img");img.dataset.src="/api/workspace/preview?"+new URLSearchParams({photo_id:photo.id,kind,rev:page.revision||0});img.alt=`${source?.name||page.name} · 照片 ${index+1}`;img.loading="lazy";img.decoding="async";img.onerror=()=>{stage.classList.add("preview-error");img.remove();const text=document.createElement("span");text.textContent="预览暂不可用，请打开素材检查";stage.append(text);};stage.append(img);observer.observe(img);stage.onclick=()=>Workspace.run(()=>openPhoto(photo.id));
      const check=document.createElement("input");check.type="checkbox";check.checked=photo.enabled;check.className="photo-select";check.setAttribute("aria-label",`选择照片 ${index+1}`);check.onchange=()=>{const enabled=check.checked;check.disabled=true;Workspace.run(async()=>{try{await Workspace.select([photo.id],enabled);}finally{check.disabled=false;check.checked=Workspace.photos().find(item=>item.photo.id===photo.id)?.photo.enabled ?? photo.enabled;}});};
      const footer=document.createElement("div");footer.className="photo-footer";const title=document.createElement("span");title.textContent=`照片 ${index+1}`;const adjust=document.createElement("button");adjust.className="adjust-button";adjust.textContent="调整裁剪";adjust.onclick=stage.onclick;footer.append(title,adjust);
      const sourceName=document.createElement("p");sourceName.className="photo-origin";sourceName.textContent=`${source?.name||page.name} · ${source?.kind==="scan"?`第 ${page.page} 页` : "导入照片"}`;sourceName.title=sourceName.textContent;
      card.append(stage,check,footer,sourceName);
      if(needsReview(photo)){const hint=document.createElement("button");hint.className="review-badge";const reasons=photo.review_reasons||[];hint.textContent=!photo.inner?"补一下画面边缘":reasons[0]||"边缘或朝向待检查";hint.title=reasons.join("；");hint.onclick=stage.onclick;card.append(hint);}
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
    if(opening||busy||dialog.open)return;opening=true;
    try{
      const item=Workspace.photos().find(item=>item.photo.id===photoId);if(!item)throw new Error("照片已移除，请重新选择。");
      setMode(item.source?.kind==="scan"?"crop":"photo");
      const navigation=navigationVersion;
      const loaded=await Workspace.activate(item.page.id,()=>navigation===navigationVersion);if(!loaded||navigation!==navigationVersion)return;
      const index=scan.photos.findIndex(photo=>photo.id===photoId);if(index<0)throw new Error("照片已更新，请重新选择。");
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
  async function openPage(pageId,manual=false){
    if(!pageId||opening||busy||dialog.open)return;opening=true;const navigation=navigationVersion;
    try{
      const loaded=await Workspace.activate(pageId,()=>navigation===navigationVersion);if(!loaded||navigation!==navigationVersion)return;open(0);
      if(manual){drawing="outer";draft=[];tab("crop");draw();canvas.focus();status("在扫描图上依次点选四角。");}
    }finally{opening=false;}
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
  $("manualAdd").onclick=()=>Workspace.run(()=>openPage($("homePage").value,true));
  $("emptyManual").onclick=()=>$("manualAdd").click();
  $("homePage").onchange=()=>Workspace.run(()=>openPage($("homePage").value));
  $("page").onchange=()=>Workspace.run(async()=>{if(dialog.open)throw new Error("请先完成当前页调整，再从来源菜单切换页面。");});
  $("trim").oninput=()=>{const photo=currentPhoto();if(photo){photo.presentation={...photo.presentation,trim:Number($("trim").value)};changed();}};
  $("occupancy").oninput=()=>{const photo=currentPhoto();if(photo){photo.presentation={...photo.presentation,occupancy:Number($("occupancy").value)/100};$("occupancyValue").textContent=$("occupancy").value+"%";changed();}};
  $("sourceFilter").onchange=()=>{windowStart=0;render();};
    $("photoSearch").oninput=()=>{windowStart=0;render();};$("onlyReview").onchange=()=>{windowStart=0;render();};$("galleryMore").onclick=()=>{windowStart=0;window.scrollTo({top:$("galleryGrid").getBoundingClientRect().top+window.scrollY-120,behavior:"smooth"});render();};
  window.addEventListener("scroll",()=>{if(!virtualInfo||document.querySelector("dialog[open]")||scrollFrame)return;scrollFrame=requestAnimationFrame(()=>{scrollFrame=null;const info=virtualInfo;if(!info)return;const row=Math.max(0,Math.min(Math.floor((window.scrollY-info.top)/info.stride)-2,info.totalRows-info.rows));if(row!==windowStart){windowStart=row;render();}});},{passive:true});
  window.addEventListener("resize",()=>{if(virtualInfo)render();});
  $("selectAll").onclick=()=>{if(mode==="library"){visibleSources().forEach(source=>selections.add(source.id));render();}else Workspace.run(()=>Workspace.select(visible().map(item=>item.photo.id),true));};
  $("selectNone").onclick=()=>{if(mode==="library"){visibleSources().forEach(source=>selections.delete(source.id));render();}else Workspace.run(()=>Workspace.select(visible().map(item=>item.photo.id),false));};
  $("removeSelectedSources").onclick=()=>Workspace.run(()=>Workspace.removeSources(visibleSources().filter(source=>selections.has(source.id)).map(source=>source.id)));
  $("clearSource").onclick=()=>{$("sourceFilter").value="";windowStart=0;render();};
  document.querySelectorAll(".mode-nav [data-mode]").forEach(button=>button.onclick=()=>setMode(button.dataset.mode));
  document.addEventListener("pointerdown",event=>{if(!$("moreMenu").contains(event.target))$("moreMenu").open=false;});
  const previousSetBusy=setBusy;setBusy=function(value){previousSetBusy(value);$("editorDone").disabled=value;$("cancelEdit").disabled=value;Workspace.updateActions();};
  render();
  return {onScan:render,open,openPhoto,refresh:Workspace.refresh,render,setMode,showImports,visible,updateSelection,get mode(){return mode;}};
})();
