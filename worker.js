/* The calculation and SQLite-backed virtual filesystem run off the UI thread. */
const PYODIDE='https://cdn.jsdelivr.net/pyodide/v0.29.4/full/';
let python=null;
self.pwaProgress=(fraction,message)=>self.postMessage({type:'progress',fraction,message});
function sync(populate=false){return new Promise((resolve,reject)=>python.FS.syncfs(populate,e=>e?reject(e):resolve()));}
async function initialize(){
  self.pwaProgress(0,'Preparing the offline scientific runtime');
  importScripts(PYODIDE+'pyodide.js');
  python=await loadPyodide({indexURL:PYODIDE});
  await python.loadPackage(['numpy','scipy','matplotlib','pillow','sqlite3']);
  python.FS.mkdirTree('/persist');python.FS.mount(python.FS.filesystems.IDBFS,{},'/persist');await sync(true);
  for(const name of ['engine.py','result_store.py','pwa_backend.py','library_transfer.py']){
    const response=await fetch(name);if(!response.ok)throw Error('Could not load '+name);
    python.FS.writeFile(name,await response.text());
  }
  python.runPython('import sys; sys.path.insert(0, "/"); from pwa_backend import dispatch');
  await sync();
}
const ready=initialize();
async function handle({id,name,body,query,payload}){
 try{
  await ready;if(name==='import')python.FS.writeFile('/tmp/import.zip',new Uint8Array(payload));python.globals.set('rpc_message',JSON.stringify({name,body,query}));
  const result=python.runPython('dispatch(rpc_message)');
  if(['calculate','restore','ui-state','init','import'].includes(name))await sync();
  if(typeof result==='string')self.postMessage({id,result:JSON.parse(result)});
  else {let bytes=result.toJs();result.destroy();self.postMessage({id,bytes},[bytes.buffer]);}
 }catch(error){self.postMessage({id,error:String(error.message||error)});}
}
let queue=Promise.resolve();
self.onmessage=e=>{queue=queue.then(()=>handle(e.data));};
