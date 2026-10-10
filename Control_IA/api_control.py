# Control_IA/api_control.py
"""Asistente IA de la aplicación (chatbot del robot).

Cada pantalla envía junto a la pregunta un resumen de lo que se está viendo
(aviso abierto, filtros, lista de avisos...). Con eso se arma un contexto corto:
  - descripción fija de la aplicación (siempre igual -> aprovecha la caché de prompts),
  - datos de la pantalla,
  - partes del histórico recuperados por similitud (RAG en Control_IA/rag.py),
y solo eso viaja al modelo, en vez de los Excel completos.

Se usa la API Responses de OpenAI con dos herramientas opcionales:
  - Búsqueda en internet (botón de la bola del mundo): herramienta web_search;
    las fuentes citadas se devuelven aparte para mostrarlas como enlaces.
  - Control de la aplicación (interruptor bajo el robot): cada pantalla envía las
    acciones que admite y se ofrecen al modelo como funciones. Las llamadas se
    devuelven al navegador, que las ejecuta y vuelve a llamar con sus resultados
    (`continuar` = id de la respuesta anterior) hasta que el modelo contesta con texto.
Además, siempre están las herramientas de manuales, que se ejecutan aquí mismo:
buscar_maquina_manuales (identifica UNA máquina) y consultar_manual (fragmentos
relevantes de los manuales de esa máquina, ver manuales/rag_manuales.py).
"""
import json
import logging
import os
import re
import time
import traceback
from typing import List

from fastapi import APIRouter, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from openai import OpenAI, APIError, AuthenticationError, RateLimitError, APIConnectionError, NotFoundError
from pydantic import BaseModel

from secretos import obtener as obtener_secreto
from historicos.api_historicos import cargar_historico
from Control_IA.rag import detectar_entidades, resumen_ambito, similares, texto_registro
from manuales import rag_manuales
from manuales.api_manuales import _drive as drive_manuales, buscar_carpetas, cargar_catalogo

logger = logging.getLogger("api_control")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | [%(funcName)s] | %(message)s'))
    logger.addHandler(ch)

router = APIRouter()
FOLDER_ID = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

MODELO_IA = "gpt-6-luna"
MAX_TOKENS_RESPUESTA = 3000  # incluye el razonamiento interno del modelo
ESFUERZO_RAZONAMIENTO = "low"  # gpt-6-luna admite none / low; con más, gasta tokens pensando
MAX_TURNOS_HISTORIAL = 6
MAX_LINEAS_AVISOS = 60
MAX_ACCIONES = 30
MAX_MANUALES_CONTEXTO = 40
MAX_FUENTES = 8
MAX_CONSULTAS_MANUAL = 3   # por pregunta, para acotar los tokens
MAX_VUELTAS_SERVIDOR = 6
MAX_PAGINAS_MOSTRADAS = 8

# Herramientas que se ejecutan en el servidor (no en el navegador).
HERRAMIENTAS_MANUALES = [
    {
        "type": "function",
        "name": "buscar_maquina_manuales",
        "description": "Busca la carpeta de manuales de UNA máquina por su marca, modelo o nombre. Devuelve las "
                       "candidatas con su carpeta_id. Úsala solo si la máquina no está ya en el CONTEXTO.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["texto"], "properties": {
            "texto": {"type": "string", "description": "Modelo o nombre de la máquina, p. ej. \"Roland VG3-640\""},
            "marca": {"type": "string"},
        }},
        "strict": False,
    },
    {
        "type": "function",
        "name": "consultar_manual",
        "description": "Busca en los manuales PDF de UNA máquina (su carpeta_id) los fragmentos más relevantes para "
                       "la consulta. No lee los manuales enteros: devuelve unos pocos fragmentos con manual, página y enlace.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["carpeta_id", "consulta"], "properties": {
            "carpeta_id": {"type": "string", "description": "carpeta_id de la máquina (del CONTEXTO o de buscar_maquina_manuales)"},
            "consulta": {"type": "string", "description": "Términos técnicos clave en español Y en inglés, con los códigos de error tal cual"},
            "manual": {"type": "string", "description": "Opcional: parte del nombre de un manual para buscar solo en él"},
        }},
        "strict": False,
    },
]
NOMBRES_HERRAMIENTAS_SERVIDOR = {h["name"] for h in HERRAMIENTAS_MANUALES}
BUSQUEDA_WEB = {
    "type": "web_search",
    "search_context_size": "medium",
    "user_location": {"type": "approximate", "country": "ES", "timezone": "Europe/Madrid"},
}

