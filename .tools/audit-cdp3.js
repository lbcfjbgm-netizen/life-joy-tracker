const http = require('http');
const { execSync } = require('child_process');

function getJSON(path) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: 9222, path }, (r) => {
      let d = '';
      r.on('data', (c) => (d += c));
      r.on('end', () => res(JSON.parse(d)));
    }).on('error', rej);
  });
}

(async () => {
  const pages = (await getJSON('/json/list')).filter((t) => t.type === 'page');
  const wsUrl = pages[0].webSocketDebuggerUrl;
  const ws = new WebSocket(wsUrl);
  await new Promise((r) => ws.addEventListener('open', () => r()));
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  const send = (method, params = {}) => new Promise((resolve) => {
    const i = ++id;
    pending.set(i, resolve);
    ws.send(JSON.stringify({ id: i, method, params }));
  });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const targets = [
    { url: 'http://127.0.0.1:8001/admin/game?layout=table', w: 375, h: 812, mobile: true },
    { url: 'http://127.0.0.1:8001/', w: 375, h: 812, mobile: true },
  ];
  for (const t of targets) {
    await send('Emulation.setDeviceMetricsOverride', { width: t.w, height: t.h, deviceScaleFactor: 1, mobile: t.mobile });
    await send('Page.navigate', { url: t.url });
    await sleep(6000);
    const expr = `(() => {
      const out = { url: location.href };
      const tbl = document.querySelector('table');
      if (tbl) {
        const wrap = tbl.parentElement;
        out.table = { w: Math.round(tbl.getBoundingClientRect().width), wrapClientW: wrap.clientWidth, wrapScrollW: wrap.scrollWidth, hScroll: wrap.scrollWidth > wrap.clientWidth };
      }
      out.docScrollW = document.documentElement.scrollWidth;
      const els = [...document.querySelectorAll('button,a')].filter(e => e.offsetParent !== null);
      const small = els.filter(e => { const r = e.getBoundingClientRect(); return r.height > 0 && r.width > 0 && (r.height < 44 || r.width < 44); });
      out.touch = { total: els.length, under44: small.length, worst: small.slice(0, 8).map(e => ({ t: (e.textContent || '').trim().slice(0, 10) || e.tagName, w: Math.round(e.getBoundingClientRect().width), h: Math.round(e.getBoundingClientRect().height) })) };
      return out;
    })()`;
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
    console.log('### ' + t.url + '\n' + JSON.stringify(r.result.result.value, null, 1));
  }
  ws.close();
})();
