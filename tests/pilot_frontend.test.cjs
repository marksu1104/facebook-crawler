const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

const root = path.resolve(__dirname,'..');
const html = fs.readFileSync(path.join(root,'app/static/pilot.html'),'utf8');
const script = fs.readFileSync(path.join(root,'app/static/pilot.js'),'utf8');
const defaultSettings = {group_url:'https://www.facebook.com/share/g/testgroup/',keywords:['臨打'],excludes:[],match_mode:'any',browser:'auto',interval_seconds:120,max_checks:3,duration_minutes:10,max_notifications_per_check:3,telegram_configured:false,telegram_chat_id:'',remember_telegram_token:true,telegram_token_saved:false,telegram_storage_supported:true,telegram_storage_error:''};
const defaultStatus = {phase:'idle',message:'請先儲存設定。',pending:[],browser_open:false,group_confirmed:false,resolved_group_url:'',baseline_ready:false,auto_running:false,remaining_checks:0,cooldown_until:0,next_check_at:0,last_checked:null};
const samplePost = {id:'post-1',author:'同學',content:'今晚臨打缺人 <img src=x onerror=alert(1)>',url:'https://www.facebook.com/groups/123456/posts/999/',posted_at:'1 分鐘前',matched:true,hits:['臨打'],is_baseline:true,notification_status:'baseline'};

async function until(predicate) {
  for (let index=0;index<100;index++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve,5)); }
  assert.fail('Pilot DOM did not reach the expected state');
}

async function setup({settings={},status={},posts=[],events=[],failures={}} = {}) {
  const dom = new JSDOM(html,{url:'http://127.0.0.1:8766/',runScripts:'outside-only'});
  const {window}=dom,document=window.document,calls=[],errors=[];
  const db={settings:{...defaultSettings,...settings},status:{...defaultStatus,...status},posts:structuredClone(posts),events:structuredClone(events)};
  window.addEventListener('error',event=>errors.push(event.error));
  window.setInterval=callback=>{window.poll=callback;return 1;};
  window.fetch=async(raw,options={})=>{
    const url = new URL(raw,window.location.href), method=options.method || 'GET';
    const body=options.body ? JSON.parse(options.body) : null;
    calls.push({path:url.pathname,method,body,headers:options.headers});
    if (failures[url.pathname]) return {ok:false,json:async()=>({detail:failures[url.pathname]})};
    let data;
    if (url.pathname==='/api/state') data=db;
    else if (url.pathname==='/api/settings'&&method==='POST') {
      const {telegram_token,...ordinary}=body;
      Object.assign(db.settings,ordinary);
      if (telegram_token) db.settings.telegram_configured=true;
      db.settings.telegram_token_saved=ordinary.remember_telegram_token && db.settings.telegram_configured;
      db.settings.telegram_storage_error='';
      data={settings:db.settings};
    } else if (url.pathname==='/api/telegram/forget') {
      db.settings.telegram_configured=false;
      db.settings.telegram_token_saved=false;
      data={settings:db.settings};
    } else if (url.pathname==='/api/telegram/discover') data={bot:{username:'own_test_bot'},chats:[{id:'1001',name:'自己 <img src=x onerror=alert(1)>'},{id:'1002',name:'其他人'}]};
    else if (url.pathname==='/api/browser/open') {db.status.browser_open=true;db.status.phase='awaiting_login';data={queued:true};}
    else if (url.pathname==='/api/browser/confirm') {db.status.group_confirmed=true;db.status.phase='ready';data={queued:true};}
    else if (url.pathname==='/api/check') {db.status.baseline_ready=true;data={queued:true};}
    else if (url.pathname==='/api/auto/start') {db.status.auto_running=true;data={ok:true};}
    else if (url.pathname==='/api/auto/stop') {db.status.auto_running=false;data={ok:true};}
    else if (url.pathname==='/api/telegram/test'||/^\/api\/posts\/[^/]+\/test-notification$/.test(url.pathname)) data={queued:true};
    else if (url.pathname==='/api/shutdown') data={ok:true};
    else throw new Error(`Unexpected request: ${method} ${url.pathname}`);
    return {ok:true,json:async()=>structuredClone(data)};
  };
  window.eval(script);
  await until(()=>document.getElementById('draft-status').textContent==='已載入本機設定');
  return {window,document,db,calls,errors,close:async()=>{await new Promise(resolve=>setImmediate(resolve));window.close();}};
}

