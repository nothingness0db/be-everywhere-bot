const $ = selector => document.querySelector(selector);
const state = {csrf:'', status:'', search:'', page:1, accounts:[], jobs:[], checking:new Set(), detailId:null, detailSnapshot:'', jobsSnapshot:'', generation:0};
const platforms = {telegram:'Telegram', twitter:'X', bilibili:'B 站', mastodon:'Mastodon', threads:'Threads', bluesky:'Bluesky'};
const statusNames = {unknown:'未检测', healthy:'正常', expired:'已失效', error:'需处理', unconfigured:'未配置', succeeded:'已成功', failed:'失败', pending:'待处理', queued:'等待重试', running:'同步中'};

function el(tag, className='', text='') {
  const node = document.createElement(tag); if (className) node.className = className; node.textContent = text; return node;
}
function icon(name) {
  const node = document.createElementNS('http://www.w3.org/2000/svg','svg'); node.classList.add('icon'); node.setAttribute('aria-hidden','true');
  const use = document.createElementNS('http://www.w3.org/2000/svg','use'); use.setAttribute('href','#i-'+name); node.append(use); return node;
}
function badge(status) { return el('span','badge '+status,statusNames[status] || status); }
function networkIcon(network) { return el('span','network-icon '+network, network==='telegram'?'↗':network==='twitter'?'𝕏':network==='bilibili'?'哔':(platforms[network] || network).slice(0,1)); }
function ago(timestamp) {
  if (!timestamp) return '尚无记录'; const seconds = Math.max(0, Date.now()/1000-timestamp);
  if (seconds < 60) return '刚刚'; if (seconds < 3600) return Math.floor(seconds/60)+' 分钟前';
  if (seconds < 86400) return Math.floor(seconds/3600)+' 小时前'; return Math.floor(seconds/86400)+' 天前';
}
function date(timestamp) { return timestamp ? new Date(timestamp*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : '—'; }
async function api(path, options={}) {
  const response = await fetch(path,{...options, headers:{'X-CSRF-Token':state.csrf,...options.headers}});
  if (response.status===401) {location.replace('/login'); throw new Error('登录已过期，请重新登录。');}
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail==='string' ? data.detail : '操作失败，请稍后再试。');
  return data;
}
let toastTimer;
function toast(text, error=false) {clearTimeout(toastTimer); const node=$('#toast'); node.textContent=text; node.className='toast'+(error?' error':''); node.hidden=false; toastTimer=setTimeout(()=>node.hidden=true,4500);}

function renderOverview(data) {
  const order={telegram:0,twitter:1,bilibili:2};
  state.accounts=data.accounts.sort((a,b)=>(order[a.network]??3)-(order[b.network]??3));
  $('#account-count').textContent=data.accounts.length;
  const healthy=data.accounts.filter(a=>a.health.status==='healthy').length;
  const problems=data.accounts.filter(a=>['expired','error','unconfigured'].includes(a.health.status)).length;
  $('#account-hint').textContent=healthy+' 个正常'+(problems?' · '+problems+' 个需处理':' · 可手动检测');
  $('#success-count').textContent=data.counts.succeeded || 0;
  $('#failed-count').textContent=data.counts.failed || 0;
  $('#failed-count').classList.toggle('has-failures',!!data.counts.failed);
  const pending=(data.counts.pending||0)+(data.counts.queued||0)+(data.counts.running||0);
  $('#pending-hint').textContent=pending?'另有 '+pending+' 个任务待处理':'暂无等待中的任务';
  const worker=$('#worker-status'); worker.classList.toggle('offline',!data.worker.running);
  worker.textContent=data.worker.running?(data.worker.busy?'正在处理任务':'服务运行中'):'同步进程未运行';
  worker.title=data.worker.last_cycle_at?'最近处理完成：'+date(data.worker.last_cycle_at):'等待第一次同步完成';
  if (data.routing.mode==='mesh') $('#routing-hint').textContent='当前使用多平台互相同步模式';
  else $('#routing-hint').textContent=Object.entries(data.routing.prefixes).map(([prefix,targets])=>'#'+prefix+' → '+targets.map(t=>platforms[t.split('/')[0]]||t).join(' / ')).join('   ·   ')+'   ·   没有标记不同步';
  renderAccounts();
}

