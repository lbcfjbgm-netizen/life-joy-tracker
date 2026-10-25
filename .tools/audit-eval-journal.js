(() => {
  const imgs=[...document.querySelectorAll('img')];
  const broken=imgs.filter(i=>i.complete&&i.naturalWidth===0);
  const heat=[...document.querySelectorAll('svg rect,.heatmap rect,[class*=heat] rect')];
  const filled=heat.filter(r=>{const f=r.getAttribute('fill')||'';return f && !/gray|#2|#3|transparent|none/i.test(f);});
  return {
    imgTotal: imgs.length,
    broken: broken.length,
    brokenSrc: broken.slice(0,8).map(i=>(i.currentSrc||i.src||'').slice(0,90)),
    brokenAlt: broken.slice(0,8).map(i=>i.getAttribute('alt')),
    heatCells: heat.length,
    heatFilled: filled.length,
    heatFills: [...new Set(heat.map(r=>r.getAttribute('fill')))].slice(0,8),
    journalCount: (document.body.textContent.match(/追踪记录\s*(\d+)/)||[])[1]
  };
})()
