import pathlib, re

p = pathlib.Path('views/memory/index.html')
h = p.read_text()

SHELL_CSS = """
  /* ── 应用外壳 ─────────────────────────── */
  body{overflow:hidden}
  .app{display:flex;height:100vh;height:100dvh;overflow:hidden}
  .sidebar{
    width:200px;flex:0 0 200px;background:var(--surface);border-right:1px solid var(--line);
    display:flex;flex-direction:column;padding:18px 0 12px;
  }
  .brand{padding:0 18px 16px;font-family:ui-serif,Georgia,"Songti SC",serif;font-size:15px;
    font-weight:600;letter-spacing:.01em}
  .brand span{display:block;font-family:inherit;font-size:11.5px;font-weight:400;color:var(--text-3);margin-top:3px}
  .nav{display:flex;flex-direction:column;gap:1px;padding:0 8px}
  .nav button{
    position:relative;display:flex;align-items:center;gap:9px;height:34px;padding:0 10px;
    border:0;border-radius:var(--r-sm);background:transparent;color:var(--text-2);
    font:inherit;font-size:13px;cursor:pointer;text-align:left;transition:background .12s ease,color .12s ease;
  }
  .nav button:hover{background:var(--hover);color:var(--text)}
  .nav button[aria-current="true"]{background:var(--accent-soft);color:var(--text);font-weight:500}
  .nav button[aria-current="true"]::before{content:"";position:absolute;left:-8px;top:8px;bottom:8px;width:2px;
    background:var(--accent);border-radius:0 2px 2px 0}
  .nav svg{flex:0 0 auto;opacity:.75}
  .side-foot{margin-top:auto;padding:12px 18px 0;border-top:1px solid var(--line);
    font-size:11.5px;color:var(--text-3);line-height:1.6}
  .side-foot b{font-weight:500;color:var(--text-2)}

  .main{flex:1;min-width:0;display:flex;flex-direction:column;overflow:hidden}
  .topbar{
    display:flex;align-items:center;gap:10px;padding:12px 20px;border-bottom:1px solid var(--line);
    background:var(--surface);flex-wrap:wrap;
  }
  .topbar h1{font-size:15px;font-weight:600;margin:0;letter-spacing:.01em}
  .topbar .grow{flex:1}
  .stage{flex:1;overflow:auto;padding:18px 20px 0}
  .view{display:none}
  .view.on{display:block}
  .footer{
    display:flex;align-items:center;gap:10px;padding:10px 20px;border-top:1px solid var(--line);
    background:var(--surface);
  }
  .footer .search{max-width:520px;flex:1}
  .footer .hint{font-size:11.5px;color:var(--text-3);white-space:nowrap}

  /* 图谱视图 */
  .graph-stage{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);overflow:hidden}
  .zoom{position:absolute;right:12px;bottom:12px;display:flex;flex-direction:column;gap:6px}
  .zoom button{
    width:30px;height:30px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--surface);
    color:var(--text-2);font-size:14px;line-height:1;cursor:pointer;transition:background .12s ease,color .12s ease;
  }
  .zoom button:hover{background:var(--hover);color:var(--text)}
  .sessions{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}
  .sess{
    text-align:left;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);
    padding:13px 14px;cursor:pointer;transition:border-color .12s ease,background .12s ease;
  }
  .sess:hover{border-color:#D6D9DD;background:var(--surface-2)}
  .sess .name{font-size:13px;font-weight:600;color:var(--text);word-break:break-all}
  .sess .meta{font-size:11.5px;color:var(--text-3);margin-top:3px}
  .kv{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);overflow:hidden}
  .kv div{display:flex;gap:16px;padding:11px 16px;border-bottom:1px solid var(--line);font-size:12.5px}
  .kv div:last-child{border-bottom:0}
  .kv .k{flex:0 0 160px;color:var(--text-3)}
  .kv .v{color:var(--text);word-break:break-all}
  .tabs{display:none}
  @media (max-width:860px){
    .sidebar{width:56px;flex-basis:56px;padding-top:14px}
    .brand{padding:0 0 12px;text-align:center;font-size:12px}
    .brand span{display:none}
    .nav button span.lbl{display:none}
    .nav button{justify-content:center;padding:0}
    .side-foot{display:none}
  }
"""