function edit(app,id,value) {
  const field=app.document.getElementById(id);
  field.value=value;
  field.dispatchEvent(new app.window.Event('input',{bubbles:true}));
}

function submit(app) {
  app.document.getElementById('settings-form').dispatchEvent(new app.window.Event('submit',{bubbles:true,cancelable:true}));
}

test('initial pilot state loads settings without opening a browser or sending any message',async()=>{
  const app=await setup();
  try {
    assert.equal(app.document.getElementById('group-url').value,defaultSettings.group_url);
    assert.equal(app.document.getElementById('interval-seconds').value,'120');
    assert.equal(app.document.getElementById('interval-seconds').max,'3600');
    assert.equal(app.document.getElementById('tracking-mode').value,'continuous');
    assert.equal(app.document.getElementById('notification-timezone').value,'Asia/Taipei');
    assert.equal(app.document.getElementById('telegram-token').value,'');
    assert.equal(app.document.getElementById('start-auto').disabled,true);
    assert.equal(app.document.getElementById('test-telegram').disabled,true);
    assert.deepEqual(app.calls.map(call=>call.method),['GET']);
    assert.match(app.document.body.textContent,/頻率不代表帳號安全保證/);
    assert.deepEqual(app.errors,[]);
  } finally { await app.close(); }
});

test('continuous tracking and limited tracking controls remain usable with old settings',async()=>{
  const app=await setup();
  try {
    assert.equal(app.document.getElementById('limited-settings').hidden,true);
    assert.equal(app.document.getElementById('max-checks').disabled,true);
    assert.match(app.document.getElementById('run-summary').textContent,/持續追蹤/);
    edit(app,'tracking-mode','limited');
    assert.equal(app.document.getElementById('limited-settings').hidden,false);
    assert.equal(app.document.getElementById('max-checks').disabled,false);
    edit(app,'max-checks','2');
    submit(app);
    await until(()=>app.calls.some(call=>call.path==='/api/settings'));
    const call=app.calls.find(call=>call.path==='/api/settings');
    assert.equal(call.body.tracking_mode,'limited');
    assert.equal(call.body.max_checks,2);
    assert.equal(call.body.notification_timezone,'Asia/Taipei');
    assert.deepEqual(app.errors,[]);
  } finally {await app.close();}
});

test('publication and detection times are displayed separately in the selected timezone',async()=>{
  const app=await setup({posts:[{...samplePost,published_at:'2026-11-11T11:00:00Z',first_seen:'2026-11-11T11:06:07Z'}]});
  try {
    const text=app.document.querySelector('.post-author').textContent;
    assert.match(text,/19:00:00/);
    assert.match(text,/首次偵測.*19:06:07/);
    assert.match(text,/GMT\+8/);
    assert.deepEqual(app.errors,[]);
  } finally {await app.close();}
});

test('retrying a continuous run still offers a stop action and shows the next check',async()=>{
  const app=await setup({settings:{tracking_mode:'continuous'},status:{phase:'retrying',auto_running:true,remaining_checks:null,next_check_at:Date.now()/1000+120,message:'連線逾時，稍後再試'}});
  try {
    assert.equal(app.document.getElementById('phase-badge').textContent,'等待重試');
    assert.equal(app.document.getElementById('remaining-checks').textContent,'持續追蹤');
    assert.equal(app.document.getElementById('next-action').dataset.action,'stop-auto');
    assert.equal(app.document.getElementById('next-action').disabled,false);
    assert.deepEqual(app.errors,[]);
  } finally {await app.close();}
});

