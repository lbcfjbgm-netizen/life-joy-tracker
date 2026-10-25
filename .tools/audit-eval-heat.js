(() => {
  const cells=[...document.querySelectorAll('div')].filter(d=>(d.className||'').toString().includes('rounded-sm')&&(d.className||'').toString().includes('w-4'));
  const byCls={};
  cells.forEach(c=>{const k=(c.className||'').toString().split(' ').find(x=>x.startsWith('bg-'))||'?';byCls[k]=(byCls[k]||0)+1;});
  const first=cells[0], last=cells[cells.length-1];
  return {cells:cells.length,byCls,firstTitle:first?first.getAttribute('title'):null,lastTitle:last?last.getAttribute('title'):null,
   todayCell: cells.filter(c=>(c.className||'').toString().match(/indigo/)).length};
})()
