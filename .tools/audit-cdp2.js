const fs = require('fs');
const http = require('http');

function getJSON(path) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: 9222, path }, (r) => {
      let d = '';
      r.on('data', (c) => (d += c));
      r.on('end', () => res(JSON.parse(d)));
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
  ready() {
    return new Promise((res) => {
      if (this.ws.readyState === 1) return res();
      this.ws.addEventListener('open', () => res());
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  close() { this.ws.close(); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const [{ webSocketDebuggerUrl }] = (await getJSON('/json/list')).filter((t) => t.type === 'page');
  const cdp = new CDP(webSocketDebuggerUrl);
  await cdp.ready();
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');

  const jobs = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

  for (const job of jobs) {
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: job.w, height: job.h, deviceScaleFactor: 1, mobile: !!job.mobile,
    });
    await cdp.send('Page.navigate', { url: job.url });
    await sleep(job.wait || 6000);
    if (job.scroll) {
      await cdp.send('Runtime.evaluate', { expression: 'window.scrollTo(0, document.body.scrollHeight)' });
      await sleep(2500);
      await cdp.send('Runtime.evaluate', { expression: 'window.scrollTo(0,0)' });
      await sleep(1200);
    }
    if (job.evalFile) {
      const expression = fs.readFileSync(job.evalFile, 'utf8');
      const r = await cdp.send('Runtime.evaluate', {
        expression, returnByValue: true, awaitPromise: true,
      });
      console.log('### ' + job.name + ' EVAL\n' + JSON.stringify(r.result.value, null, 1));
      if (r.exceptionDetails) console.log('EXC', JSON.stringify(r.exceptionDetails).slice(0, 400));
    }
    if (job.shot) {
      const shot = await cdp.send('Page.captureScreenshot', {
        format: 'png', captureBeyondViewport: !!job.full, fromSurface: true,
      });
      fs.writeFileSync(job.shot, Buffer.from(shot.data, 'base64'));
      console.log('### ' + job.name + ' SHOT -> ' + job.shot);
    }
  }
  cdp.close();
}

main().catch((e) => { console.error('ERR', e.message); process.exit(1); });
