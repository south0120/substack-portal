const CACHE_NAME = "fyl-v1";
const SHELL = [
  "./",
  "./index.html",
  "./guide.html",
  "./manifest.webmanifest",
  "./assets/icons/apple-touch-icon.png",
  "./assets/icons/icon_64.png",
  "./assets/icons/icon_192.png",
  "./assets/icons/icon_512.png"
];

self.addEventListener("install", event => {
  event.waitUntil((async () => {
    try{
      const cache = await caches.open(CACHE_NAME);
      await cache.addAll(SHELL);
      await self.skipWaiting();
    }catch(error){
      console.warn("Precache failed",error);
    }
  })());
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    try{
      const names = await caches.keys();
      await Promise.all(names.filter(name => name !== CACHE_NAME).map(name => caches.delete(name)));
      await self.clients.claim();
    }catch(error){
      console.warn("Cache cleanup failed",error);
    }
  })());
});

self.addEventListener("fetch", event => {
  if(event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if(url.origin !== self.location.origin) return;
  event.respondWith((async () => {
    try{
      const cache = await caches.open(CACHE_NAME);
      const response = await fetch(event.request);
      if(response && response.ok){
        try{
          await cache.put(event.request,response.clone());
        }catch(error){
          console.warn("Cache write failed",error);
        }
      }
      return response;
    }catch(error){
      try{
        const cache = await caches.open(CACHE_NAME);
        const cached = await cache.match(event.request);
        if(cached) return cached;
        if(event.request.mode === "navigate") return cache.match("./index.html");
      }catch(cacheError){
        console.warn("Cache fallback failed",cacheError);
      }
      throw error;
    }
  })());
});