_clientes_ia = {}


def cliente_ia():
    """Cliente de OpenAI con la API key de contraseñas.txt (Drive); se rehace si la key cambia."""
    clave = obtener_secreto("OPENAI_API_KEY")
    if clave not in _clientes_ia:
        _clientes_ia.clear()
        _clientes_ia[clave] = OpenAI(api_key=clave)
    return _clientes_ia[clave]

NOMBRES_MODULO = {
    'inicio': 'Inicio (menú principal)',
    'generador': 'Introducir Aviso',
    'gestor': 'Organizador de Avisos',
    'adjudicados': 'Avisos Asignados',
    'historicos': 'Históricos SAT',
    'manuales': 'Manuales Técnicos',
}

# Prompt fijo: no cambia entre peticiones para que OpenAI lo cachee.
PROMPT_SISTEMA = """Eres el asistente técnico de Sugraf, empresa de servicio técnico (SAT) de maquinaria de artes gráficas: impresoras digitales, plotters, CTP, láseres, prensas, grapadoras, laminadoras, etc. Te usan técnicos (a menudo desde el móvil, en casa del cliente) y la oficina.

La aplicación tiene estos módulos:
- Introducir Aviso (generador): se registra un aviso nuevo con fecha, hora, cliente, población, una o varias máquinas del cliente, descripción del problema y etiquetas (Nuevo, Urgente, Parado, Reclama, Presupuesto, Piezas, Garantía, Mantenimiento, Instalación, Revisar). Se guarda en "Avisos Sin Tratar".
- Organizador de Avisos (gestor): panel central con todos los avisos abiertos, sin tratar y tratados. Desde aquí se asigna técnico (al asignarlo pasa a "Avisos Tratados"), se marca si está metido en Pirineos (el ERP), el estado de las piezas, y se editan o eliminan avisos. Se filtra por técnico, Pirineos o etiqueta.
- Avisos Asignados (adjudicados): cada técnico ve sus avisos (pendientes de empezar, en progreso y cerrados pendientes de pasar a Pirineos) y rellena el parte: fecha, horas, resuelto o pendiente, piezas necesarias y utilizadas, solución y observaciones internas; al cerrar firman técnico y cliente y se envía el PDF por correo. Si queda pendiente o faltan piezas se genera un aviso de seguimiento. El botón "Histórico" de cada aviso muestra los partes anteriores de esa máquina (misma unidad y mismo modelo en otros clientes) y de ese cliente; el botón "Manuales" muestra los manuales técnicos de esa máquina para abrirlos.
- Históricos SAT (historicos): consulta de los partes realizados exportados de Pirineos desde 2008, filtrando por cliente, máquina (modelo), nº de serie y fechas, con descarga a Excel.
- Manuales Técnicos (manuales): manuales de las máquinas (PDF y manuales online) de la carpeta "Manuales tecnicos" de Drive, organizados por marca y modelo, filtrando por cliente (sus máquinas de Maquinas.xlsx), marca, máquina/modelo o nombre del manual. Puedes consultar el contenido de los manuales PDF de una máquina con consultar_manual.

Reglas:
- Responde en español, breve y práctico. Usa listas cortas con "- " y **negrita** solo para lo importante.
- Usa únicamente la información del bloque CONTEXTO, de la conversación, de los resultados de las funciones de la app y, si la tienes, de la búsqueda en internet. Si algo no está, dilo claramente. Nunca inventes partes, fechas, clientes, piezas ni soluciones.
- Los partes del histórico del CONTEXTO son una selección automática de los más parecidos, no el histórico completo. Para totales usa las líneas de resumen.
- Cuando te describan un fallo, compáralo con los partes del histórico: primero los de la misma unidad, luego los del mismo modelo y después el resto. Resume qué se hizo y qué funcionó, citando nº de parte y fecha. Si no hay nada parecido, dilo; puedes proponer comprobaciones generales, pero indicando que son sugerencias tuyas y no salen del histórico.
- Cuando el CONTEXTO incluya un "RESUMEN EXACTO" de avisos, usa sus cifras y listas tal cual: ya están calculadas, no vuelvas a contar el detalle ni señales diferencias con él. No enumeres todos los avisos salvo que te lo pidan.
- Al proponer el reparto de avisos entre técnicos nunca des prioridad a un aviso solo por ser más antiguo.
- Explica cómo se usa la aplicación cuando te lo pregunten, según la descripción de los módulos.
- Para que el usuario abra un manual, dale su enlace en formato [título](url) con la url que aparece en el CONTEXTO o en el resultado de una función.

Contenido de los manuales (herramientas buscar_maquina_manuales y consultar_manual):
- Úsalas cuando pregunten cómo se hace algo en una máquina, un procedimiento, un ajuste, un error o una especificación que pueda estar en su manual.
- Primero identifica UNA máquina concreta: la del aviso o el filtro en pantalla (sus carpetas de manuales aparecen en el CONTEXTO con su carpeta_id) o búscala con buscar_maquina_manuales. Si hay varias candidatas y no está claro cuál es, pregunta al usuario antes de consultar. Nunca consultes varias máquinas para una misma pregunta.
- En consultar_manual escribe la consulta con los términos técnicos clave en español y en inglés (muchos manuales están en inglés), con los códigos de error tal cual. Si los fragmentos no responden, puedes repetir con otros términos (como mucho 3 consultas).
- Responde solo con lo que digan los fragmentos y cita manual y página con su enlace, así: [Manual, pág. 12](enlace). Si no lo encuentras, dilo.

Búsqueda en internet (solo si tienes la herramienta de búsqueda web):
- Úsala para lo que no esté en el CONTEXTO: códigos de error, procedimientos y boletines del fabricante, referencias de piezas, firmware, manuales. Da prioridad a las webs oficiales de los fabricantes.
- Distingue siempre lo que sale del histórico de Sugraf de lo que sale de internet, y cita las fuentes con enlace.

Control de la aplicación (solo si tienes funciones de la app):
- Con esas funciones manejas la aplicación por el usuario: cambiar de módulo, filtrar, abrir avisos, partes, históricos y manuales, y rellenar campos. Úsalas cuando el usuario te pida hacer algo en la app, no para responder preguntas que ya puedes contestar con el CONTEXTO.
- Para referirte a un aviso usa su id del CONTEXTO. Si cambias de módulo, las funciones de la nueva pantalla estarán disponibles en el siguiente paso.
- Guardar el parte, crear un aviso, asignar técnico o marcar Pirineos cambia datos reales: hazlo solo si el usuario lo pide expresamente (la app le pedirá confirmación). Antes de guardar, rellena los campos y comprueba el resultado.
- No puedes cerrar avisos definitivos (necesitan las firmas) ni eliminar avisos: explica al usuario cómo hacerlo él.
- Si una función devuelve un error, explícalo; no repitas la misma llamada.
- Al terminar, resume en una o dos frases lo que has hecho y lo que le queda por hacer al usuario."""


