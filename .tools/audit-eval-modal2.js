(() => {
  const btn = [...document.querySelectorAll('button')].find((b) => {
    const t = (b.textContent || '').trim();
    return t === '已完成' || t === '已暂停' || t === '进行中';
  });
  if (!btn) return { clicked: false };
  btn.click();
  return new Promise((resolve) => {
    let tries = 0;
    const tick = () => {
      tries += 1;
      const overlay = [...document.querySelectorAll('div')].find((d) =>
        (d.className || '').toString().includes('fixed inset-0') && (d.className || '').toString().includes('z-50'));
      const inputs = overlay ? overlay.querySelectorAll('input,select,textarea') : [];
      if ((inputs.length > 0 || tries > 24)) {
        const info = {
          clicked: true,
          tries,
          overlay: !!overlay,
          inputCount: inputs.length,
          fields: [...inputs].map((e) => ({
            tag: e.tagName,
            type: e.type || '', name: e.name || '',
            id: e.id || '',
            hasLabel: !!document.querySelector('label[for="' + e.id + '"]'),
            ariaLabel: e.getAttribute('aria-label'),
            placeholder: e.getAttribute('placeholder'),
            value: (e.value || '').slice(0, 20),
          })),
          buttons: overlay ? [...overlay.querySelectorAll('button')].map((b) => (b.textContent || '').trim().slice(0, 14)).filter(Boolean) : [],
          roleDialog: document.querySelectorAll('[role="dialog"]').length,
          ariaModal: document.querySelectorAll('[aria-modal]').length,
          activeEl: document.activeElement ? document.activeElement.tagName : null,
          bodyOverflow: getComputedStyle(document.body).overflow,
          scrollY: window.scrollY,
        };
        resolve(info);
        return;
      }
      setTimeout(tick, 300);
    };
    setTimeout(tick, 400);
  });
})()
