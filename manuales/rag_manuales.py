# manuales/rag_manuales.py
"""Búsqueda en el contenido de los manuales (RAG), siempre dentro de UNA máquina.

Índices: un fichero JSON comprimido por carpeta de modelo (Marca/Modelo) en la
subcarpeta "_indice_manuales" de "Manuales tecnicos". Cada índice guarda el texto
de sus PDF partido en fragmentos de unos 900 caracteres con su página:

    {"version": 1, "marca": "Roland", "modelo": "VG3-640",
     "manuales": [{"ruta": "VG3-640_USE_ES.pdf", "tamano": 123, "paginas": 80, "estado": "ok"}],
     "trozos": [[0, 12, "texto del fragmento..."], ...]}      # [nº de manual, página, texto]

Los genera manuales/indexar_manuales.py desde la carpeta sincronizada de Drive
(H:\\Mi unidad\\Manuales tecnicos); extraer el texto de un PDF grande tarda
minutos, así que no se hace al preguntar. Si a una máquina le falta algún PDF
pequeño en su índice (manual recién subido), se indexa al vuelo y se guarda en
memoria.

Al preguntar, una búsqueda BM25 sobre los fragmentos de esa máquina devuelve
solo los pocos más relevantes (con manual, página y enlace), en vez del
documento entero. El modelo escribe la consulta en español e inglés porque
muchos manuales están en inglés.
"""
import gzip
import io
import json
import logging
import math
import re
import threading
import unicodedata
from collections import Counter

INDICE_VERSION = 1
CARPETA_INDICES = '_indice_manuales'
TAM_TROZO = 900
SOLAPE_TROZO = 150
MIN_CARACTERES_PAGINA = 40
MAX_RESULTADOS = 6
MAX_CARACTERES_RESULTADO = 1200
# Al vuelo solo se indexan PDF pequeños: los grandes tardan demasiado para una pregunta.
MAX_BYTES_AL_VUELO = 25 * 1024 * 1024
MAX_PDFS_AL_VUELO = 4

logging.getLogger('pypdf').setLevel(logging.ERROR)

PALABRAS_VACIAS = set('''
a al algo ante con contra de del desde donde durante e el ella ellos en entre era es esa ese eso esta este esto
ha han hay la las le les lo los mas me mi muy no nos o os para pero por que se si sin sobre su sus te tu un una
uno unos y ya como cual cuando debe puede ser son todo
an and are as at be by can do for from has have how if in into is it its of on or that the their then there
these this to was what when where which while will with you your not no may must should
'''.split())

SUFIJOS = ('aciones', 'iciones', 'amiento', 'imiento', 'acion', 'icion', 'mente', 'ings', 'ing', 'es', 's')


# ---------------------------------------------------------------------------
# Texto: normalización, fragmentos y palabras clave
# ---------------------------------------------------------------------------

def _sin_acentos(texto):
    texto = unicodedata.normalize('NFKD', str(texto or ''))
    return ''.join(c for c in texto if not unicodedata.combining(c))


def nombre_indice(marca, modelo):
    """Nombre del fichero de índice de una carpeta de modelo (igual en el script y en el servidor)."""
    base = _sin_acentos(f'{marca}__{modelo}')
    return re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('_') + '.json.gz'


def _raiz(palabra):
    """Reducción mínima (plurales y sufijos comunes) para que "cuchillas" encuentre "cuchilla"."""
    if palabra.isdigit() or len(palabra) <= 4:
        return palabra
    for sufijo in SUFIJOS:
        if palabra.endswith(sufijo) and len(palabra) - len(sufijo) >= 4:
            return palabra[:-len(sufijo)]
    return palabra


def palabras_clave(texto):
    """Términos de búsqueda. Los códigos se guardan enteros y por partes: "116-324" -> 116324, 116, 324."""
    texto = _sin_acentos(texto).lower()
    terminos = []
    for t in re.findall(r'[a-z0-9]+(?:[-./][a-z0-9]+)*', texto):
        if re.search(r'[-./]', t):
            partes = [p for p in re.split(r'[-./]', t) if p]
            terminos.append(''.join(partes))
            terminos += [_raiz(p) for p in partes if len(p) > 1 and p not in PALABRAS_VACIAS]
        elif len(t) > 1 and t not in PALABRAS_VACIAS:
            terminos.append(_raiz(t))
    return terminos


