// ui-common.js - overlay de carga (spinner) y modales de aviso/confirmación
// reemplazan al alert()/confirm() nativo del navegador en toda la app.
(function () {
  function ensureLoadingOverlay() {
    let el = document.getElementById('app-loading-overlay');
    if (!el) {
      el = document.createElement('div');
      el.id = 'app-loading-overlay';
      el.innerHTML = '<div class="app-spinner"></div>';
      document.body.appendChild(el);
    }
    return el;
  }

  function ensureModalOverlay() {
    let el = document.getElementById('app-modal-overlay');
    if (!el) {
      el = document.createElement('div');
      el.id = 'app-modal-overlay';
      document.body.appendChild(el);
    }
    return el;
  }

  let loadingCount = 0;

  window.showLoading = function () {
    loadingCount++;
    ensureLoadingOverlay().classList.add('show');
  };

  window.hideLoading = function (force) {
    loadingCount = force ? 0 : Math.max(0, loadingCount - 1);
    if (loadingCount === 0) {
      const el = document.getElementById('app-loading-overlay');
      if (el) el.classList.remove('show');
    }
  };

  const ICONS = { info: 'i', success: '✓', error: '!', question: '?' };

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.innerText = str == null ? '' : String(str);
    return div.innerHTML;
  }

  function openModal({ title, message, type, buttons }) {
    return new Promise((resolve) => {
      const overlay = ensureModalOverlay();
      const icon = ICONS[type] || ICONS.info;
      const btnsHtml = buttons
        .map(
          (b, i) =>
            `<button type="button" data-i="${i}" class="${b.primary ? 'app-modal-btn-primary' : 'app-modal-btn-secondary'}">${escapeHtml(b.label)}</button>`
        )
        .join('');

      overlay.innerHTML = `
        <div class="app-modal-card">
          <div class="app-modal-icon ${type}">${icon}</div>
          ${title ? `<div class="app-modal-title">${escapeHtml(title)}</div>` : ''}
          <div class="app-modal-msg">${escapeHtml(message)}</div>
          <div class="app-modal-actions">${btnsHtml}</div>
        </div>`;

      overlay.classList.add('show');

      const cleanup = (value) => {
        overlay.classList.remove('show');
        overlay.innerHTML = '';
        resolve(value);
      };

      overlay.querySelectorAll('button').forEach((btn) => {
        btn.onclick = () => cleanup(buttons[parseInt(btn.dataset.i, 10)].value);
      });
    });
  }

  window.appAlert = function (message, opts = {}) {
    return openModal({
      title: opts.title || '',
      message,
      type: opts.type || 'info',
      buttons: [{ label: opts.okText || 'Aceptar', primary: true, value: true }],
    });
  };

  window.appConfirm = function (message, opts = {}) {
    return openModal({
      title: opts.title || '',
      message,
      type: opts.type || 'question',
      buttons: [
        { label: opts.cancelText || 'Cancelar', primary: false, value: false },
        { label: opts.okText || 'Confirmar', primary: true, value: true },
      ],
    });
  };
})();