# ==========================================
# Drive (solo para el diagnóstico /test-drive)
# ==========================================
def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_raw:
        raise ValueError("Faltan las credenciales de Google Drive (GOOGLE_CREDENTIALS_JSON).")
    return build("drive", "v3", credentials=service_account.Credentials.from_service_account_info(
        json.loads(creds_raw), scopes=["https://www.googleapis.com/auth/drive"]
    ), cache_discovery=False)


# ==========================================
# Avisos que envían las pantallas
# ==========================================
def _linea_aviso(av):
    campos = [
        f"id {av.get('id')}" if av.get('id') not in (None, '') else '',
        av.get('fecha'), av.get('cliente'), av.get('poblacion'), av.get('maquina'),
        f"Técnico: {av.get('tecnico') or 'Pendiente'}", f"Estado: {av.get('estado') or '-'}",
        f"Pirineos: {av.get('pirineos') or 'NO'}", ', '.join(av.get('etiquetas') or []), _t(av.get('descripcion'), 160),
    ]
    return ' | '.join(_t(c) for c in campos if str(c or '').strip())


ETIQUETAS_RESUMEN = ['Urgente', 'Parado', 'Reclama', 'Garantía', 'Piezas Pendiente', 'Piezas Listo']


def _bloque_avisos(titulo, avisos):
    """Resumen calculado aquí (recuentos y listas exactas, que el modelo no tenga que
    contar) + el detalle de cada aviso."""
    avisos = [a for a in (avisos or []) if isinstance(a, dict)][:MAX_LINEAS_AVISOS]
    if not avisos:
        return f"{titulo}: ninguno."

    def nombre(a):
        tec = a.get('tecnico') or 'Pendiente'
        return f"{_t(a.get('cliente'), 60)} – {_t(a.get('maquina'), 50)} ({'sin asignar' if tec == 'Pendiente' else tec})"

    por_tecnico, por_estado = {}, {}
    for a in avisos:
        tec = a.get('tecnico') or 'Pendiente'
        por_tecnico[tec] = por_tecnico.get(tec, 0) + 1
        por_estado[a.get('estado') or '-'] = por_estado.get(a.get('estado') or '-', 0) + 1
    resumen = [
        f"Total: {len(avisos)} avisos.",
        "Por técnico: " + ', '.join(f"{'Sin asignar' if t == 'Pendiente' else t} {n}" for t, n in sorted(por_tecnico.items())) + '.',
        "Por estado: " + ', '.join(f"{e} {n}" for e, n in sorted(por_estado.items())) + '.',
    ]
    for etiqueta in ETIQUETAS_RESUMEN:
        con = [a for a in avisos if etiqueta in (a.get('etiquetas') or [])]
        if con:
            resumen.append(f"{etiqueta} ({len(con)}): " + '; '.join(nombre(a) for a in con) + '.')
    urgentes_libres = [a for a in avisos if 'Urgente' in (a.get('etiquetas') or []) and (a.get('tecnico') or 'Pendiente') == 'Pendiente']
    resumen.append(f"Urgentes sin asignar: {len(urgentes_libres)}.")

    return (f"{titulo} — RESUMEN EXACTO (úsalo para recuentos y listas):\n" + '\n'.join(resumen)
            + f"\nDetalle de cada aviso:\n" + '\n'.join(f"- {_linea_aviso(a)}" for a in avisos))


