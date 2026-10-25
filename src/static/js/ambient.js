/* ==========================================================================
   ambient.js —— 列表页「横版背景大图」环境层（第 1 批）

   只在「列表页（.lib-stage / .lj-stage 存在）+ 首张卡片 data-image 能抠到 Steam
   appid」时启用；其余页面一个类都不加、一个节点都不插。
   背景图规则：https://media.steampowered.com/steam/apps/<appid>/library_hero.jpg
   约 27% 游戏没有 library_hero.jpg：加载失败就把图藏掉，只留纯色底 + 光晕。

   公开 API（第 2 波代理要用，名字和参数不许改）：
     window.LJ_AMBIENT = {
       setArt(urlOrNull),      // 传 null / 空 时退回纯色底
       setConcentration(n),    // 0~1
       enabled: true/false
     }
   联动事件：document 上监听 'lj:focus'，e.detail.image 传竖版封面 URL。
   ========================================================================== */
(function () {
  'use strict';

  if (window.LJ_AMBIENT && window.LJ_AMBIENT.__built) return;

  var HERO_PREFIX = 'https://media.steampowered.com/steam/apps/';
  var HERO_SUFFIX = '/library_hero.jpg';
  var APPID_RE = /steam\/apps\/(\d+)/;
  var STATE_CLASS = 'lj-ambient-on';

  /* 背景大图走本站 /image_relay：Steam 那张 library_hero.jpg 直连要 1.4 秒
     （DNS 1ms + TCP 14ms + TLS/首字节 1417ms），而 relay 热缓存只要 28~88ms。
     relay 已放行 media.steampowered.com（app/image_relay.py 的 CACHE_ONLY_HOSTS），
     取不到时 relay 会 302 回原始地址，所以兜底链还是通的。 */
  var RELAY_PATH = '/image_relay?url=';
  function relayUrl(raw) {
    return RELAY_PATH + encodeURIComponent(raw);
  }

  var root = document.documentElement;
  var state = {
    enabled: false,
    concentration: null,
    art: null,
    appid: null,
    loadToken: 0,
  };
  var els = null;

  /* ---- 判断：当前页是不是列表页 ------------------------------------------ */
  function listPage() {
    return !!document.querySelector('.lib-stage, .lj-stage');
  }

  /* ---- 从首张卡片 data-image 抠 appid ------------------------------------ */
  function detectAppid() {
    var card = document.querySelector('.libcard[data-image]');
    if (!card) return null;
    var m = APPID_RE.exec(card.getAttribute('data-image') || '');
    return m ? m[1] : null;
  }

  /* ---- 首张卡片的封面地址（书/影/剧走这条） ------------------------------ */
  function detectCover() {
    var card = document.querySelector('.libcard[data-image]');
    if (!card) return null;
    return normalizeCover(card.getAttribute('data-image'));
  }

  /* 封面地址过滤：空值、IMG_NONE 占位 SVG、已知的破图占位一律当没有 */
  function normalizeCover(url) {
    if (!url || typeof url !== 'string') return null;
    var u = url.trim();
    if (!u) return null;
    if (/themoviedb\.org\/assets\/.*glyphicons/i.test(u)) return null;   // IMG_NONE 占位图
    if (!/^(https?:\/\/|\/)/i.test(u)) return null;                      // 只认绝对地址和站内相对路径
    return u;
  }

  /* ---- 解析传入的 URL，返回 { src, mode } --------------------------------
     横版模式（mode='wide'）：Steam appid → library_hero.jpg，游戏页专用
     竖版模式（mode='cover'）：其它 http(s) 绝对地址 / 站内相对路径，书影剧专用
     返回 null 表示这张图用不了，退回纯色底。
     ---------------------------------------------------------------------- */
  function resolveArt(url) {
    if (!url || typeof url !== 'string') return null;
    var m = APPID_RE.exec(url);
    if (m) {
      // 传进来的已经是 hero 地址（原始 Steam URL）就原样复用，不要再拼一次；
      // 其它情况按 appid 拼 hero 原始地址。最终交给浏览器加载的只有 relay 地址。
      var raw = /library_hero\.jpg/i.test(url)
        ? url
        : HERO_PREFIX + m[1] + HERO_SUFFIX;
      return { src: relayUrl(raw), mode: 'wide' };                     // 竖版封面 → 横版 hero
    }
    var cover = normalizeCover(url);
    if (cover) return { src: cover, mode: 'cover' };                    // 原样用，竖版模糊铺满
    return null;
  }

  /* ---- 保留旧签名：只返回 URL，给内部判定用 ------------------------------ */
  function resolveSrc(url) {
    var r = resolveArt(url);
    return r ? r.src : null;
  }

  /* ---- 建节点 ------------------------------------------------------------- */
  function build() {
    if (els) return els;
    var wrap = document.createElement('div');
    wrap.className = 'ambient';
    wrap.setAttribute('aria-hidden', 'true');

    var art = document.createElement('div');
    art.className = 'ambient-art';

    var img = document.createElement('img');
    img.alt = '';
    img.decoding = 'async';
    img.setAttribute('aria-hidden', 'true');
    art.appendChild(img);

    var glow = document.createElement('div');
    glow.className = 'ambient-glow';

    var fade = document.createElement('div');
    fade.className = 'ambient-fade';

    var grain = document.createElement('div');
    grain.className = 'ambient-grain';

    wrap.appendChild(art);
    wrap.appendChild(glow);
    wrap.appendChild(fade);
    wrap.appendChild(grain);

    // 必须是 body 的第一个子节点：这样它画的层级在 main / 卡片之下
    if (document.body.firstChild) document.body.insertBefore(wrap, document.body.firstChild);
    else document.body.appendChild(wrap);

    els = { wrap: wrap, art: art, img: img, glow: glow, fade: fade, grain: grain };
    return els;
  }

  function destroy() {
    if (els && els.wrap && els.wrap.parentNode) els.wrap.parentNode.removeChild(els.wrap);
    els = null;
    state.art = null;
    state.appid = null;
    state.loadToken++;
  }

  function applyConcentration() {
    if (state.concentration === null) {
      root.style.removeProperty('--lj-amb-alpha');
    } else {
      root.style.setProperty('--lj-amb-alpha', String(state.concentration));
    }
  }

  function enable() {
    if (state.enabled) return;
    state.enabled = true;
    root.classList.add(STATE_CLASS);
    applyConcentration();
  }

  function disable() {
    state.enabled = false;
    root.classList.remove(STATE_CLASS);
    destroy();
  }

  /* ---- 设图（公开 API） --------------------------------------------------- */
  function setArt(urlOrNull) {
    if (!listPage()) return false;
    var art = resolveArt(urlOrNull);
    build();
    enable();

    var token = ++state.loadToken;

    if (!art) {
      // null / 抠不到可用地址：退回纯色底 + 光晕，不加载任何图
      state.art = null;
      state.appid = null;
      els.wrap.classList.remove('is-cover');
      els.wrap.classList.add('no-art');
      els.art.classList.remove('on');
      els.img.removeAttribute('src');
      return true;
    }

    // 图源模式跟着走：Steam appid → 横版，其它封面 → 竖版模糊
    if (art.mode === 'cover') els.wrap.classList.add('is-cover');
    else els.wrap.classList.remove('is-cover');

    if (art.src === state.art) return true;   // 同一张不重载

    var probe = new Image();
    probe.onload = function () {
      if (token !== state.loadToken) return;
      state.art = art.src;
      els.img.src = art.src;
      els.wrap.classList.remove('no-art');
      els.art.classList.add('on');
    };
    probe.onerror = function () {
      if (token !== state.loadToken) return;
      // 没有大图：藏图，只留纯色底 + 光晕（不出破图、不塌黑块）
      state.art = null;
      state.appid = null;
      els.wrap.classList.add('no-art');
      els.art.classList.remove('on');
      els.img.removeAttribute('src');
    };
    probe.src = art.src;
    return true;
  }

  /* ---- 浓度（公开 API） --------------------------------------------------- */
  function setConcentration(n) {
    if (n === null || n === undefined || n === '') {
      state.concentration = null;
    } else {
      var v = Number(n);
      if (isNaN(v)) return;
      state.concentration = Math.max(0, Math.min(1, v));
    }
    applyConcentration();
  }

  /* ---- 初始化 / 软导航后重算 --------------------------------------------- */
  var timer = null;
  function sync(delay) {
    if (timer) { clearTimeout(timer); timer = null; }
    var run = function () {
      timer = null;
      if (!listPage()) {                 // 首页 / 统计 / 日志 / 搜索 / 列表 / 日历 / 详情
        disable();
        return;
      }
      // ① 首卡抠得到 Steam appid → 横版模式（游戏，行为与上一批完全一致）
      var appid = detectAppid();
      if (appid) {
        if (state.appid !== appid || !state.art) {
          state.appid = appid;
          setArt(HERO_PREFIX + appid + HERO_SUFFIX);   // 原始 Steam URL 入参，setArt 内部换 relay
        } else {
          enable();
          build();
          els.wrap.classList.remove('is-cover');
          els.wrap.classList.remove('no-art');
          els.art.classList.add('on');
        }
        return;
      }
      // ② 抠不到 appid，但首卡有能用的封面（书籍 / 电影 / 剧集）→ 竖版模糊模式
      var cover = detectCover();
      if (cover) {
        setArt(cover);
        return;
      }
      // ③ 两样都没有：这张列表页不启用
      disable();
    };
    if (delay) timer = setTimeout(run, delay);
    else run();
  }

  /* ---- 公开 API ----------------------------------------------------------- */
  var api = {
    __built: true,
    setArt: setArt,
    setConcentration: setConcentration,
  };
  Object.defineProperty(api, 'enabled', {
    enumerable: true,
    get: function () { return state.enabled; },
    set: function (v) { if (v) { build(); enable(); } else { disable(); } },
  });
  window.LJ_AMBIENT = api;

  /* ---- 与「首卡特写」那批联动 -------------------------------------------- */
  document.addEventListener('lj:focus', function (e) {
    var img = e && e.detail && e.detail.image;
    if (img) window.LJ_AMBIENT.setArt(img);
  });

  /* ---- 软导航（hx-boost 只换 #main-content）后重算 ----------------------- */
  document.addEventListener('htmx:afterSettle', function () { sync(80); });
  document.addEventListener('htmx:afterSwap', function () { sync(80); });
  window.addEventListener('popstate', function () { sync(80); });

  function boot() { sync(0); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
