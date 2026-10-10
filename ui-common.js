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

  // ---------- Dictado por voz en cajas de texto ----------
  // Igual que el micrófono del asistente (chatbot.js): reconocimiento del
  // navegador, sin coste. Basta con poner data-dictado en un <textarea> para
  // que aparezca un botón de micrófono en su esquina. Se sigue escuchando
  // aunque haya pausas hasta pulsar otra vez el botón.
  const Reconocimiento = window.SpeechRecognition || window.webkitSpeechRecognition;
  // En Android el modo continuo repite frases; allí se usan frases sueltas
  // que se encadenan reiniciando el reconocimiento tras cada pausa.
  const CONTINUO = !/Android/i.test(navigator.userAgent);
  const ICONO_MIC = (color) => `<svg viewBox="0 0 24 24" fill="none"><rect x="9" y="3" width="6" height="11" rx="3" stroke="${color}" stroke-width="1.8"/><path d="M5.5 11a6.5 6.5 0 0013 0M12 17.5V21" stroke="${color}" stroke-width="1.8" stroke-linecap="round"/></svg>`;
  let dictadoActivo = null; // solo se dicta en una caja a la vez

  function activarDictado(textarea) {
    if (textarea.dataset.dictadoListo) return;
    textarea.dataset.dictadoListo = '1';

    const wrap = document.createElement('div');
    wrap.className = 'dictado-wrap';
    // El margen inferior pasa al contenedor para que el botón quede dentro de la caja.
    wrap.style.marginBottom = getComputedStyle(textarea).marginBottom;
    textarea.style.marginBottom = '0';
    textarea.parentNode.insertBefore(wrap, textarea);
    wrap.appendChild(textarea);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'dictado-btn';
    wrap.appendChild(btn);

    let rec = null;
    let grabando = false;

    function estilo(activo) {
      btn.classList.toggle('grabando', activo);
      btn.innerHTML = ICONO_MIC(activo ? '#fff' : '#10263B');
      btn.title = activo ? 'Parar de dictar' : 'Dictar por voz';
    }

    function parar() {
      grabando = false;
      if (rec) { try { rec.stop(); } catch (e) {} }
      if (dictadoActivo === parar) dictadoActivo = null;
      estilo(false);
    }

    function escuchar() {
      const base = textarea.value.trim();
      rec = new Reconocimiento();
      rec.lang = 'es-ES';
      rec.interimResults = true;
      rec.continuous = CONTINUO;
      rec.onresult = e => {
        // Si la caja se ha ocultado (p. ej. se cerró el modal), se deja de escuchar.
        if (!textarea.offsetParent) { parar(); return; }
        // Se recompone todo lo oído en esta sesión para no duplicar frases.
        let oido = '';
        for (let i = 0; i < e.results.length; i++) oido += e.results[i][0].transcript;
        textarea.value = `${base} ${oido.trim()}`.trim();
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
      };
      rec.onerror = e => {
        if (e.error === 'no-speech' || e.error === 'aborted') return; // pausa: se reinicia en onend
        parar();
        window.appAlert(e.error === 'not-allowed' || e.error === 'service-not-allowed'
          ? 'Permite el acceso al micrófono para poder dictar.'
          : `Error del micrófono: ${e.error}`, { type: 'error' });
      };
      rec.onend = () => {
        // El navegador corta tras un silencio: si no se ha pulsado parar, se sigue escuchando.
        if (grabando && textarea.offsetParent) setTimeout(() => { if (grabando) escuchar(); }, 250);
        else parar();
      };
      rec.start();
    }

    btn.addEventListener('click', () => {
      if (!Reconocimiento) {
        window.appAlert('Este navegador no permite dictar por voz. Usa Chrome, Edge o Safari actualizados.', { type: 'error' });
        return;
      }
      if (grabando) {
        parar();
        textarea.focus();
        return;
      }
      if (textarea.disabled || textarea.readOnly) return;
      if (dictadoActivo) dictadoActivo();
      dictadoActivo = parar;
      grabando = true;
      estilo(true);
      escuchar();
    });

    estilo(false);
  }

  window.activarDictado = activarDictado;
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('textarea[data-dictado]').forEach(activarDictado);
  });
})();
