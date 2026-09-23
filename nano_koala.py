# nano_koala.py  ← app.py 同目录
# atlas(koala_assets.png/json)与切好的图层(nano_koala_assets/)都在 assets/ 下
# import 时若图层缺失才自动切图，并注册静态文件

import json, re, pathlib
from nicegui import ui, app as _napp
from loguru import logger

_BASE         = pathlib.Path(__file__).parent
_ASSETS_DIR   = _BASE / 'assets' / 'nano_koala_assets'
_ATLAS_PNG    = _BASE / 'assets' / 'koala_assets.png'
_ATLAS_JSON   = _BASE / 'assets' / 'koala_assets.json'
_STATIC_MOUNT = '/nka'
_SETUP_DONE   = False

# ── 切图 ─────────────────────────────────────────────────────────────────
def _extract_layers():
    try:
        from PIL import Image
    except ImportError:
        logger.warning('[nano_koala] 缺少 Pillow：pip install Pillow'); return False
    if not _ATLAS_PNG.exists() or not _ATLAS_JSON.exists():
        logger.warning('[nano_koala] 找不到 atlas'); return False
    _ASSETS_DIR.mkdir(exist_ok=True)
    atlas = Image.open(_ATLAS_PNG).convert('RGBA')
    with open(_ATLAS_JSON, encoding='utf-8') as f:
        meta = json.load(f)
    n = 0
    for name, fd in meta['frames'].items():
        m = re.search(r'\((\d+_\w+)', name)
        if not m: continue
        key = m.group(1).rstrip(')').strip()
        r = fd['frame']
        atlas.crop((r['x'], r['y'], r['x']+r['w'], r['y']+r['h'])).save(
            _ASSETS_DIR / f'{key}.png', 'PNG')
        n += 1
    logger.debug(f'[nano_koala] 切割 {n} 层 → {_ASSETS_DIR.name}/')
    return True

