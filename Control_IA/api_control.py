# Control_IA/api_control.py
"""Asistente IA de la aplicación (chatbot del robot).

Cada pantalla envía junto a la pregunta un resumen de lo que se está viendo
(aviso abierto, filtros, lista de avisos...). Con eso se arma un contexto corto:
  - descripción fija de la aplicación (siempre igual -> aprovecha la caché de prompts),
  - datos de la pantalla,
  - partes del histórico recuperados por similitud (RAG en Control_IA/rag.py),
y solo eso viaja al modelo, en vez de los Excel completos.
"""
import json
import logging
import os
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
- Manuales Técnicos (manuales): manuales de las máquinas (PDF y manuales online) de la carpeta "Manuales tecnicos" de Drive, organizados por marca y modelo, filtrando por cliente (sus máquinas de Maquinas.xlsx), marca, máquina/modelo o nombre del manual. Tú no puedes leer el contenido de los manuales.

Reglas:
- Responde en español, breve y práctico. Usa listas cortas con "- " y **negrita** solo para lo importante.
- Usa únicamente la información del bloque CONTEXTO y de la conversación. Si algo no está, dilo claramente. Nunca inventes partes, fechas, clientes, piezas ni soluciones.
- Los partes del histórico del CONTEXTO son una selección automática de los más parecidos, no el histórico completo. Para totales usa las líneas de resumen.
- Cuando te describan un fallo, compáralo con los partes del histórico: primero los de la misma unidad, luego los del mismo modelo y después el resto. Resume qué se hizo y qué funcionó, citando nº de parte y fecha. Si no hay nada parecido, dilo; puedes proponer comprobaciones generales, pero indicando que son sugerencias tuyas y no salen del histórico.
- Cuando el CONTEXTO incluya un "RESUMEN EXACTO" de avisos, usa sus cifras y listas tal cual: ya están calculadas, no vuelvas a contar el detalle ni señales diferencias con él. No enumeres todos los avisos salvo que te lo pidan.
- Al proponer el reparto de avisos entre técnicos nunca des prioridad a un aviso solo por ser más antiguo.
- Explica cómo se usa la aplicación cuando te lo pregunten, según la descripción de los módulos."""


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


class PeticionChat(BaseModel):
    mensaje: str
    modulo: str = 'inicio'
    usuario: str = ''
    pantalla: dict = {}
    historial: List[MensajeChat] = []


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
                f"cliente {_t(aviso.get('cliente'))}; máquina {_t(aviso.get('maquina'))}; marca {_t(aviso.get('marca'))}; "
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
@router.post("/chat")
def chat_ia(req: PeticionChat):
    inicio = time.time()
    if not req.mensaje.strip():
        raise HTTPException(status_code=400, detail="El mensaje está vacío.")
    modulo = req.modulo if req.modulo in NOMBRES_MODULO else 'inicio'
    req.modulo = modulo

    try:
        texto_pantalla, ambito, extra = contexto_pantalla(req)
        texto_historico, partes_usados = contexto_historico(req, ambito, extra)
        contexto = (
            f"Pantalla actual: {NOMBRES_MODULO[modulo]}. Usuario: {_t(req.usuario) or 'desconocido'}. "
            f"Fecha de hoy: {time.strftime('%Y-%m-%d')}.\n{texto_pantalla}\n\n{texto_historico}"
        )

        mensajes = [{"role": "system", "content": PROMPT_SISTEMA}]
        for m in req.historial[-MAX_TURNOS_HISTORIAL:]:
            mensajes.append({"role": "assistant" if m.rol == 'assistant' else "user", "content": _t(m.texto, 800)})
        mensajes.append({"role": "user", "content": f"CONTEXTO:\n{contexto}\n\nPREGUNTA:\n{req.mensaje.strip()}"})

        respuesta = cliente_ia().chat.completions.create(
            model=MODELO_IA,
            messages=mensajes,
            max_completion_tokens=MAX_TOKENS_RESPUESTA,
            reasoning_effort=ESFUERZO_RAZONAMIENTO,
        )
        texto = (respuesta.choices[0].message.content or '').strip()
        uso = respuesta.usage
        logger.info(
            f"/chat {modulo} | {round(time.time() - inicio, 2)}s | tokens entrada={getattr(uso, 'prompt_tokens', '?')} "
            f"salida={getattr(uso, 'completion_tokens', '?')} | partes={len(partes_usados)}"
        )
        return {
            "respuesta": texto or "No he podido generar una respuesta. Prueba a reformular la pregunta.",
            "partes": partes_usados,
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
