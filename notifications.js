const WatchlistNotifications = (() => {
  const API='https://yutai-notifications.ultraclearsky.chatgpt.site/api';
  const STORAGE='yutai-notification-device-v1';
  let registration,publicKey,credentials,enabled=false,busy=false;
  const el=id=>document.getElementById(id);
  const standalone=()=>window.matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
  const supported=()=>location.origin==='https://zeraora.github.io'&&'serviceWorker' in navigator&&'PushManager' in window&&'Notification' in window;
  function message(text){el('notification-status').textContent=text;}
  function draw(){
    el('notification-enable').disabled=busy||!registration||!publicKey||enabled||(typeof Notification!=='undefined'&&Notification.permission==='denied');
    el('notification-test').hidden=!enabled;el('notification-stop').hidden=!enabled;
    el('notification-test').disabled=busy;el('notification-stop').disabled=busy;
    el('notification-code-row').hidden=enabled||Boolean(credentials);
    el('notification-badge').textContent=enabled?'オン':'オフ';
  }
  function bytes(value){const s=atob(value.replace(/-/g,'+').replace(/_/g,'/'));return Uint8Array.from(s,c=>c.charCodeAt(0));}
  function randomToken(){return btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32)))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
  const errors={code_invalid:'登録コードが違うか、有効期限が切れています。案内されたコードを確認してください。',try_later:'少し時間を空けて、もう一度お試しください。',device_limit:'登録できる端末数に達しています。不要な端末の通知を停止してください。',subscription_expired:'通知の登録が切れています。もう一度「通知を受け取る」を押してください。',not_registered:'通知が登録されていません。もう一度登録してください。',push_failed:'通知を送信できませんでした。時間を空けてお試しください。',device_conflict:'端末の登録を確認できませんでした。登録コードからやり直してください。'};
  async function call(path,method='GET',body){
    const response=await fetch(API+path,{method,headers:{...(body?{'Content-Type':'application/json'}:{}),...(credentials?{Authorization:'Bearer '+credentials.manageToken}:{})},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(12000)});
    let data;try{data=await response.json();}catch{throw Error('通知サーバーに接続できませんでした。しばらくしてから再読み込みしてください。');}
    if(!response.ok){const e=new Error(errors[data.error]||'通知サーバーに接続できませんでした。しばらくしてから再読み込みしてください。');e.code=data.error;throw e;}
    return data;
  }
  async function init(){
    if(!el('notifications'))return;
    el('notification-install').hidden=standalone();
    if(!standalone()&&/iPhone|iPad|iPod/.test(navigator.userAgent)){
      message('Safariで共有ボタンから「ホーム画面に追加」し、追加したアプリを開いてください。');draw();return;
    }
    if(!supported()){message('通知設定は、対応するiPhoneのホーム画面アプリから行ってください。');draw();return;}
    try{
      const saved=JSON.parse(localStorage.getItem(STORAGE));
      if(saved&&typeof saved.id==='string'&&typeof saved.manageToken==='string')credentials=saved;
      registration=await navigator.serviceWorker.register('/yutai-app/sw.js',{scope:'/yutai-app/',updateViaCache:'none'});
      registration=await Promise.race([navigator.serviceWorker.ready,new Promise((_,reject)=>setTimeout(()=>reject(Error('通知の準備に時間がかかっています。再読み込みしてください。')),12000))]);
      const config=await call('/config');publicKey=config.publicKey;
      if(credentials){
        try{const state=await call('/devices/'+credentials.id);enabled=state.enabled&&Notification.permission==='granted'&&Boolean(await registration.pushManager.getSubscription());}
        catch(error){if(error.code==='not_registered'){credentials=null;localStorage.removeItem(STORAGE);}else throw error;}
      }
      message(enabled?'通知はオンです。候補の追加・除外があった日にお知らせします。':Notification.permission==='denied'?'通知が許可されていません。iPhoneの「設定」→「通知」からこのアプリの通知を許可してください。':'「通知を受け取る」を押し、iPhoneの確認画面で許可してください。');
    }catch(error){message(error.message||'通知設定を読み込めませんでした。再読み込みしてください。');}
    draw();
  }
  async function enable(){
    if(busy||!registration||!publicKey)return;
    const code=el('notification-code').value.trim();
    if(!credentials&&!code){message('案内された登録コードを入力してください。');return;}
    busy=true;draw();
    // Request directly from the tap, before any network or service-worker await.
    try{
      const permissionRequest=Notification.requestPermission();
      if(await permissionRequest!=='granted')throw Error('通知が許可されませんでした。iPhoneの通知設定から変更できます。');
      if(!credentials){credentials={id:crypto.randomUUID(),manageToken:randomToken()};try{localStorage.setItem(STORAGE,JSON.stringify(credentials));}catch{credentials=null;throw Error('端末に通知設定を保存できません。通常モードでアプリを開いてください。');}}
      let subscription=await registration.pushManager.getSubscription();
      if(!subscription)subscription=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:bytes(publicKey)});
      try{await call('/devices','POST',{...credentials,code,subscription:subscription.toJSON()});}
      catch(error){if(['code_invalid','device_conflict'].includes(error.code)){credentials=null;localStorage.removeItem(STORAGE);}throw error;}
      enabled=true;el('notification-code').value='';message('通知をオンにしました。「テスト通知」でiPhoneに届くか確認できます。');
    }catch(error){message(error.message||'通知を登録できませんでした。');}
    busy=false;draw();
  }
  async function test(){
    if(!enabled||busy)return;busy=true;draw();
    try{await call('/devices/'+credentials.id+'/test','POST',{});message('テスト通知を送信しました。iPhoneの通知センターでご確認ください。');}
    catch(error){if(error.code==='subscription_expired')enabled=false;message(error.message);}
    busy=false;draw();
  }
  async function stop(){
    if(!enabled||busy)return;busy=true;draw();
    try{await call('/devices/'+credentials.id,'DELETE');enabled=false;const sub=await registration.pushManager.getSubscription();if(sub)await sub.unsubscribe();message('通知を停止しました。「通知を受け取る」で再開できます。');}
    catch(error){message(enabled?error.message:'通知の配信は停止しました。');}
    busy=false;draw();
  }
  function install(){
    el('notification-enable').addEventListener('click',enable);
    el('notification-test').addEventListener('click',test);
    el('notification-stop').addEventListener('click',stop);
    if(location.hash==='#notifications')el('notifications').open=true;
    init();
  }
  return {install};
})();
WatchlistNotifications.install();
