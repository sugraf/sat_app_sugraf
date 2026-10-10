// chatbot.js — Asistente IA de Sugraf (robot flotante en todas las pantallas).
//
// Cada página puede definir window.sugrafBotContexto = () => ({...}) con lo que
// se ve en pantalla; el asistente lo envía con cada pregunta para que el
// servidor busque solo la información relevante (RAG) y no todo el histórico.
// Si la página tiene un elemento #bot-embebido, el chat se monta dentro de él
// a pantalla completa (Control IA) en lugar de como botón flotante.
(function () {
  const usuario = localStorage.getItem('sugraf_user');
  if (!usuario) return;

  const CLAVE_CONVERSACION = 'sugraf_bot_conversacion';
  const MAX_GUARDADOS = 30;
  const MAX_HISTORIAL_ENVIO = 6;

  const MODULOS = {
    inicio: 'Inicio', generador: 'Introducir Aviso', gestor: 'Organizador de Avisos',
    adjudicados: 'Avisos Asignados', historicos: 'Históricos SAT', control: 'Control IA',
  };
  const SUGERENCIAS = {
    inicio: ['¿Qué puedo hacer en cada módulo?', 'Busca averías parecidas a "error 116" en Xerox'],
    generador: ['¿Este cliente ha tenido este problema antes?', '¿Qué etiquetas debería marcar?'],
    gestor: ['¿Qué avisos urgentes están sin asignar?', '¿Cuántos avisos tiene cada técnico?'],
    adjudicados: ['¿Ha pasado algo parecido antes en esta máquina?', 'Resume el historial de este cliente'],
    historicos: ['¿Cuáles son las averías más repetidas?', '¿Qué se cambió la última vez?'],
    control: ['¿Qué avisos urgentes están sin asignar?', 'Propón un reparto de avisos entre técnicos'],
  };

  const ruta = window.location.pathname;
  const modulo = (ruta.match(/^\/vista\/([a-z]+)/) || [])[1] || 'inicio';
  const embebido = document.getElementById('bot-embebido');

  // ---------- Estilos ----------
  const css = `
  .sbot-fab{ position:fixed; right:20px; bottom:20px; width:58px; height:58px; border-radius:50%; border:none; cursor:pointer;
    background:linear-gradient(160deg,#00806c 0%,#006858 60%,#004d40 100%); box-shadow:0 10px 26px rgba(0,0,0,0.35), 0 0 0 4px rgba(0,104,88,0.18);
    display:flex; align-items:center; justify-content:center; z-index:10000; transition:transform .2s ease; }
  .sbot-fab:hover{ transform:translateY(-2px) scale(1.04); }
  .sbot-fab svg{ width:32px; height:32px; }
  .sbot-fab.oculto{ display:none; }
  .sbot-panel{ position:fixed; right:20px; bottom:20px; width:390px; height:600px; max-height:calc(100vh - 40px); z-index:10001;
    background:#fff; color:#10263B; border-radius:18px; box-shadow:0 24px 60px rgba(0,0,0,0.45); display:none; flex-direction:column; overflow:hidden;
    font-family:'Montserrat',sans-serif; }
  .sbot-panel.abierto{ display:flex; animation:sbotIn .18s ease; }
  .sbot-panel.embebido{ position:relative; right:auto; bottom:auto; width:100%; height:100%; max-height:none; display:flex; box-shadow:0 24px 60px rgba(0,0,0,0.35); }
  @keyframes sbotIn{ from{ opacity:0; transform:translateY(10px); } to{ opacity:1; transform:none; } }
  .sbot-head{ background:linear-gradient(160deg,#173654 0%,#10263B 100%); color:#fff; padding:14px 16px; display:flex; align-items:center; gap:10px; }
  .sbot-head .sbot-avatar{ width:36px; height:36px; border-radius:50%; background:rgba(0,104,88,0.35); display:flex; align-items:center; justify-content:center; flex-shrink:0; }
  .sbot-head .sbot-avatar svg{ width:24px; height:24px; }
  .sbot-head .sbot-titulo{ flex:1; min-width:0; }
  .sbot-head strong{ display:block; font-size:0.95rem; }
  .sbot-head small{ display:block; font-size:0.72rem; color:#a9bcd6; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .sbot-head button{ background:none; border:none; color:#a9bcd6; cursor:pointer; font-size:0.72rem; font-weight:700; font-family:inherit; padding:6px; border-radius:8px; }
  .sbot-head button:hover{ color:#fff; background:rgba(255,255,255,0.08); }
  .sbot-head .sbot-cerrar{ font-size:1.4rem; line-height:1; }
  .sbot-msgs{ flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:10px; background:#f4f7fa; }
  .sbot-msg{ max-width:86%; padding:10px 13px; border-radius:14px; font-size:0.86rem; line-height:1.45; word-break:break-word; }
  .sbot-msg.user{ align-self:flex-end; background:#006858; color:#fff; border-bottom-right-radius:4px; white-space:pre-wrap; }
  .sbot-msg.assistant{ align-self:flex-start; background:#fff; color:#10263B; border:1px solid #e2e8f0; border-bottom-left-radius:4px; }
  .sbot-msg.error{ align-self:flex-start; background:#fef2f2; color:#b91c1c; border:1px solid #fecaca; }
  .sbot-msg ul{ margin:4px 0 4px 18px; padding:0; }
  .sbot-msg p{ margin:0 0 6px; } .sbot-msg p:last-child{ margin-bottom:0; }
  .sbot-fuente{ display:block; margin-top:6px; font-size:0.68rem; color:#64748b; }
  .sbot-escribiendo{ display:inline-flex; gap:4px; } .sbot-escribiendo i{ width:6px; height:6px; border-radius:50%; background:#94a3b8; animation:sbotPunto 1s infinite; }
  .sbot-escribiendo i:nth-child(2){ animation-delay:.15s; } .sbot-escribiendo i:nth-child(3){ animation-delay:.3s; }
  @keyframes sbotPunto{ 0%,80%,100%{ opacity:.3; } 40%{ opacity:1; } }
  .sbot-sugerencias{ display:flex; flex-wrap:wrap; gap:6px; }
  .sbot-sugerencias button{ background:#fff; border:1px solid #bfd8d2; color:#006858; border-radius:999px; padding:6px 11px; font-size:0.75rem; font-weight:600; cursor:pointer; font-family:inherit; text-align:left; }
  .sbot-sugerencias button:hover{ background:rgba(0,104,88,0.08); }
  .sbot-input{ display:flex; gap:8px; padding:10px; border-top:1px solid #e2e8f0; background:#fff; align-items:flex-end; }
  .sbot-input textarea{ flex:1; resize:none; border:1px solid #B6BEC4; border-radius:12px; padding:10px 12px; font-family:inherit; font-size:0.88rem; max-height:110px; min-height:42px; color:#10263B; outline:none; }
  .sbot-input textarea:focus{ border-color:#006858; box-shadow:0 0 0 3px rgba(0,104,88,0.15); }
  .sbot-btn{ width:42px; height:42px; border-radius:12px; border:none; cursor:pointer; display:flex; align-items:center; justify-content:center; flex-shrink:0; }
  .sbot-btn svg{ width:20px; height:20px; }
  .sbot-enviar{ background:#006858; } .sbot-enviar:hover{ background:#004d40; }
  .sbot-enviar:disabled{ opacity:.5; cursor:not-allowed; }
  .sbot-mic{ background:#eef2f6; } .sbot-mic:hover{ background:#e2e8f0; }
  .sbot-mic.grabando{ background:#ef4444; animation:sbotPulso 1.2s infinite; }
  @keyframes sbotPulso{ 0%{ box-shadow:0 0 0 0 rgba(239,68,68,.5); } 70%{ box-shadow:0 0 0 10px rgba(239,68,68,0); } 100%{ box-shadow:0 0 0 0 rgba(239,68,68,0); } }
  .sbot-estado{ font-size:0.72rem; color:#ef4444; padding:0 14px 8px; background:#fff; display:none; }
  @media (max-width:560px){
    .sbot-fab{ right:14px; bottom:14px; width:54px; height:54px; }
    .sbot-panel:not(.embebido){ right:0; bottom:0; width:100%; height:100%; max-height:none; border-radius:0; }
  }`;
  const estilo = document.createElement('style');
  estilo.textContent = css;
  document.head.appendChild(estilo);

  const ROBOT = `<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
    <line x1="16" y1="3.5" x2="16" y2="7.5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <circle cx="16" cy="3.2" r="1.7" fill="#2fd9b8"/>
    <rect x="6" y="8" width="20" height="16" rx="5" fill="#fff"/>
    <rect x="3" y="13" width="3" height="6" rx="1.5" fill="#fff"/><rect x="26" y="13" width="3" height="6" rx="1.5" fill="#fff"/>
    <circle cx="12" cy="15.5" r="2.2" fill="#006858"/><circle cx="20" cy="15.5" r="2.2" fill="#006858"/>
    <path d="M12.5 20.2c1 .9 2.2 1.3 3.5 1.3s2.5-.4 3.5-1.3" stroke="#006858" stroke-width="1.6" stroke-linecap="round"/>
    <rect x="11" y="25.5" width="10" height="3" rx="1.5" fill="#fff" opacity=".85"/></svg>`;
  const ICONO_ENVIAR = `<svg viewBox="0 0 24 24" fill="none"><path d="M4 12l15-7-5 15-2.5-6.5L4 12z" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/></svg>`;
  const ICONO_MIC = (color) => `<svg viewBox="0 0 24 24" fill="none"><rect x="9" y="3" width="6" height="11" rx="3" stroke="${color}" stroke-width="1.8"/><path d="M5.5 11a6.5 6.5 0 0013 0M12 17.5V21" stroke="${color}" stroke-width="1.8" stroke-linecap="round"/></svg>`;

  // ---------- Estructura ----------
  const fab = document.createElement('button');
  fab.className = 'sbot-fab';
  fab.type = 'button';
  fab.title = 'Asistente Sugraf';
  fab.setAttribute('aria-label', 'Abrir asistente');
  fab.innerHTML = ROBOT;

  const panel = document.createElement('div');
  panel.className = 'sbot-panel' + (embebido ? ' embebido' : '');
  panel.innerHTML = `
    <div class="sbot-head">
      <div class="sbot-avatar">${ROBOT}</div>
      <div class="sbot-titulo"><strong>Asistente Sugraf</strong><small class="sbot-viendo"></small></div>
      <button type="button" class="sbot-nueva" title="Empezar una conversación nueva">Nueva</button>
      ${embebido ? '' : '<button type="button" class="sbot-cerrar" aria-label="Cerrar">&times;</button>'}
    </div>
    <div class="sbot-msgs"></div>
    <div class="sbot-estado"></div>
    <div class="sbot-input">
      <textarea rows="1" placeholder="Escribe o pulsa el micrófono..."></textarea>
      <button type="button" class="sbot-btn sbot-mic" title="Dictar por voz">${ICONO_MIC('#10263B')}</button>
      <button type="button" class="sbot-btn sbot-enviar" title="Enviar">${ICONO_ENVIAR}</button>
    </div>`;

  if (embebido) {
    embebido.appendChild(panel);
  } else {
    document.body.appendChild(fab);
    document.body.appendChild(panel);
  }

  const $msgs = panel.querySelector('.sbot-msgs');
  const $texto = panel.querySelector('textarea');
  const $enviar = panel.querySelector('.sbot-enviar');
  const $mic = panel.querySelector('.sbot-mic');
  const $estado = panel.querySelector('.sbot-estado');
  const $viendo = panel.querySelector('.sbot-viendo');

  // ---------- Conversación (se conserva al cambiar de pantalla) ----------
  function leerConversacion() {
    try { return JSON.parse(sessionStorage.getItem(CLAVE_CONVERSACION)) || []; } catch (e) { return []; }
  }
  function guardarConversacion(conv) {
    try { sessionStorage.setItem(CLAVE_CONVERSACION, JSON.stringify(conv.slice(-MAX_GUARDADOS))); } catch (e) {}
  }
  let conversacion = leerConversacion();

  function escHtml(t) {
    return String(t ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  // Markdown mínimo: **negrita**, listas con "- " y párrafos.
  function formatear(texto) {
    const lineas = escHtml(texto).split('\n');
    let html = '', enLista = false;
    lineas.forEach(l => {
      const item = l.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
      if (item) {
        if (!enLista) { html += '<ul>'; enLista = true; }
        html += `<li>${item[1]}</li>`;
      } else {
        if (enLista) { html += '</ul>'; enLista = false; }
        if (l.trim()) html += `<p>${l}</p>`;
      }
    });
    if (enLista) html += '</ul>';
    return html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  }

  function contexto() {
    try { return typeof window.sugrafBotContexto === 'function' ? (window.sugrafBotContexto() || {}) : {}; }
    catch (e) { console.warn('sugrafBotContexto', e); return {}; }
  }

  function actualizarCabecera() {
    const ctx = contexto();
    $viendo.textContent = 'Viendo: ' + (ctx.etiqueta ? `${MODULOS[modulo]} · ${ctx.etiqueta}` : MODULOS[modulo]);
  }

  function burbuja(m) {
    const div = document.createElement('div');
    div.className = `sbot-msg ${m.rol}`;
    if (m.rol === 'assistant') {
      div.innerHTML = formatear(m.texto) +
        (m.partes && m.partes.length ? `<span class="sbot-fuente">Consultados ${m.partes.length} partes del histórico</span>` : '');
    } else {
      div.textContent = m.texto;
    }
    return div;
  }

  function pintar() {
    $msgs.innerHTML = '';
    if (!conversacion.length) {
      const hola = document.createElement('div');
      hola.className = 'sbot-msg assistant';
      hola.innerHTML = formatear(`Hola, ${usuario}. Soy el asistente de Sugraf y veo lo mismo que tú en **${MODULOS[modulo]}**. Puedes escribirme o hablarme con el micrófono.`);
      $msgs.appendChild(hola);
      const sug = document.createElement('div');
      sug.className = 'sbot-sugerencias';
      (SUGERENCIAS[modulo] || []).forEach(s => {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = s;
        b.addEventListener('click', () => enviar(s));
        sug.appendChild(b);
      });
      $msgs.appendChild(sug);
    }
    conversacion.forEach(m => $msgs.appendChild(burbuja(m)));
    $msgs.scrollTop = $msgs.scrollHeight;
  }

  // ---------- Envío ----------
  let enviando = false;
  async function enviar(texto) {
    if (texto === undefined && grabando) pararDictado(); // Enviar también corta el dictado en curso
    texto = (texto ?? $texto.value).trim();
    if (!texto || enviando) return;
    enviando = true;
    $enviar.disabled = true;
    $texto.value = '';
    ajustarAltura();

    const historial = conversacion.filter(m => m.rol !== 'error').slice(-MAX_HISTORIAL_ENVIO)
      .map(m => ({rol: m.rol, texto: m.texto}));
    conversacion.push({rol: 'user', texto});
    guardarConversacion(conversacion);
    pintar();

    const pensando = document.createElement('div');
    pensando.className = 'sbot-msg assistant';
    pensando.innerHTML = '<span class="sbot-escribiendo"><i></i><i></i><i></i></span>';
    $msgs.appendChild(pensando);
    $msgs.scrollTop = $msgs.scrollHeight;

    try {
      const res = await fetch('/api/control/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mensaje: texto, modulo, usuario, pantalla: contexto(), historial}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Error HTTP ${res.status}`);
      conversacion.push({rol: 'assistant', texto: data.respuesta, partes: data.partes || []});
    } catch (e) {
      conversacion.push({rol: 'error', texto: `No he podido responder: ${e.message}`});
    }
    guardarConversacion(conversacion);
    enviando = false;
    $enviar.disabled = false;
    pintar();
  }

  function ajustarAltura() {
    $texto.style.height = 'auto';
    $texto.style.height = Math.min($texto.scrollHeight, 110) + 'px';
  }

  $enviar.addEventListener('click', () => enviar());
  $texto.addEventListener('input', ajustarAltura);
  $texto.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); enviar(); }
  });
  panel.querySelector('.sbot-nueva').addEventListener('click', () => {
    conversacion = [];
    guardarConversacion(conversacion);
    pintar();
  });

  // ---------- Voz (reconocimiento del navegador, sin coste) ----------
  // El dictado se escribe en la caja de texto y NO se envía solo: se sigue
  // escuchando aunque haya pausas hasta pulsar otra vez el micrófono, y el
  // mensaje sale cuando el usuario pulsa Enviar (pudiendo corregirlo antes).
  const Reconocimiento = window.SpeechRecognition || window.webkitSpeechRecognition;
  // En Android el modo continuo repite frases; allí se usan frases sueltas
  // que se encadenan reiniciando el reconocimiento tras cada pausa.
  const CONTINUO = !/Android/i.test(navigator.userAgent);
  let rec = null;
  let grabando = false;   // el usuario quiere seguir dictando

  function mostrarEstado(texto, esError = true) {
    $estado.textContent = texto;
    $estado.style.color = esError ? '#ef4444' : '#006858';
    $estado.style.display = texto ? 'block' : 'none';
  }

  function estiloMic(activo) {
    $mic.classList.toggle('grabando', activo);
    $mic.innerHTML = ICONO_MIC(activo ? '#fff' : '#10263B');
    $mic.title = activo ? 'Parar de dictar' : 'Dictar por voz';
  }

  function pararDictado() {
    grabando = false;
    if (rec) { try { rec.stop(); } catch (e) {} }
    estiloMic(false);
    mostrarEstado('');
  }

  function escuchar() {
    const base = $texto.value.trim();
    rec = new Reconocimiento();
    rec.lang = 'es-ES';
    rec.interimResults = true;
    rec.continuous = CONTINUO;
    rec.onresult = e => {
      // Se recompone todo lo oído en esta sesión para no duplicar frases.
      let oido = '';
      for (let i = 0; i < e.results.length; i++) oido += e.results[i][0].transcript;
      $texto.value = `${base} ${oido.trim()}`.trim();
      ajustarAltura();
    };
    rec.onerror = e => {
      if (e.error === 'no-speech' || e.error === 'aborted') return; // pausa: se reinicia en onend
      grabando = false;
      mostrarEstado(e.error === 'not-allowed' || e.error === 'service-not-allowed'
        ? 'Permite el acceso al micrófono para poder dictar.'
        : `Error del micrófono: ${e.error}`);
    };
    rec.onend = () => {
      // El navegador corta tras un silencio: si no se ha pulsado parar, se sigue escuchando.
      if (grabando) setTimeout(() => { if (grabando) escuchar(); }, 250);
      else estiloMic(false);
    };
    rec.start();
  }

  $mic.addEventListener('click', () => {
    if (!Reconocimiento) {
      mostrarEstado('Este navegador no permite dictar por voz. Usa Chrome, Edge o Safari actualizados.');
      return;
    }
    if (grabando) {
      pararDictado();
      $texto.focus();
      return;
    }
    grabando = true;
    estiloMic(true);
    mostrarEstado('Escuchando… Pulsa el micrófono para parar y luego Enviar.', false);
    escuchar();
  });

  // ---------- Abrir / cerrar ----------
  function abrir() {
    actualizarCabecera();
    pintar();
    panel.classList.add('abierto');
    fab.classList.add('oculto');
    setTimeout(() => $texto.focus(), 50);
  }
  function cerrar() {
    if (grabando) pararDictado();
    panel.classList.remove('abierto');
    fab.classList.remove('oculto');
  }
  fab.addEventListener('click', abrir);
  const btnCerrar = panel.querySelector('.sbot-cerrar');
  if (btnCerrar) btnCerrar.addEventListener('click', cerrar);

  if (embebido) {
    actualizarCabecera();
    pintar();
  }

  window.SugrafBot = { abrir, cerrar, enviar };
})();