def trocear(paginas):
    """[(página, texto del fragmento)] a partir del texto de cada página."""
    trozos = []
    for n, texto in enumerate(paginas, 1):
        texto = re.sub(r'[ \t\u00a0]+', ' ', texto or '')
        texto = re.sub(r'\s*\n\s*', '\n', texto).strip()
        if len(texto) < MIN_CARACTERES_PAGINA:
            continue
        i = 0
        while i < len(texto):
            fin = min(len(texto), i + TAM_TROZO)
            if fin < len(texto):
                # Se corta en un salto de línea o un punto para no partir frases.
                corte = max(texto.rfind('\n', i + TAM_TROZO // 2, fin), texto.rfind('. ', i + TAM_TROZO // 2, fin))
                if corte > i:
                    fin = corte + 1
            trozos.append((n, texto[i:fin].strip()))
            if fin >= len(texto):
                break
            i = max(fin - SOLAPE_TROZO, i + 1)
    return trozos


def extraer_paginas(origen):
    """(texto de cada página, estado) de un PDF (ruta o bytes). Usa PyMuPDF si está
    instalado (mucho más rápido, solo en local) y si no pypdf."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=origen, filetype='pdf') if isinstance(origen, (bytes, bytearray)) else fitz.open(origen)
        paginas = [p.get_text() for p in doc]
        doc.close()
    except ImportError:
        from pypdf import PdfReader
        try:
            lector = PdfReader(io.BytesIO(origen) if isinstance(origen, (bytes, bytearray)) else origen)
            paginas = []
            for pagina in lector.pages:
                try:
                    paginas.append(pagina.extract_text() or '')
                except Exception:
                    paginas.append('')
        except Exception as e:
            return [], f'error: {e}'
    except Exception as e:
        return [], f'error: {e}'
    con_texto = sum(1 for p in paginas if len(p.strip()) >= MIN_CARACTERES_PAGINA)
    return paginas, ('ok' if con_texto else 'sin_texto')


def indexar_pdf(origen):
    """(nº de páginas, estado, [(página, texto)])."""
    paginas, estado = extraer_paginas(origen)
    return len(paginas), estado, trocear(paginas) if estado == 'ok' else []


def leer_indice(contenido):
    datos = json.loads(gzip.decompress(contenido).decode('utf-8'))
    return datos if datos.get('version') == INDICE_VERSION else None


def escribir_indice(datos):
    return gzip.compress(json.dumps(datos, ensure_ascii=False, separators=(',', ':')).encode('utf-8'), compresslevel=6)


# ---------------------------------------------------------------------------
# BM25 sobre los fragmentos de una máquina
# ---------------------------------------------------------------------------

class Buscador:
    K1 = 1.4
    B = 0.75

    def __init__(self, trozos):
        """trozos: [(nº de manual, página, texto)]."""
        self.trozos = trozos
        self.frecuencias = [Counter(palabras_clave(t)) for _, _, t in trozos]
        self.longitudes = [sum(f.values()) for f in self.frecuencias]
        self.media = (sum(self.longitudes) / len(self.longitudes)) if self.longitudes else 1
        self.documentos = Counter()
        for f in self.frecuencias:
            self.documentos.update(f.keys())
        # Páginas de índice del manual ("Limpieza ........ 242"): casi nunca responden a nada.
        self.es_indice = [len(re.findall(r'\.{5,}', t)) >= 3 for _, _, t in trozos]

    def buscar(self, consulta, manuales=None, k=MAX_RESULTADOS):
        """Posiciones de los k fragmentos más relevantes; `manuales` limita a esos nº de manual."""
        terminos = list(dict.fromkeys(palabras_clave(consulta)))
        if not terminos or not self.trozos:
            return []
        n = len(self.trozos)
        idf = {t: math.log(1 + (n - self.documentos[t] + 0.5) / (self.documentos[t] + 0.5)) for t in terminos if self.documentos[t]}
        puntos = []
        for i, f in enumerate(self.frecuencias):
            if manuales is not None and self.trozos[i][0] not in manuales:
                continue
            p = 0.0
            for t, peso in idf.items():
                tf = f.get(t)
                if tf:
                    p += peso * tf * (self.K1 + 1) / (tf + self.K1 * (1 - self.B + self.B * self.longitudes[i] / self.media))
            if p > 0:
                # Premio a los fragmentos que reúnen más términos distintos de la consulta.
                cubiertos = sum(1 for t in idf if t in f)
                puntos.append((p * (1 + 0.15 * cubiertos) * (0.25 if self.es_indice[i] else 1), i))
        puntos.sort(reverse=True)

        # Como mucho dos fragmentos de la misma página, para dar variedad.
        elegidos, por_pagina = [], Counter()
        for _, i in puntos:
            clave = self.trozos[i][:2]
            if por_pagina[clave] >= 2:
                continue
            por_pagina[clave] += 1
            elegidos.append(i)
            if len(elegidos) == k:
                break
        return elegidos


# ---------------------------------------------------------------------------
# Índices de las carpetas (Drive) y consulta
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cache = {}  # id de carpeta -> (firma, índice, Buscador)


def _pdfs_de(catalogo, carpeta):
    """[(archivo público, datos de descarga)] de los PDF de una carpeta de modelo."""
    return [(a, catalogo['descargas'][a['id']]) for a in carpeta['archivos']
            if a['tipo'] == 'pdf' and a['id'] in catalogo['descargas']]


def _descargar(drive, file_id):
    from googleapiclient.http import MediaIoBaseDownload
    buffer = io.BytesIO()
    descarga = MediaIoBaseDownload(buffer, drive.files().get_media(fileId=file_id, supportsAllDrives=True),
                                   chunksize=16 * 1024 * 1024)
    terminado = False
    while not terminado:
        _, terminado = descarga.next_chunk()
    return buffer.getvalue()


def _indice_carpeta(drive, catalogo, carpeta):
    """(índice, Buscador, pendientes) de una carpeta: el guardado en Drive más los PDF
    pequeños que le falten, indexados al vuelo. `pendientes` = PDF sin indexar."""
    pdfs = _pdfs_de(catalogo, carpeta)
    nombre = nombre_indice(carpeta['marca'], carpeta['nombre'])
    guardado = catalogo.get('indices', {}).get(nombre)
    firma = (guardado or {}).get('modificado', ''), tuple(sorted((d['ruta'], d['tamano']) for _, d in pdfs))

    with _lock:
        if carpeta['id'] in _cache and _cache[carpeta['id']][0] == firma:
            return _cache[carpeta['id']][1:]

    indice = None
    if guardado:
        try:
            indice = leer_indice(drive.files().get_media(fileId=guardado['id'], supportsAllDrives=True).execute())
        except Exception as e:
            logging.getLogger('rag_manuales').warning(f'No se pudo leer el índice {nombre}: {e}')
    indice = indice or {'version': INDICE_VERSION, 'marca': carpeta['marca'], 'modelo': carpeta['nombre'],
                        'manuales': [], 'trozos': []}

    # Lo que ya no está en la carpeta o ha cambiado de tamaño no vale.
    actuales = {(d['ruta'], d['tamano']) for _, d in pdfs}
    validos = [i for i, m in enumerate(indice['manuales']) if (m['ruta'], m['tamano']) in actuales]
    nuevo_numero = {viejo: nuevo for nuevo, viejo in enumerate(validos)}
    indice['manuales'] = [indice['manuales'][i] for i in validos]
    indice['trozos'] = [[nuevo_numero[m], p, t] for m, p, t in indice['trozos'] if m in nuevo_numero]

    indexados = {(m['ruta'], m['tamano']) for m in indice['manuales']}
    faltan = sorted((d for _, d in pdfs if (d['ruta'], d['tamano']) not in indexados), key=lambda d: d['tamano'])
    pendientes, al_vuelo, bytes_vuelo = [], 0, 0
    for d in faltan:
        if al_vuelo >= MAX_PDFS_AL_VUELO or bytes_vuelo + d['tamano'] > MAX_BYTES_AL_VUELO:
            pendientes.append(d['ruta'])
            continue
        try:
            paginas, estado, trozos = indexar_pdf(_descargar(drive, d['drive_id']))
        except Exception as e:
            paginas, estado, trozos = 0, f'error: {e}', []
        numero = len(indice['manuales'])
        indice['manuales'].append({'ruta': d['ruta'], 'tamano': d['tamano'], 'paginas': paginas, 'estado': estado})
        indice['trozos'] += [[numero, p, t] for p, t in trozos]
        al_vuelo += 1
        bytes_vuelo += d['tamano']

    buscador = Buscador([tuple(t) for t in indice['trozos']])
    with _lock:
        _cache[carpeta['id']] = (firma, indice, buscador, pendientes)
    return indice, buscador, pendientes


def buscar_maquina(catalogo, buscar_carpetas, texto, marca=''):
    """Carpetas de modelo candidatas para un nombre de máquina (como mucho 8)."""
    candidatas = buscar_carpetas(catalogo, marca, texto, texto)
    if not candidatas:
        clave = re.sub(r'[^A-Z0-9]', '', _sin_acentos(texto).upper())
        candidatas = [c['publica'] for c in catalogo['carpetas']
                      if clave and len(clave) >= 3
                      and clave in re.sub(r'[^A-Z0-9]', '', _sin_acentos(f"{c['publica']['marca']}{c['publica']['nombre']}").upper())]
    return [{
        'carpeta_id': c['id'], 'marca': c['marca'], 'modelo': c['nombre'],
        'manuales_pdf': sum(1 for a in c['archivos'] if a['tipo'] == 'pdf'),
        'aproximado': c.get('aproximado', False),
    } for c in candidatas[:8]]


def consultar(drive, catalogo, carpeta_id, consulta, manual=''):
    """Fragmentos de los manuales de UNA carpeta de modelo más relevantes para la consulta."""
    carpeta = next((c['publica'] for c in catalogo['carpetas'] if c['publica']['id'] == carpeta_id), None)
    if not carpeta:
        return {'ok': False, 'error': 'No existe esa carpeta de manuales. Búscala antes con buscar_maquina_manuales.'}
    pdfs = _pdfs_de(catalogo, carpeta)
    maquina = f"{carpeta['marca']} · {carpeta['nombre']}"
    if not pdfs:
        online = [a['titulo'] for a in carpeta['archivos'] if a['tipo'] == 'web']
        return {'ok': False, 'maquina': maquina, 'error': 'Esta máquina no tiene manuales en PDF que se puedan consultar.'
                + (f" Tiene manuales online: {', '.join(online)}." if online else '')}

    indice, buscador, pendientes = _indice_carpeta(drive, catalogo, carpeta)
    por_ruta = {d['ruta']: a for a, d in pdfs}
    numeros = None
    if manual:
        buscado = _sin_acentos(manual).lower()
        numeros = {i for i, m in enumerate(indice['manuales']) if buscado in _sin_acentos(m['ruta']).lower()} or None

    resultados = []
    for i in buscador.buscar(consulta, numeros):
        m, pagina, texto = buscador.trozos[i]
        archivo = por_ruta.get(indice['manuales'][m]['ruta'], {})
        resultados.append({
            'manual': archivo.get('titulo', indice['manuales'][m]['ruta']),
            'pagina': pagina,
            'enlace': f"{archivo['url']}#page={pagina}" if archivo.get('url') else '',
            'texto': texto[:MAX_CARACTERES_RESULTADO],
        })

    sin_texto = [m['ruta'] for m in indice['manuales'] if m['estado'] != 'ok']
    salida = {'ok': True, 'maquina': maquina, 'manuales_consultados': len(indice['manuales']) - len(sin_texto),
              'resultados': resultados}
    if not resultados:
        salida['aviso'] = 'Ningún fragmento coincide. Prueba con otros términos (en inglés si el manual está en inglés).'
    if sin_texto:
        salida['sin_texto'] = sin_texto
    if pendientes:
        salida['pendientes_de_indexar'] = pendientes
    return salida
