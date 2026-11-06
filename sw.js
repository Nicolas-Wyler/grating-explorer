const VERSION='1.3.3';
const APP_CACHE='grating-app-'+VERSION;
const RUNTIME_CACHE='grating-pyodide-0.29.4';
const APP_FILES=['./','index.html','app.js','worker.js','engine.py','result_store.py','pwa_backend.py','library_transfer.py','manifest.webmanifest','icon-192.png','icon-512.png'];
self.addEventListener('install',event=>event.waitUntil((async()=>{const c=await caches.open(APP_CACHE);await c.addAll(APP_FILES);await self.skipWaiting();})()));
self.addEventListener('activate',event=>event.waitUntil((async()=>{await self.clients.claim();for(const name of await caches.keys())if(name.startsWith('grating-app-')&&name!==APP_CACHE)await caches.delete(name);})()));
self.addEventListener('fetch',event=>{
 const request=event.request;if(request.method!=='GET')return;const url=new URL(request.url);
 const runtime=url.origin==='https://cdn.jsdelivr.net'&&url.pathname.startsWith('/pyodide/v0.29.4/full/');
 const app=url.origin===self.location.origin&&url.pathname.startsWith(new URL('./',self.registration.scope).pathname);
 if(!runtime&&!app)return;
 event.respondWith((async()=>{const cache=await caches.open(runtime?RUNTIME_CACHE:APP_CACHE);const saved=await cache.match(request,{ignoreSearch:!runtime});if(saved)return saved;const response=await fetch(request);if(response.ok)await cache.put(request,response.clone());return response;})());
});
