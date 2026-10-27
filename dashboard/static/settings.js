const $ = selector => document.querySelector(selector);
const state = {csrf:'', settings:null, qrId:null, qrGeneration:0, qrTimer:null, countdownTimer:null};

async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf,...options.headers}});
  if(response.status===401){location.replace('/login');throw new Error('登录已过期，请重新登录。');}
  const data=await response.json();
  if(!response.ok){const error=new Error(typeof data.detail==='string'?data.detail:'操作失败，请稍后重试。');error.status=response.status;throw error;}
  return data;
}
function post(path, body){return api(path,{method:'POST',body:JSON.stringify(body)});}
function result(selector,text,error=false){const node=$(selector);node.textContent=text;node.classList.toggle('error',error);node.hidden=false;}
function badge(selector,text,healthy){const node=$(selector);node.textContent=text;node.className='badge'+(healthy?' healthy':'');}
function fillLabels(selector,accounts){const list=$(selector);list.replaceChildren();for(const account of accounts){const option=document.createElement('option');option.value=account.label;list.append(option);}}
function selectedTelegram(){return state.settings?.telegram.find(a=>a.label===$('#tg-label').value);}
function selectTelegram(){const account=selectedTelegram();$('#tg-token').value='';$('#tg-channel').value=account?.channel_id || '';$('#tg-token').required=!account?.has_token;$('#tg-token').placeholder=account?.has_token?'已保存；留空继续使用当前 Token':'粘贴从 BotFather 获取的 Token';$('#tg-token-hint').textContent=account?.has_token?'Token 已保存，页面不回显。只有更换 Token 时才需要填写。':'先向 @BotFather 创建 Bot，获取 Token。';}
function updateBiliBadge(){const account=state.settings?.bilibili.find(a=>a.label===$('#bili-label').value);badge('#bili-badge',account?account.name:'未连接',account?.health.status==='healthy');}
function renderSettings(data, initial=false){
  state.settings=data;
  fillLabels('#tg-labels',data.telegram);fillLabels('#bili-labels',data.bilibili);
  const summary=$('#setup-summary');summary.replaceChildren();
  for(const [name,count] of [['Telegram',data.telegram.length],['B 站',data.bilibili.length]]){const item=document.createElement('span');item.className='setup-item'+(count?' connected':'');item.textContent=name+' · '+(count?'已连接 '+count+' 个账号':'尚未连接');summary.append(item);}
  badge('#tg-badge',data.telegram.length?'已连接':'未连接',!!data.telegram.length);
  updateBiliBadge();
  if(initial){
    const params=new URLSearchParams(location.search);const network=params.get('network');const label=params.get('label');
    if(label && /^[A-Za-z0-9_-]{1,64}$/.test(label)){if(network==='telegram')$('#tg-label').value=label;if(network==='bilibili')$('#bili-label').value=label;}
    selectTelegram();updateBiliBadge();
    const prefixes=Object.entries(data.routing.prefixes);$('#prefix').value=prefixes.find(([,targets])=>targets.includes('bilibili'))?.[0] || 'b';
    $('#poll-interval').value=data.routing.poll_interval_seconds;$('#album-settle').value=data.routing.album_settle_seconds;updatePreview();
  }
}
async function reload(){renderSettings(await api('/api/settings'));}
function updatePreview(){$('#prefix-preview').textContent='今天更新了一个功能 #'+($('#prefix').value || 'b');}

async function saveForm(event, selector, action){
  event.preventDefault();const button=event.currentTarget.querySelector('button[type="submit"]');const text=button.textContent;button.disabled=true;button.textContent='正在验证并保存…';$(selector).hidden=true;
  try{await action();}catch(error){result(selector,error.message,true);}finally{button.disabled=false;button.textContent=text;}
}
$('#tg-form').addEventListener('submit',event=>saveForm(event,'#tg-result',async()=>{
  const token=$('#tg-token').value.trim();
  await post('/api/settings/telegram',{label:$('#tg-label').value,bot_token:token || null,channel_id:$('#tg-channel').value.trim()});
  $('#tg-token').value='';await reload();selectTelegram();result('#tg-result','TG 频道已连接，配置已生效。可以回工作台查看状态。');
}));
$('#bili-form').addEventListener('submit',event=>saveForm(event,'#bili-result',async()=>{
  const sessdata=$('#sessdata').value.trim(),jct=$('#bili-jct').value.trim();if(!sessdata || !jct)throw new Error('请填写 SESSDATA 和 bili_jct，或使用上方的扫码登录。');
  const account=await post('/api/settings/bilibili/cookie',{label:$('#bili-label').value,sessdata,bili_jct:jct,buvid3:$('#buvid3').value.trim() || null});
  $('#sessdata').value='';$('#bili-jct').value='';$('#buvid3').value='';await reload();result('#bili-result','已连接 '+account.name+'，Cookie 验证通过。');
}));
$('#routing-form').addEventListener('submit',event=>saveForm(event,'#routing-result',async()=>{
  const data=await post('/api/settings/routing',{prefix:$('#prefix').value,poll_interval_seconds:Number($('#poll-interval').value),album_settle_seconds:Number($('#album-settle').value)});
  state.settings.routing=data.routing;$('#prefix').value=Object.keys(data.routing.prefixes)[0];updatePreview();result('#routing-result','同步规则已保存并生效。没有标记的消息只留在 TG。');
}));
$('#tg-label').addEventListener('input',selectTelegram);$('#bili-label').addEventListener('input',updateBiliBadge);$('#prefix').addEventListener('input',updatePreview);