# ── CSS（注入到 <head>）──────────────────────────────────────────────────
_CSS = """
/* mount div 강제 full width */
[id^='nkph-']{width:100%!important;display:block!important;margin:0!important;padding:0!important;}
/* ══ 考拉面板（全宽嵌入抽屉，无左右上间距）══════════════════════════════ */
#koala-panel { margin:0; width:100%; position:relative; }

#koala-frame {
  /* 浅色化：背景改透明，让 app.py 里包着这个组件的外层卡片
     （.theme-card，自带浅色背景+边框+阴影）直接透出来，不在这里
     重复定义一套颜色，也不会跟外层卡片形成"双层边框"。 */
  background:transparent;
  border-radius:0;
  border:none;
  overflow:hidden;
  position:relative;
}

/* 角标：全宽无圆角时不显示 */
.kp-c { display:none; }

/* 顶栏（NANO.SYS / LIVE 状态条）已去掉——纯装饰性的"终端感"文字，
   跟浅色化之后的整体风格不搭，直接不渲染这块（HTML 注入那边已经
   去掉了对应的 div，这两条规则留着不会生效，删掉避免误导）。 */
.kp-left{display:flex;align-items:center;gap:5px;}
.kp-dot {
  width:5px;height:5px;border-radius:50%;background:#818cf8;flex-shrink:0;
  animation:kp-pulse 2.4s ease-in-out infinite;
}
@keyframes kp-pulse {
  0%,100%{opacity:.35;box-shadow:0 0 0 0 rgba(129,140,248,0);}
  50%     {opacity:1;  box-shadow:0 0 5px 2px rgba(129,140,248,0.35);}
}
.kp-sys { font-family:'Fira Code','JetBrains Mono',monospace;font-size:9px;color:#475569;letter-spacing:.08em; }
.kp-act { font-family:'Fira Code','JetBrains Mono',monospace;font-size:9px;color:#6366f1;opacity:.55;letter-spacing:.05em; }

/* 屏幕区——背景同样改透明，留白加大（原来 8px 0 6px 0 太局促）。 */
#koala-screen {
  background:transparent;
  position:relative;
  display:flex;align-items:center;justify-content:center;
  padding:28px 0 24px 0;
}
/* 扫描线——CRT 屏幕特效，是给深色屏幕设计的，浅色背景下只会显得
   像一层脏污，直接关掉（display:none，不删规则，方便以后想恢复
   深色模式时直接打开）。 */
.kp-scanlines {
  display:none;
}
/* 老电视雪花噪点层：SVG feTurbulence + 快速位移模拟静电 */
@keyframes kp-noise-drift{
  0%  {transform:translate(0,0)}
  12% {transform:translate(-3px,-2px)}
  25% {transform:translate(2px,3px)}
  37% {transform:translate(-2px,-3px)}
  50% {transform:translate(3px,2px)}
  62% {transform:translate(-3px,3px)}
  75% {transform:translate(2px,-2px)}
  87% {transform:translate(-2px,2px)}
  100%{transform:translate(0,0)}
}
/* 噪点 + 暗角——同样是给深色 CRT 屏幕设计的特效，浅色背景下噪点会
   变成一层灰雾、暗角会在四周留下诡异的灰边，一起关掉。 */
.kp-noise {
  display:none;
}
.kp-vignette {
  display:none;
}

/* ══ 考拉舞台 ═══════════════════════════════════════════════════════════ */
#koala-stage {
  position:relative;width:200px;height:216px;
  image-rendering:pixelated;image-rendering:crisp-edges;
  filter:drop-shadow(0 0 8px rgba(99,102,241,0.3));
}
/* 所有图层：Quasar reset 覆盖 */
#koala-stage img {
  position:absolute!important;left:0!important;top:0!important;
  width:100%!important;height:100%!important;
  image-rendering:pixelated!important;image-rendering:crisp-edges!important;
  display:block!important;pointer-events:none;
}

/* ══ Keyframes ══════════════════════════════════════════════════════════ */
@keyframes k-breathe{0%,100%{transform:translateY(0) scaleY(1)}50%{transform:translateY(-1.5px) scaleY(1.012)}}
@keyframes k-badge-n{0%,100%{transform:rotate(-2deg)}50%{transform:rotate(2deg)}}
@keyframes k-badge-f{0%,100%{transform:rotate(-5deg)}50%{transform:rotate(5deg)}}
@keyframes k-chip-sl{0%,100%{opacity:.55}50%{opacity:1}}
@keyframes k-chip-md{0%,100%{opacity:.3}50%{opacity:1}}
@keyframes k-chip-ft{0%,18%{opacity:1}19%,55%{opacity:.12}56%,100%{opacity:.78}}
@keyframes k-tilt-l {0%,100%{transform:rotate(0)}35%,65%{transform:rotate(-4deg)}}
@keyframes k-tilt-r {0%,100%{transform:rotate(0)}35%,65%{transform:rotate(4deg)}}
@keyframes k-look-l {0%,100%{transform:translateX(0)}30%,70%{transform:translateX(-3px)}}
@keyframes k-look-r {0%,100%{transform:translateX(0)}30%,70%{transform:translateX(3px)}}
@keyframes k-look-u {0%,100%{transform:translateY(0)}30%,70%{transform:translateY(-3px)}}
/* k-ear-t: 已废弃，改用 skewX */
@keyframes k-sway   {0%,100%{transform:translateX(0)}25%{transform:translateX(-2px)}75%{transform:translateX(2px)}}
@keyframes k-shake  {0%,100%{transform:translateX(0)}15%{transform:translateX(-5px)}30%{transform:translateX(5px)}45%{transform:translateX(-4px)}60%{transform:translateX(4px)}75%{transform:translateX(-2px)}90%{transform:translateX(2px)}}
/* emote */
@keyframes k-zzz   {0%{transform:translateY(0);opacity:1}100%{transform:translateY(-22px);opacity:0}}
@keyframes k-qst   {0%{clip-path:inset(0 100% 0 0)}28%{clip-path:inset(0 67% 0 0)}55%{clip-path:inset(0 33% 0 0)}78%,90%{clip-path:inset(0 0% 0 0)}100%{clip-path:inset(0 100% 0 0)}}
@keyframes k-dots  {0%{clip-path:inset(0 100% 0 0)}28%{clip-path:inset(0 68% 0 0)}55%{clip-path:inset(0 35% 0 0)}78%,90%{clip-path:inset(0 0% 0 0)}100%{clip-path:inset(0 100% 0 0)}}
@keyframes k-sweat {0%{transform:translateY(0);opacity:1}100%{transform:translateY(16px);opacity:0}}
/* wifi 弧线 */
@keyframes k-wf1{0%,30%{opacity:1}31%,92%{opacity:.1}93%,100%{opacity:1}}
@keyframes k-wf2{0%,20%{opacity:.1}21%,52%{opacity:1}53%,100%{opacity:.1}}
@keyframes k-wf3{0%,42%{opacity:.1}43%,72%{opacity:1}73%,100%{opacity:.1}}

/* ══ 工具类（JS 动态添加/移除）══════════════════════════════════════════ */
.ka-breathe{animation:k-breathe 3.2s ease-in-out infinite!important;transform-origin:50% 88%!important;}
.ka-badge-n{animation:k-badge-n 2.8s ease-in-out infinite!important;transform-origin:50% 57%!important;}
.ka-badge-f{animation:k-badge-f 0.9s ease-in-out infinite!important;transform-origin:50% 57%!important;}
.ka-chip-sl{animation:k-chip-sl 4s   ease-in-out infinite!important;}
.ka-chip-md{animation:k-chip-md 1.6s ease-in-out infinite!important;}
.ka-chip-ft{animation:k-chip-ft 0.5s ease-in-out infinite!important;}
.ka-chip-of{animation:k-chip-sl 12s  ease-in-out infinite!important;opacity:.25!important;}
/* 耳朵抽动：双层方案
   k-ear-l/r     = 完整耳朵，永远静止，提供稳定耳根
   k-ear-l/r-tip = 仅显示耳尖（clip-path 裁切 = transform-origin y），skewX 只动耳尖
   裁切线 = transform-origin y → 裁切边界偏移恒为 0 → 耳根纹丝不动 */
@keyframes k-ear-l-skew{
  0%,100%{transform:skewX(0deg)}
  20%    {transform:skewX(3deg)}
  55%    {transform:skewX(-1.2deg)}
  82%    {transform:skewX(0.4deg)}
}
@keyframes k-ear-r-skew{
  0%,100%{transform:skewX(0deg)}
  20%    {transform:skewX(-3deg)}
  55%    {transform:skewX(1.2deg)}
  82%    {transform:skewX(-0.4deg)}
}
/* 耳朵抽动：2° 极小角 skewX，弧线最大偏移 <2px（人眼不可感知）
   以耳根弧线中心为 origin，角度极小确保连接处无可见位移 */
.ka-ear-l{animation:k-ear-l-skew .4s ease-out!important;transform-origin:22% 42.6%!important;}
.ka-ear-r{animation:k-ear-r-skew .4s ease-out!important;transform-origin:79% 42.2%!important;}
.ka-look-l {animation:k-look-l  1.2s ease-in-out!important;}
.ka-look-r {animation:k-look-r  1.2s ease-in-out!important;}
.ka-look-u {animation:k-look-u  1.0s ease-in-out!important;}
.ka-look-d {animation:k-look-d  1.0s ease-in-out!important;}
@keyframes k-look-d{0%,100%{transform:translateY(0)}30%,70%{transform:translateY(3px)}}
@keyframes k-nose-wgl{0%,100%{transform:translateY(0)}30%{transform:translateY(-1.5px)}70%{transform:translateY(1px)}}
.ka-nose-wgl{animation:k-nose-wgl .5s ease-in-out!important;}
@keyframes k-stretch{0%,100%{transform:translateY(0) scaleY(1)}40%,60%{transform:translateY(-3px) scaleY(1.025)}}
.ka-stretch{animation:k-stretch .9s ease-in-out!important;transform-origin:50% 88%!important;}
.ka-tilt-l {animation:k-tilt-l  1.2s ease-in-out!important;transform-origin:50% 68%!important;}
.ka-tilt-r {animation:k-tilt-r  1.2s ease-in-out!important;transform-origin:50% 68%!important;}

.ka-sway   {animation:k-sway    1.2s ease-in-out!important;}
.ka-shake  {animation:k-shake   0.5s ease-in-out!important;}
/* emote 激活态 */
.ka-zzz  {animation:k-zzz   2.2s ease-in-out infinite!important;}
.ka-qst  {animation:k-qst   2.4s ease-in-out infinite!important;}
.ka-dots {animation:k-dots  2.0s ease-in-out infinite!important;}
.ka-sweat{animation:k-sweat 0.9s ease-in    forwards!important;}
/* WiFi PNG 逐帧遮罩：dot→大弧→中弧→小弧→暗，循环 */
@keyframes k-wifi-png{
  0%,8%   {clip-path:inset(100% 0 0 0);}
  16%,32% {clip-path:inset(8.98% 0 0 0);}
  40%,55% {clip-path:inset(6.25% 0 0 0);}
  63%,78% {clip-path:inset(2.73% 0 0 0);}
  86%,94% {clip-path:inset(0% 0 0 0);}
  100%    {clip-path:inset(100% 0 0 0);}
}
.ka-wifi-png{animation:k-wifi-png 2.8s steps(1) infinite!important;}
/* wifi CSS弧线（fallback）*/
#k-wifi-wrap{position:absolute;left:0;top:0;width:100%;height:100%;opacity:0;pointer-events:none;transition:opacity .3s;}
.k-wa{position:absolute;border:2px solid #60a5fa;border-bottom-color:transparent;border-left-color:transparent;border-right-color:transparent;border-radius:50%;left:50%;transform:translateX(-50%) rotate(-45deg);transform-origin:center bottom;}
#k-wa-dot{position:absolute;width:4px;height:4px;border-radius:50%;background:#60a5fa;left:50%;bottom:68%;transform:translateX(-50%);}
#k-wa1{width:10px;height:8px; bottom:67.5%;animation:k-wf1 1.4s ease-in-out infinite;}
#k-wa2{width:18px;height:14px;bottom:67%;  animation:k-wf2 1.4s ease-in-out infinite;}
#k-wa3{width:28px;height:20px;bottom:66.5%;animation:k-wf3 1.4s ease-in-out infinite;}
"""

