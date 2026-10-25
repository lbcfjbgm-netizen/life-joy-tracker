(() => {
  const btn = [...document.querySelectorAll('button')].find((b) => (b.textContent || '').includes('已完成') || (b.textContent || '').includes('已暂停'));
  if (btn) btn.click();
  return new Promise((resolve) => {
    setTimeout(() => {
      const overlay = document.querySelector('.fixed.inset-0.z-50');
      const dialog = overlay ? overlay.querySelector('div') : null;
      const info = {
        clicked: !!btn,
        overlayPresent: !!overlay,
        roleDialog: document.querySelectorAll('[role="dialog"]').length,
        ariaModal: document.querySelectorAll('[aria-modal]').length,
        focusTrapped: false,
        activeElement: document.activeElement ? document.activeElement.tagName + ':' + (document.activeElement.textContent || '').trim().slice(0, 20) : null,
        dialogFields: [],
        dialogButtons: [],
        labels: 0,
        inputsWithoutLabel: 0,
      };
      if (dialog) {
        info.dialogFields = [...dialog.querySelectorAll('input,select,textarea')].map((e) => ({
          name: e.name, type: e.type || e.tagName, hasLabel: !!document.querySelector('label[for="' + e.id + '"]'), ariaLabel: e.getAttribute('aria-label'), placeholder: e.getAttribute('placeholder'),
        }));
        info.dialogButtons = [...dialog.querySelectorAll('button')].map((b) => (b.textContent || '').trim().slice(0, 16)).filter(Boolean);
        info.labels = dialog.querySelectorAll('label').length;
        info.inputsWithoutLabel = info.dialogFields.filter((f) => !f.hasLabel && !f.ariaLabel).length;
      }
      info.bodyScrollLocked = getComputedStyle(document.body).overflow;
      resolve(info);
    }, 3500);
  });
})()
