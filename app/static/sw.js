var B="__BUILD__",C="stock-"+B;
self.addEventListener("install",function(e){e.waitUntil(caches.open(C).then(function(c){return c.addAll(["/","/manifest.webmanifest","/icon-180.png"])}));self.skipWaiting()});
self.addEventListener("activate",function(e){e.waitUntil(caches.keys().then(function(ks){return Promise.all(ks.filter(function(k){return k!=C}).map(function(k){return caches.delete(k)}))}).then(function(){return self.clients.claim()}))});
// Server down or answering with an error: open the saved app rather than an error page.
// The page: network first, so an update shows on the very next open. Falls back to the saved copy when offline or slower than 4s.
function page(req){return caches.open(C).then(function(c){return new Promise(function(res){var done=false,t=setTimeout(function(){c.match("/").then(function(h){if(h&&!done){done=true;res(h)}})},4000);
 fetch(req).then(function(r){clearTimeout(t);if(r.ok){keep(c,r.clone());if(!done){done=true;res(r)}return}
  c.match("/").then(function(h){if(!done){done=true;res(h||r)}})}).catch(function(){clearTimeout(t);c.match("/").then(function(h){if(!done){done=true;res(h||Response.error())}})})})})}
// Only the app itself becomes the offline copy: a 200 login or interstitial page from something in front of the app must not.
function keep(c,r){var ct=r.headers.get("content-type")||"";if(ct.indexOf("text/html")<0)return;r.text().then(function(t){if(t.indexOf('id="bnav"')>-1)c.put("/",new Response(t,{headers:{"content-type":ct}}))}).catch(function(){})}
// Icons, manifest, photos: saved copy first, refreshed in the background. The API is never cached.
function swr(req){return caches.open(C).then(function(c){return c.match(req).then(function(hit){var net=fetch(req).then(function(r){if(r.ok)c.put(req,r.clone());return r}).catch(function(){return hit});return hit||net})})}
self.addEventListener("fetch",function(e){var u=new URL(e.request.url);
 if(e.request.method!="GET"||u.origin!=location.origin||u.pathname.indexOf("/api/")==0||u.pathname=="/sw.js")return;
 e.respondWith(e.request.mode=="navigate"||u.pathname=="/"?page(e.request):swr(e.request))});