# ── DOM 注入 JS ───────────────────────────────────────────────────────────
def _build_inject_js(mount_id):
    wifi_png = (_ASSETS_DIR / '31_emote_wifi.png').exists()
    wifi_img = f"<img src='/nka/31_emote_wifi.png' style='position:absolute;left:0;top:0;width:100%;height:100%;image-rendering:pixelated;'>" if wifi_png else ""

    return f"""
(function(){{
  var ph = document.getElementById('{mount_id}');
  if (!ph || ph.querySelector('#koala-panel')) return;
  /* mount div 및 NiceGUI 래퍼 강제 full width */
  ph.style.cssText = 'width:100%;display:block;margin:0;padding:0;';
  var parent = ph.parentElement;
  if(parent) parent.style.cssText += ';width:100%;margin:0;padding:0;';
  var P = '/nka/';

  /* ── 面板 HTML ── */
  ph.innerHTML = `
  <div id="koala-panel">
    <div id="koala-frame">
      <div class="kp-c kp-tl"></div><div class="kp-c kp-tr"></div>
      <div class="kp-c kp-bl"></div><div class="kp-c kp-br"></div>
      <div id="koala-screen">
        <div id="koala-stage"></div>
        <div class="kp-scanlines"></div>
        <div class="kp-noise"></div>
        <div class="kp-vignette"></div>
      </div>
    </div>
  </div>`;

  var stage = document.getElementById('koala-stage');

  /* ── 图层定义 [id, file, z-index, opacity, inHeadGroup, isEye, isMouth] ── */
  var LAYERS = [
    ['k-shadow',  '01_shadow.png',          1,  1, 0,0,0],
    ['k-body',    '02_body_base.png',        2,  1, 0,0,0],
    ['k-head',    '03_head_base.png',        3,  1, 1,0,0],  // head group
    ['k-belly',   '04_belly.png',            4,  1, 0,0,0],
    ['k-arm-l',   '06_arm_left_idle.png',    6,  1, 0,0,0],
    ['k-arm-r',   '07_arm_right_idle.png',   7,  1, 0,0,0],
    ['k-ear-l',   '08_ear_left.png',         8,  1, 1,0,0],  // head group, ear
    ['k-ear-r',   '09_ear_right.png',        9,  1, 1,0,0],  // head group, ear
    ['k-chip-sk', '05_chip_socket.png',      5,  1, 1,0,0],  // 移到耳朵之后！底座贴在耳朵表面
    ['k-nose',    '10_nose.png',             10, 1, 1,0,0],  // head group
    ['k-e-open',  '11_eyes_open.png',        11, 1, 1,1,0],  // eye
    ['k-e-blink', '12_eyes_blink.png',       11, 0, 1,1,0],
    ['k-e-happy', '15_eyes_happy.png',       11, 0, 1,1,0],
    ['k-e-sleep', '16_eyes_sleep.png',       11, 0, 1,1,0],
    ['k-e-error', '17_eyes_error.png',       11, 0, 1,1,0],
    ['k-m-closed','18_mouth_closed.png',     12, 1, 1,0,1],  // mouth
    ['k-m-open',  '19_mouth_open.png',       12, 0, 1,0,1],
    ['k-m-yawn',  '20_mouth_yawn.png',       12, 0, 1,0,1],
    ['k-badge',   '21_badge.png',            13, 1, 0,0,0],
    ['k-chip-l',  '22_chip_lines.png',       14, 1, 0,0,0],
    ['k-chip-d',  '23_chip_dots.png',        15, 1, 0,0,0],
    ['k-laptop',  '24_laptop.png',           20, 0, 0,0,0],
    ['k-docs',    '25_docs.png',             20, 0, 0,0,0],
    ['k-sweat',   '26_sweat_drop.png',       20, 0, 0,0,0],
    ['k-zzz',     '27_emote_zzz.png',        21, 0, 0,0,0],
    ['k-qst',     '28_emote_question.png',   21, 0, 0,0,0],
    ['k-dots',    '29_emote_dots.png',       21, 0, 0,0,0],
    ['k-star',    '30_emote_star.png',       21, 0, 0,0,0],
  ];

  var ELS = {{}};
  LAYERS.forEach(function(L) {{
    var img = document.createElement('img');
    img.id = L[0];
    img.src = P + L[1];
    /* z-index 제거: DOM append 순서 = Aseprite 레이어 순서 = 렌더 순서 */
    /* filter:drop-shadow stacking context에서 inline z-index가 무시되는 버그 방지 */
    img.style.opacity = L[3];
    stage.appendChild(img);
    ELS[L[0]] = img;
  }});

  /* WiFi 容器 */
  var wifiWrap = document.createElement('div');
  wifiWrap.id = 'k-wifi-wrap';
  wifiWrap.style.zIndex = '22';
  wifiWrap.innerHTML = `{wifi_img}
    <div id="k-wa-dot"></div>
    <div class="k-wa" id="k-wa1"></div>
    <div class="k-wa" id="k-wa2"></div>
    <div class="k-wa" id="k-wa3"></div>`;
  stage.appendChild(wifiWrap);
  /* WiFi PNG 存在时启用帧动画，否则用 CSS 弧线 */
  var wifiPngEl = wifiWrap.querySelector('img[src*="31_emote_wifi"]');
  if (wifiPngEl) {{
    wifiPngEl.style.position = 'absolute';
    wifiPngEl.style.left = '0'; wifiPngEl.style.top = '0';
    wifiPngEl.style.width = '100%'; wifiPngEl.style.height = '100%';
    wifiPngEl.style.imageRendering = 'pixelated';
    wifiPngEl.style.clipPath = 'inset(100% 0 0 0)'; /* 初始隐藏 */
    /* 弧线 CSS fallback 不显示 */
    ['k-wa-dot','k-wa1','k-wa2','k-wa3'].forEach(function(id){{
      var el = document.getElementById(id); if(el) el.style.display='none';
    }});
    wifiWrap._hasPng = true;
  }}

  /* 逻辑分组 */
  var HEAD_IDS = ['k-head','k-ear-l','k-ear-r','k-chip-sk','k-chip-l','k-chip-d','k-nose',
                  'k-e-open','k-e-blink','k-e-happy','k-e-sleep','k-e-error',
                  'k-m-closed','k-m-open','k-m-yawn'];
  var EYE_IDS  = ['k-e-open','k-e-blink','k-e-happy','k-e-sleep','k-e-error'];
  var MTH_IDS  = ['k-m-closed','k-m-open','k-m-yawn'];

  /* ── 持续动画（初始设置）── */
  stage.classList.add('ka-breathe');
  ELS['k-badge'].classList.add('ka-badge-n');
  ELS['k-badge'].style.transformOrigin = '50% 8%';
  ELS['k-chip-l'].classList.add('ka-chip-sl');
  ELS['k-chip-d'].classList.add('ka-chip-sl');

  /* ── 工具函数 ── */
  function $$(id){{ return document.getElementById(id); }}

  function anim(el, cls, dur, torig) {{
    if (!el) return;
    el.classList.remove(cls); void el.offsetWidth;
    if (torig) el.style.transformOrigin = torig;
    el.classList.add(cls);
    if (dur) setTimeout(function() {{
      el.classList.remove(cls);
      if (torig) el.style.transformOrigin = '';
    }}, dur);
  }}

  /* 对多个元素应用同一动画 */
  function animGroup(ids, cls, dur, torig) {{
    /* reflow 之前：批量移除旧 class + 批量设置 transformOrigin */
    ids.forEach(function(id) {{
      var el=ELS[id]; if(!el) return;
      el.classList.remove(cls);
      if(torig) el.style.transformOrigin = torig;
    }});
    /* 单次 reflow，把所有样式变更一次性 flush */
    void (ELS[ids[0]] || document.body).offsetWidth;
    /* reflow 之后：只做 classList.add，无任何中间样式操作，保证同帧启动 */
    ids.forEach(function(id) {{
      var el=ELS[id]; if(!el) return;
      el.classList.add(cls);
    }});
    if(dur) setTimeout(function(){{
      ids.forEach(function(id){{
        var el=ELS[id]; if(!el) return;
        el.classList.remove(cls);
        if(torig) el.style.transformOrigin='';
      }});
    }}, dur);
  }}

  function setEye(id) {{
    EYE_IDS.forEach(function(eid) {{ ELS[eid].style.opacity = eid === id ? '1' : '0'; }});
  }}
  function setMouth(id) {{
    MTH_IDS.forEach(function(mid) {{ ELS[mid].style.opacity = mid === id ? '1' : '0'; }});
  }}
  function showProp(id, v)  {{ if(ELS[id]) ELS[id].style.opacity = v ? '1' : '0'; }}
  function showEmote(id, v) {{ if(ELS[id]) ELS[id].style.opacity = v ? '1' : '0'; }}

  function hideAllProps()  {{ ['k-laptop','k-docs','k-sweat'].forEach(function(x){{showProp(x,false);}});}}
  function hideAllEmotes() {{
    ['k-zzz','k-qst','k-dots','k-star'].forEach(function(x){{showEmote(x,false);}});
    wifiWrap.style.opacity = '0';
    if (wifiWrap._hasPng) {{
      var pngEl = wifiWrap.querySelector('img');
      if (pngEl) pngEl.classList.remove('ka-wifi-png');
    }}
    /* 移除 emote 动画类 */
    if(ELS['k-zzz'])  ELS['k-zzz'].classList.remove('ka-zzz');
    if(ELS['k-qst'])  ELS['k-qst'].classList.remove('ka-qst');
    if(ELS['k-dots']) ELS['k-dots'].classList.remove('ka-dots');
    if(ELS['k-sweat'])ELS['k-sweat'].classList.remove('ka-sweat');
  }}

  function showEmoteAnim(id, animCls) {{
    showEmote(id, true);
    if (!animCls) return;          /* 空 class 跳过，避免 classList.add('') 报错 */
    var el = ELS[id];
    if (!el) return;
    el.classList.remove(animCls); void el.offsetWidth;
    el.classList.add(animCls);
    /* 汗滴：一次性，结束后隐藏 */
    if (animCls === 'ka-sweat') {{
      el.addEventListener('animationend', function() {{
        showEmote(id, false);
        el.classList.remove(animCls);
      }}, {{once: true}});
    }}
  }}

  function chipSpeed(s) {{
    var map = {{slow:'ka-chip-sl',med:'ka-chip-md',fast:'ka-chip-ft',off:'ka-chip-of'}};
    var cls = map[s] || 'ka-chip-sl';
    ['k-chip-l','k-chip-d'].forEach(function(id) {{
      var el = ELS[id];
      el.classList.remove('ka-chip-sl','ka-chip-md','ka-chip-ft','ka-chip-of');
      el.classList.add(cls);
    }});
  }}

  function blink(n, cb) {{
    if (n <= 0) {{ if (cb) cb(); return; }}
    setEye('k-e-blink');
    setTimeout(function() {{
      setEye('k-e-open');
      setTimeout(function() {{ blink(n-1, cb); }}, 180);
    }}, 120);
  }}

  function rand(a, b) {{ return a + Math.floor(Math.random() * (b - a + 1)); }}

  /* ── 调度器 ── */
  var curState = 'idle', prevState = 'idle', isSleeping = false, lastAct = Date.now();
  var tMicro, tSpecial, tSleep, toolInterval = null, routingTimer = null;
  var taskStartTime = 0, actionBusy = false;

  function clearTimers() {{
    clearTimeout(tMicro); clearTimeout(tSpecial); clearTimeout(tSleep);
    if(toolInterval){{ clearInterval(toolInterval); toolInterval=null; }}
    if(routingTimer) {{ clearTimeout(routingTimer);  routingTimer=null; }}
  }}

  /* ── 待机微动作（B 类）── */
  var micro = [
    function(cb){{ blink(1,cb); }},
    function(cb){{ blink(1,cb); }},
    function(cb){{ blink(1,cb); }},
    function(cb){{ blink(2,cb); }},
    /* 慢眨 */
    function(cb){{ setEye('k-e-blink'); setTimeout(function(){{ setEye('k-e-open'); setTimeout(cb,260); }},370); }},
    /* 看左右上 */
    function(cb){{ animGroup(EYE_IDS,'ka-look-l',1200); setTimeout(cb,1400); }},
    function(cb){{ animGroup(EYE_IDS,'ka-look-r',1200); setTimeout(cb,1400); }},
    function(cb){{ animGroup(EYE_IDS,'ka-look-u',1000); setTimeout(cb,1200); }},
    /* 歪头 - 对整个头部组应用，transform-origin 统一在脖子处 */
    function(cb){{ animGroup(HEAD_IDS,'ka-tilt-l',1200,'50% 68%'); setTimeout(cb,1400); }},
    function(cb){{ animGroup(HEAD_IDS,'ka-tilt-r',1200,'50% 68%'); setTimeout(cb,1400); }},
    /* 耳抖 - 各自的旋转原点在耳根 */
    function(cb){{ anim(ELS['k-ear-l'],'ka-ear-l',600); setTimeout(cb,700); }},
    function(cb){{ anim(ELS['k-ear-r'],'ka-ear-r',600); setTimeout(cb,700); }},
    function(cb){{
      anim(ELS['k-ear-l'],'ka-ear-l',600);
      anim(ELS['k-ear-r'],'ka-ear-r',600);
      setTimeout(cb,700);
    }},
    /* 打哈欠 */
    function(cb){{
      setMouth('k-m-yawn');
      animGroup(HEAD_IDS,'ka-tilt-r',800,'50% 68%');
      setTimeout(function(){{ setMouth('k-m-closed'); cb(); }},900);
    }},
    /* 身体晃 */
    function(cb){{ anim(stage,'ka-sway',1200); setTimeout(cb,1300); }},
    /* 看下 */
    function(cb){{ animGroup(EYE_IDS,'ka-look-d',1000); setTimeout(cb,1200); }},
    /* 鼻子轻动 */
    function(cb){{ anim(ELS['k-nose'],'ka-nose-wgl',500); setTimeout(cb,600); }},
    /* 伸懒腰 */
    function(cb){{ anim(stage,'ka-stretch',900); setTimeout(cb,1000); }},
  ];

  /* ── 待机特技（C 类）── */
  var special = [
    /* 四下张望 */
    function(cb){{
      animGroup(EYE_IDS,'ka-look-l',900);
      setTimeout(function(){{ animGroup(EYE_IDS,'ka-look-r',900); setTimeout(cb,1100); }},1000);
    }},
    /* 好奇 */
    function(cb){{
      animGroup(HEAD_IDS,'ka-tilt-l',1200,'50% 68%');
      showEmoteAnim('k-qst','ka-qst');
      setTimeout(function(){{ hideAllEmotes(); cb(); }},1700);
    }},
    /* 冒星 */
    function(cb){{
      setEye('k-e-happy'); showEmoteAnim('k-star','');
      ELS['k-star'].style.opacity='1';
      setTimeout(function(){{ setEye('k-e-open'); ELS['k-star'].style.opacity='0'; cb(); }},1600);
    }},
    /* 困倦哈欠 */
    function(cb){{
      blink(1,function(){{
        setTimeout(function(){{
          setMouth('k-m-yawn');
          setTimeout(function(){{ setMouth('k-m-closed'); cb(); }},700);
        }},300);
      }});
    }},
    /* 自言自语 */
    function(cb){{
      showEmoteAnim('k-dots','ka-dots'); setMouth('k-m-open');
      setTimeout(function(){{ setMouth('k-m-closed'); hideAllEmotes(); cb(); }},1400);
    }},
  ];

  function scheduleMicro() {{
    if (curState !== 'idle') return;
    tMicro = setTimeout(function() {{
      if (curState !== 'idle') return;
      if (actionBusy) {{ scheduleMicro(); return; }}
      actionBusy = true;
      micro[Math.floor(Math.random() * micro.length)](function() {{
        actionBusy = false;
        scheduleMicro();
      }});
    }}, rand(3500,7000));
  }}
  function scheduleSpecial() {{
    if (curState !== 'idle') return;
    tSpecial = setTimeout(function() {{
      if (curState !== 'idle') return;
      if (actionBusy) {{ scheduleSpecial(); return; }}
      actionBusy = true;
      special[Math.floor(Math.random() * special.length)](function() {{
        actionBusy = false;
        scheduleSpecial();
      }});
    }}, rand(14000,28000));
  }}
  function scheduleSleep() {{
    tSleep = setTimeout(function() {{
      if (curState === 'idle' && (Date.now() - lastAct) > 30000) enterSleep();
      else scheduleSleep();
    }}, 5000);
  }}

  /* ── 睡眠 ── */
  function enterSleep() {{
    if (isSleeping) return; isSleeping = true; curState = 'sleep';
    setEye('k-e-sleep'); setMouth('k-m-closed'); hideAllProps(); hideAllEmotes();
    showEmoteAnim('k-zzz','ka-zzz'); chipSpeed('off');
  }}
  function exitSleep() {{
    if (!isSleeping) return; isSleeping = false;
    setEye('k-e-open'); hideAllEmotes();
    anim(ELS['k-ear-l'],'ka-ear-l',600);
    anim(ELS['k-ear-r'],'ka-ear-r',600);
    chipSpeed('slow');
  }}

  /* ── 主状态机 ── */
  function applyState(s, sk, ragHit, fullHit) {{
    lastAct = Date.now();
    var wasWorking = prevState === 'working';
    var wasIdle    = prevState === 'idle' || prevState === 'sleep';
    var userSent   = wasIdle && (s === 'CORE_THINKING' || s === 'ROUTING...');

    if (isSleeping && s !== 'sleep') exitSleep();
    clearTimers(); actionBusy = false; hideAllProps(); hideAllEmotes();
    ELS['k-badge'].classList.remove('ka-badge-f');
    ELS['k-badge'].classList.add('ka-badge-n');
    curState = (s === 'SYS_IDLE') ? 'idle' : s;

    if (s === 'SYS_IDLE') {{
      prevState = 'idle';
      setEye('k-e-open'); setMouth('k-m-closed'); chipSpeed('slow');
      scheduleMicro(); scheduleSpecial(); scheduleSleep();
      /* 任务完成回弹 */
      if (wasWorking) {{
        setTimeout(function(){{
          setEye('k-e-happy'); anim(stage,'ka-sway',600);
          setTimeout(function(){{ setEye('k-e-open'); }}, 700);
        }}, 150);
      }}
      /* RAG 冒星 */
      if (ragHit) {{
        setEye('k-e-happy'); ELS['k-star'].style.opacity = '1';
        setTimeout(function(){{ setEye('k-e-open'); ELS['k-star'].style.opacity='0'; }}, 1400);
      }}
      /* 全文命中：docs + 开心嘴 */
      if (fullHit) {{
        showProp('k-docs', true); setMouth('k-m-open');
        setTimeout(function(){{ showProp('k-docs',false); setMouth('k-m-closed'); }}, 1600);
      }}

    }} else if (s === 'CORE_THINKING') {{
      prevState = 'thinking';
      setEye('k-e-open'); setMouth('k-m-closed'); chipSpeed('med');
      /* 用户发消息 → 耳朵一抖 */
      if (userSent) {{
        anim(ELS['k-ear-l'],'ka-ear-l',600);
        anim(ELS['k-ear-r'],'ka-ear-r',600);
      }}
      animGroup(HEAD_IDS,'ka-tilt-l',1400,'50% 68%');
      showEmoteAnim('k-qst','ka-qst');

    }} else if (s === 'ROUTING...') {{
      prevState = 'routing';
      setEye('k-e-open'); setMouth('k-m-closed'); chipSpeed('med');
      /* 路由扫视：眼睛左右交替扫 */
      var scanSide = 0;
      function doScan() {{
        if (curState !== 'ROUTING...') return;
        animGroup(EYE_IDS, scanSide%2===0 ? 'ka-look-l' : 'ka-look-r', 700);
        scanSide++;
        routingTimer = setTimeout(doScan, 950);
      }}
      routingTimer = setTimeout(doScan, 200);

    }} else if (s === 'TOOL_EXECUTING') {{
      prevState = 'working';
      taskStartTime = Date.now();
      setMouth('k-m-closed'); chipSpeed('fast');
      /* 工作微抖 + 长任务耐心 */
      toolInterval = setInterval(function() {{
        if (curState !== 'TOOL_EXECUTING') {{ clearInterval(toolInterval); toolInterval=null; return; }}
        var elapsed = Date.now() - taskStartTime;
        if (elapsed > 10000) {{
          if (Math.random() < 0.4) {{
            var isL = Math.random() < 0.5;
            anim(ELS[isL?'k-ear-l':'k-ear-r'], isL?'ka-ear-l':'ka-ear-r', 600);
          }}
        }} else {{
          if (Math.random() < 0.3) anim(stage,'ka-sway',700);
        }}
      }}, 3500);
      // ⚠️ 这里要列全所有联网工具的名字。前三个是历史遗留名，
      //    留着不碍事；漏掉新名字的后果是**上网时考拉不再亮 wifi**，
      //    而那是个没人会报的 bug —— 动画不亮不会报错。
      var WEB = ['SearchTheWeb','WebSearch','google_search','fetch_web_search'];
      var KB  = ['LocalKB','query_local_knowledge'];
      var SW  = ['SkillWriter','WriteSkill'];
      if (WEB.indexOf(sk) >= 0) {{
        animGroup(EYE_IDS,'ka-look-u',99999);
        wifiWrap.style.opacity = '1';
        if (wifiWrap._hasPng) {{
          var pngEl = wifiWrap.querySelector('img');
          if (pngEl) {{ pngEl.classList.remove('ka-wifi-png'); void pngEl.offsetWidth; pngEl.classList.add('ka-wifi-png'); }}
        }}
      }} else if (KB.indexOf(sk) >= 0) {{
        showProp('k-docs', true); animGroup(EYE_IDS,'ka-look-l',1200);
      }} else if (SW.indexOf(sk) >= 0) {{
        showProp('k-laptop', true); showEmoteAnim('k-dots','ka-dots');
      }} else {{
        showProp('k-laptop', true);
      }}

    }} else if (s === 'AWAITING_APPROVAL') {{
      prevState = 'awaiting';
      setEye('k-e-happy'); setMouth('k-m-closed'); chipSpeed('med');
      animGroup(EYE_IDS,'ka-look-u',99999);
      ELS['k-badge'].classList.remove('ka-badge-n');
      ELS['k-badge'].classList.add('ka-badge-f');
      anim(ELS['k-ear-l'],'ka-ear-l',600);
      anim(ELS['k-ear-r'],'ka-ear-r',600);

    }} else if (s === 'ERROR') {{
      prevState = 'error';
      setEye('k-e-error'); setMouth('k-m-open'); chipSpeed('slow');
      showEmoteAnim('k-sweat','ka-sweat');
      anim(stage,'ka-shake',500);

    }} else if (s === 'CRITICAL_SYSTEM_HALT') {{
      prevState = 'halt';
      setEye('k-e-error'); setMouth('k-m-open'); chipSpeed('off');
      showEmoteAnim('k-sweat','ka-sweat');
      anim(stage,'ka-shake',600);
    }}
  }}

  /* 模型切换：Python _sync 检测变化后调用 */
  window._koalaModelChanged = function() {{
    blink(2, null);
    setTimeout(function() {{
      anim(ELS['k-ear-l'],'ka-ear-l',600);
      anim(ELS['k-ear-r'],'ka-ear-r',600);
    }}, 400);
  }};

  /* ── 点击彩蛋 ── */

  /* 头部透明点击区（覆盖整个头部区域，避免透明像素漏检）*/
  var headZone = document.createElement('div');
  headZone.style.cssText = 'position:absolute;left:10%;top:4%;width:50%;height:42%;cursor:pointer;z-index:30;';
  headZone.addEventListener('click', function() {{
    if (isSleeping) {{
      /* 睡觉时点击 → 随机唤醒效果 */
      exitSleep();
      var wakeIdx = Math.floor(Math.random() * 2);
      if (wakeIdx === 0) {{
        /* 方案1：打哈欠 + 双眨 */
        setTimeout(function(){{ setMouth('k-m-yawn'); }}, 200);
        setTimeout(function(){{ setMouth('k-m-closed'); blink(2, null); }}, 900);
      }} else {{
        /* 方案2：歪头 + 问号 */
        setTimeout(function(){{
          animGroup(HEAD_IDS,'ka-tilt-l',1200,'50% 68%');
          showEmoteAnim('k-qst','ka-qst');
          setTimeout(function(){{ hideAllEmotes(); }}, 1400);
        }}, 300);
      }}
    }} else {{
      /* 正常状态：happy + 弹跳 */
      setEye('k-e-happy');
      anim(stage,'ka-sway',800);
      setTimeout(function(){{ setEye('k-e-open'); }},900);
    }}
  }});
  stage.appendChild(headZone);

  /* 工牌点击 */
  ELS['k-badge'].style.pointerEvents = 'auto';
  ELS['k-badge'].addEventListener('click', function() {{
    ELS['k-badge'].classList.remove('ka-badge-n','ka-badge-f');
    void ELS['k-badge'].offsetWidth;
    ELS['k-badge'].classList.add('ka-badge-f');
    setTimeout(function(){{
      ELS['k-badge'].classList.remove('ka-badge-f');
      ELS['k-badge'].classList.add('ka-badge-n');
    }},2000);
  }});

  /* ── 暴露给 NiceGUI timer 调用 ── */
  window._koalaSetState = applyState;

  /* ── 初始化 ── */
  applyState('SYS_IDLE','',false,false);
}})();
"""

