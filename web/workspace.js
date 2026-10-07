"use strict";
/* Batch metadata is authoritative on the server. The legacy crop editor only
   receives a clone of the page it is editing, never an aggregate photo array. */
window.Workspace = (() => {
  let state = {sources:[],pages:[],tasks:[]}, importKind = "scan", polling = false;
  let undoToken = null, batchFields = [], stoppedUploads = false, displayedSignature="",taskSignature="";
  let activeUploads=0,batchSwitching=false;
  let snapshotVersion=0,activationVersion=0,pendingSelections=0,selectionChain=Promise.resolve(),batchIds=[];
  const downloads = new Set(), downloaded = new Set();
  const native = () => window.pywebview?.api;
  const request = async (path, body, raw = false) => {
    if (body !== undefined) return (await api(path, body, raw)).json();
    const response = await fetch(path,{headers:{"X-Session-Token":token}});
    const value = await response.json(); if (!response.ok) throw new Error(value.error || "请求失败"); return value;
  };
  function photos() {
    return state.pages.flatMap(page => page.photos.map((photo,index) => ({photo,page,source:state.sources.find(source=>source.id===page.source_id),index})));
  }
  function ids(all=false) { return (window.Gallery?Gallery.visible():photos()).filter(item=>all || item.photo.enabled).map(item=>item.photo.id); }
  function updateActions() {
    const count=ids().length, exists=state.sources.length>0,blocked=busy||batchSwitching||pendingSelections>0;
    $("export").disabled=!count||blocked;$("exportLabel").textContent=count?`导出 ${count} 张照片`:"导出照片";
    $("saveProject").disabled=!exists||blocked;$("batchEdit").disabled=!count||blocked;
    $("exportAll").disabled=!ids(true).length||blocked;$("exportFolder").disabled=!count||blocked;
    $("selectAll").disabled=blocked;$("selectNone").disabled=blocked;
    $("manualAdd").disabled=!state.pages.some(page=>state.sources.find(source=>source.id===page.source_id)?.kind==="scan") || busy;
    $("retryScan").disabled=!scan || busy;$("selectionSummary").textContent=`当前结果已选 ${count} / ${ids(true).length} 张`;
    if(window.Gallery)Gallery.updateSelection();
  }
  function filterSources() {
    const old=$("sourceFilter").value;
    $("sourceFilter").replaceChildren(new Option("全部来源",""),...state.sources.map(source=>new Option(source.name,source.id)));
    $("sourceFilter").value=state.sources.some(s=>s.id===old)?old:"";
    $("removeSource").hidden=!$("sourceFilter").value;
    const issues=$("sourceIssues");issues.replaceChildren();
    state.sources.filter(source=>source.error || source.status==="failed" || (source.kind==="video" && source.status!=="paired")).forEach(source=>{
      const line=document.createElement("p");line.textContent=`${source.name}：${source.error || (source.kind==="video"?"视频已导入，等待可靠配对":"处理失败")}`;issues.append(line);
    });
  }
  function install(snapshot) {
    snapshotVersion++;
    state={sources:[],pages:[],tasks:[],...snapshot};
    const signature=JSON.stringify([state.id,state.sources,state.pages]);
    const changed=signature!==displayedSignature;displayedSignature=signature;
    if(changed)filterSources();
    if(!window.Gallery){$("welcome").hidden=state.sources.length>0;$("galleryArea").hidden=!state.sources.length;}
    updateActions();if(changed&&window.Gallery)Gallery.render();
  }
  async function refresh() { const version=++snapshotVersion,snapshot=await request("/api/workspace");if(version===snapshotVersion)install(snapshot); }
  async function activate(pageId,isCurrent=()=>true) {
    if (busy) throw new Error("当前照片正在保存，请稍候。");
    const workspaceId=state.id,version=++activationVersion;
    const owns=()=>version===activationVersion&&workspaceId===state.id&&!batchSwitching&&isCurrent();
    try{
      const result=await request("/api/workspace/page",{page_id:pageId});
      if(!owns())return null;
      const installed=await installScan(result.page && typeof result.page==="object" ? result.page : result,null,owns);
      if(!installed||!owns())return null;setBusy(false);return scan;
    }catch(error){if(!owns())return null;throw error;}
  }
  async function savePage() {
    if(!scan)return;
    const page=scan,workspaceId=state.id;
    const result=await request("/api/workspace/update",{page_id:page.id || page.job,photos:page.photos,revision:page.revision});
    if(result.revision!==undefined&&scan===page&&state.id===workspaceId)page.revision=result.revision;
  }
  async function select(photoIds,enabled) {
    if(!photoIds.length)return;if(batchSwitching)throw new Error("正在切换批次，请稍候再选择。");
    snapshotVersion++;pendingSelections++;updateActions();
    const workspaceId=state.id,selectedIds=[...photoIds];
    const operation=selectionChain.then(async()=>{if(workspaceId!==state.id)return;await request("/api/workspace/select",{photo_ids:selectedIds,enabled});await refresh();});
    selectionChain=operation.catch(()=>{});
    try{return await operation;}finally{pendingSelections--;updateActions();}
  }
  async function importFiles(files,kind=importKind) {
    if(batchSwitching){status("正在切换批次，请稍候再导入。");return;}
    if(window.Gallery)Gallery.showImports(kind);
    importKind=kind; stoppedUploads=false;
    const selectedFiles=Array.from(files);let failures=0;
    activeUploads++;
    try {
    for(let i=0;i<selectedFiles.length;i++){
      if(stoppedUploads)break;
      const file=selectedFiles[i];status(`正在加入 ${i+1}/${selectedFiles.length}：${file.name}`);
      try { await request("/api/workspace/import?"+new URLSearchParams({kind,name:file.name}),file,true); }
      catch(error){failures++;status(`${file.name}：${error.message}`);}
    }
    await refresh();await poll();if(!batchSwitching)status(failures?`${failures} 个文件无法加入，其他文件继续处理。`:"文件已加入当前批次，后台按顺序整理。");
    } finally {activeUploads--;}
  }
  async function choose(kind,folder=false) {
    if(batchSwitching){status("正在切换批次，请稍候再导入。");return;}
    if(!token){status("正在连接工作区，请稍候。");return;}
    if(window.Gallery)Gallery.showImports(kind);
    importKind=kind;
    try {
      if(native()?.import_workspace){activeUploads++;try{const result=await native().import_workspace(kind,folder);if(result.error)throw new Error(result.error);if(!result.cancelled){await refresh();await poll();}}finally{activeUploads--;}return;}
      if(folder){$("folderFiles").click();return;}
      $(kind==="scan"?"file":"photoFiles").click();
    }catch(error){status(error.message);}
  }
  function track(result,download=false){if(result.error)throw new Error(result.error);if(result.task_id&&download)downloads.add(result.task_id);poll();return result;}
  async function save(kind,all=false){
    try{
      if(batchSwitching){status("正在切换批次，请稍候再保存。");return;}
      if(pendingSelections){status("正在保存选择，请稍候再导出或保存。");return;}
      if($("editDialog").open){status("请先完成或取消当前照片调整，再保存整个批次。");return;}
      const data={photo_ids:ids(all)};
      if(kind!=="project"&&!data.photo_ids.length){status("当前结果没有可导出的照片。");return;}
      if(native()?.save_workspace){const result=await native().save_workspace(kind,data);if(result.cancelled){status("已取消保存。");return;}track(result);}
      else {const path=kind==="project"?"/api/workspace/project-save":"/api/workspace/export";track(await request(path,{...data,zip_output:kind!=="folder"}),kind!=="folder");}
      status("后台正在保存；照片仍可查看和调整。");
    }catch(error){status(error.message);}
  }
  function artifact(task){
    const value=task.artifact_id || task.results?.artifact_id || task.result?.artifact_id;
    if(value)return value;
    return Array.isArray(task.results)?task.results.find(item=>item?.artifact_id || item?.result?.artifact_id)?.result?.artifact_id || task.results.find(item=>item?.artifact_id)?.artifact_id:null;
  }
  function renderTasks(tasks){
    const signature=JSON.stringify(tasks);if(signature===taskSignature)return;taskSignature=signature;
    $("taskPanel").hidden=!tasks.length;const list=$("taskList");list.replaceChildren();
    tasks.slice(-8).reverse().forEach(task=>{
      const row=document.createElement("div");row.className="task-row";
      const label=document.createElement("div");const names={import:"导入",retry_import:"重试导入",export:"导出",project:"保存项目",redetect:"重新识别",save_project:"保存项目",save_export:"导出照片",save_folder:"导出照片文件夹"};
      const stateNames={queued:"等待",running:"处理中",paused:"已暂停",complete:"完成",completed:"完成",failed:"失败",partial:"部分完成",cancelled:"已取消",interrupted:"已中断",cancelling:"正在取消"};
      const partial=task.results?.some(entry=>entry.result?.partial);
      const title=document.createElement("strong");title.textContent=`${names[task.kind] || "后台处理"} · ${partial?"部分完成":stateNames[task.state] || "状态待恢复"}`;
      const detail=document.createElement("p");detail.textContent=`${task.message || ""}${task.total?` (${task.done || 0}/${task.total})`:""}`;
      label.append(title,detail);const progress=document.createElement("progress");progress.max=task.total || 100;progress.value=task.total?(task.done || 0):(task.progress || 0);label.append(progress);
      if(task.errors?.length){const errors=document.createElement("details"),summary=document.createElement("summary");summary.textContent=`${task.errors.length} 项需要检查`;errors.append(summary);task.errors.forEach(error=>{const p=document.createElement("p");p.textContent=typeof error==="string"?error:`${error.name || error.photo_id || ""} ${error.error || error.message || JSON.stringify(error)}`;errors.append(p);});label.append(errors);}
      const actions=document.createElement("div");
      const control=(action,text)=>{const button=document.createElement("button");button.textContent=text;button.onclick=()=>run(async()=>{if(action==="cancel")stoppedUploads=true;const next=await request("/api/tasks/control",{task_id:task.id,action});if(next.task_id)track(next,downloads.has(task.id));await poll();});actions.append(button);};
      if(["running","queued"].includes(task.state)){control("pause","暂停后续");control("cancel","取消");}
      if(task.state==="paused"){control("resume","继续");control("cancel","取消");}
      if(["failed","partial","interrupted"].includes(task.state))control("retry","重试失败项");
      const folderResult=Array.isArray(task.results)?task.results.find(item=>item?.result?.path && !item.result.artifact_id)?.result:task.results;
      if(folderResult?.path && !artifact(task)){const location=document.createElement("p");location.textContent="照片已保存到："+folderResult.path;label.append(location);}
      const art=artifact(task);if(art){const link=document.createElement("a");link.href="/api/artifacts/"+encodeURIComponent(art)+"?token="+encodeURIComponent(token);link.textContent="下载结果";link.setAttribute("download","");actions.append(link);if(downloads.has(task.id)&&!downloaded.has(task.id)){downloaded.add(task.id);link.click();}}
      row.append(label,actions);list.append(row);
    });
  }
  async function poll(){
    if(!token || polling)return;polling=true;
    try{const result=await request("/api/tasks");const tasks=Array.isArray(result)?result:result.tasks || [];renderTasks(tasks);if(!pendingSelections)await refresh();$("workspaceError").hidden=true;}
    catch(error){$("workspaceError").textContent=`工作区暂时无法载入：${error.message}`;$("workspaceError").hidden=false;status(error.message);}finally{polling=false;}
  }
  async function run(action){try{await action();}catch(error){status(error.message);}}
  async function redetectPage(pageId){
    if(!confirm("重新识别只影响这页，但会重置该页四角与修复编辑。继续？"))return;
    track(await request("/api/workspace/redetect",{page_id:pageId,sensitivity:Number($("sensitivity").value),min_area:Number($("minArea").value)}));await refresh();
  }
  function batchDialog(){
    if($("editDialog").open){status("请先完成或取消当前单张调整，再应用批量参数。");return;}
    if(pendingSelections||batchSwitching)return;
    batchIds=ids();const count=batchIds.length;if(!count)return;
    batchFields=[];$("batchCount").textContent=`将影响选中的 ${count} 张照片`;const parent=$("batchFields");parent.replaceChildren();
    const photo=currentPhoto() || photos().find(item=>item.photo.enabled)?.photo;
    const defaults={enabled:true,auto_border:true,auto_image:false,adjustments:false,sensitivity:40,max_diameter:14,radius:3,brightness:0,contrast:0,saturation:0,sharpen:0,denoise:0};
    const definitions=[["restoration","enabled","启用清理","bool"],["restoration","auto_border","自动白边清理","bool"],["restoration","auto_image","自动画面修复","bool"],["restoration","sensitivity","清理强度",1,100],["restoration","max_diameter","污点直径",3,60],["restoration","radius","修复范围",1,12],["restoration","adjustments","启用调色","bool"],["restoration","brightness","亮度",-40,40],["restoration","contrast","对比度",-40,40],["restoration","saturation","饱和度",-100,100],["restoration","sharpen","锐化",0,100],["restoration","denoise","降噪",0,10],["presentation","trim","收边像素",0,50],["presentation","occupancy","背景中的照片大小",.2,.95]];
    definitions.forEach(([group,key,title,min,max])=>{const row=document.createElement("label");row.className="batch-field";const include=document.createElement("input");include.type="checkbox";include.setAttribute("aria-label",`应用${title}`);const name=document.createElement("span");name.textContent=title;const input=document.createElement("input");input.type=min==="bool"?"checkbox":"number";input.setAttribute("aria-label",title+"值");if(min==="bool")input.checked=photo?.[group]?.[key] ?? defaults[key];else{input.min=min;input.max=max;input.step=key==="occupancy"?.01:1;input.value=photo?.[group]?.[key] ?? (key==="trim"?0:key==="occupancy"?.78:defaults[key]);}row.append(include,name,input);parent.append(row);batchFields.push({group,key,include,input});});
    $("batchDialog").showModal();
  }
  $("batchApply").onclick=()=>run(async()=>{const fields={};batchFields.filter(field=>field.include.checked).forEach(({group,key,input})=>{(fields[group] ||= {})[key]=input.type==="checkbox"?input.checked:Number(input.value);});if(!Object.keys(fields).length)throw new Error("先勾选要应用的参数。");const result=await request("/api/workspace/apply",{photo_ids:batchIds,fields});undoToken=result.undo_token;$("batchUndo").disabled=!undoToken;$("batchDialog").close();await refresh();status("已应用批量参数，手工区域各自保留。");});
  $("batchCancel").onclick=()=>$("batchDialog").close();$("batchEdit").onclick=batchDialog;
  $("batchUndo").onclick=()=>run(async()=>{await request("/api/workspace/undo",{undo_token:undoToken});undoToken=null;$("batchUndo").disabled=true;await refresh();status("已撤销该次批量参数。");});
  $("applyRepairAll").onclick=batchDialog;
  $("openButton").onclick=()=>choose("scan");$("emptyOpen").onclick=()=>choose(document.body.dataset.mode==="photo"?"photo":"scan");$("openPhotos").onclick=()=>choose("photo");
  $("libraryToCrop").onclick=()=>choose("scan");$("libraryToPhoto").onclick=()=>choose("photo");
  $("importFolder").onclick=()=>choose(document.body.dataset.mode==="crop"?"scan":"photo",true);
  $("scanFolder").onclick=()=>choose("scan",true);$("photoFolder").onclick=()=>choose("photo",true);
  $("file").onchange=()=>run(async()=>{await importFiles($("file").files,"scan");$("file").value="";});
  $("photoFiles").onchange=()=>run(async()=>{await importFiles($("photoFiles").files,"photo");$("photoFiles").value="";});
  $("folderFiles").onchange=()=>run(async()=>{const files=Array.from($("folderFiles").files).filter(file=>file.webkitRelativePath.split("/").length===2);await importFiles(files,importKind);$("folderFiles").value="";});
  document.body.addEventListener("dragover",event=>{if($("editDialog").open)return;event.preventDefault();document.body.classList.add("dragging");});
  document.body.addEventListener("dragleave",event=>{if(!event.relatedTarget)document.body.classList.remove("dragging");});
  document.body.addEventListener("drop",event=>{if($("editDialog").open)return;event.preventDefault();event.stopImmediatePropagation();document.body.classList.remove("dragging");const mode=document.body.dataset.mode;if(mode==="library"){status("请进入扫描裁剪或照片调整，再拖入对应素材。");return;}run(()=>importFiles(event.dataTransfer.files,mode==="crop"?"scan":"photo"));},true);
  $("export").onclick=()=>save("folder");$("exportAll").onclick=()=>save("folder",true);$("exportFolder").onclick=()=>save("export");$("saveProject").onclick=()=>save("project");
  $("openProject").onclick=()=>run(async()=>{if($("editDialog").open)throw new Error("请先完成或取消当前照片调整。");if(native()?.open_workspace_project){const result=await native().open_workspace_project();if(result.error)throw new Error(result.error);if(!result.cancelled)install(result.workspace || result);}else $("projectFile").click();});
  $("projectFile").onchange=()=>run(async()=>{const file=$("projectFile").files[0];if(!file)return;install(await request("/api/workspace/project-open",file,true));$("projectFile").value="";status("整批项目已恢复。");});
  $("newBatch").onclick=()=>run(async()=>{
    if(batchSwitching||!confirm("新建批次会停止当前批次未完成的任务，并保留已有素材和编辑记录为本机历史。继续？"))return;
    batchSwitching=true;stoppedUploads=true;$("newBatch").disabled=true;
    const surfaces=[document.querySelector(".home"),document.querySelector(".workspace-nav"),document.querySelector(".header-actions")].filter(Boolean);
    surfaces.forEach(surface=>surface.inert=true);
    try{
      await selectionChain;
      while(activeUploads){status("正在等待当前上传结束，后续文件已停止加入。");await new Promise(resolve=>setTimeout(resolve,500));}
      for(;;){
        const result=await request("/api/workspace/new",{cancel_tasks:true});
        if(!result.pending){scan=null;undoToken=null;$("batchUndo").disabled=true;install(result);status("已新建空批次；上一批素材和编辑记录已保留。");break;}
        const detail=(result.active_tasks||[]).map(task=>`${({import:"导入图片",retry_import:"重试导入",redetect:"重新识别",save_project:"保存项目",save_export:"导出照片",save_folder:"导出照片"})[task.kind]||"后台处理"}${task.total?`（${task.done||0}/${task.total}）`:""}`).join("、");
        status(`正在安全停止 ${detail||"当前任务"}，完成后自动新建批次；已完成成果保留。`);
        await new Promise(resolve=>setTimeout(resolve,750));
      }
    }finally{batchSwitching=false;$("newBatch").disabled=false;surfaces.forEach(surface=>surface.inert=false);updateActions();}
  });
  async function removeSources(sourceIds){
    const sources=state.sources.filter(source=>sourceIds.includes(source.id));if(!sources.length)return;
    if(!confirm(`从图片库移除 ${sources.length} 个来源及相关裁剪成图？外部原文件不受影响。`))return;
    try{for(const source of sources)await request("/api/workspace/remove",{source_id:source.id});}
    finally{await refresh();}
    status(`已移除 ${sources.length} 个来源。`);
  }
  $("removeSource").onclick=()=>run(()=>removeSources([$("sourceFilter").value]));
  $("retryScan").onclick=()=>run(()=>redetectPage(scan?.id || scan?.job));$("redetect").onclick=$("retryScan").onclick;
  redetect=()=>run(()=>redetectPage(scan?.id || scan?.job));
  $("gentleCleanup").onchange=()=>run(async()=>{if(!ids().length)return;const result=await request("/api/workspace/apply",{photo_ids:ids(),fields:{restoration:{enabled:$("gentleCleanup").checked,auto_border:$("gentleCleanup").checked}}});undoToken=result.undo_token;$("batchUndo").disabled=!undoToken;await refresh();});
  setInterval(poll,1500);
  return {request,refresh,activate,savePage,photos,ids,select,updateActions,choose,importFiles,save,run,track,redetectPage,removeSources,get state(){return state;}};
})();
