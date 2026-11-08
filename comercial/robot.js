// comercial/robot.js — Robot asistente de Sugraf Comercial (igual que el del SAT y el de la app de escritorio).
//
// La página (comercial/comercial.html) define:
//   - window.sugrafBotContexto = () => ({...}): lo que se ve en pantalla (vista, cliente, visita a medio rellenar...).
//   - window.sugrafBotAcciones = {nombre: {descripcion, parametros, etiqueta(args), ejecutar(args)}}:
//     lo que el robot puede hacer en la app cuando el modo control está activo.
//
// Modo control (interruptor bajo el robot): el servidor devuelve llamadas a esas acciones, se ejecutan
// aquí a la vista del usuario y su resultado vuelve al modelo hasta que responde con texto.
// Búsqueda en internet: botón de la bola del mundo. Dictado por voz: micrófono.
// Se sirve en /comercial/robot.js; la API es POST /api/comercial/asistente (con el token del comercial).
(function () {
  const leerLS = k => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const token = () => leerLS('sugraf_com_token');
  const usuario = leerLS('sugraf_com_nombre') || '';
  if (!token()) return;
  if (window.SugrafBot) return;

  const CLAVE_CONVERSACION = 'sugraf_com_bot_conversacion';
  const CLAVE_CONTROL = 'sugraf_com_bot_control';
  const CLAVE_WEB = 'sugraf_com_bot_web';
  const MAX_GUARDADOS = 40;
  const MAX_HISTORIAL_ENVIO = 6;
  const MAX_RONDAS = 8;
  const PAUSA_ACCION = 450;

  const VISTAS = {hoy: 'Hoy', clientes: 'Clientes', ficha: 'Ficha de cliente', precios: 'Precios', agenda: 'Agenda', ruta: 'Ruta', seguimiento: 'Seguimiento'};
  const SUGERENCIAS = {
    hoy: ['¿A qué clientes me toca ir esta semana?', '¿Qué seguimientos tengo pendientes?'],
    clientes: ['¿Qué clientes estoy visitando de más?', '¿Qué clientes dormidos compraban más?'],
    ficha: ['Prepárame la visita a este cliente', '¿Qué está dejando de comprar?'],
    precios: ['¿A qué precio le cobramos esto a este cliente?', '¿Qué precios le he dado últimamente?'],
    agenda: ['¿Cuántas visitas he hecho esta semana?', '¿Cuánto duran de media mis visitas?'],
    ruta: ['¿Qué clientes me conviene visitar por aquí?'],
    seguimiento: ['¿Qué ofertas llevan más tiempo sin respuesta?'],
  };
  const SUGERENCIAS_CONTROL = {
    hoy: ['Prográmame para el lunes visitas a los clientes que toca ver', 'Apunta una visita a un cliente de 10:00 a 11:00'],
    clientes: ['Abre la ficha del primer cliente al que toca ir'],
    ficha: ['Abre una visita nueva a este cliente', 'Enséñame sus compras'],
    precios: ['Busca el precio de una referencia para este cliente'],
    agenda: ['Enséñame la agenda del mes'],
    ruta: ['Llévame a Hoy'],
  };

  const vistaActual = () => {
    const v = (location.hash || '#hoy').slice(1).split('/')[0];
    return VISTAS[v] ? v : 'hoy';
  };
  function leerPref(clave) { try { return localStorage.getItem(clave) === '1'; } catch (e) { return false; } }
  function guardarPref(clave, valor) { try { localStorage.setItem(clave, valor ? '1' : '0'); } catch (e) {} }
  let modoControl = leerPref(CLAVE_CONTROL);
  let modoWeb = leerPref(CLAVE_WEB);

  // ---------- Estilos ----------
  const css = `
  .sbot-dock{ position:fixed; right:16px; bottom:calc(80px + env(safe-area-inset-bottom)); z-index:250; display:flex; flex-direction:column; align-items:center; gap:7px; }
  .sbot-dock.oculto, body.sin-sesion .sbot-dock, body.ruta-pie-on .sbot-dock{ display:none; }
  body.hoja-abierta .sbot-dock{ bottom:calc(84px + env(safe-area-inset-bottom)); z-index:400; }
  .sbot-fab{ position:relative; width:58px; height:58px; border-radius:50%; border:none; cursor:pointer;
    background:linear-gradient(160deg,#3a82c8 0%,#2f6fb0 60%,#24588c 100%); box-shadow:0 10px 26px rgba(0,0,0,0.35), 0 0 0 4px rgba(47,111,176,0.22);
    display:flex; align-items:center; justify-content:center; transition:transform .2s ease; }
  .sbot-fab:hover{ transform:translateY(-2px) scale(1.04); }
  .sbot-fab svg{ width:32px; height:32px; }
  .sbot-fab.trabajando::after{ content:""; position:absolute; inset:-6px; border-radius:50%; border:3px solid transparent; border-top-color:#8fc4ec; border-right-color:#8fc4ec; animation:sbotGiro .9s linear infinite; }
  .sbot-fab.aviso::before{ content:""; position:absolute; top:1px; right:1px; width:13px; height:13px; border-radius:50%; background:#ef4444; border:2px solid #fff; }
  @keyframes sbotGiro{ to{ transform:rotate(360deg); } }
  .sbot-switch{ display:flex; align-items:center; background:rgba(10,24,38,0.88); border:1px solid rgba(182,190,196,0.28);
    padding:4px; border-radius:999px; cursor:pointer; box-shadow:0 6px 16px rgba(0,0,0,.3); transition:border-color .2s; }
  .sbot-switch:hover{ border-color:rgba(143,196,236,.6); }
  .sbot-switch .sbot-pista{ width:28px; height:16px; border-radius:999px; background:#4a5a72; position:relative; transition:background .2s; flex-shrink:0; }
  .sbot-switch .sbot-pista::after{ content:""; position:absolute; top:2px; left:2px; width:12px; height:12px; border-radius:50%; background:#fff; transition:transform .2s; }
  .sbot-switch[aria-checked="true"]{ border-color:rgba(143,196,236,.6); }
  .sbot-switch[aria-checked="true"] .sbot-pista{ background:#8fc4ec; }
  .sbot-switch[aria-checked="true"] .sbot-pista::after{ transform:translateX(12px); }
  .sbot-panel{ position:fixed; right:20px; bottom:20px; width:390px; height:600px; max-height:calc(100vh - 40px); z-index:10001;
    background:#fff; color:#10263B; border-radius:18px; box-shadow:0 24px 60px rgba(0,0,0,0.45); display:none; flex-direction:column; overflow:hidden;
    font-family:'Montserrat',sans-serif; }
  .sbot-panel.abierto{ display:flex; animation:sbotIn .18s ease; }
  @keyframes sbotIn{ from{ opacity:0; transform:translateY(10px); } to{ opacity:1; transform:none; } }
  .sbot-head{ background:linear-gradient(160deg,#173654 0%,#10263B 100%); color:#fff; padding:14px 12px 14px 16px; display:flex; align-items:center; gap:8px; }
  .sbot-head .sbot-avatar{ width:36px; height:36px; border-radius:50%; background:rgba(47,111,176,0.4); display:flex; align-items:center; justify-content:center; flex-shrink:0; }
  .sbot-head .sbot-avatar svg{ width:24px; height:24px; }
  .sbot-head .sbot-titulo{ flex:1; min-width:0; }
  .sbot-head strong{ display:block; font-size:0.95rem; }
  .sbot-head small{ display:block; font-size:0.72rem; color:#a9bcd6; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .sbot-head button{ background:none; border:none; color:#a9bcd6; cursor:pointer; font-size:0.72rem; font-weight:700; font-family:inherit; padding:6px; border-radius:8px; }
  .sbot-head button:hover{ color:#fff; background:rgba(255,255,255,0.08); }
  .sbot-head .sbot-cerrar{ font-size:1.4rem; line-height:1; }
  .sbot-head .sbot-web{ width:30px; height:30px; padding:0; display:flex; align-items:center; justify-content:center; flex-shrink:0;
    border:1px solid rgba(182,190,196,0.25); transition:background .15s, border-color .15s; }
  .sbot-head .sbot-web svg{ width:17px; height:17px; }
  .sbot-head .sbot-web.activo{ background:#2f6fb0; border-color:#8fc4ec; box-shadow:0 0 0 3px rgba(143,196,236,0.2); }
  .sbot-head .sbot-web.activo:hover{ background:#3a82c8; }
  .sbot-msgs{ flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:10px; background:#f4f7fa; }
  .sbot-msg{ max-width:86%; padding:10px 13px; border-radius:14px; font-size:0.86rem; line-height:1.45; word-break:break-word; }
  .sbot-msg.user{ align-self:flex-end; background:#2f6fb0; color:#fff; border-bottom-right-radius:4px; white-space:pre-wrap; }
  .sbot-msg.assistant{ align-self:flex-start; background:#fff; color:#10263B; border:1px solid #e2e8f0; border-bottom-left-radius:4px; }
  .sbot-msg.error{ align-self:flex-start; background:#fef2f2; color:#b91c1c; border:1px solid #fecaca; }
  .sbot-msg a{ color:#2f6fb0; font-weight:600; }
  .sbot-msg ul{ margin:4px 0 4px 18px; padding:0; }
  .sbot-msg p{ margin:0 0 6px; } .sbot-msg p:last-child{ margin-bottom:0; }
  .sbot-fuente{ display:block; margin-top:6px; font-size:0.68rem; color:#64748b; }
  .sbot-fuentes{ margin-top:8px; padding-top:7px; border-top:1px dashed #e2e8f0; display:flex; flex-direction:column; gap:3px; }
  .sbot-fuentes b{ font-size:0.66rem; color:#64748b; text-transform:uppercase; letter-spacing:.06em; }
  .sbot-fuentes a{ font-size:0.74rem; text-decoration:none; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .sbot-fuentes a:hover{ text-decoration:underline; }
  .sbot-pagina{ font-size:0.74rem; color:#4a5a72; line-height:1.4; word-break:break-word; }
  .sbot-fuentes .sbot-pagina a{ white-space:normal; overflow:visible; }
  .sbot-paso{ align-self:center; display:flex; align-items:center; gap:6px; max-width:94%; font-size:0.72rem; font-weight:600; color:#24588c;
    background:#e8f1fa; border:1px solid #c9dcf0; padding:4px 11px; border-radius:999px; }
  .sbot-paso svg{ width:13px; height:13px; flex-shrink:0; }
  .sbot-paso.fallo{ color:#b45309; background:#fef3c7; border-color:#fde68a; }
  .sbot-escribiendo{ display:inline-flex; gap:4px; } .sbot-escribiendo i{ width:6px; height:6px; border-radius:50%; background:#94a3b8; animation:sbotPunto 1s infinite; }
  .sbot-escribiendo i:nth-child(2){ animation-delay:.15s; } .sbot-escribiendo i:nth-child(3){ animation-delay:.3s; }
  @keyframes sbotPunto{ 0%,80%,100%{ opacity:.3; } 40%{ opacity:1; } }
  .sbot-sugerencias{ display:flex; flex-wrap:wrap; gap:6px; }
  .sbot-sugerencias button{ background:#fff; border:1px solid #bcd3ea; color:#2f6fb0; border-radius:999px; padding:6px 11px; font-size:0.75rem; font-weight:600; cursor:pointer; font-family:inherit; text-align:left; }
  .sbot-sugerencias button:hover{ background:rgba(47,111,176,0.08); }
  .sbot-input{ display:flex; gap:8px; padding:10px; border-top:1px solid #e2e8f0; background:#fff; align-items:flex-end; }
  .sbot-input textarea{ flex:1; resize:none; background:#fff !important; border:1px solid #B6BEC4 !important; width:auto !important; border-radius:12px; padding:10px 12px; font-family:inherit; font-size:0.88rem; max-height:110px; min-height:42px; color:#10263B; outline:none; }
  .sbot-input textarea:focus{ border-color:#2f6fb0; box-shadow:0 0 0 3px rgba(47,111,176,0.15); }
  .sbot-btn{ width:42px; height:42px; border-radius:12px; border:none; cursor:pointer; display:flex; align-items:center; justify-content:center; flex-shrink:0; }
  .sbot-btn svg{ width:20px; height:20px; }
  .sbot-enviar{ background:#2f6fb0; } .sbot-enviar:hover{ background:#24588c; }
  .sbot-enviar:disabled{ opacity:.5; cursor:not-allowed; }
  .sbot-mic{ background:#eef2f6; } .sbot-mic:hover{ background:#e2e8f0; }
  .sbot-mic.grabando{ background:#ef4444; animation:sbotPulso 1.2s infinite; }
  @keyframes sbotPulso{ 0%{ box-shadow:0 0 0 0 rgba(239,68,68,.5); } 70%{ box-shadow:0 0 0 10px rgba(239,68,68,0); } 100%{ box-shadow:0 0 0 0 rgba(239,68,68,0); } }
  .sbot-estado{ font-size:0.72rem; color:#ef4444; padding:0 14px 8px; background:#fff; display:none; }
  .sbot-toast{ position:fixed; right:20px; bottom:calc(190px + env(safe-area-inset-bottom)); max-width:340px; z-index:10002; display:none; align-items:center; gap:8px;
    background:#10263B; color:#fff; border:1px solid rgba(143,196,236,0.45); padding:10px 13px; border-radius:12px;
    font-family:'Montserrat',sans-serif; font-size:0.8rem; font-weight:600; box-shadow:0 12px 28px rgba(0,0,0,.35); }
  .sbot-toast.visible{ display:flex; animation:sbotIn .18s ease; }
  .sbot-resalte{ outline:3px solid #8fc4ec !important; outline-offset:2px !important; box-shadow:0 0 0 7px rgba(143,196,236,.3) !important;
    transition:outline-color .2s, box-shadow .2s; }
  @media (max-width:560px){
    .sbot-dock{ right:12px; }
    .sbot-toast{ left:14px; right:14px; max-width:none; }
    .sbot-fab{ width:54px; height:54px; }
    .sbot-panel{ right:0; bottom:0; width:100%; height:100%; height:100dvh; max-height:none; border-radius:0; }
    .sbot-input{ padding-bottom:calc(10px + env(safe-area-inset-bottom)); }
  }`;
  const estilo = document.createElement('style');
  estilo.textContent = css;
  document.head.appendChild(estilo);

  const ROBOT = `<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
    <line x1="16" y1="3.5" x2="16" y2="7.5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
    <circle cx="16" cy="3.2" r="1.7" fill="#8fc4ec"/>
    <rect x="6" y="8" width="20" height="16" rx="5" fill="#fff"/>
    <rect x="3" y="13" width="3" height="6" rx="1.5" fill="#fff"/><rect x="26" y="13" width="3" height="6" rx="1.5" fill="#fff"/>
    <circle cx="12" cy="15.5" r="2.2" fill="#2f6fb0"/><circle cx="20" cy="15.5" r="2.2" fill="#2f6fb0"/>
    <path d="M12.5 20.2c1 .9 2.2 1.3 3.5 1.3s2.5-.4 3.5-1.3" stroke="#2f6fb0" stroke-width="1.6" stroke-linecap="round"/>
    <rect x="11" y="25.5" width="10" height="3" rx="1.5" fill="#fff" opacity=".85"/></svg>`;
  const ICONO_ENVIAR = `<svg viewBox="0 0 24 24" fill="none"><path d="M4 12l15-7-5 15-2.5-6.5L4 12z" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/></svg>`;
  const ICONO_MIC = (color) => `<svg viewBox="0 0 24 24" fill="none"><rect x="9" y="3" width="6" height="11" rx="3" stroke="${color}" stroke-width="1.8"/><path d="M5.5 11a6.5 6.5 0 0013 0M12 17.5V21" stroke="${color}" stroke-width="1.8" stroke-linecap="round"/></svg>`;
  const ICONO_MUNDO = `<svg viewBox="0 0 24 24" fill="none" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.25" stroke="#a9bcd6"/><path d="M3.75 12h16.5M12 3.75c2.3 2.4 3.4 5.15 3.4 8.25S14.3 17.85 12 20.25C9.7 17.85 8.6 15.1 8.6 12S9.7 6.15 12 3.75Z" stroke="#8fc4ec"/></svg>`;
  const ICONO_MUNDO_ACTIVO = ICONO_MUNDO.replace('stroke="#a9bcd6"', 'stroke="#fff"').replace('stroke="#8fc4ec"', 'stroke="#fff"');
  const ICONO_PASO = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>`;

  // ---------- Estructura ----------
  const dock = document.createElement('div');
  dock.className = 'sbot-dock';
  dock.innerHTML = `
    <button type="button" class="sbot-fab" title="Asistente comercial" aria-label="Abrir asistente">${ROBOT}</button>
    <button type="button" class="sbot-switch" role="switch" aria-label="Control del asistente" title="Permite que el asistente use la aplicación por ti">
      <span class="sbot-pista"></span>
    </button>`;
  const fab = dock.querySelector('.sbot-fab');
  const $switch = dock.querySelector('.sbot-switch');

  const panel = document.createElement('div');
  panel.className = 'sbot-panel';
  panel.innerHTML = `
    <div class="sbot-head">
      <div class="sbot-avatar">${ROBOT}</div>
      <div class="sbot-titulo"><strong>Asistente comercial</strong><small class="sbot-viendo"></small></div>
      <button type="button" class="sbot-web" aria-pressed="false"></button>
      <button type="button" class="sbot-nueva" title="Empezar una conversación nueva">Nueva</button>
      <button type="button" class="sbot-cerrar" aria-label="Cerrar">&times;</button>
    </div>
    <div class="sbot-msgs"></div>
    <div class="sbot-estado"></div>
    <div class="sbot-input">
      <textarea rows="1"></textarea>
      <button type="button" class="sbot-btn sbot-mic" title="Dictar por voz">${ICONO_MIC('#10263B')}</button>
      <button type="button" class="sbot-btn sbot-enviar" title="Enviar">${ICONO_ENVIAR}</button>
    </div>`;

  const toast = document.createElement('div');
  toast.className = 'sbot-toast';

  document.body.appendChild(dock);
  document.body.appendChild(panel);
  document.body.appendChild(toast);

  const $msgs = panel.querySelector('.sbot-msgs');
  const $texto = panel.querySelector('textarea');
  const $enviar = panel.querySelector('.sbot-enviar');
  const $mic = panel.querySelector('.sbot-mic');
  const $estado = panel.querySelector('.sbot-estado');
  const $viendo = panel.querySelector('.sbot-viendo');
  const $web = panel.querySelector('.sbot-web');

  const pausa = ms => new Promise(r => setTimeout(r, ms));
  const esMovil = () => window.matchMedia('(max-width:560px)').matches;

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

  // Solo enlaces web o rutas de la propia app (p. ej. /api/manuales/archivo/...).
  function enlace(url, texto) {
    const seguro = /^(https?:\/\/|\/(?!\/))/i.test(url) ? url : '#';
    return `<a href="${seguro}" target="_blank" rel="noopener">${texto}</a>`;
  }

  // Markdown mínimo: **negrita**, listas con "- ", párrafos y enlaces [texto](url) o sueltos.
  function formatear(texto) {
    const enlaces = [];
    const guardar = html => `\u0000${enlaces.push(html) - 1}\u0000`;
    const conEnlaces = escHtml(texto)
      .replace(/\[([^\]\n]+)\]\(((?:https?:\/\/|\/)[^\s)]+)\)/g, (_, txt, url) => guardar(enlace(url, txt)))
      .replace(/https?:\/\/[^\s<\u0000]+[^\s<.,;:!?)\u0000]/g, url => guardar(enlace(url, url.replace(/^https?:\/\/(www\.)?/, '').slice(0, 45))));
    const lineas = conEnlaces.split('\n');
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
    return html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\u0000(\d+)\u0000/g, (_, i) => enlaces[i]);
  }

  function contexto() {
    try { return typeof window.sugrafBotContexto === 'function' ? (window.sugrafBotContexto() || {}) : {}; }
    catch (e) { console.warn('sugrafBotContexto', e); return {}; }
  }

  function actualizarCabecera() {
    const ctx = contexto();
    const modos = [modoControl && 'Control', modoWeb && 'Internet'].filter(Boolean).join(' · ');
    const v = VISTAS[vistaActual()];
    $viendo.textContent = 'Viendo: ' + (ctx.etiqueta ? `${v} · ${ctx.etiqueta}` : v) + (modos ? ` · ${modos}` : '');
  }

  function pintarModos() {
    $switch.setAttribute('aria-checked', modoControl ? 'true' : 'false');
    $switch.title = modoControl ? 'El asistente puede usar la aplicación (pulsa para desactivar)'
                                : 'Permite que el asistente use la aplicación por ti';
    $web.classList.toggle('activo', modoWeb);
    $web.setAttribute('aria-pressed', modoWeb ? 'true' : 'false');
    $web.innerHTML = modoWeb ? ICONO_MUNDO_ACTIVO : ICONO_MUNDO;
    $web.title = modoWeb ? 'Búsqueda en internet activada (pulsa para desactivar)' : 'Buscar también en internet';
    $texto.placeholder = modoWeb ? 'Pregunta (también buscaré en internet)...' : 'Escribe o pulsa el micrófono...';
    actualizarCabecera();
  }

  function burbuja(m) {
    const div = document.createElement('div');
    if (m.rol === 'paso') {
      div.className = `sbot-paso ${m.fallo ? 'fallo' : ''}`;
      div.innerHTML = ICONO_PASO + `<span>${escHtml(m.texto)}</span>`;
      return div;
    }
    div.className = `sbot-msg ${m.rol}`;
    if (m.rol === 'assistant') {
      div.innerHTML = formatear(m.texto) +
        (m.fuentes && m.fuentes.length
          ? `<div class="sbot-fuentes"><b>Fuentes</b>${m.fuentes.map(f => enlace(f.url, escHtml(f.titulo || f.url))).join('')}</div>` : '') +
        '';
    } else {
      div.textContent = m.texto;
    }
    return div;
  }

  let trabajando = false;
  function pintar() {
    $msgs.innerHTML = '';
    if (!conversacion.length) {
      const hola = document.createElement('div');
      hola.className = 'sbot-msg assistant';
      hola.innerHTML = formatear(`Hola, ${usuario}. Soy el asistente comercial de Sugraf y veo lo mismo que tú en **${VISTAS[vistaActual()]}**. Pregúntame por clientes, visitas, compras o precios; puedes escribirme o hablarme con el micrófono.` +
        (modoControl ? '\nEl **control** está activado: puedo usar la aplicación por ti.' : ''));
      $msgs.appendChild(hola);
      const sug = document.createElement('div');
      sug.className = 'sbot-sugerencias';
      [...(modoControl ? SUGERENCIAS_CONTROL[vistaActual()] || [] : []), ...(SUGERENCIAS[vistaActual()] || [])].slice(0, 3).forEach(s => {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = s;
        b.addEventListener('click', () => enviar(s));
        sug.appendChild(b);
      });
      $msgs.appendChild(sug);
    }
    conversacion.forEach(m => $msgs.appendChild(burbuja(m)));
    if (trabajando) {
      const pensando = document.createElement('div');
      pensando.className = 'sbot-msg assistant';
      pensando.innerHTML = '<span class="sbot-escribiendo"><i></i><i></i><i></i></span>';
      $msgs.appendChild(pensando);
    }
    $msgs.scrollTop = $msgs.scrollHeight;
  }

  function anotar(m) {
    conversacion.push(m);
    guardarConversacion(conversacion);
    pintar();
  }

  // ---------- Acciones sobre la aplicación (modo control) ----------
  function acciones() {
    return window.sugrafBotAcciones || {};
  }

  function catalogoAcciones() {
    return Object.entries(acciones()).map(([nombre, a]) => ({
      nombre, descripcion: a.descripcion || '',
      parametros: a.parametros || {type: 'object', properties: {}, additionalProperties: false},
    }));
  }

  // Lleva la vista al elemento y lo marca un momento, para que se vea qué toca el asistente.
  async function resaltar(el) {
    if (!el) return;
    try { el.scrollIntoView({behavior: 'smooth', block: 'center'}); } catch (e) {}
    el.classList.add('sbot-resalte');
    setTimeout(() => el.classList.remove('sbot-resalte'), 1500);
    await pausa(350);
  }

  // Escribe en un campo como si se tecleara (rápido) y lanza sus eventos input/change.
  async function escribir(el, valor, {eventos = true} = {}) {
    if (!el) return;
    const texto = String(valor ?? '');
    await resaltar(el);
    if (el.tagName === 'SELECT' || el.type === 'date' || el.type === 'time' || texto.length > 400) {
      el.value = texto;
    } else {
      const paso = Math.max(1, Math.ceil(texto.length / 24));
      el.value = '';
      for (let i = 0; i < texto.length; i += paso) {
        el.value = texto.slice(0, i + paso);
        await pausa(16);
      }
      el.value = texto;
    }
    if (eventos) {
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }

  function etiquetaDe(llamada) {
    const accion = acciones()[llamada.nombre];
    try { return (accion && accion.etiqueta && accion.etiqueta(llamada.args || {})) || `Usando ${llamada.nombre}`; }
    catch (e) { return `Usando ${llamada.nombre}`; }
  }

  async function ejecutar(llamada) {
    const accion = acciones()[llamada.nombre];
    if (!accion) return {ok: false, error: `La acción ${llamada.nombre} no está disponible en esta pantalla.`};
    try {
      const r = await accion.ejecutar(llamada.args || {});
      return r === undefined ? {ok: true} : r;
    } catch (e) {
      return {ok: false, error: e.message || String(e)};
    }
  }

  function mostrarToast(texto, ms = 0) {
    toast.textContent = texto;
    toast.classList.add('visible');
    clearTimeout(mostrarToast.t);
    if (ms) mostrarToast.t = setTimeout(() => toast.classList.remove('visible'), ms);
  }

  // ---------- Envío y bucle de acciones ----------
  async function llamar(extra) {
    const res = await fetch('/api/comercial/asistente', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token()},
      body: JSON.stringify({
        contexto: contexto(), web: modoWeb, control: modoControl,
        acciones: modoControl ? catalogoAcciones() : [], ...extra,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) throw new Error('Tu sesión ha caducado. Sal y vuelve a entrar.');
    if (!res.ok) throw new Error(data.detail || `Error HTTP ${res.status}`);
    return data;
  }

  let cancelado = false;

  // Ejecuta las acciones que pide el modelo y le devuelve los resultados, hasta que
  // responde con texto. Devuelve la respuesta final, o null si la página va a cambiar.
  async function resolverAcciones(data) {
    const fuentes = [...(data.fuentes || [])];
    for (let ronda = 0; data.acciones && data.acciones.length; ronda++) {
      if (cancelado || !modoControl) {
        anotar({rol: 'paso', texto: 'Control detenido', fallo: true});
        return {respuesta: 'He parado de usar la aplicación.', fuentes: []};
      }
      if (ronda >= MAX_RONDAS) {
        return {respuesta: 'He necesitado demasiados pasos y me he detenido. Dime cómo seguir.', fuentes: []};
      }
      if (data.respuesta) anotar({rol: 'assistant', texto: data.respuesta});
      if (esMovil()) {
        // En el móvil el chat ocupa la pantalla: se oculta para que se vea lo que hace.
        cerrar();
        fab.classList.add('trabajando');
      }

      // Las consultas a manuales de esta misma respuesta ya las ha resuelto el servidor.
      const resultados = [...(data.resultados_servidor || [])];
      for (let i = 0; i < data.acciones.length; i++) {
        const llamada = data.acciones[i];
        const accion = acciones()[llamada.nombre];
        const etiqueta = etiquetaDe(llamada);
        anotar({rol: 'paso', texto: etiqueta});
        if (esMovil()) mostrarToast(etiqueta);

        if (accion && accion.terminal) {
          // Acción que deja la página (p. ej. crear el aviso): no se espera respuesta.
          const r = await ejecutar(llamada);
          if (r && r.ok === false) {
            resultados.push({call_id: llamada.call_id, salida: JSON.stringify(r)});
            anotar({rol: 'paso', texto: `No se ha podido: ${r.error}`, fallo: true});
            continue;
          }
          return {respuesta: r.mensaje || 'Hecho.', fuentes: []};
        }

        const r = await ejecutar(llamada);
        resultados.push({call_id: llamada.call_id, salida: JSON.stringify(r)});
        if (r && r.ok === false) anotar({rol: 'paso', texto: `No se ha podido: ${r.error}`, fallo: true});
        await pausa(PAUSA_ACCION);
      }
      data = await llamar({continuar: data.respuesta_id, resultados});
      (data.fuentes || []).forEach(f => { if (!fuentes.some(x => x.url === f.url)) fuentes.push(f); });
    }
    return {...data, fuentes};
  }

  async function trabajar(primeraLlamada) {
    trabajando = true;
    cancelado = false;
    $enviar.disabled = true;
    pintar();
    try {
      const data = await primeraLlamada();
      const final = await resolverAcciones(data);
      anotar({rol: 'assistant', texto: final.respuesta || 'Hecho.', fuentes: final.fuentes || []});
    } catch (e) {
      anotar({rol: 'error', texto: `No he podido responder: ${e.message}`});
    } finally {
      trabajando = false;
      $enviar.disabled = false;
      fab.classList.remove('trabajando');
      pintar();
      if (!panel.classList.contains('abierto')) {
        fab.classList.add('aviso');
        mostrarToast('Listo. Toca el robot para ver la respuesta.', 4500);
      } else {
        toast.classList.remove('visible');
      }
    }
  }

  async function enviar(texto) {
    // Enviar corta el dictado (aunque ya se hubiera parado, puede quedar una frase en camino).
    if (texto === undefined) pararDictado(true);
    texto = (texto ?? $texto.value).trim();
    if (!texto || trabajando) return;
    $texto.value = '';
    ajustarAltura();

    const historial = conversacion.filter(m => m.rol === 'user' || m.rol === 'assistant').slice(-MAX_HISTORIAL_ENVIO)
      .map(m => ({rol: m.rol, texto: m.texto}));
    conversacion.push({rol: 'user', texto});
    guardarConversacion(conversacion);
    await trabajar(() => llamar({mensaje: texto, historial}));
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
    if (trabajando) cancelado = true;
    conversacion = [];
    guardarConversacion(conversacion);
    pintar();
  });
  $web.addEventListener('click', () => {
    modoWeb = !modoWeb;
    guardarPref(CLAVE_WEB, modoWeb);
    pintarModos();
  });
  $switch.addEventListener('click', () => {
    modoControl = !modoControl;
    guardarPref(CLAVE_CONTROL, modoControl);
    if (!modoControl && trabajando) cancelado = true;
    pintarModos();
    mostrarToast(modoControl ? 'Control activado: el asistente puede usar la aplicación.' : 'Control desactivado.', 2500);
    if (panel.classList.contains('abierto') || !conversacion.length) pintar();
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
    $estado.style.color = esError ? '#ef4444' : '#2f6fb0';
    $estado.style.display = texto ? 'block' : 'none';
  }

  function estiloMic(activo) {
    $mic.classList.toggle('grabando', activo);
    $mic.innerHTML = ICONO_MIC(activo ? '#fff' : '#10263B');
    $mic.title = activo ? 'Parar de dictar' : 'Dictar por voz';
  }

  // descartar=true (al enviar): se cancela el reconocimiento y se ignora lo que
  // llegue después; si no, el navegador escribiría la última frase en la caja
  // ya vaciada. Con el botón del micrófono se usa stop() para no perder esa frase.
  function pararDictado(descartar = false) {
    grabando = false;
    if (rec) {
      if (descartar) {
        rec.onresult = null;
        try { rec.abort(); } catch (e) {}
      } else {
        try { rec.stop(); } catch (e) {}
      }
    }
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
    pintarModos();
    pintar();
    panel.classList.add('abierto');
    dock.classList.add('oculto');
    fab.classList.remove('aviso');
    toast.classList.remove('visible');
    setTimeout(() => $texto.focus(), 50);
  }
  function cerrar() {
    if (grabando) pararDictado();
    panel.classList.remove('abierto');
    dock.classList.remove('oculto');
  }
  fab.addEventListener('click', abrir);
  panel.querySelector('.sbot-cerrar').addEventListener('click', cerrar);

  window.addEventListener('hashchange', () => {
    actualizarCabecera();
    if (!conversacion.length && panel.classList.contains('abierto') && !trabajando) pintar();
  });
  pintarModos();
  window.SugrafBot = { abrir, cerrar, enviar, resaltar, escribir, pausa, olvidar: () => { conversacion = []; guardarConversacion(conversacion); } };
})();
