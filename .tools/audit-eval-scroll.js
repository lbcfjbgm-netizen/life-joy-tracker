(() => {
  const cards = document.querySelectorAll('.media-grid > div').length;
  const indicator = document.querySelector('#loading-indicator');
  const totalTxt = document.body.textContent.match(/(\d[\d,]*)\s*条/);
  return {
    cardsLoaded: cards,
    hasNextPageLoader: indicator ? getComputedStyle(indicator).display : 'missing',
    scrollY: window.scrollY,
    docHeight: document.documentElement.scrollHeight,
    viewportH: window.innerHeight,
    countBadges: totalTxt ? totalTxt[0] : null,
    paginationControls: document.querySelectorAll('[href*="page="]').length,
    backToTop: document.querySelectorAll('[href="#top"]').length,
  };
})()