# ==========================================
# Contexto por pantalla
# ==========================================
class MensajeChat(BaseModel):
    rol: str
    texto: str


class AccionApp(BaseModel):
    nombre: str
    descripcion: str = ''
    parametros: dict = {}


class ResultadoAccion(BaseModel):
    call_id: str
    salida: str = ''


class PeticionChat(BaseModel):
    mensaje: str = ''
    modulo: str = 'inicio'
    usuario: str = ''
    pantalla: dict = {}
    historial: List[MensajeChat] = []
    web: bool = False            # búsqueda en internet activada
    control: bool = False        # el asistente puede manejar la app
    acciones: List[AccionApp] = []  # lo que admite la pantalla actual
    continuar: str = ''          # id de la respuesta cuyas llamadas a funciones se han ejecutado
    resultados: List[ResultadoAccion] = []


def _t(valor, maximo=300):
    texto = ' '.join(str(valor or '').split())
    return texto if len(texto) <= maximo else texto[:maximo] + '…'


def contexto_pantalla(req):
    """Devuelve (texto de la pantalla, ámbito del histórico, texto extra para la búsqueda)."""
    p = req.pantalla or {}
    ambito = {'cliente': '', 'modelo': '', 'n_serie': '', 'desde': '', 'hasta': ''}
    extra_busqueda = ''
    lineas = []

    if req.modulo == 'adjudicados':
        lineas.append(f"Técnico cuyos avisos se ven: {_t(p.get('tecnico'))}. Vista: {_t(p.get('vista'))}.")
        aviso = p.get('aviso') or {}
        if aviso:
            lineas.append(
                "Aviso abierto en pantalla: "
                f"id {_t(aviso.get('id'))}; cliente {_t(aviso.get('cliente'))}; máquina {_t(aviso.get('maquina'))}; marca {_t(aviso.get('marca'))}; "
                f"modelo {_t(aviso.get('modelo'))}; nº serie {_t(aviso.get('n_serie'))}; fecha entrada {_t(aviso.get('fecha'))}; "
                f"estado {_t(aviso.get('estado'))}; motivo del aviso: {_t(aviso.get('descripcion'), 500)}"
                + (f"; solución escrita hasta ahora: {_t(aviso.get('solucion'), 400)}" if aviso.get('solucion') else '')
            )
            ambito.update(cliente=aviso.get('cliente', ''), modelo=aviso.get('modelo', ''), n_serie=aviso.get('n_serie', ''))
            extra_busqueda = aviso.get('descripcion', '')
        lineas.append(_bloque_avisos("Avisos del técnico en pantalla", p.get("avisos")))

    elif req.modulo == 'historicos':
        f = p.get('filtros') or {}
        lineas.append(
            "Filtros aplicados en Históricos SAT: "
            + (', '.join(f"{k}={_t(v)}" for k, v in f.items() if v) or 'ninguno')
            + f". Resultados en pantalla: {_t(p.get('total'))}."
        )
        ambito.update({k: f.get(k, '') for k in ambito})

    elif req.modulo == 'manuales':
        f = p.get('filtros') or {}
        lineas.append(
            "Filtros aplicados en Manuales Técnicos: "
            + (', '.join(f"{k}={_t(v)}" for k, v in f.items() if v) or 'ninguno') + '.'
        )
        if p.get('resumen'):
            lineas.append(f"En pantalla: {_t(p.get('resumen'))}.")
        ambito.update(cliente=f.get('cliente', ''), modelo=f.get('maquina', ''))

    elif req.modulo == 'gestor':
        f = p.get('filtros') or {}
        lineas.append("Filtros del organizador: " + (', '.join(f"{k}={_t(v)}" for k, v in f.items() if v) or 'ninguno') + '.')
        lineas.append(_bloque_avisos("Avisos visibles en el organizador", p.get("avisos")))

    elif req.modulo == 'generador':
        f = p.get('formulario') or {}
        lineas.append(
            "Formulario de nuevo aviso: "
            f"cliente {_t(f.get('cliente'))}; población {_t(f.get('poblacion'))}; "
            f"máquinas {_t(', '.join(f.get('maquinas') or []))}; descripción {_t(f.get('descripcion'), 500)}; "
            f"etiquetas {_t(', '.join(f.get('etiquetas') or []))}."
        )
        ambito['cliente'] = f.get('cliente', '')
        extra_busqueda = f"{f.get('descripcion', '')} {' '.join(f.get('maquinas') or [])}"

    # Manuales visibles (módulo de Manuales o manuales de la máquina de un aviso), por carpeta de máquina.
    manuales = [m for m in (p.get('manuales') or []) if isinstance(m, dict) and m.get('url')][:MAX_MANUALES_CONTEXTO]
    if manuales:
        grupos = {}
        for m in manuales:
            grupos.setdefault((_t(m.get('grupo'), 120), _t(m.get('carpeta_id'), 80)), []).append(m)
        lineas.append("Manuales en pantalla (carpeta de cada máquina y enlace para abrirlos):\n" + '\n'.join(
            f"- {grupo}" + (f" [carpeta_id: {carpeta}]" if carpeta else '') + ": "
            + '; '.join(f"[{_t(m.get('titulo'), 150)}]({m['url']})" for m in lista)
            for (grupo, carpeta), lista in grupos.items()))
    if p.get('sin_manuales'):
        lineas.append("Máquinas sin manuales en la carpeta: " + '; '.join(_t(x, 80) for x in p['sin_manuales'][:20]) + '.')

    return '\n'.join(lineas), ambito, extra_busqueda


