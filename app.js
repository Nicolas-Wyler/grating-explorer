/* PWA bridge. Results are committed to IndexedDB before a job reports success. */
let engineWorker, sequence=0, pending=new Map(), engineReady, pwaState=null, pwaBusy=false, pwaSerial=0, pendingReload=false;
let pwaStart=0, pwaEstimate=0, pwaLastFraction=0;
const elapsed=()=>Math.max(0,(Date.now()-pwaStart)/1000);
function engineCall(name,body=null,query={},payload=null){
 return new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});engineWorker.postMessage({id,name,body,query,payload});});
}
function createEngine(){
 engineWorker=new Worker('worker.js');
 engineWorker.onmessage=e=>{
  const d=e.data;
  if(d.type==='progress'){
   if(pwaBusy){pwaLastFraction=d.fraction;pwaState={...pwaState,progress:d.fraction,message:d.message};}
   else if(document.getElementById('alert'))document.getElementById('alert').textContent=d.message+'…';
   return;
  }
  const t=pending.get(d.id);if(!t)return;pending.delete(d.id);
  if(d.error)t.reject(Error(d.error));else t.resolve(d.bytes||d.result);
 };
 engineWorker.onerror=e=>{for(const p of pending.values())p.reject(Error(e.message||'Calculation worker stopped.'));pending.clear();};
 return engineCall('init').then(s=>{pwaState=s;return s;});
}
async function setupPWA(){
 if(!('serviceWorker' in navigator))throw Error('This browser does not support offline installation. Open the app in Chrome.');
 const oldController=!!navigator.serviceWorker.controller;
 const reg=await navigator.serviceWorker.register('./sw.js',{updateViaCache:'none'});
 await navigator.serviceWorker.ready;
 if(!navigator.serviceWorker.controller)await new Promise(resolve=>navigator.serviceWorker.addEventListener('controllerchange',resolve,{once:true}));
 let wasControlled=oldController;
 navigator.serviceWorker.addEventListener('controllerchange',()=>{
  if(!wasControlled){wasControlled=true;return;}
  if(pwaBusy){pendingReload=true;document.getElementById('alert').textContent='An update is ready; it will open after this calculation is saved.';}
  else reloadAfterUpdate();
 });
 const check=()=>{if(navigator.onLine)reg.update().catch(()=>{});};window.addEventListener('online',check);setInterval(check,3600000);check();
 if(navigator.storage&&navigator.storage.persist)navigator.storage.persist().catch(()=>{});
 if(navigator.locks){
  const acquired=new Promise((resolve,reject)=>navigator.locks.request('grating-explorer-library',{ifAvailable:true},lock=>{
   if(!lock){reject(Error('Another Grating Explorer window is using the saved-results library. Close it, then reload this window.'));return;}
   resolve();return new Promise(()=>{});
  }).catch(reject));await acquired;
 }
 await createEngine();
 document.getElementById('offlineStatus').textContent='Ready offline · updates automatically when online';
 document.getElementById('alert').textContent='Ready.';
}
engineReady=setupPWA().catch(error=>{document.getElementById('offlineStatus').textContent='Offline setup could not finish';throw error});
function reloadAfterUpdate(){Promise.resolve(typeof saveUI==='function'?saveUI():undefined).finally(()=>location.reload());}
function queryArgs(path){let u=new URL(path,location.href);return {name:u.pathname.split('/').pop(),query:Object.fromEntries(u.searchParams)};}
async function pwaApi(path,body){
 await engineReady;const {name,query}=queryArgs(path);
 if(name==='status'){
  if(pwaBusy){let seconds=elapsed(),remaining=pwaLastFraction>.035?Math.max(0,seconds/pwaLastFraction*(1-pwaLastFraction)):Math.max(0,pwaEstimate-seconds);return {...pwaState,elapsed_seconds:seconds,remaining_seconds:remaining};}
  return pwaState||await engineCall('status');
 }
 if(name==='start'){
  if(pwaBusy)throw Error('A calculation is already running.');
  let estimate=await engineCall('estimate',body);pwaBusy=true;pwaStart=Date.now();pwaEstimate=estimate.seconds;pwaLastFraction=0;pwaSerial++;
  pwaState={...(pwaState||{}),running:true,progress:0,message:'Preparing calculation',job:body.kind,error:null,serial:pwaSerial,elapsed_seconds:0,remaining_seconds:pwaEstimate};
  engineCall('calculate',body).then(result=>{
   pwaBusy=false;pwaState={...result,running:false,progress:1,serial:pwaSerial,elapsed_seconds:elapsed(),remaining_seconds:0,message:result.cache_hit?'Loaded saved result':'Complete and saved'};
   if(pendingReload)setTimeout(reloadAfterUpdate,1200);
  }).catch(error=>{if(error.message==='Cancelled')return;pwaBusy=false;pwaState={...(pwaState||{}),running:false,message:'Calculation failed',error:error.message,serial:pwaSerial};});
  return {serial:pwaSerial,kind:body.kind,cached:estimate.cached};
 }
 if(name==='cancel'){
  if(pwaBusy){engineWorker.terminate();for(const p of pending.values())p.reject(Error('Cancelled'));pending.clear();pwaBusy=false;pwaState={...pwaState,running:false,message:'Cancelled',error:null};engineReady=createEngine();}
  return {ok:true};
 }
 if(name==='quit'){return {ok:true};}
 if(name==='stats'){
  const s=await engineCall(name);const q=await navigator.storage.estimate();return {...s,free_bytes:Math.max(0,(q.quota||0)-(q.usage||0))};
 }
 const result=await engineCall(name,body===undefined?null:body,query);
 if(name==='restore')pwaState=await engineCall('status');
 return result;
}
async function pwaAsset(path){await engineReady;const {name,query}=queryArgs(path);const data=await engineCall(name,null,query);return URL.createObjectURL(new Blob([data],{type:name==='csv'?'text/csv':name==='backup'?'application/zip':'image/png'}));}

async function importLibrary(file){await engineReady;if(pwaBusy)throw Error('Wait for the calculation to finish.');return engineCall('import',{}, {},await file.arrayBuffer());}
