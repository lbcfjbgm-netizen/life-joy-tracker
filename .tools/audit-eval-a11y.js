(() => {
  const res = { url: location.href, title: document.title };
  const focusables = [...document.querySelectorAll('a[href],button:not([disabled]),input:not([type=hidden]),select,textarea,[tabindex]:not([tabindex="-1"])')]
    .filter((e) => e.offsetParent !== null);
  res.focusableCount = focusables.length;
  res.firstTabs = focusables.slice(0, 14).map((e) => ({
    tag: e.tagName,
    txt: (e.textContent || e.getAttribute('placeholder') || e.getAttribute('aria-label') || '').trim().slice(0, 16),
    tabIndex: e.tabIndex,
  }));
  const noLabel = focusables.filter((e) => {
    const t = (e.textContent || '').trim();
    const al = e.getAttribute('aria-label');
    const lb = e.getAttribute('title');
    const ph = e.getAttribute('placeholder');
    return !t && !al && !lb && !ph;
  });
  res.iconOnlyNoLabel = noLabel.length;
  res.iconOnlySample = noLabel.slice(0, 8).map((e) => ({
    tag: e.tagName, cls: (e.className || '').toString().slice(0, 40), html: e.outerHTML.slice(0, 90),
  }));
  res.h1Count = document.querySelectorAll('h1').length;
  res.h1s = [...document.querySelectorAll('h1')].map((h) => h.textContent.trim().slice(0, 30));
  res.landmarks = {
    main: document.querySelectorAll('main').length,
    nav: document.querySelectorAll('nav').length,
    aside: document.querySelectorAll('aside').length,
    skipLink: !!document.querySelector('a[href^="#main"]'),
  };
  res.liveRegions = document.querySelectorAll('[aria-live],[role=status],[role=alert]').length;
  res.ariaLiveDetail = [...document.querySelectorAll('[aria-live],[role=status],[role=alert]')].map((e) => ({
    tag: e.tagName, live: e.getAttribute('aria-live'), role: e.getAttribute('role'), id: e.id,
  })).slice(0, 6);
  const btns = [...document.querySelectorAll('button')];
  res.buttonsNoType = btns.filter((b) => !b.getAttribute('type')).length;
  res.imgNoAlt = [...document.querySelectorAll('img')].filter((i) => !i.hasAttribute('alt')).length;
  res.imgAltEmpty = [...document.querySelectorAll('img')].filter((i) => i.getAttribute('alt') === '').length;
  res.imgTotal = document.querySelectorAll('img').length;
  return res;
})()
