(() => {
  const burger = document.querySelector('button.lg\\:hidden') || [...document.querySelectorAll('button')].find((b) => (b.className || '').toString().includes('lg:hidden'));
  if (burger) burger.click();
  return new Promise((resolve) => {
    setTimeout(() => {
      const aside = document.querySelector('aside');
      const r = aside.getBoundingClientRect();
      const links = [...aside.querySelectorAll('a')].map((a) => {
        const rr = a.getBoundingClientRect();
        return { txt: (a.textContent || '').trim().slice(0, 10), h: Math.round(rr.height) };
      });
      resolve({
        burgerFound: !!burger,
        asideLeft: Math.round(r.left),
        asideWidth: Math.round(r.width),
        asideVisible: r.left >= 0,
        hasCloseButton: !!aside.querySelector('button'),
        escTrap: false,
        overlayClickCloses: !!document.querySelector('.fixed.inset-0.bg-black\\/50'),
        linkHeights: links.slice(0, 8),
        drawerCoversContent: r.width >= 300,
      });
    }, 900);
  });
})()
