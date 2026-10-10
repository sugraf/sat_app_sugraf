# manuales/api_manuales.py
"""Manuales técnicos: catálogo de la carpeta "Manuales tecnicos" de Drive y
emparejamiento de máquinas con sus manuales.

La carpeta está organizada como  Marca / Modelo / ficheros  (PDF, accesos
directos .url a manuales online, zip...). Un mismo nombre de carpeta puede
cubrir varios modelos: "SG2-640_540_300", "VS-640i-540i-340i",
"SC-P7000_P9000", "IP-5620_5520 (Color painter W-64s)". De cada nombre se
sacan todas sus variantes para poder encontrar la carpeta a partir de la
marca, el modelo o el nombre de la máquina de un aviso o de Maquinas.xlsx.

Los manuales se abren a través de la app (/api/manuales/archivo/...), que los
descarga de Drive con la cuenta de servicio y los envía en streaming: así no
hace falta tener sesión en Google y cada dispositivo los abre con su
aplicación (visor de PDF, Excel, Word...). Solo se sirven ficheros del
catálogo. El catálogo y el Excel de máquinas se guardan en memoria unos minutos.

Lo usan la vista de Manuales (rutas /api/manuales/...) y la sección de
adjudicados (POST /api/manuales/buscar).
"""
import io
import json
import os
import re
import sys
import threading
import time
import unicodedata
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from pydantic import BaseModel

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)
from generador.api_generador import leer_clientes_maquinas
from manuales.rag_manuales import CARPETA_INDICES

FOLDER_ID = "13PVvdyJUQCrDnamESBT0dqVhQQUaA5Na"  # Mi unidad/Manuales tecnicos
MIME_CARPETA = 'application/vnd.google-apps.folder'
MIME_ACCESO_DIRECTO = 'application/vnd.google-apps.shortcut'
SEGUNDOS_CACHE = 600
PADRES_POR_CONSULTA = 40
MAX_NIVELES = 6
BYTES_POR_TROZO = 8 * 1024 * 1024

# Ficheros de sistema que la sincronización de Drive puede subir y no son manuales.
FICHEROS_IGNORADOS = {'desktop.ini', 'thumbs.db', '.ds_store'}

# Documentos de Google: se descargan convertidos al formato de Office equivalente.
EXPORTAR_GOOGLE = {
    'application/vnd.google-apps.spreadsheet': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx'),
    'application/vnd.google-apps.document': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.docx'),
    'application/vnd.google-apps.presentation': ('application/vnd.openxmlformats-officedocument.presentationml.presentation', '.pptx'),
    'application/vnd.google-apps.drawing': ('application/pdf', '.pdf'),
}
# Lo que el navegador sabe mostrar se abre en él; el resto se descarga para abrirlo con su aplicación.
MIME_EN_NAVEGADOR = ('application/pdf', 'image/', 'video/', 'audio/', 'text/plain')

# Palabras que acompañan al modelo en algunos nombres de carpeta ("PLOTER ROLAND VF2-640").
PALABRAS_GENERICAS = {'PLOTER', 'PLOTTER', 'IMPRESORA', 'MAQUINA', 'MODELO', 'MANUAL', 'MANUALES'}
ALIAS_MARCA = {'HEWLETTPACKARD': 'HP', 'KONICA': 'KONICAMINOLTA', 'MINOLTA': 'KONICAMINOLTA', 'FUJI': 'FUJIFILM'}

router = APIRouter()

_lock = threading.Lock()
_cache = {'catalogo': None, 'catalogo_leido': 0.0, 'clientes': None, 'clientes_leido': 0.0}


def _drive():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON")), scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _email_cuenta_servicio():
    try:
        return json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON") or '{}').get('client_email', '')
    except ValueError:
        return ''