h = h.replace("</style>", SHELL_CSS + "</style>", 1)

# 外壳开始：sidebar + main + topbar
old_open = '<div class="page">\n  <header class="head">'
new_open = '''<div class="app">
  <aside class="sidebar">
    <div class="brand">全局记忆<span>Memory Workspace</span></div>
    <nav class="nav">
      <button data-view="graph" aria-current="true">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="6" cy="6" r="2.2"/><circle cx="18" cy="8" r="2.2"/><circle cx="12" cy="18" r="2.2"/><path d="M8 7l8 1M7 8l4 8M17 9l-4 7"/></svg>
        <span class="lbl">知识图谱</span>
      </button>
      <button data-view="list">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6h16M4 12h16M4 18h10"/></svg>
        <span class="lbl">记忆管理</span>
      </button>
      <button data-view="sessions">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6a2 2 0 0 1 2-2h9l5 5v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M14 4v6h6"/></svg>
        <span class="lbl">会话</span>
      </button>
      <button data-view="settings">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/></svg>
        <span class="lbl">设置</span>
      </button>
    </nav>
    <div class="side-foot">记忆库 <b id="footCount">—</b> 条<br>跨会话共享</div>
  </aside>

  <div class="main">
    <div class="topbar">
      <h1 id="viewTitle">知识图谱</h1>
      <span class="grow"></span>
      <button class="btn" id="btnFullscreen" title="全屏查看">全屏</button>
    </div>
    <div class="stage">

  <header class="head">'''

h = h.replace(old_open, new_open, 1)

# 关闭外壳：scrim 之前收尾 stage/main，并补 footer
old_tail = '<div class="scrim" id="scrim"></div>'
new_tail = '''    </div>
    <div class="footer">
      <div class="search">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input id="q2" placeholder="搜索记忆，回车跳到记忆管理" autocomplete="off" />
      </div>
      <span class="hint" id="footHint">—</span>
    </div>
  </div>
</div>

<div class="scrim" id="scrim"></div>'''
h = h.replace(old_tail, new_tail, 1)

# 隐藏原 header 内的标题区（改用 topbar），但保留 Embedding 状态与按钮
h = h.replace('''  <header class="head">
    <div>
      <h1>全局记忆</h1>
      <p>管理跨会话共享的记忆信息</p>
    </div>
    <div class="head-actions">''', '''  <header class="head" style="margin-top:-6px">
    <div class="head-actions">''', 1)

# 把现有内容包成三个视图：list / graph / sessions / settings
h = h.replace('  <section class="overview">', '  <section class="view on" id="vList">\n  <section class="overview">', 1)
h = h.replace('    <div id="viewGraph" hidden>', '''    </section>

    <section class="view" id="vGraph">
      <div class="graph-stage">''', 1)
h = h.replace('      <div class="graph-wrap" style="position:relative"><canvas id="graph"></canvas><div class="g-tip" id="gTip"></div></div>',
              '''        <div class="graph-wrap" style="position:relative"><canvas id="graph"></canvas><div class="g-tip" id="gTip"></div></div>
        <div class="zoom">
          <button id="zIn" title="放大">+</button>
          <button id="zOut" title="缩小">−</button>
          <button id="zReset" title="重置视角">⌂</button>
        </div>
      </div>''', 1)
h = h.replace('''      <div class="g-legend">''', '''    <div class="g-legend">''', 1)
h = h.replace('''      <div class="graph-note" id="graphNote"></div>
    </div>''', '''    <div class="graph-note" id="graphNote"></div>
    </section>

    <section class="view" id="vSessions">
      <div class="sessions" id="sessList"></div>
      <div class="empty" id="sessEmpty" hidden><div class="big">还没有会话归属</div>
        <div class="hint">新版写入的记忆会记下来自哪个群或私聊</div></div>
    </section>

    <section class="view" id="vSettings">
      <div class="kv" id="settingsBox"></div>
    </section>''', 1)

p.write_text(h)
print("shell ok:", h.count('class="sidebar"'), h.count('id="vGraph"'), h.count('id="vList"'))