test('settings submit parses generic rules and sends token only on explicit save',async()=>{
  const app=await setup();
  try {
    edit(app,'keywords','臨打、台北,週六\n臨打');
    edit(app,'excludes','已滿，取消');
    edit(app,'excluded-authors','排除同學\nSmith, John\n排除同學');
    edit(app,'telegram-token','123456:private_token_value');
    edit(app,'telegram-chat-id','1001');
    edit(app,'notification-timezone','UTC');
    assert.equal(app.calls.filter(call=>call.method!=='GET').length,0);
    assert.equal(app.document.getElementById('open-browser').disabled,true);
    submit(app);
    await until(()=>app.calls.some(call=>call.path==='/api/settings')&&app.document.getElementById('draft-status').textContent==='已載入本機設定');
    const call=app.calls.find(call=>call.path==='/api/settings');
    assert.deepEqual(call.body.keywords,['臨打','台北','週六']);
    assert.deepEqual(call.body.excludes,['已滿','取消']);
    assert.deepEqual(call.body.excluded_authors,['排除同學','Smith, John']);
    assert.equal(call.body.interval_seconds,120);
    assert.equal(call.body.notification_timezone,'UTC');
    assert.equal(call.body.tracking_mode,'continuous');
    assert.equal(call.body.telegram_token,'123456:private_token_value');
    assert.equal(call.body.remember_telegram_token,true);
    assert.equal(call.headers['X-App-Request'],'1');
    assert.equal(app.document.getElementById('telegram-token').value,'');
    assert.equal(app.window.localStorage.length,0);
    assert.equal(app.document.body.textContent.includes('private_token_value'),false);
    assert.match(app.document.getElementById('token-storage-status').textContent,/已加密保存/);
    submit(app);
    await until(()=>app.calls.filter(item=>item.path==='/api/settings').length===2);
    assert.equal(Object.hasOwn(app.calls.filter(item=>item.path==='/api/settings')[1].body,'telegram_token'),false);
    assert.deepEqual(app.errors,[]);
  } finally { await app.close(); }
});

test('author exclusions load independently from content words and explain excluded posts',async()=>{
  const app=await setup({settings:{excluded_authors:['排除同學','Smith, John']},posts:[{...samplePost,author:'排除同學',matched:false,hits:[],author_excluded:true}]});
  try {
    assert.equal(app.document.getElementById('excluded-authors').value,'排除同學\nSmith, John');
    assert.equal(app.document.getElementById('excludes').value,'');
    assert.match(app.document.querySelector('.post-match').textContent,/已排除這位發文者/);
    edit(app,'excluded-authors','尚未儲存的作者');
    app.window.poll();
    await new Promise(resolve=>setTimeout(resolve,10));
    assert.equal(app.document.getElementById('excluded-authors').value,'尚未儲存的作者');
    assert.equal(app.calls.some(call=>call.method==='POST'),false);
    app.document.getElementById('filter-matched').click();
    assert.equal(app.document.querySelectorAll('#posts .post').length,0);
    assert.deepEqual(app.errors,[]);
  } finally {await app.close();}
});

test('restored token enables Telegram with an empty write-only input and explicit forget clears it',async()=>{
  const app=await setup({settings:{telegram_configured:true,telegram_token_saved:true,telegram_chat_id:'1001'}});
  try {
    assert.equal(app.document.getElementById('telegram-token').value,'');
    assert.equal(app.document.getElementById('remember-telegram-token').checked,true);
    assert.equal(app.document.getElementById('test-telegram').disabled,false);
    assert.equal(app.calls.some(call=>call.method==='POST'),false);
    app.document.getElementById('telegram-details').open=true;
    app.document.getElementById('forget-telegram-token').click();
    await until(()=>app.document.getElementById('test-telegram').disabled);
    assert.equal(app.calls.filter(call=>call.path==='/api/telegram/forget').length,1);
    assert.equal(app.document.getElementById('forget-telegram-token').disabled,true);
    assert.equal(app.window.localStorage.length,0);
    assert.deepEqual(app.errors,[]);
  } finally {await app.close();}
});