# ── 自动初始化（import 时）───────────────────────────────────────────────
def _auto_setup():
    global _SETUP_DONE
    if _SETUP_DONE: return
    _SETUP_DONE = True
    _ASSETS_DIR.mkdir(exist_ok=True)
    if not (_ASSETS_DIR / '01_shadow.png').exists():
        if _ATLAS_PNG.exists():
            _extract_layers()
        else:
            logger.warning(f'[nano_koala] 未找到 atlas，请手动放图层到 {_ASSETS_DIR}')
    try:
        _napp.add_static_files(_STATIC_MOUNT, str(_ASSETS_DIR))
        logger.debug(f'[nano_koala] 静态文件 → {_STATIC_MOUNT}')
    except Exception as e:
        logger.warning(f'[nano_koala] 静态文件挂载失败: {e}')

_auto_setup()

# ── 渲染入口 ─────────────────────────────────────────────────────────────
def render_nano_koala_avatar(state_getter=None, web_ui=None, height: int = 236):
    """
    用法 A（推荐）：render_nano_koala_avatar(state_getter=self._build_koala_state)
    用法 B（兼容）：render_nano_koala_avatar(web_ui=self)
    """
    if state_getter is None and web_ui is not None:
        _u = web_ui
        def state_getter():
            try:
                return {
                    'status':        (_u.status_lbl.text  if _u.status_lbl  else 'SYS_IDLE'),
                    'current_skill': getattr(_u,'_koala_current_skill','') or '',
                    'rag_hit':       bool(_u.rag_lbl      and _u.rag_lbl.text      =='HIT'),
                    'full_file_hit': bool(_u.full_file_lbl and _u.full_file_lbl.text=='HIT'),
                }
            except Exception:
                return {'status':'SYS_IDLE'}
    if state_getter is None:
        state_getter = lambda: {'status':'SYS_IDLE'}

    # 注入 CSS（幂等，重复调用无副作用）
    ui.add_head_html(f'<style>{_CSS}</style>')

    # 占位 div
    uid = f'nkph-{abs(id(state_getter))}'
    ui.html(f'<div id="{uid}"></div>')

    # 一次性 DOM 注入
    js = _build_inject_js(uid)
    ui.timer(0.4, lambda: ui.run_javascript(js), once=True)

    # 状态同步（仅变化时触发）
    _prev = {'k': '', 'model': ''}
    def _sync():
        try:
            s      = state_getter()
            status = (s.get('status') or 'SYS_IDLE')
            # 🪦 2026-08-29：这两行原本各自手工转义（`.replace("'", "\'")` 等）。
            #
            # 🔴 `skill` 那道是**双重转义**：下面已经过 `json.dumps(skill)`，
            #    而 json.dumps 本来就把引号处理对了 —— 手工那道是在**污染输出**
            #    （带引号的名字会多出反斜杠）。技能名是 PascalCase 撞不上，
            #    但 **MCP 工具名会走到这里**（mcp__server__tool 里可能带各种字符）。
            # 🔴 `model` 那道更彻底：它**从没被拼进 JS** —— 下面只拿它做相等比较，
            #    转义纯属死代码。
            # 📌 **一个「看起来是安全措施」的东西，如果没人核过它到底防谁，
            #    下一个人只会在它旁边再加一道** —— 而两道叠起来就开始改内容了。
            skill  = (s.get('current_skill') or '')
            rag    = 'true' if s.get('rag_hit')       else 'false'
            full   = 'true' if s.get('full_file_hit') else 'false'
            model  = (s.get('model') or '')
            k = f'{status}|{skill}|{rag}|{full}'
            if k != _prev['k']:
                _prev['k'] = k
                import json as _json
                _sj = _json.dumps(status); _kj = _json.dumps(skill)
                ui.run_javascript(
                    f"if(window._koalaSetState)"
                    f"window._koalaSetState({_sj},{_kj},{rag},{full});"
                )
            if model and _prev['model'] and model != _prev['model']:
                ui.run_javascript(
                    "if(window._koalaModelChanged) window._koalaModelChanged();"
                )
            if model:
                _prev['model'] = model
        except Exception:
            pass
    ui.timer(0.2, _sync)
