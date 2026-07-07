"""Dashboard HTML — a single self-contained page. No framework, no CDN, no
build step: keeps the hub lean and the attack surface tiny. The API key is
entered once and held in sessionStorage; every fetch sends it as x-api-key.
"""

_STYLE = """
  :root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--fg:#e6edf3;
        --muted:#8b949e;--accent:#58a6ff;--good:#3fb950;--bad:#f85149;--warn:#d29922}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
  header{display:flex;align-items:center;gap:16px;padding:12px 20px;
         border-bottom:1px solid var(--border);background:var(--panel);
         position:sticky;top:0;z-index:10}
  header h1{font-size:16px;margin:0;letter-spacing:.5px}
  header .status{margin-left:auto;display:flex;gap:12px;align-items:center}
  .pill{padding:3px 10px;border-radius:20px;font-size:12px;border:1px solid var(--border)}
  .pill.good{color:var(--good);border-color:var(--good)}
  .pill.bad{color:var(--bad);border-color:var(--bad)}
  button{background:var(--panel);color:var(--fg);border:1px solid var(--border);
         padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px}
  button:hover{border-color:var(--accent)}
  button.danger{color:var(--bad);border-color:var(--bad)}
  .grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;padding:20px;max-width:1200px;margin:0 auto}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;overflow:hidden}
  .card.wide{grid-column:1/3}
  .card h2{margin:0 0 12px;font-size:13px;text-transform:uppercase;
           letter-spacing:.5px;color:var(--muted)}
  .row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #21262d;gap:10px}
  .row:last-child{border-bottom:none}
  .muted{color:var(--muted)}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
  input[type=text]{background:var(--bg);border:1px solid var(--border);color:var(--fg);
                   padding:8px;border-radius:6px;width:100%}
  .metric{font-size:26px;font-weight:600}
  .metrics{display:flex;gap:24px}
  .bar{height:8px;background:#21262d;border-radius:4px;overflow:hidden;margin-top:4px}
  .bar>span{display:block;height:100%;background:var(--accent)}
  .scroll{max-height:280px;overflow-y:auto}
  .tag{font-size:11px;padding:1px 6px;border-radius:4px;background:#21262d;color:var(--muted)}
  a{color:var(--accent)}
"""

LOGIN_HTML = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>JARVIS</title><style>{_STYLE}
  .login{{max-width:360px;margin:12vh auto;text-align:center}}
  .login .card{{padding:28px}}</style></head><body>
<div class=login><div class=card>
  <h1 style="letter-spacing:2px">J A R V I S</h1>
  <p class=muted>Enter your API key to continue.</p>
  <input type=text id=key placeholder="JARVIS_API_KEY" autocomplete=off>
  <p><button onclick="login()" style="width:100%;margin-top:10px">Unlock</button></p>
  <p class=muted id=err style="color:var(--bad)"></p>