function renderAccounts() {
  const parent=$('#accounts'); parent.replaceChildren();
  $('#check-all').disabled=!state.accounts.length || !!state.checking.size;
  if (!state.accounts.length) {const empty=el('div','empty','还没有连接账号。');const link=el('a','','前往账号与配置，连接 TG 和 B 站 →');link.href='/settings';empty.append(el('br'),link);parent.append(empty);return;}
  for (const account of state.accounts) {
    const card=el('article','account-card'); const top=el('div','account-top'); const title=el('div','account-title');
    title.append(el('strong','',platforms[account.network] || account.network),el('small','',account.name+' · '+account.label));
    top.append(networkIcon(account.network),title,badge(account.health.status));
    const detail=el('p','account-detail',account.health.detail);
    if (account.token_expires_at) detail.title='当前用户令牌到期时间：'+date(account.token_expires_at)+(account.auto_refresh?'；程序会自动刷新。':'');
    const bottom=el('div','account-bottom'); const button=el('button','check-button',state.checking.has(account.id)?'检测中…':'检测账号');
    button.disabled=state.checking.has(account.id); button.addEventListener('click',()=>checkOne(account.id));
    const controls=el('div','account-controls');controls.append(button);
    if(['telegram','bilibili'].includes(account.network)){const edit=el('a','check-button',account.network==='bilibili'?'重新登录':'配置');edit.href='/settings?'+new URLSearchParams({network:account.network,label:account.label});controls.append(edit);}
    bottom.append(el('span','',account.health.checked_at?'状态更新于 '+ago(account.health.checked_at):'尚未检测'),controls);
    card.append(top,detail,bottom); parent.append(card);
  }
}

function renderJobs(data) {
  const snapshot=JSON.stringify(data); if(snapshot===state.jobsSnapshot)return; state.jobsSnapshot=snapshot;
  state.jobs=data.items; const body=$('#jobs'); body.replaceChildren();
  if (!data.items.length) {
    const row=el('tr'); const cell=el('td','empty'); cell.colSpan=5;
    const emptyIcon=icon('inbox'); emptyIcon.classList.add('empty-icon');
    cell.append(emptyIcon,el('div','',state.search || state.status ? '没有符合条件的同步记录。':'还没有同步记录。在 TG 频道发送带 #标记 的消息后，结果会显示在这里。'));
    row.append(cell); body.append(row);
  }
  for (const job of data.items) {
    const row=el('tr'); const message=el('td'); const preview=el('button','message-preview',job.text || '（仅媒体消息）');
    preview.addEventListener('click',()=>openDetail(job.id));
    const meta=el('div','message-meta',((job.source?.network==='telegram'?'TG':'来源')+' #'+job.source_post_ids.join(', '))+(job.media_count?' · '+job.media_count+' 个媒体':''));
    message.append(preview,meta);
    const destination=el('td'); const target=el('div','row-destination'); const targetName=el('div','',platforms[job.destination?.network] || job.destination?.network || '账号已移除');
    targetName.append(el('small','',job.destination?.label || '')); target.append(networkIcon(job.destination?.network || '?'),targetName); destination.append(target);
    const status=el('td'); status.append(badge(job.status));
    if (job.error) {const error=el('div','row-error',job.error); error.title=job.error; status.append(error);}
    const time=el('td','time-cell',date(job.updated_at)); time.append(el('div','message-meta','已尝试 '+job.attempts+' 次'));
    const actions=el('td','right');
    if (job.status==='failed') {const retry=el('button','row-action retry','重试'); retry.prepend(icon('refresh')); retry.addEventListener('click',()=>retryJob(job.id,retry)); actions.append(retry);}
    else if (job.links.length) {const link=el('a','row-action','查看动态'); link.href=job.links[0]; link.target='_blank'; link.rel='noopener noreferrer'; link.append(icon('link')); actions.append(link);}
    else {const detail=el('button','row-action','查看详情'); detail.addEventListener('click',()=>openDetail(job.id)); actions.append(detail);}
    row.append(message,destination,status,time,actions); body.append(row);
  }
  const pages=Math.max(1,Math.ceil(data.total/data.page_size));
  $('#result-count').textContent='共 '+data.total+' 条同步记录'; $('#page-number').textContent=data.page+' / '+pages;
  $('#previous').disabled=data.page<=1; $('#next').disabled=data.page>=pages;
}