def contexto_historico(req, ambito, extra_busqueda):
    """Resumen + partes recuperados por similitud. Devuelve (texto, nº de partes usados)."""
    try:
        indice, _ = cargar_historico()
    except Exception as e:
        logger.warning(f"Histórico no disponible: {e}")
        return "El histórico de SAT no está disponible ahora mismo.", []

    # Cliente / modelo / serie citados en la pregunta completan lo que no da la pantalla.
    for clave, valor in detectar_entidades(indice, req.mensaje).items():
        if valor and not ambito.get(clave):
            ambito[clave] = valor

    # La pregunta anterior ayuda en seguimientos cortos ("¿y cómo se solucionó?").
    anterior = next((m.texto for m in reversed(req.historial) if m.rol == 'user'), '')
    consulta = f"{req.mensaje} {anterior if len(req.mensaje.split()) < 6 else ''} {extra_busqueda}"

    res = similares(indice, consulta, **ambito)
    bloques = []
    resumen = resumen_ambito(indice, ambito['cliente'], ambito['modelo'], ambito['n_serie'])
    if resumen:
        bloques.append("Resumen del histórico:\n" + resumen)
    bloques.append(
        "Partes del histórico más parecidos a la consulta:\n" + '\n'.join(texto_registro(r) for r in res['similares'])
        if res['similares'] else "Partes del histórico parecidos a la consulta: ninguno."
    )
    if res['recientes']:
        bloques.append("Últimos partes de esta máquina / cliente:\n" + '\n'.join(texto_registro(r) for r in res['recientes']))
    usados = [r['n_parte'] for r in res['similares'] + res['recientes']]
    return '\n\n'.join(bloques), usados


