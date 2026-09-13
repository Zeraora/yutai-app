const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');const {webcrypto}=require('node:crypto');
function fixture({standalone=true,permission='default',denied=false}={}){
 const elements={};const el=id=>elements[id]??={hidden:false,disabled:false,open:false,value:'',textContent:'',listeners:{},addEventListener(event,handler){this.listeners[event]=handler}};
 const store=new Map();const calls=[];let registered=false,sub=null,asked=0,serverEnabled=false;
 const registration={pushManager:{async getSubscription(){return sub},async subscribe(){registered=true;sub={toJSON(){return {endpoint:'https://web.push.apple.com/test',keys:{}}},async unsubscribe(){sub=null;return true}};return sub}}};
 const context={document:{getElementById:el},location:{origin:'https://zeraora.github.io',hash:'#notifications'},navigator:{userAgent:'iPhone',standalone,serviceWorker:{async register(){return registration},ready:Promise.resolve(registration)}},Notification:{permission,async requestPermission(){asked++;this.permission=denied?'denied':'granted';return this.permission}},PushManager:function(){},matchMedia:()=>({matches:standalone}),localStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)},crypto:webcrypto,atob,btoa,Uint8Array,URL,JSON,AbortSignal,setTimeout(fn,ms){const t=setTimeout(fn,ms);t.unref();return t},
 async fetch(url,options){calls.push({url,options});const path=new URL(url).pathname;let body={};if(path.endsWith('/config'))body={publicKey:'B'+'A'.repeat(86)};else if(path==='/api/devices'&&options.method==='POST'){serverEnabled=true;body={ok:true};}else if(options.method==='DELETE'){serverEnabled=false;body={ok:true};}else if(path.endsWith('/test'))body={accepted:true};else body={enabled:serverEnabled};return {ok:true,async json(){return body}}}};
 context.window=context;vm.createContext(context);vm.runInContext(fs.readFileSync('notifications.js','utf8'),context);
 return {context,el,calls,store,get asked(){return asked},get registered(){return registered},get serverEnabled(){return serverEnabled}};
}
const settle=()=>new Promise(r=>setImmediate(r));
test('iPhone browser shows installation instructions without requesting permission',async()=>{
 const f=fixture({standalone:false});await settle();assert.match(f.el('notification-status').textContent,/ホーム画面に追加/);assert.equal(f.el('notification-enable').disabled,true);assert.equal(f.asked,0);
});
test('permission is requested only after enable; test, pause and resume work with the same device',async()=>{
 const f=fixture();await settle();assert.equal(f.asked,0);assert.equal(f.el('notifications').open,true);assert.equal(f.el('notification-enable').disabled,false);
 f.el('notification-code').value='PRIVATECODE';await f.el('notification-enable').listeners.click();
 assert.equal(f.asked,1);assert.equal(f.serverEnabled,true);assert.equal(f.el('notification-badge').textContent,'オン');assert.equal(f.el('notification-test').hidden,false);
 const enrollment=JSON.parse(f.calls.find(c=>new URL(c.url).pathname==='/api/devices').options.body);assert.equal(enrollment.code,'PRIVATECODE');assert.equal(enrollment.manageToken.length,43);
 await f.el('notification-test').listeners.click();assert.match(f.el('notification-status').textContent,/テスト通知を送信/);
 await f.el('notification-stop').listeners.click();assert.equal(f.serverEnabled,false);assert.equal(f.el('notification-badge').textContent,'オフ');assert.equal(f.el('notification-code').value,'');
 await f.el('notification-enable').listeners.click();assert.equal(f.serverEnabled,true);assert.equal(f.store.size,1);
});
test('denied notification permission never creates a server registration',async()=>{
 const f=fixture({denied:true});await settle();f.el('notification-code').value='PRIVATECODE';await f.el('notification-enable').listeners.click();assert.equal(f.serverEnabled,false);assert.equal(f.registered,false);assert.match(f.el('notification-status').textContent,/許可されません/);
});
test('service worker always displays a notification and rejects an off-site click target',async()=>{
 const handlers={};const shown=[];const context={self:{location:{origin:'https://zeraora.github.io'},addEventListener:(event,fn)=>handlers[event]=fn,registration:{async showNotification(...args){shown.push(args)}}},URL};vm.createContext(context);vm.runInContext(fs.readFileSync('docs/sw.js','utf8'),context);
 let finished;handlers.push({data:{json(){return {title:'test',url:'https://evil.test'}}},waitUntil(p){finished=p}});await finished;assert.equal(shown.length,1);assert.equal(new URL(shown[0][1].data.url).origin,'https://zeraora.github.io');
 handlers.push({data:{json(){throw Error('bad')}},waitUntil(p){finished=p}});await finished;assert.equal(shown.length,2);
});