async function load() {
  const generation=++state.generation;
  try {
    const query=new URLSearchParams({status:state.status,search:state.search,page:state.page,page_size:20});
    const [overview,jobs]=await Promise.all([api('/api/overview'),api('/api/jobs?'+query)]);
    if (generation!==state.generation) return;
    if (state.page>1 && !jobs.items.length) {state.page=Math.max(1,Math.ceil(jobs.total/jobs.page_size));await load();return;}
    renderOverview(overview); renderJobs(jobs); $('#page-error').hidden=true;
    $('#last-refresh').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
    if (state.detailId!==null && $('#detail').open) {
      const detailId=state.detailId;
      const job=jobs.items.find(j=>j.id===detailId) || await api('/api/jobs/'+detailId);
      if(state.detailId===detailId && $('#detail').open)renderDetail(job);
    }
  } catch(e) {if(generation!==state.generation)return; $('#page-error').textContent=e.message || '暂时无法连接服务，请稍后刷新。'; $('#page-error').hidden=false;}
}

async function checkOne(id, quiet=false) {
  state.checking.add(id); renderAccounts();
  try {const account=await api('/api/accounts/'+id+'/check',{method:'POST'}); state.accounts=state.accounts.map(a=>a.id===id?account:a); if(!quiet)toast(account.health.detail,account.health.status!=='healthy');}
  catch(e){toast(e.message,true);}
  finally {state.checking.delete(id); renderAccounts();}
}
async function retryJob(id,button) {
  if(button){button.disabled=true;button.textContent='提交中…';}
  try {await api('/api/jobs/'+id+'/retry',{method:'POST'}); toast('已加入重试队列，会从尚未成功的部分继续发送。'); await load(); if(state.detailId===id) await openDetail(id);}
  catch(e){toast(e.message,true); if(button){button.disabled=false;button.textContent='重试';}}
}
function renderDetail(job) {
  const snapshot=JSON.stringify(job); if(snapshot===state.detailSnapshot)return; state.detailSnapshot=snapshot;
  const body=$('#detail-body'); body.replaceChildren();
  const meta=el('div','detail-meta'); meta.append(badge(job.status),el('span','',job.source?.name || '来源账号'),icon('arrow'),el('span','',(platforms[job.destination?.network] || job.destination?.network || '目标账号')+' · '+(job.destination?.name || '')));
  body.append(meta,el('div','detail-text',job.text || '（仅媒体消息）'),el('p','subtle','创建于 '+date(job.created_at)+' · 已尝试 '+job.attempts+' 次'+(job.media_count?' · '+job.media_count+' 个媒体':'')));
  if(job.error)body.append(el('div','detail-error',job.error));
  const actions=el('div','detail-actions');
  if(job.status==='failed'){const retry=el('button','button primary','重试到 '+(platforms[job.destination?.network] || job.destination?.network));retry.addEventListener('click',()=>retryJob(job.id,retry));actions.append(retry);}
  for(const [index,url] of job.links.entries()){const link=el('a','button secondary',job.links.length>1?'查看第 '+(index+1)+' 段动态':'查看已发布动态');link.href=url;link.target='_blank';link.rel='noopener noreferrer';actions.append(link);}
  if(['queued','running'].includes(job.status))actions.append(el('span','subtle','任务正在处理中，结果会自动更新。'));
  body.append(actions);
}
async function openDetail(id) {try{const job=await api('/api/jobs/'+id);state.detailId=id;renderDetail(job);if(!$('#detail').open)$('#detail').showModal();}catch(e){toast(e.message,true);}}
$('#close-detail').addEventListener('click',()=>$('#detail').close());
$('#detail').addEventListener('close',()=>{state.detailId=null;state.detailSnapshot='';});
$('#logout').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST'});location.replace('/login');}catch(e){toast(e.message,true);}});
$('#refresh').addEventListener('click',async()=>{const button=$('#refresh');button.disabled=true;try{await load();}finally{button.disabled=false;}});
$('#check-all').addEventListener('click',async()=>{const ids=state.accounts.map(a=>a.id);ids.forEach(id=>state.checking.add(id));renderAccounts();for(const id of ids)await checkOne(id,true);await load();toast('账号检测已完成。');});
document.querySelectorAll('[data-status]').forEach(button=>button.addEventListener('click',()=>{state.status=button.dataset.status;state.page=1;document.querySelectorAll('[data-status]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',b===button?'true':'false');});load();}));
let searchTimer;$('#search').addEventListener('input',event=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.search=event.target.value;state.page=1;load();},250);});
$('#previous').addEventListener('click',()=>{state.page--;load();});$('#next').addEventListener('click',()=>{state.page++;load();});
(async()=>{try{const session=await api('/api/session');state.csrf=session.csrf_token;await load();setInterval(()=>{if(!document.hidden)load();},10000);}catch(e){toast(e.message,true);}})();