# ==========================================
# Rutas
# ==========================================
def herramientas(req):
    """Búsqueda web, manuales y acciones de la pantalla, en el formato de herramientas de la API Responses."""
    tools = [dict(BUSQUEDA_WEB)] if req.web else []
    tools += HERRAMIENTAS_MANUALES
    if req.control:
        for a in req.acciones[:MAX_ACCIONES]:
            if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', a.nombre) or a.nombre in NOMBRES_HERRAMIENTAS_SERVIDOR:
                continue
            tools.append({
                "type": "function",
                "name": a.nombre,
                "description": _t(a.descripcion, 600),
                "parameters": a.parametros or {"type": "object", "properties": {}},
                "strict": False,
            })
    return tools


def leer_respuesta(respuesta):
    """(texto, llamadas a funciones, fuentes web citadas, nº de búsquedas web)."""
    llamadas, fuentes, busquedas = [], {}, 0
    for item in respuesta.output or []:
        tipo = getattr(item, 'type', '')
        if tipo == 'function_call':
            try:
                args = json.loads(item.arguments or '{}')
            except ValueError:
                args = {}
            llamadas.append({"call_id": item.call_id, "nombre": item.name, "args": args if isinstance(args, dict) else {}})
        elif tipo == 'web_search_call':
            busquedas += 1
        elif tipo == 'message':
            for parte in getattr(item, 'content', None) or []:
                for ann in getattr(parte, 'annotations', None) or []:
                    url = getattr(ann, 'url', '')
                    if getattr(ann, 'type', '') == 'url_citation' and url and url not in fuentes:
                        fuentes[url] = getattr(ann, 'title', '') or url
    texto = (getattr(respuesta, 'output_text', '') or '').strip()
    return texto, llamadas, [{"titulo": t, "url": u} for u, t in list(fuentes.items())[:MAX_FUENTES]], busquedas