test('opting out is an explicit saved preference and polling preserves the checkbox draft',async()=>{
  const app=await setup({settings:{telegram_configured:true,telegram_token_saved:true}});
  try {
    app.document.getElementById('telegram-details').open=true;
    app.document.getElementById('remember-telegram-token').click();
    assert.match(app.document.getElementById('token-storage-status').textContent,/儲存設定後會移除/);
    app.window.poll();
    await new Promise(resolve=>setTimeout(resolve,10));
    assert.equal(app.document.getElementById('remember-telegram-token').checked,false);
    assert.equal(app.calls.some(call=>call.method==='POST'),false);
    submit(app);
    await until(()=>app.document.getElementById('draft-status').textContent==='已載入本機設定');
    assert.equal(app.calls.find(call=>call.path==='/api/settings').body.remember_telegram_token,false);
    assert.equal(app.db.settings.telegram_token_saved,false);
    assert.equal(app.db.settings.telegram_configured,true);
    assert.deepEqual(app.errors,[]);
  } finally {await app.close();}
});

test('unsupported platforms and a backend awaiting restart disable the remember control',async()=>{
  for (const supported of [false,undefined]) {
    const app=await setup({settings:{telegram_storage_supported:supported}});
    try {
      const checkbox=app.document.getElementById('remember-telegram-token');
      assert.equal(checkbox.disabled,true);
      assert.equal(checkbox.checked,false);
      assert.match(app.document.getElementById('token-storage-status').textContent,
        supported===false ? /此系統目前/ : /重新啟動程式/);
      submit(app);
      await until(()=>app.calls.some(call=>call.path==='/api/settings'));
      assert.equal(app.calls.find(call=>call.path==='/api/settings').body.remember_telegram_token,false);
      assert.deepEqual(app.errors,[]);
    } finally {await app.close();}
  }
});

test('a saved token decryption warning stays visible and conflicting clears are disabled',async()=>{
  const app=await setup({settings:{telegram_token_saved:true,telegram_storage_error:'先前儲存的 Token 無法讀取，請重新貼入並儲存一次。'},status:{pending:['checking']}});
  try {
    assert.match(app.document.getElementById('token-storage-status').textContent,/無法讀取/);
    assert.equal(app.document.getElementById('forget-telegram-token').disabled,true);
    assert.equal(app.document.getElementById('remember-telegram-token').disabled,true);
    assert.equal(app.calls.some(call=>call.method==='POST'),false);
    assert.deepEqual(app.errors,[]);
  } finally {await app.close();}
});

test('polling never populates a returned token or replaces an unsaved settings draft',async()=>{
  const app=await setup({settings:{telegram_configured:true,telegram_chat_id:'1001',telegram_token:'999999:must_not_render'}});
  try {
    assert.equal(app.document.getElementById('telegram-token').value,'');
    edit(app,'keywords','我的草稿');
    edit(app,'telegram-token','123456:new_unsaved_token');
    const before=app.calls.length;
    app.window.poll();
    await new Promise(resolve=>setTimeout(resolve,10));
    assert.ok(app.calls.length>before);
    app.document.getElementById('refresh-state').click();
    await until(()=>app.calls.length>before);
    assert.equal(app.document.getElementById('keywords').value,'我的草稿');
    assert.equal(app.document.getElementById('telegram-token').value,'123456:new_unsaved_token');
    assert.equal(app.document.body.textContent.includes('must_not_render'),false);
    assert.deepEqual(app.errors,[]);
  } finally { await app.close(); }
});