def _carpeta_raiz(drive):
    """Id real de "Manuales tecnicos" (si FOLDER_ID es un acceso directo, el de su destino).
    Si la cuenta de servicio no la ve, el error dice con qué correo hay que compartirla."""
    compartir = (f"Compártela (como Lector basta) con la cuenta de servicio de la app: {_email_cuenta_servicio()}"
                 if _email_cuenta_servicio() else "Compártela con la cuenta de servicio de la app.")
    try:
        carpeta = drive.files().get(
            fileId=FOLDER_ID, fields="id, name, mimeType, shortcutDetails", supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        if e.resp.status in (403, 404):
            raise RuntimeError(f"La app no tiene acceso a la carpeta 'Manuales tecnicos' de Drive. {compartir}")
        raise
    if carpeta.get('mimeType') == MIME_ACCESO_DIRECTO:
        return carpeta['shortcutDetails']['targetId'], carpeta['name'], compartir
    return carpeta['id'], carpeta['name'], compartir


# ---------------------------------------------------------------------------
# Normalización y variantes de modelo
# ---------------------------------------------------------------------------

def _sin_acentos(texto):
    texto = unicodedata.normalize('NFKD', str(texto or ''))
    return ''.join(c for c in texto if not unicodedata.combining(c)).upper()


def _alfanum(texto):
    return re.sub(r'[^A-Z0-9]', '', _sin_acentos(texto))


def clave_marca(marca):
    clave = _alfanum(marca)
    return ALIAS_MARCA.get(clave, clave)


def marcas_compatibles(a, b):
    return not a or not b or a in b or b in a


def _forma(texto):
    """'P9000' -> '[A-Z]+[0-9]+': patrón de letras/dígitos de una variante."""
    return ''.join('[0-9]+' if t[0].isdigit() else '[A-Z]+' for t in re.findall(r'[0-9]+|[A-Z]+', texto))


def _expandir_guiones(base):
    """'VS-640I-540I-340I' -> VS-640I, VS-540I, VS-340I; 'XPJ-1641SR-P' se queda igual."""
    trozos = base.split('-')
    if len(trozos) >= 3 and ' ' not in trozos[1] and all(_forma(t) == _forma(trozos[1]) for t in trozos[2:]):
        return [f'{trozos[0]}-{t}' for t in trozos[1:]]
    return [base]


def _expandir(nombre):
    """'SC-P7000_P9000' -> SC-P7000, SC-P9000; 'CJV150_75_107_160' -> CJV150, CJV75, ..."""
    nombre = re.sub(r'\s+-\s+', '_', nombre)
    trozos = [t.strip() for t in nombre.split('_') if t.strip()]
    if not trozos:
        return set()
    bases = _expandir_guiones(trozos[0])
    variantes = set(bases)
    base = bases[0]
    for alt in trozos[1:]:
        if ' ' in alt:  # nombre completo: "ACCURIOPRESS C3070_ACCURIOPRINT C3070L"
            variantes.add(alt)
            continue
        m = re.search(_forma(alt) + '$', base) if _forma(alt) else None
        if m:
            variantes.add(base[:m.start()] + alt)
        elif ' ' in base:  # "BIZHUB PRO C1060L_C1060" -> sustituye la última palabra
            variantes.add(base.rsplit(' ', 1)[0] + ' ' + alt)
        else:
            variantes.add(alt)
    return variantes


def variantes_modelo(nombre_carpeta, marca=''):
    """Claves alfanuméricas de todos los modelos que cubre una carpeta."""
    texto = _sin_acentos(nombre_carpeta)
    alias = re.findall(r'\(([^)]*)\)', texto)
    principal = re.sub(r'\([^)]*\)', ' ', texto)
    quitar = PALABRAS_GENERICAS | set(re.findall(r'[A-Z0-9]+', _sin_acentos(marca)))

    variantes = set()
    for parte in [principal, *alias]:
        parte = ' '.join(p for p in parte.split() if p not in quitar)
        variantes |= _expandir(parte)
    claves = {_alfanum(v) for v in variantes}
    # En el Excel el modelo suele venir sin la familia: "DESIGNJET Z6200" -> también "Z6200".
    claves |= {_alfanum(v.split()[-1]) for v in variantes if ' ' in v and _contenible(_alfanum(v.split()[-1]))}
    return {c for c in claves if len(c) >= 3}


def _contenible(clave):
    """Una clave corta y sin dígitos ("PRO", "640") no sirve para buscarla dentro de otra."""
    tiene_digito = any(c.isdigit() for c in clave)
    tiene_letra = any(c.isalpha() for c in clave)
    return (len(clave) >= 3 and tiene_digito and tiene_letra) or len(clave) >= 6


def _puntuacion(claves, k_modelo, k_maquina):
    """(nivel, longitud) de la mejor coincidencia entre la carpeta y la máquina:
    3 = modelo idéntico, 2 = el modelo de la carpeta aparece en el modelo o en el
    nombre de la máquina, 1 = el modelo de la máquina aparece en el de la carpeta."""
    mejor = None
    for v in claves:
        if v in (k_modelo, k_maquina):
            p = (3, len(v))
        elif _contenible(v) and (v in k_modelo or v in k_maquina):
            p = (2, len(v))
        elif k_modelo and len(k_modelo) >= 4 and _contenible(k_modelo) and k_modelo in v:
            p = (1, len(k_modelo))
        else:
            continue
        if mejor is None or p > mejor:
            mejor = p
    return mejor


def buscar_carpetas(catalogo, marca='', modelo='', maquina=''):
    """Carpetas de modelo que corresponden a una máquina: las de mejor coincidencia
    (puede haber varias, p. ej. "VF2-640" y "PLOTER ROLAND VF2-640")."""
    k_marca, k_modelo, k_maquina = clave_marca(marca), _alfanum(modelo), _alfanum(maquina)
    if not k_modelo and not k_maquina:
        return []
    puntuadas = []
    for carpeta in catalogo['carpetas']:
        if not marcas_compatibles(carpeta['clave_marca'], k_marca):
            continue
        p = _puntuacion(carpeta['claves'], k_modelo, k_maquina)
        if p:
            puntuadas.append((p, carpeta))
    if not puntuadas:
        return []
    mejor = max(p for p, _ in puntuadas)
    return [{**c['publica'], 'aproximado': mejor[0] < 3} for p, c in puntuadas if p == mejor]


# ---------------------------------------------------------------------------
# Lectura de la carpeta de Drive
# ---------------------------------------------------------------------------

def _listar_hijos(drive, padres):
    """Elementos dentro de cualquiera de las carpetas `padres` (consultas agrupadas)."""
    elementos = []
    padres = list(padres)
    for i in range(0, len(padres), PADRES_POR_CONSULTA):
        lote = padres[i:i + PADRES_POR_CONSULTA]
        q = '(' + ' or '.join(f"'{p}' in parents" for p in lote) + ') and trashed = false'
        token = None
        while True:
            res = drive.files().list(
                q=q, pageSize=1000, pageToken=token,
                fields="nextPageToken, files(id, name, mimeType, parents, size, modifiedTime, webViewLink, shortcutDetails)",
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            elementos += res.get('files', [])
            token = res.get('nextPageToken')
            if not token:
                break
    return elementos


def _es_carpeta(e):
    return e['mimeType'] == MIME_CARPETA or (
        e['mimeType'] == MIME_ACCESO_DIRECTO and e.get('shortcutDetails', {}).get('targetMimeType') == MIME_CARPETA
    )


def _id_real(e):
    return e.get('shortcutDetails', {}).get('targetId') or e['id']


def _tipo(nombre, mime):
    ext = nombre.rsplit('.', 1)[-1].lower() if '.' in nombre else ''
    if ext == 'pdf' or mime == 'application/pdf':
        return 'pdf'
    if ext in ('url', 'webloc', 'website'):
        return 'web'
    if ext in ('zip', 'rar', '7z'):
        return 'zip'
    if ext in ('doc', 'docx', 'odt', 'rtf', 'txt') or mime == 'application/vnd.google-apps.document':
        return 'doc'
    if ext in ('xls', 'xlsx', 'csv') or mime == 'application/vnd.google-apps.spreadsheet':
        return 'hoja'
    if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp') or mime.startswith('image/'):
        return 'imagen'
    if ext in ('mp4', 'mov', 'avi') or mime.startswith('video/'):
        return 'video'
    return 'otro'


def _url_acceso_web(drive, file_id):
    """Dirección guardada en un acceso directo de Windows (.url): línea URL=..."""
    try:
        contenido = drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        m = re.search(r'^\s*URL\s*=\s*(\S+)', contenido.decode('utf-8', 'ignore'), re.MULTILINE | re.IGNORECASE)
        return m.group(1) if m else ''
    except Exception:
        return ''


def _ignorado(e):
    nombre = e['name'].strip().lower()
    return nombre in FICHEROS_IGNORADOS or nombre.startswith(('~$', '.'))


def _archivo(drive, e, ruta, descargas):
    """Datos públicos del fichero; en `descargas` se guarda lo necesario para servirlo."""
    nombre = e['name']
    mime = e.get('shortcutDetails', {}).get('targetMimeType') or e.get('mimeType', '')
    tipo = _tipo(nombre, mime)
    url = ''
    if tipo == 'web':
        url = _url_acceso_web(drive, e['id'])
    if not url:
        mime_descarga, extension = EXPORTAR_GOOGLE.get(mime, (mime or 'application/octet-stream', ''))
        if extension and not nombre.lower().endswith(extension):
            nombre += extension
        descargas[e['id']] = {
            'drive_id': _id_real(e), 'mime_drive': mime, 'mime': mime_descarga, 'nombre': nombre,
            'ruta': '/'.join([*ruta, e['name']]),  # dentro de la carpeta de modelo; la usan los índices del RAG
            'tamano': 0 if extension else int(e.get('size') or 0),
        }
        url = f"/api/manuales/archivo/{e['id']}/{quote(nombre)}"
    titulo = re.sub(r'\.(pdf|url|webloc|website)$', '', nombre, flags=re.IGNORECASE)
    return {
        'id': e['id'],
        'nombre': nombre,
        'titulo': ' / '.join([*ruta, titulo]),
        'tipo': tipo,
        'tamano': int(e['size']) if e.get('size') else 0,
        'url': url,
    }


def _orden_natural(texto):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', _sin_acentos(texto))]


def leer_catalogo(drive):
    raiz, nombre, compartir = _carpeta_raiz(drive)
    hijos = _listar_hijos(drive, [raiz])
    # Las carpetas que empiezan por "_" no son marcas (p. ej. los índices del asistente).
    marcas = [e for e in hijos if _es_carpeta(e) and not e['name'].startswith('_')]
    carpeta_indices = next((e for e in hijos if _es_carpeta(e) and e['name'] == CARPETA_INDICES), None)
    if not marcas:
        raise RuntimeError(
            f"La app ve la carpeta '{nombre}' de Drive pero no encuentra dentro carpetas de marcas "
            f"({len(hijos)} elemento(s) visibles). Si las subcarpetas no están dentro de esa carpeta "
            f"compartida, la app no las ve. {compartir}"
        )
    marca_de = {_id_real(m): m['name'].strip() for m in marcas}
    descargas = {}  # id del fichero -> datos para servirlo (solo se sirven los del catálogo)

    # Carpetas de modelo: segundo nivel. Los ficheros sueltos en la carpeta de una
    # marca se agrupan en un "modelo" General de esa marca.
    modelos = {}  # id -> {marca, nombre, archivos}
    ubicacion = {}  # id de carpeta -> (id de la carpeta de modelo, ruta de subcarpetas)
    pendientes = []
    for e in _listar_hijos(drive, marca_de):
        if _ignorado(e):
            continue
        marca_id = e['parents'][0] if e.get('parents') else None
        marca = marca_de.get(marca_id, '')
        if _es_carpeta(e):
            modelos[_id_real(e)] = {'marca': marca, 'nombre': e['name'].strip(), 'archivos': []}
            ubicacion[_id_real(e)] = (_id_real(e), [])
            pendientes.append(_id_real(e))
        else:
            general = f'general-{marca_id}'
            modelos.setdefault(general, {'marca': marca, 'nombre': 'General', 'archivos': []})
            modelos[general]['archivos'].append(_archivo(drive, e, [], descargas))

    # Contenido de cada modelo, incluidas sus subcarpetas (nivel a nivel).
    for _ in range(MAX_NIVELES):
        if not pendientes:
            break
        siguientes = []
        for e in _listar_hijos(drive, pendientes):
            padre = next((p for p in e.get('parents', []) if p in ubicacion), None)
            if padre is None or _ignorado(e):
                continue
            modelo_id, ruta = ubicacion[padre]
            if _es_carpeta(e):
                ubicacion[_id_real(e)] = (modelo_id, [*ruta, e['name'].strip()])
                siguientes.append(_id_real(e))
            else:
                modelos[modelo_id]['archivos'].append(_archivo(drive, e, ruta, descargas))
        pendientes = siguientes

    carpetas = []
    for modelo_id, m in modelos.items():
        m['archivos'].sort(key=lambda a: _orden_natural(a['titulo']))
        publica = {'id': modelo_id, 'marca': m['marca'], 'nombre': m['nombre'], 'archivos': m['archivos']}
        carpetas.append({
            'publica': publica,
            'clave_marca': clave_marca(m['marca']),
            'claves': variantes_modelo(m['nombre'], m['marca']) if m['nombre'] != 'General' else set(),
        })
    carpetas.sort(key=lambda c: (_orden_natural(c['publica']['marca']), _orden_natural(c['publica']['nombre'])))
    indices = {}  # nombre del fichero de índice -> {id, modificado}
    if carpeta_indices:
        for e in _listar_hijos(drive, [_id_real(carpeta_indices)]):
            if e['name'].endswith('.json.gz'):
                indices[e['name']] = {'id': e['id'], 'modificado': e.get('modifiedTime', '')}
    return {'carpetas': carpetas, 'descargas': descargas, 'indices': indices, 'leido': time.time()}


def cargar_catalogo(refrescar=False):
    with _lock:
        if not refrescar and _cache['catalogo'] and time.time() - _cache['catalogo_leido'] < SEGUNDOS_CACHE:
            return _cache['catalogo']
    catalogo = leer_catalogo(_drive())
    with _lock:
        _cache.update(catalogo=catalogo, catalogo_leido=time.time())
    return catalogo


def cargar_clientes(refrescar=False):
    with _lock:
        if not refrescar and _cache['clientes'] is not None and time.time() - _cache['clientes_leido'] < SEGUNDOS_CACHE:
            return _cache['clientes']
    clientes = leer_clientes_maquinas(_drive())
    if 'error' in clientes and isinstance(clientes['error'], str):
        raise RuntimeError(clientes['error'])
    with _lock:
        _cache.update(clientes=clientes, clientes_leido=time.time())
    return clientes


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class MaquinaBuscada(BaseModel):
    marca: str = ''
    modelo: str = ''
    maquina: str = ''


class PeticionBuscar(BaseModel):
    maquinas: List[MaquinaBuscada]


@router.get("/catalogo")
def get_catalogo(refrescar: bool = False):
    """Todas las marcas con sus modelos y manuales, y los clientes de Maquinas.xlsx
    con las carpetas de manuales de cada una de sus máquinas."""
    try:
        catalogo = cargar_catalogo(refrescar)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo leer la carpeta de manuales: {e}")

    marcas = {}
    for c in catalogo['carpetas']:
        marcas.setdefault(c['publica']['marca'], []).append(c['publica'])

    clientes, error_clientes = [], ''
    try:
        for cliente in cargar_clientes(refrescar).values():
            maquinas = []
            for m in cliente['maquinas']:
                carpetas = buscar_carpetas(catalogo, m['marca'], m['modelo'], m['nombre'])
                maquinas.append({
                    'nombre': m['nombre'], 'equipo': m['equipo'], 'marca': m['marca'],
                    'modelo': m['modelo'], 'n_serie': m['n_serie'],
                    'carpetas': [c['id'] for c in carpetas],
                    'aproximado': any(c['aproximado'] for c in carpetas),
                })
            clientes.append({'nombre': cliente['nombre'], 'maquinas': maquinas})
        clientes.sort(key=lambda c: _orden_natural(c['nombre']))
    except Exception as e:
        error_clientes = f"No se pudo leer Maquinas.xlsx: {e}"

    return {
        'marcas': [{'nombre': nombre, 'modelos': modelos} for nombre, modelos in marcas.items()],
        'clientes': clientes,
        'error_clientes': error_clientes,
        'total_modelos': len(catalogo['carpetas']),
        'total_manuales': sum(len(c['publica']['archivos']) for c in catalogo['carpetas']),
    }


@router.post("/buscar")
def post_buscar(peticion: PeticionBuscar):
    """Carpetas de manuales de cada máquina pedida (mismo orden que la petición)."""
    try:
        catalogo = cargar_catalogo()
        return {'resultados': [buscar_carpetas(catalogo, m.marca, m.modelo, m.maquina) for m in peticion.maquinas]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo leer la carpeta de manuales: {e}")


def _trozos(peticion):
    """Contenido del fichero de Drive en trozos, para enviarlo según llega."""
    buffer = io.BytesIO()
    descarga = MediaIoBaseDownload(buffer, peticion, chunksize=BYTES_POR_TROZO)
    terminado = False
    while not terminado:
        _, terminado = descarga.next_chunk()
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()


@router.get("/archivo/{file_id}/{nombre}")
def get_archivo(file_id: str, nombre: str = ''):
    """Abre un manual: lo descarga de Drive y lo envía en streaming (sin el límite
    de tamaño de respuesta de Vercel). El nombre de la ruta solo sirve para que el
    navegador o el móvil guarden el fichero con su nombre."""
    try:
        catalogo = cargar_catalogo()
        if file_id not in catalogo['descargas'] and time.time() - catalogo['leido'] > 60:
            catalogo = cargar_catalogo(refrescar=True)  # puede ser un manual recién subido
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo leer la carpeta de manuales: {e}")
    datos = catalogo['descargas'].get(file_id)
    if not datos:
        raise HTTPException(status_code=404, detail="Este manual ya no está en la carpeta de Manuales técnicos.")

    drive = _drive()
    if datos['mime_drive'] in EXPORTAR_GOOGLE:
        peticion = drive.files().export_media(fileId=datos['drive_id'], mimeType=datos['mime'])
    else:
        peticion = drive.files().get_media(fileId=datos['drive_id'], supportsAllDrives=True)

    # El primer trozo se pide antes de responder para poder devolver un error claro.
    trozos = _trozos(peticion)
    try:
        primero = next(trozos)
    except HttpError as e:
        raise HTTPException(status_code=502, detail=f"Drive no ha entregado el manual: {e.reason}")

    def contenido():
        yield primero
        yield from trozos

    en_navegador = datos['mime'].startswith(MIME_EN_NAVEGADOR)
    nombre_ascii = re.sub(r'[^A-Za-z0-9._ -]', '_', unicodedata.normalize('NFKD', datos['nombre'])
                          .encode('ascii', 'ignore').decode()).strip() or 'manual'
    cabeceras = {
        'Content-Disposition': f"{'inline' if en_navegador else 'attachment'}; filename=\"{nombre_ascii}\"; "
                               f"filename*=UTF-8''{quote(datos['nombre'])}",
        'Cache-Control': 'private, max-age=3600',
    }
    if datos['tamano']:
        cabeceras['Content-Length'] = str(datos['tamano'])
    return StreamingResponse(contenido(), media_type=datos['mime'], headers=cabeceras)