def ejecutar_en_servidor(llamada, estado):
    """Ejecuta una herramienta de manuales y devuelve su salida (JSON) para el modelo."""
    args = llamada['args']
    try:
        catalogo = cargar_catalogo()
        if llamada['nombre'] == 'buscar_maquina_manuales':
            candidatas = rag_manuales.buscar_maquina(catalogo, buscar_carpetas, str(args.get('texto', '')), str(args.get('marca', '')))
            salida = {'ok': True, 'candidatas': candidatas} if candidatas else {
                'ok': False, 'error': 'No hay ninguna carpeta de manuales para esa máquina.'}
        elif estado['consultas'] >= MAX_CONSULTAS_MANUAL:
            salida = {'ok': False, 'error': 'Ya se han hecho las consultas permitidas para esta pregunta: responde con lo encontrado.'}
        else:
            estado['consultas'] += 1
            salida = rag_manuales.consultar(drive_manuales(), catalogo, str(args.get('carpeta_id', '')),
                                            str(args.get('consulta', '')), str(args.get('manual', '') or ''))
            for r in salida.get('resultados', []):
                pagina = {'titulo': r['manual'], 'pagina': r['pagina'], 'url': r['enlace']}
                if pagina not in estado['paginas']:
                    estado['paginas'].append(pagina)
        logger.info(f"{llamada['nombre']} {json.dumps(args, ensure_ascii=False)[:200]} -> "
                    f"{len(salida.get('resultados', salida.get('candidatas', [])))} resultados")
    except Exception as e:
        logger.error(f"Error en {llamada['nombre']}:\n{traceback.format_exc()}")
        salida = {'ok': False, 'error': f'No se han podido consultar los manuales: {e}'}
    return json.dumps(salida, ensure_ascii=False)