test('baseline posts and HTML remain inert; a test notification requires an explicit button click',async()=>{
  const app=await setup({settings:{telegram_configured:true,telegram_chat_id:'1001'},status:{phase:'ready',browser_open:true,group_confirmed:true,baseline_ready:true},posts:[samplePost,{...samplePost,id:'bad',url:'https://evil.test/groups/123/posts/456',author:'<script>alert(1)</script>'}]});
  try {
    assert.equal(app.document.querySelectorAll('#posts img,#posts script').length,0);
    assert.match(app.document.querySelector('.post-content').textContent,/<img src=x/);
    assert.equal(app.document.querySelectorAll('[data-test-post]').length,1);
    assert.equal(app.calls.filter(call=>call.method==='POST').length,0);
    const link=app.document.querySelector('.post-link');
    assert.equal(link.href,samplePost.url);
    app.document.querySelector('[data-test-post]').click();
    await until(()=>app.calls.some(call=>call.path==='/api/posts/post-1/test-notification'));
    assert.equal(app.calls.filter(call=>call.path.includes('test-notification')).length,1);
    assert.deepEqual(app.errors,[]);
  } finally { await app.close(); }
});

test('chat discovery does not choose or save a recipient automatically',async()=>{
  const app=await setup({settings:{telegram_configured:true}});
  try {
    assert.equal(app.document.getElementById('discover-chats').disabled,false);
    app.document.getElementById('discover-chats').click();
    await until(()=>!app.document.getElementById('discovered-chats-wrap').hidden);
    assert.equal(app.document.getElementById('telegram-chat-id').value,'');
    assert.equal(app.document.querySelector('#discovered-chats img'),null);
    const select=app.document.getElementById('discovered-chats');
    select.value='1001';select.dispatchEvent(new app.window.Event('change',{bubbles:true}));
    assert.equal(app.document.getElementById('telegram-chat-id').value,'1001');
    assert.equal(app.calls.filter(call=>call.path==='/api/settings').length,0);
    assert.equal(app.document.getElementById('test-telegram').disabled,true);
    assert.deepEqual(app.errors,[]);
  } finally { await app.close(); }
});

test('restriction state prevents read and automatic restart while permitting explicit confirmation',async()=>{
  const app=await setup({settings:{telegram_configured:true,telegram_chat_id:'1001'},status:{phase:'paused',message:'需要你處理驗證',browser_open:true,group_confirmed:true,baseline_ready:true}});
  try {
    assert.equal(app.document.getElementById('check-once').disabled,true);
    assert.equal(app.document.getElementById('start-auto').disabled,true);
    assert.equal(app.document.getElementById('confirm-browser').disabled,false);
    assert.equal(app.document.getElementById('confirm-browser').textContent,'確認登入／限制已處理');
    assert.deepEqual(app.errors,[]);
  } finally { await app.close(); }
});

test('queued operations and confirming phase disable conflicting actions',async()=>{
  const app=await setup({status:{pending:['open']}});
  try {
    assert.equal(app.document.getElementById('open-browser').disabled,true);
    assert.equal(app.document.getElementById('save-settings').disabled,true);
    assert.equal(app.document.getElementById('stop-auto').disabled,false);
    app.db.status.pending=[];
    app.db.status.phase='confirming';
    app.document.getElementById('refresh-state').click();
    await until(()=>app.document.getElementById('phase-badge').textContent==='確認社團中');
    assert.equal(app.document.getElementById('save-settings').disabled,true);
    assert.equal(app.document.getElementById('confirm-browser').disabled,true);
    assert.deepEqual(app.errors,[]);
  } finally { await app.close(); }
});

test('upstream error details cannot expose a token in visible messages',async()=>{
  const token='123456:private_error_token';
  const app=await setup({settings:{telegram_configured:true,telegram_chat_id:'1001'},failures:{'/api/telegram/test':`request https://api.telegram.org/bot${token}/sendMessage failed`}});
  try {
    app.document.getElementById('test-telegram').click();
    await until(()=>app.document.getElementById('action-message').classList.contains('error'));
    assert.equal(app.document.body.textContent.includes(token),false);
    assert.match(app.document.getElementById('action-message').textContent,/Token 已隱藏/);
    assert.deepEqual(app.errors,[]);
  } finally { await app.close(); }
});

