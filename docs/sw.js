/* Notifications only. Pages always load from the network so daily data stays fresh. */
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('push',event=>{
  let data={};try{data=event.data?.json()||{};}catch{}
  const title=typeof data.title==='string'?data.title:'優待ウォッチリスト';
  let url=new URL('/yutai-app/?notification=1#watchlist',self.location.origin).href;
  try{const candidate=new URL(data.url);if(candidate.origin===self.location.origin&&candidate.pathname==='/yutai-app/')url=candidate.href;}catch{}
  event.waitUntil(self.registration.showNotification(title,{
    body:typeof data.body==='string'?data.body:'候補銘柄の更新があります。アプリで確認してください。',
    icon:'/yutai-app/icons/icon-192.png',badge:'/yutai-app/icons/icon-192.png',
    tag:typeof data.tag==='string'?data.tag:'watchlist-update',data:{url},
  }));
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  event.waitUntil((async()=>{
    const url=event.notification.data?.url||new URL('/yutai-app/',self.location.origin).href;
    const clients=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    const existing=clients.find(c=>new URL(c.url).pathname.startsWith('/yutai-app/'));
    if(existing){await existing.navigate(url);return existing.focus();}
    return self.clients.openWindow(url);
  })());
});