@router.post("/chat")
def chat_ia(req: PeticionChat):
    inicio = time.time()
    continuando = bool(req.continuar)
    if not continuando and not req.mensaje.strip():
        raise HTTPException(status_code=400, detail="El mensaje está vacío.")
    modulo = req.modulo if req.modulo in NOMBRES_MODULO else 'inicio'
    req.modulo = modulo

    try:
        texto_pantalla, ambito, extra = contexto_pantalla(req)
        partes_usados = []
        if continuando:
            # Resultado de las acciones ejecutadas en el navegador. La pantalla puede haber
            # cambiado (otro módulo, filtros, parte abierto...), así que se añade al final.
            entrada = [{"type": "function_call_output", "call_id": r.call_id, "output": _t(r.salida, 6000)}
                       for r in req.resultados]
            if entrada:
                entrada[-1]["output"] += f"\n\nPANTALLA AHORA ({NOMBRES_MODULO[modulo]}):\n{texto_pantalla}"
        else:
            texto_historico, partes_usados = contexto_historico(req, ambito, extra)
            modos = [m for m, activo in (("búsqueda en internet", req.web), ("control de la app", req.control)) if activo]
            contexto = (
                f"Pantalla actual: {NOMBRES_MODULO[modulo]}. Usuario: {_t(req.usuario) or 'desconocido'}. "
                f"Fecha de hoy: {time.strftime('%Y-%m-%d')}. "
                f"Herramientas activadas: {', '.join(modos) or 'ninguna'}.\n{texto_pantalla}\n\n{texto_historico}"
            )
            entrada = []
            for m in req.historial[-MAX_TURNOS_HISTORIAL:]:
                entrada.append({"role": "assistant" if m.rol == 'assistant' else "user", "content": _t(m.texto, 800)})
            entrada.append({"role": "user", "content": f"CONTEXTO:\n{contexto}\n\nPREGUNTA:\n{req.mensaje.strip()}"})

        parametros = dict(
            model=MODELO_IA,
            instructions=PROMPT_SISTEMA,
            input=entrada,
            max_output_tokens=MAX_TOKENS_RESPUESTA,
            reasoning={"effort": ESFUERZO_RAZONAMIENTO},
        )
        tools = herramientas(req)
        if tools:
            parametros["tools"] = tools
        if continuando:
            parametros["previous_response_id"] = req.continuar

        # Las herramientas de manuales se resuelven aquí; las de la app vuelven al navegador.
        estado = {'consultas': 0, 'paginas': []}
        fuentes, busquedas, resultados_servidor = [], 0, []
        for vuelta in range(MAX_VUELTAS_SERVIDOR):
            respuesta = cliente_ia().responses.create(**parametros)
            texto, llamadas, fuentes_vuelta, busquedas_vuelta = leer_respuesta(respuesta)
            fuentes += [f for f in fuentes_vuelta if f not in fuentes]
            busquedas += busquedas_vuelta
            en_servidor = [c for c in llamadas if c['nombre'] in NOMBRES_HERRAMIENTAS_SERVIDOR]
            llamadas = [c for c in llamadas if c['nombre'] not in NOMBRES_HERRAMIENTAS_SERVIDOR]
            if not en_servidor:
                break
            salidas = [{"type": "function_call_output", "call_id": c['call_id'], "output": ejecutar_en_servidor(c, estado)}
                       for c in en_servidor]
            if llamadas:
                # También hay acciones para el navegador: estas salidas vuelven con las suyas.
                resultados_servidor = [{"call_id": x["call_id"], "salida": x["output"]} for x in salidas]
                break
            if vuelta == MAX_VUELTAS_SERVIDOR - 1:
                texto = texto or "He necesitado demasiadas consultas a los manuales. Prueba a concretar más la pregunta."
                break
            parametros.update(previous_response_id=respuesta.id, input=salidas)
        uso = respuesta.usage
        logger.info(
            f"/chat {modulo} | {round(time.time() - inicio, 2)}s | tokens entrada={getattr(uso, 'input_tokens', '?')} "
            f"salida={getattr(uso, 'output_tokens', '?')} | partes={len(partes_usados)} | web={busquedas} "
            f"| manuales={estado['consultas']} | acciones={[c['nombre'] for c in llamadas]}"
        )
        if not texto and not llamadas:
            texto = ("La respuesta se ha cortado antes de terminar. Prueba a preguntar algo más concreto."
                     if getattr(respuesta, 'status', '') == 'incomplete'
                     else "No he podido generar una respuesta. Prueba a reformular la pregunta.")
        return {
            "respuesta": texto,
            "acciones": llamadas,
            "respuesta_id": respuesta.id,
            "resultados_servidor": resultados_servidor,
            "partes": partes_usados,
            "fuentes": fuentes[:MAX_FUENTES],
            "paginas": estado['paginas'][:MAX_PAGINAS_MOSTRADAS],
            "ambito": {k: v for k, v in ambito.items() if v},
        }
    except AuthenticationError:
        raise HTTPException(status_code=500, detail="La API key de OpenAI no es válida o ha caducado.")
    except NotFoundError:
        raise HTTPException(status_code=500, detail=f"El modelo '{MODELO_IA}' no está disponible para esta API key.")
    except RateLimitError:
        raise HTTPException(status_code=500, detail="Se ha alcanzado el límite de uso de OpenAI. Inténtalo en un momento.")
    except APIConnectionError:
        raise HTTPException(status_code=500, detail="No se ha podido conectar con OpenAI.")
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Error de OpenAI: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en /chat:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/test-ia")
def test_ia():
    try:
        res = cliente_ia().chat.completions.create(
            model=MODELO_IA,
            messages=[{"role": "user", "content": "Responde solo: hola"}],
            max_completion_tokens=50,
        )
        return {"success": True, "modelo": MODELO_IA, "respuesta": res.choices[0].message.content}
    except Exception as e:
        return {"success": False, "modelo": MODELO_IA, "error_type": type(e).__name__, "message": str(e)}


@router.get("/test-drive")
def test_drive():
    try:
        drive = get_drive_service()
        res = drive.files().list(q=f"'{FOLDER_ID}' in parents and trashed = false", fields="files(id, name, mimeType)").execute()
        files = res.get("files", [])
        return {"success": True, "folder_id": FOLDER_ID, "files_found": len(files),
                "file_list": [{"name": f["name"], "type": f["mimeType"]} for f in files]}
    except Exception as e:
        return {"success": False, "folder_id": FOLDER_ID, "error_type": type(e).__name__, "message": str(e)}
