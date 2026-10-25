(() => {
  const cv = document.createElement('canvas');
  cv.width = 1; cv.height = 1;
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  function toRGB(c) {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = c;
    ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2], d[3]];
  }
  function lum(rgb) {
    const m = rgb.slice(0, 3).map((v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2];
  }
  function effBg(el) {
    let n = el;
    while (n && n !== document.documentElement) {
      const rgb = toRGB(getComputedStyle(n).backgroundColor);
      if (rgb[3] > 0.5) return getComputedStyle(n).backgroundColor;
      n = n.parentElement;
    }
    return getComputedStyle(document.body).backgroundColor;
  }
  const out = [];
  document.querySelectorAll('main *, aside *').forEach((el) => {
    const txt = (el.textContent || '').trim();
    if (!txt || el.children.length) return;
    if (el.offsetParent === null) return;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return;
    const b = effBg(el);
    const l1 = lum(toRGB(s.color));
    const l2 = lum(toRGB(b));
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const px = parseFloat(s.fontSize);
    const bold = parseInt(s.fontWeight, 10) >= 700;
    const large = px >= 24 || (px >= 18.66 && bold);
    const need = large ? 3.0 : 4.5;
    if (ratio < need) out.push({ t: txt.slice(0, 20), color: s.color, bg: b, fs: s.fontSize, ratio: +ratio.toFixed(2), need });
  });
  return { theme: document.documentElement.getAttribute('data-theme'), lowCount: out.length, sample: out.slice(0, 15) };
})()