function stopQR(){
  state.qrGeneration++;clearTimeout(state.qrTimer);clearInterval(state.countdownTimer);
  const id=state.qrId;state.qrId=null;if(id)api('/api/settings/bilibili/qr/'+id,{method:'DELETE'}).catch(()=>{});
}
function qrMessage(message,terminal=false){$('#qr-status').textContent=message;if(terminal){clearTimeout(state.qrTimer);clearInterval(state.countdownTimer);$('#qr-image').hidden=true;$('#qr-placeholder').textContent='请重新获取二维码';$('#qr-placeholder').hidden=false;$('#qr-countdown').textContent='';}}
async function pollQR(generation,id){
  if(generation!==state.qrGeneration || !$('#qr-dialog').open)return;
  try{
    const data=await post('/api/settings/bilibili/qr/'+id+'/poll',{});
    if(generation!==state.qrGeneration || !$('#qr-dialog').open)return;
    if(data.status==='success'){
      state.qrId=null;$('#qr-dialog').close();$('#sessdata').value='';$('#bili-jct').value='';$('#buvid3').value='';await reload();result('#bili-result','已登录 '+data.account.name+'，登录状态已保存，立即生效。');return;
    }
    if(data.status==='expired'){qrMessage('二维码已过期，请重新获取。',true);return;}
    qrMessage(data.scanned?'已扫描，请在手机上确认登录。':'请使用哔哩哔哩 APP 扫描二维码。');
  }catch(error){
    if(generation!==state.qrGeneration || !$('#qr-dialog').open)return;
    if([400,404,409,410].includes(error.status)){qrMessage(error.message,true);return;}
    qrMessage('连接暂时中断，正在重试…');
  }
  state.qrTimer=setTimeout(()=>pollQR(generation,id),2000);
}
async function generateQR(){
  stopQR();const generation=state.qrGeneration;const label=$('#bili-label').value;
  $('#qr-refresh').disabled=true;$('#qr-image').hidden=true;$('#qr-placeholder').hidden=false;$('#qr-placeholder').textContent='正在生成二维码…';$('#qr-countdown').textContent='';qrMessage('正在连接 B 站…');
  try{
    const data=await post('/api/settings/bilibili/qr',{label});
    if(generation!==state.qrGeneration || !$('#qr-dialog').open){api('/api/settings/bilibili/qr/'+data.id,{method:'DELETE'}).catch(()=>{});return;}
    state.qrId=data.id;$('#qr-image').src=data.image;$('#qr-image').hidden=false;$('#qr-placeholder').hidden=true;
    qrMessage('请使用哔哩哔哩 APP 扫描二维码。');const expires=Date.now()+data.expires_in*1000;
    const countdown=()=>{const remaining=Math.max(0,Math.ceil((expires-Date.now())/1000));$('#qr-countdown').textContent='有效时间 '+Math.floor(remaining/60)+':'+String(remaining%60).padStart(2,'0');if(!remaining){qrMessage('二维码已过期，请重新获取。',true);stopQR();}};
    countdown();state.countdownTimer=setInterval(countdown,1000);state.qrTimer=setTimeout(()=>pollQR(generation,data.id),2000);
  }catch(error){if(generation===state.qrGeneration && $('#qr-dialog').open)qrMessage(error.message,true);}
  finally{if(generation===state.qrGeneration)$('#qr-refresh').disabled=false;}
}
$('#qr-open').addEventListener('click',()=>{if(!$('#bili-label').reportValidity())return;$('#qr-dialog').showModal();generateQR();});
$('#qr-close').addEventListener('click',()=>$('#qr-dialog').close());$('#qr-dialog').addEventListener('close',stopQR);$('#qr-refresh').addEventListener('click',generateQR);window.addEventListener('pagehide',stopQR);
$('#logout').addEventListener('click',async()=>{try{stopQR();await post('/api/logout',{});location.replace('/login');}catch(error){result('#page-error',error.message,true);}});
(async()=>{try{state.csrf=(await api('/api/session')).csrf_token;renderSettings(await api('/api/settings'),true);document.querySelectorAll('.settings-form button, #qr-open').forEach(b=>b.disabled=false);}catch(error){result('#page-error',error.message,true);}})();
