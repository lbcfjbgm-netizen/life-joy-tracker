// 只读探测：量可用高度 / 无写库操作
const http = require('http');
const fs = require('fs');
function getJSON(path) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: 9222, path }, (r) => {
      let d = ''; r.on('data', (c) => (d += c)); r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(e); } });
    }).on('error', rej);
  });
}
class CDP {
  constructor(wsUrl) { this.ws = new WebSocket(wsUrl); this.id = 0; this.pending = new Map();
    this.ws.addEventListener('message', (e) => { const m = JSON.parse(e.data);
      if (m.id && this.pending.has(m.id)) { const { resolve, reject } = this.pending.get(m.id); this.pending.delete(m.id);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result); } }); }
  ready() { return new Promise((res) => { if (this.ws.readyState === 1) return res(); this.ws.addEventListener('open', () => res()); }); }
  send(method, params = {}) { const id = ++this.id; return new Promise((resolve, reject) => { this.pending.set(id, { resolve, reject }); this.ws.send(JSON.stringify({ id, method, params })); }); }
  close() { this.ws.close(); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function main() {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const [{ webSocketDebuggerUrl }] = (await getJSON('/json/list')).filter((t) => t.type === 'page');
  const cdp = new CDP(webSocketDebuggerUrl); await cdp.ready();
  await cdp.send('Page.enable'); await cdp.send('Runtime.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: cfg.w || 1600, height: cfg.h || 1000, deviceScaleFactor: 1, mobile: false });
  const evalJS = async (expr) => { const r = await cdp.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return { ERR: r.exceptionDetails.text }; return r.result.value; };
  for (const job of cfg.jobs) {
    console.log('\n=== ' + job.name);
    if (job.theme) {
      await cdp.send('Page.navigate', { url: 'http://127.0.0.1:8001/' }); await sleep(2500);
      await evalJS(`localStorage.setItem('yamtrack-theme','${job.theme}')`);
    }
    if (job.url) { await cdp.send('Page.navigate', { url: job.url }); await sleep(job.wait || 4500); }
    for (const step of (job.actions || [])) {
      if (step.eval) console.log('  eval -> ' + JSON.stringify(await evalJS(step.eval)));
      if (step.click) console.log('  click ' + step.click + ' -> ' + JSON.stringify(await evalJS(`(()=>{const e=document.querySelector(${JSON.stringify(step.click)}); if(!e) return 'NOTFOUND'; e.click(); return 'ok';})()`)));
      if (step.wait) await sleep(step.wait);
    }
    if (job.probe) console.log('  PROBE ' + JSON.stringify(await evalJS(job.probe), null, 1));
    if (job.shot) { const s = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: !!job.full, fromSurface: true });
      fs.writeFileSync(job.shot, Buffer.from(s.data, 'base64')); console.log('  SHOT -> ' + job.shot); }
  }
  cdp.close();
}
main().catch((e) => { console.error('ERR', e.message); process.exit(1); });
