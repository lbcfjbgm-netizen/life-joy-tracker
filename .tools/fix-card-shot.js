// 只读截图 + DOM 测量：fix-card-*.png。不写数据库、不改站点。
const http = require('http');
const fs = require('fs');

function getJSON(path) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: 9222, path }, (r) => {
      let d = '';
      r.on('data', (c) => (d += c));
      r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(e); } });
    }).on('error', rej);
  });
}

class CDP {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.id = 0;
    this.pending = new Map();
    this.ws.addEventListener('message', (e) => {
      const msg = JSON.parse(e.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      }
    });
  }
  ready() { return new Promise((res) => { if (this.ws.readyState === 1) return res(); this.ws.addEventListener('open', () => res()); }); }
  send(method, params = {}) { const id = ++this.id; return new Promise((resolve, reject) => { this.pending.set(id, { resolve, reject }); this.ws.send(JSON.stringify({ id, method, params })); }); }
  close() { this.ws.close(); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const [{ webSocketDebuggerUrl }] = (await getJSON('/json/list')).filter((t) => t.type === 'page');
  const cdp = new CDP(webSocketDebuggerUrl);
  await cdp.ready();
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  const evalJS = async (expr) => {
    const r = await cdp.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return { ERR: r.exceptionDetails.text, detail: r.exceptionDetails.exception && r.exceptionDetails.exception.description };
    return r.result.value;
  };

  for (const job of cfg.jobs) {
    console.log('\n=== ' + job.out);
    if (job.w) {
      await cdp.send('Emulation.setDeviceMetricsOverride', { width: job.w, height: job.h, deviceScaleFactor: 1, mobile: !!job.mobile });
    }
    if (job.reload) {
      await cdp.send('Page.reload', { ignoreCache: true });
      await sleep(job.wait || 5000);
    } else if (job.url) {
      await cdp.send('Page.navigate', { url: job.url });
      await sleep(job.wait || 6500);
    }
    for (const step of (job.actions || [])) {
      if (step.eval) { console.log('  eval -> ' + JSON.stringify(await evalJS(step.eval))); }
      if (step.click) {
        const r = await evalJS(`(() => { const el = document.querySelectorAll(${JSON.stringify(step.click)})[${step.n || 0}]; if (!el) return 'NOTFOUND'; el.scrollIntoView({block:'center'}); el.click(); return 'clicked'; })()`);
        console.log('  click ' + step.click + ' -> ' + JSON.stringify(r));
      }
      if (step.wait) await sleep(step.wait);
    }
    if (job.probe) console.log('  PROBE ' + JSON.stringify(await evalJS(job.probe), null, 1));
    if (job.shot) {
      const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false, fromSurface: true });
      fs.writeFileSync(job.shot, Buffer.from(shot.data, 'base64'));
      console.log('  SHOT -> ' + job.shot);
    }
  }
  cdp.close();
}
main().catch((e) => { console.error('ERR', e.message); process.exit(1); });