test('the main action guides connection, preview, notification setup and tracking',async()=>{
  const app=await setup();
  const next=()=>app.document.getElementById('next-action');
  try {
    assert.equal(next().dataset.action,'open-browser');
    next().click();
    await until(()=>next().dataset.action==='confirm-browser'&&!next().disabled);
    next().click();
    await until(()=>next().dataset.action==='check-once'&&!next().disabled);
    app.db.posts=[samplePost];
    next().click();
    await until(()=>next().dataset.action==='telegram'&&!next().disabled);
    assert.equal(app.document.getElementById('post-count').textContent,'1');
    next().click();
    assert.equal(app.document.getElementById('telegram-details').open,true);
    edit(app,'telegram-token','123456:local_test_only');
    edit(app,'telegram-chat-id','1001');
    submit(app);
    await until(()=>next().dataset.action==='start-auto'&&!next().disabled);
    next().click();
    await until(()=>next().dataset.action==='stop-auto'&&!next().disabled);
    assert.deepEqual(app.calls.filter(call=>call.method==='POST').map(call=>call.path),[
      '/api/browser/open','/api/browser/confirm','/api/check','/api/settings','/api/auto/start'
    ]);
    assert.deepEqual(app.errors,[]);
  } finally {await app.close();}
});

test('read limits support older settings and expand invalid advanced fields',async()=>{
  const app=await setup();
  try {
    assert.equal(app.document.getElementById('max-posts-per-check').value,'20');
    edit(app,'max-posts-per-check','51');
    submit(app);
    assert.equal(app.document.getElementById('advanced-details').open,true);
    assert.equal(app.calls.some(call=>call.path==='/api/settings'),false);
    edit(app,'max-posts-per-check','30');
    submit(app);
    await until(()=>app.calls.some(call=>call.path==='/api/settings'));
    assert.equal(app.calls.find(call=>call.path==='/api/settings').body.max_posts_per_check,30);
    assert.deepEqual(app.errors,[]);
  } finally {await app.close();}
});

test('editing a draft keeps result updates and matched filters working',async()=>{
  const app=await setup({status:{phase:'ready',browser_open:true,group_confirmed:true,baseline_ready:true},posts:[samplePost]});
  try {
    edit(app,'keywords','尚未儲存的條件');
    app.db.posts.push({...samplePost,id:'post-2',matched:false,hits:[],content:'另外一篇貼文'});
    app.db.status.scan_summary={loaded_posts:2,unlinked_posts:1};
    app.window.poll();
    await until(()=>app.document.getElementById('post-count').textContent==='2');
    assert.equal(app.document.getElementById('keywords').value,'尚未儲存的條件');
    assert.match(app.document.getElementById('results-caption').textContent,/本次讀取 2 篇/);
    app.document.getElementById('filter-matched').click();
    assert.equal(app.document.querySelectorAll('#posts .post').length,1);
    app.document.getElementById('filter-all').click();
    assert.equal(app.document.querySelectorAll('#posts .post').length,2);
    assert.deepEqual(app.errors,[]);
  } finally {await app.close();}
});

test('the latest preview retains the source order ahead of older stored posts',async()=>{
  const posts=[{...samplePost,id:'old',post_id:'old'},
    {...samplePost,id:'second',post_id:'second'}, {...samplePost,id:'first',post_id:'first'}];
  const app=await setup({posts,status:{preview_post_ids:['first','second']}});
  try {
    assert.deepEqual([...app.document.querySelectorAll('[data-test-post]')].map(button=>button.dataset.testPost),
      ['first','second','old']);
    assert.deepEqual(app.errors,[]);
  } finally {await app.close();}
});