</div></div>
<script>
async function login(){{
  const k=document.getElementById('key').value.trim();
  if(!k)return;
  const r=await fetch('/dashboard/api/overview',{{headers:{{'x-api-key':k}}}});
  if(r.ok){{sessionStorage.setItem('jarvis_key',k);location.href='/dashboard';}}
  else document.getElementById('err').textContent='Rejected ('+r.status+').';
}}
document.getElementById('key').addEventListener('keydown',e=>{{if(e.key==='Enter')login()}});
</script></body></html>"""


DASHBOARD_HTML = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>JARVIS · dashboard</title><style>{_STYLE}</style></head><body>
<header>
  <h1>J A R V I S</h1>
  <div class=status>
    <span class=pill id=ks>…</span>
    <button id=killbtn onclick=toggleKill()>…</button>
    <button onclick=refreshAll()>↻</button>
  </div>
</header>
<div class=grid>
  <div class=card>
    <h2>Overview</h2>
    <div class=metrics>
      <div><div class=metric id=m_tasks>–</div><div class=muted>open tasks</div></div>
      <div><div class=metric id=m_docs>–</div><div class=muted>memories</div></div>
      <div><div class=metric id=m_backlog>–</div><div class=muted>bus backlog</div></div>
    </div>
  </div>
  <div class=card>
    <h2>Nodes</h2><div id=nodes></div>
  </div>
  <div class=card>
    <h2>Model routing (recent)</h2><div id=routing></div>
  </div>
  <div class=card>
    <h2>Tools registered</h2><div id=tools class=mono></div>
  </div>
  <div class="card wide">
    <h2>Memory search</h2>
    <input type=text id=q placeholder="search your memory…">
    <div id=searchres class=scroll style="margin-top:10px"></div>
  </div>
  <div class=card>
    <h2>Tasks</h2><div id=tasks class=scroll></div>
  </div>
  <div class=card>
    <h2>Timeline (24h)</h2><div id=timeline class="scroll mono"></div>
  </div>
  <div class="card wide">
    <h2>Audit &amp; bus (live)</h2><div id=logs class="scroll mono"></div>
  </div>
</div>
<script>
const KEY=sessionStorage.getItem('jarvis_key');
if(!KEY)location.href='/';
const H={{'x-api-key':KEY}};
const esc=s=>String(s).replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));
const fmt=ts=>new Date(ts*1000).toLocaleString();
async function get(p){{const r=await fetch(p,{{headers:H}});
  if(r.status===401){{sessionStorage.clear();location.href='/';}}
  if(r.status===503){{return {{paused:true}};}} return r.json();}}

async function loadOverview(){{
  const o=await get('/dashboard/api/overview'); if(o.paused)return;
  const ks=o.killswitch.state==='paused';
  const kp=document.getElementById('ks');
  kp.textContent=ks?'PAUSED':'ACTIVE'; kp.className='pill '+(ks?'bad':'good');
  const kb=document.getElementById('killbtn');
  kb.textContent=ks?'Resume':'Pause'; kb.className=ks?'':'danger';
  document.getElementById('m_tasks').textContent=o.open_tasks;
  document.getElementById('m_docs').textContent=o.memory_docs;
  document.getElementById('m_backlog').textContent=o.bus_backlog;
  document.getElementById('nodes').innerHTML=Object.entries(o.nodes).map(([n,s])=>
    `<div class=row><span>${{esc(n)}}</span><span class="pill ${{s.status==='up'?'good':'bad'}}">${{esc(s.status||'?')}}${{s.latency_ms?' '+s.latency_ms+'ms':''}}</span></div>`
  ).join('')||'<div class=muted>no nodes probed</div>';
  const tot=Object.values(o.tier_usage).reduce((a,b)=>a+b,0)||1;
  document.getElementById('routing').innerHTML=Object.entries(o.tier_usage).sort((a,b)=>b[1]-a[1]).map(([t,c])=>
    `<div class=row><span>${{esc(t)}}</span><span class=muted>${{c}}</span></div><div class=bar><span style="width:${{100*c/tot}}%"></span></div>`
  ).join('')||'<div class=muted>no requests yet</div>';
  document.getElementById('tools').innerHTML=o.tools.map(t=>`<span class=tag>${{esc(t)}}</span>`).join(' ');
}}
async function loadTasks(){{
  const d=await get('/dashboard/api/tasks'); if(d.paused)return;
  document.getElementById('tasks').innerHTML=d.tasks.map(t=>
    `<div class=row><span>${{t.status==='done'?'✓ ':''}}${{esc(t.title)}}</span>`+
    `<span class=muted>${{t.due_ts?fmt(t.due_ts):''}}</span></div>`
  ).join('')||'<div class=muted>no tasks</div>';
}}
async function loadTimeline(){{
  const d=await get('/dashboard/api/timeline?hours=24'); if(d.paused)return;
  document.getElementById('timeline').innerHTML=d.events.slice().reverse().map(e=>
    `<div class=row><span>[${{esc(e.kind)}}] ${{esc((e.preview||'').slice(0,70))}}</span>`+
    `<span class=muted>${{fmt(e.ts)}}</span></div>`
  ).join('')||'<div class=muted>nothing captured</div>';
}}
async function loadLogs(){{
  const d=await get('/dashboard/api/logs?limit=40'); if(d.paused)return;
  document.getElementById('logs').innerHTML=d.audit.slice().reverse().map(a=>
    `<div class=row><span>${{esc(a.actor)}} · ${{esc(a.action)}}</span>`+
    `<span class="muted">${{esc(a.outcome)}} ${{fmt(a.ts)}}</span></div>`
  ).join('')||'<div class=muted>no audit entries</div>';
}}
async function doSearch(){{
  const q=document.getElementById('q').value.trim(); if(!q)return;
  const d=await get('/dashboard/api/search?q='+encodeURIComponent(q));
  if(d.paused)return;
  document.getElementById('searchres').innerHTML=(d.results||[]).map(r=>
    `<div class=row><span>${{esc(r.text.slice(0,140))}}</span>`+
    `<span class=muted>${{esc(r.kind)}} ${{r.score.toFixed(3)}}</span></div>`
  ).join('')||'<div class=muted>no matches</div>';
}}
async function toggleKill(){{
  const paused=document.getElementById('ks').textContent==='PAUSED';
  await fetch(paused?'/control/resume':'/control/pause',
    {{method:'POST',headers:{{...H,'content-type':'application/json'}},
      body:paused?null:JSON.stringify({{reason:'dashboard'}})}});
  loadOverview();
}}
document.getElementById('q').addEventListener('keydown',e=>{{if(e.key==='Enter')doSearch()}});
function refreshAll(){{loadOverview();loadTasks();loadTimeline();loadLogs();}}
refreshAll(); setInterval(()=>{{loadOverview();loadLogs();}},5000);
</script></body></html>"""
