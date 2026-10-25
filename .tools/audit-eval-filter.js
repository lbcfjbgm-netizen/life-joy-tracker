(() => {
  const statusBtn = [...document.querySelectorAll('button')].find((b) => {
    const t = (b.textContent || '').trim();
    return t === '已完成' || t === '全部' || t === '已暂停';
  });
  const sortBtn = [...document.querySelectorAll('button')].find((b) => (b.textContent || '').includes('评分'));
  const out = {
    statusLabel: statusBtn ? statusBtn.textContent.trim() : null,
    sortLabel: sortBtn ? sortBtn.textContent.trim() : null,
    statusHasAriaExpanded: statusBtn ? statusBtn.getAttribute('aria-expanded') : null,
    statusHasAriaHaspopup: statusBtn ? statusBtn.getAttribute('aria-haspopup') : null,
    layoutButtons: [...document.querySelectorAll('a[title="Grid View"],a[title="Table View"]')].map((a) => ({
      title: a.getAttribute('title'), aria: a.getAttribute('aria-label'), ariaPressed: a.getAttribute('aria-pressed'),
    })),
    countText: (document.body.textContent.match(/\d+\s*条/) || [null])[0],
    resultCountShown: /共\s*\d+|显示\s*\d+|\d+\s*个/.test(document.body.textContent),
    searchPlaceholder: (document.querySelector('input[type="text"]') || {}).placeholder,
    hasClearSearch: !!document.querySelector('[aria-label*="清除"],[aria-label*="clear"]'),
    urlState: location.search,
  };
  return out;
})()
