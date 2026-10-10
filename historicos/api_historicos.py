# historicos/api_historicos.py
"""Históricos SAT: lectura, búsqueda y API del histórico exportado desde Pirineos.

La carpeta "Historico SAT" de Drive contiene:
  - SATRealizados_<AÑO>.xlsx  -> un fichero por año
  - SATRealizadosActual.xlsx  -> partes del año en curso, se actualiza a menudo
  - SATRealizados.xlsx        -> histórico completo (solo se usa si no hay ficheros anuales)

Los partes se deduplican por N_Parte, prevaleciendo SATRealizadosActual.xlsx.
Los ficheros descargados se guardan en memoria mientras la instancia siga viva
y solo se vuelven a descargar si cambia su modifiedTime en Drive.

Lo usan la vista de Históricos (rutas /api/historicos/...) y la sección de
adjudicados (buscar_por_aviso).
"""
import io
import json
import math
import os
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import google_auth_httplib2
import httplib2
import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from openpyxl import load_workbook

FOLDER_ID = "1o8Y2jrD86LBkxxmCnfH5tiacuFd1IJ51"  # Pirineos/Historico SAT
FICHERO_ACTUAL = 'SATRealizadosActual.xlsx'
FICHERO_COMPLETO = 'SATRealizados.xlsx'
PATRON_ANUAL = re.compile(r'^SATRealizados_(\d{4})\.xlsx$', re.IGNORECASE)
MIME_SHEETS = 'application/vnd.google-apps.spreadsheet'
MIME_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
MAX_REGISTROS = 1000
MAX_POR_PAGINA = 200

FORMAS_JURIDICAS = {
    'SA', 'SL', 'SLU', 'SAU', 'SLL', 'SLNE', 'SC', 'SCP', 'SCOOP', 'SCL', 'CB', 'COOP',
}

router = APIRouter()

_lock = threading.Lock()
_cache_ficheros = {}  # file_id -> (modifiedTime, [registros])
_cache_indice = {}  # version (ids + modifiedTime) -> IndiceHistorico ya construido


def _credenciales():
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    return service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    )


def _ficheros_a_leer(drive, folder_id):
    """Devuelve los ficheros del histórico en orden de prioridad creciente."""
    res = drive.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id, name, mimeType, modifiedTime)",
        pageSize=200,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    por_nombre = {f['name']: f for f in res.get('files', [])}

    anuales = sorted(
        (f for nombre, f in por_nombre.items() if PATRON_ANUAL.match(nombre)),
        key=lambda f: f['name'],
    )
    ficheros = anuales or ([por_nombre[FICHERO_COMPLETO]] if FICHERO_COMPLETO in por_nombre else [])
    if FICHERO_ACTUAL in por_nombre:
        ficheros.append(por_nombre[FICHERO_ACTUAL])
    return ficheros


def _texto(valor):
    if valor is None:
        return ''
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return str(valor).strip()


def _fecha(valor):
    if isinstance(valor, datetime):
        return valor.strftime('%Y-%m-%d')
    return _texto(valor)[:10]


def _leer_registros(contenido):
    wb = load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
    filas = wb.worksheets[0].iter_rows(values_only=True)
    cabecera = [_texto(c) for c in next(filas, [])]
    registros = []
    for fila in filas:
        d = dict(zip(cabecera, fila))
        n_parte = _texto(d.get('N_Parte'))
        if not n_parte:
            continue
        registros.append({
            'n_parte': n_parte,
            'fecha_entrada': _fecha(d.get('Fecha_Entrada')),
            'fecha': _fecha(d.get('Fecha_Realizado')) or _fecha(d.get('Fecha_Entrada')),
            'cliente': _texto(d.get('Nombre')),
            'nombre_comercial': _texto(d.get('Nombre_Comercial')),
            'equipo': _texto(d.get('Equipo')),
            'marca': _texto(d.get('Marca')),
            'modelo': _texto(d.get('Modelo')),
            'n_serie': _texto(d.get('N_Serie')),
            'tecnico': _texto(d.get('Tecnico')),
            'anomalia': _texto(d.get('Anomalia')),
            'solucion': _texto(d.get('Solucion')),
        })
    wb.close()
    return registros


def _descargar(drive, creds, fichero):
    if fichero.get('mimeType') == MIME_SHEETS:
        peticion = drive.files().export_media(fileId=fichero['id'], mimeType=MIME_XLSX)
    else:
        peticion = drive.files().get_media(fileId=fichero['id'], supportsAllDrives=True)
    # httplib2 no es thread-safe: cada descarga usa su propia conexión.
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=60))
    return _leer_registros(peticion.execute(http=http))


def cargar_historico():
    """Devuelve (IndiceHistorico, nombres_de_ficheros) con todo el histórico deduplicado."""
    creds = _credenciales()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    ficheros = _ficheros_a_leer(drive, FOLDER_ID)
    if not ficheros:
        raise RuntimeError(
            "No se encuentran los Excel en la carpeta Historico SAT de Drive. "
            "Comprueba que está compartida con la cuenta de servicio de la app."
        )
    nombres = [f['name'] for f in ficheros]
    version = tuple((f['id'], f.get('modifiedTime')) for f in ficheros)

    with _lock:
        if _cache_indice.get('version') == version:
            return _cache_indice['indice'], nombres
        pendientes = [
            f for f in ficheros
            if _cache_ficheros.get(f['id'], (None,))[0] != f.get('modifiedTime')
        ]
    if pendientes:
        with ThreadPoolExecutor(max_workers=8) as pool:
            descargados = list(pool.map(lambda f: _descargar(drive, creds, f), pendientes))
        with _lock:
            for f, registros in zip(pendientes, descargados):
                _cache_ficheros[f['id']] = (f.get('modifiedTime'), registros)

    por_parte = {}
    with _lock:
        for f in ficheros:
            for r in _cache_ficheros[f['id']][1]:
                por_parte[r['n_parte']] = r
    indice = IndiceHistorico(list(por_parte.values()))
    with _lock:
        _cache_indice.update(version=version, indice=indice)
    return indice, nombres


# ---------------------------------------------------------------------------
# Normalización y búsqueda
# ---------------------------------------------------------------------------

def _sin_acentos(texto):
    texto = unicodedata.normalize('NFKD', _texto(texto))
    return ''.join(c for c in texto if not unicodedata.combining(c)).upper()


def _alfanum(texto):
    return re.sub(r'[^A-Z0-9]', '', _sin_acentos(texto))


def clave_cliente(nombre):
    """'GISTEL, S.L. INDUSTRIAS GRAFICAS' -> 'GISTEL INDUSTRIAS GRAFICAS'."""
    texto = _sin_acentos(nombre)
    # Une las siglas con puntos (S.L.U. -> SLU) antes de separar en palabras.
    texto = re.sub(r'\b((?:[A-Z]\.){1,3}[A-Z]?)\.?(?=\W|$)', lambda m: m.group(1).replace('.', ''), texto)
    palabras = re.findall(r'[A-Z0-9]+', texto)
    return ' '.join(p for p in palabras if p not in FORMAS_JURIDICAS)


def clave_serie(serie):
    valor = _texto(serie)
    if valor.endswith('.0'):
        valor = valor[:-2]
    valor = _alfanum(valor).lstrip('0')
    return valor if len(valor) >= 3 else ''


def clave_modelo(modelo):
    valor = _alfanum(modelo)
    return valor if len(valor) >= 3 else ''


class IndiceHistorico:
    """Registros del histórico con sus claves de búsqueda precalculadas."""

    def __init__(self, registros):
        self.registros = registros
        self.claves = [
            {
                'clientes': {clave_cliente(r['cliente']), clave_cliente(r['nombre_comercial'])} - {''},
                'serie': clave_serie(r['n_serie']),
                'modelo': clave_modelo(r['modelo']),
                # Textos normalizados para los filtros de la vista de Históricos.
                'txt_cliente': _sin_acentos(f"{r['cliente']} {r['nombre_comercial']}"),
                'alf_modelo': _alfanum(r['modelo']),
                'alf_serie': _alfanum(r['n_serie']),
            }
            for r in registros
        ]
        # Un "modelo" que coincide con un tipo de equipo (GRAPADORA, RETRACTILADORA...)
        # es genérico y no sirve para buscar la misma máquina en otros clientes.
        self.modelos_genericos = {_alfanum(r['equipo']) for r in registros} - {''}
        self.modelos = {k['modelo'] for k in self.claves} - {''} - self.modelos_genericos


def opciones(indice):
    """Combinaciones distintas cliente / modelo / nº serie para los desplegables
    (la vista deduce de ellas qué modelos tiene un cliente y qué series un modelo)."""
    combinaciones = {}
    for r in indice.registros:
        clave = (r['cliente'], r['modelo'], r['n_serie'])
        if clave not in combinaciones:
            combinaciones[clave] = {
                'cliente': r['cliente'], 'comercial': r['nombre_comercial'],
                'modelo': r['modelo'], 'n_serie': r['n_serie'],
            }
    fechas = sorted(r['fecha'] for r in indice.registros if r['fecha'])
    return {
        'total': len(indice.registros),
        'combinaciones': list(combinaciones.values()),
        'fecha_min': fechas[0] if fechas else '',
        'fecha_max': fechas[-1] if fechas else '',
    }


def _coincide(valor, buscado, conocidos):
    """Igualdad si lo buscado es un valor existente (elegido en el desplegable);
    si se ha escrito a mano, basta con que esté contenido."""
    return valor == buscado if buscado in conocidos else buscado in valor


def filtrar(indice, cliente='', modelo='', n_serie='', desde='', hasta='', orden='desc'):
    """Filtro de la vista de Históricos. Devuelve las posiciones que cumplen todos
    los filtros, ordenadas por fecha. Ignora acentos, mayúsculas y signos
    ("VG3-640" = "VG3640"); el cliente exige que aparezcan todas sus palabras."""
    palabras_cliente = _sin_acentos(cliente).split()
    modelo_a, serie_a = _alfanum(modelo), _alfanum(n_serie)
    modelos = {k['alf_modelo'] for k in indice.claves} if modelo_a else set()
    series = {k['alf_serie'] for k in indice.claves} if serie_a else set()

    posiciones = []
    for i, (r, k) in enumerate(zip(indice.registros, indice.claves)):
        if desde and r['fecha'] < desde:
            continue
        if hasta and r['fecha'] > hasta:
            continue
        if palabras_cliente and not all(p in k['txt_cliente'] for p in palabras_cliente):
            continue
        if modelo_a and not _coincide(k['alf_modelo'], modelo_a, modelos):
            continue
        if serie_a and not _coincide(k['alf_serie'], serie_a, series):
            continue
        posiciones.append(i)

    registros = indice.registros
    posiciones.sort(key=lambda i: (registros[i]['fecha'], registros[i]['n_parte']), reverse=(orden != 'asc'))
    return posiciones


def _contiene_cliente(claves_registro, clave):
    """Coincidencia laxa: uno de los nombres contiene al otro como palabras completas."""
    for otra in claves_registro:
        if len(otra) >= 6 and (f' {otra} ' in f' {clave} ' or f' {clave} ' in f' {otra} '):
            return True
    return False


def _marcas_compatibles(marca_a, marca_b):
    a, b = _alfanum(marca_a), _alfanum(marca_b)
    return not a or not b or a in b or b in a


def _modelo_desde_nombre(indice, maquina):
    """Busca el modelo más largo (con algún dígito) contenido en el nombre de la máquina."""
    texto = _alfanum(maquina)
    encontrados = [m for m in indice.modelos if len(m) >= 4 and re.search(r'\d', m) and m in texto]
    return max(encontrados, key=len) if encontrados else ''


def buscar_por_aviso(indice, cliente='', maquina='', marca='', modelo='', n_serie=''):
    registros, claves = indice.registros, indice.claves
    # "SIJALON / SUMINISTROS INDUSTRIALES JALON SL": nombre comercial / razón social.
    claves_cli = {clave_cliente(p) for p in [cliente, *_texto(cliente).split('/')]} - {''}
    serie = clave_serie(n_serie)

    # --- Histórico del cliente (todas sus máquinas) ---
    pos_cliente = []
    criterio_cliente = ''
    if claves_cli:
        pos_cliente = [i for i, k in enumerate(claves) if k['clientes'] & claves_cli]
        criterio_cliente = 'nombre exacto'
        if not pos_cliente:
            pos_cliente = [
                i for i, k in enumerate(claves)
                if any(_contiene_cliente(k['clientes'], c) for c in claves_cli)
            ]
            criterio_cliente = 'nombre aproximado'
    set_cliente = set(pos_cliente)

    # --- Histórico de la máquina (misma unidad + mismo modelo en otros clientes) ---
    pos_unidad = [i for i, k in enumerate(claves) if serie and k['serie'] == serie]
    set_unidad = set(pos_unidad)
    modelos = {clave_modelo(modelo)} | {claves[i]['modelo'] for i in pos_unidad}
    modelos -= {''} | indice.modelos_genericos

    def por_modelo(modelos):
        return [
            i for i, k in enumerate(claves)
            if i not in set_unidad and k['modelo'] in modelos
            and _marcas_compatibles(registros[i]['marca'], marca)
        ]

    pos_modelo = por_modelo(modelos) if modelos else []
    if not pos_unidad and not pos_modelo and maquina:
        # Sin resultados por serie/modelo: se deduce el modelo del nombre de la máquina.
        deducido = _modelo_desde_nombre(indice, maquina)
        if deducido:
            modelos = {deducido}
            pos_modelo = por_modelo(modelos)

    criterio_maquina = []
    if serie:
        criterio_maquina.append(f'nº serie {n_serie}')
    if modelos:
        criterio_maquina.append('modelo ' + ', '.join(sorted(modelos)))
    pos_maquina = pos_unidad + pos_modelo

    def anotar(posiciones):
        posiciones = sorted(
            posiciones,
            key=lambda i: (registros[i]['fecha'], registros[i]['n_parte']),
            reverse=True,
        )
        return [
            {**registros[i], 'misma_unidad': i in set_unidad, 'mismo_cliente': i in set_cliente}
            for i in posiciones[:MAX_REGISTROS]
        ], len(posiciones)

    reg_maquina, total_maquina = anotar(pos_maquina)
    reg_cliente, total_cliente = anotar(pos_cliente)
    return {
        'maquina': {
            'registros': reg_maquina,
            'total': total_maquina,
            'otros_clientes': sum(1 for i in pos_maquina if i not in set_cliente),
            'criterio': ' / '.join(criterio_maquina),
        },
        'cliente': {
            'registros': reg_cliente,
            'total': total_cliente,
            'criterio': criterio_cliente,
        },
    }


# ---------------------------------------------------------------------------
# Recuperación de partes similares (RAG del asistente IA)
#
# En vez de mandar el histórico entero al modelo, se indexan anomalía y
# solución con BM25 y en cada pregunta solo viajan los partes más parecidos,
# priorizando la misma unidad, el mismo modelo y el mismo cliente.
# ---------------------------------------------------------------------------

PALABRAS_VACIAS = {
    'DE', 'LA', 'EL', 'LOS', 'LAS', 'UN', 'UNA', 'UNOS', 'UNAS', 'Y', 'O', 'U', 'E', 'A', 'EN', 'CON', 'SIN',
    'POR', 'PARA', 'DEL', 'AL', 'SE', 'SU', 'SUS', 'LE', 'LES', 'LO', 'ME', 'TE', 'NOS', 'MI', 'TU', 'QUE',
    'ES', 'SON', 'HA', 'HAN', 'HAY', 'NO', 'SI', 'YA', 'MAS', 'MUY', 'PERO', 'COMO', 'CUAL', 'CUALES',
    'ESTE', 'ESTA', 'ESTO', 'ESE', 'ESA', 'ESO', 'DONDE', 'CUANDO', 'QUIEN', 'TIENE', 'TIENEN', 'TENIDO',
    'HACE', 'HECHO', 'SIDO', 'ESTAN', 'ALGO', 'ALGUN', 'ALGUNA', 'OTRO', 'OTRA', 'OTROS', 'TODO',
    'TODOS', 'TODAS', 'SOBRE', 'ENTRE', 'HASTA', 'DESDE', 'PUEDE', 'PUEDES', 'DIME', 'DECIR', 'SABER',
    'QUIERO', 'NECESITO', 'BUSCA', 'BUSCAR', 'HISTORICO', 'HISTORICOS', 'PARTE', 'PARTES', 'CLIENTE',
    'MAQUINA', 'SIMILAR', 'SIMILARES', 'PARECIDO', 'PARECIDA', 'ANTES', 'VEZ', 'VECES', 'FALLO', 'FALLA',
    # Vocabulario de la propia aplicación (preguntas de uso, no de averías).
    'AVISO', 'AVISOS', 'TECNICO', 'TECNICOS', 'ASIGNAR', 'ASIGNO', 'ASIGNADO', 'ASIGNADOS', 'URGENTE',
    'URGENTES', 'MODULO', 'APLICACION', 'APP', 'PANTALLA', 'CREAR', 'CREO', 'USAR', 'USO', 'CUANTOS',
    'CUANTAS', 'CLIENTES', 'MAQUINAS', 'REPARTO', 'PROPON', 'RESUME', 'RESUMEN', 'HISTORIAL', 'ULTIMA',
    'ULTIMO', 'REPETIDAS', 'AVERIAS', 'AVERIA',
}
# Palabras que aparecen en muchos nombres de cliente y no sirven para identificarlo.
PALABRAS_CLIENTE_GENERICAS = {
    'GRAFICAS', 'GRAFICA', 'ARTES', 'IMPRENTA', 'DIGITAL', 'INDUSTRIAS', 'INDUSTRIAL', 'INDUSTRIALES',
    'SERVICIOS', 'COMERCIAL', 'EDITORIAL', 'DISTRIBUCIONES', 'ROTULOS', 'ROTULACION', 'IMPRESION',
    'IMPRESIONES', 'SUMINISTROS', 'PUBLICIDAD', 'COMUNICACION', 'DISENO', 'FUNDACION', 'GRUPO',
    'SOLUCIONES', 'TALLERES', 'ZARAGOZA', 'HUESCA', 'TERUEL', 'ARAGON', 'SUGRAF', 'TECNICO', 'TECNICA',
    'SERVICIO', 'MAQUINARIA', 'REPUESTOS', 'CENTRO', 'TALLER', 'NUEVO', 'NUEVA', 'AVISO', 'EMPRESA',
}


def _raiz(palabra):
    """Quita el plural para que 'CABEZALES' y 'CABEZAL' coincidan."""
    if len(palabra) > 5 and palabra.endswith('ES'):
        return palabra[:-2]
    if len(palabra) > 4 and palabra.endswith('S'):
        return palabra[:-1]
    return palabra


def tokens_busqueda(texto):
    return [_raiz(p) for p in re.findall(r'[A-Z0-9]+', _sin_acentos(texto))
            if len(p) > 1 and p not in PALABRAS_VACIAS]


class _IndiceRAG:
    K1, B = 1.4, 0.75

    def __init__(self, indice):
        registros = indice.registros
        # --- Índice invertido BM25 sobre anomalía + solución + equipo/modelo ---
        self.postings = {}
        self.longitudes = []
        for i, r in enumerate(registros):
            conteo = {}
            for t in tokens_busqueda(f"{r['anomalia']} {r['solucion']} {r['equipo']} {r['modelo']}"):
                conteo[t] = conteo.get(t, 0) + 1
            self.longitudes.append(sum(conteo.values()) or 1)
            for t, n in conteo.items():
                self.postings.setdefault(t, []).append((i, n))
        self.n = len(registros)
        self.media = sum(self.longitudes) / max(self.n, 1)

        # --- Diccionarios para detectar cliente / modelo / serie citados en una pregunta ---
        self.clientes_por_palabra = {}
        for r in registros:
            for nombre in (r['cliente'], r['nombre_comercial']):
                for p in clave_cliente(nombre).split():
                    if len(p) >= 4 and p not in PALABRAS_CLIENTE_GENERICAS and p not in PALABRAS_VACIAS:
                        self.clientes_por_palabra.setdefault(p, set()).add(r['cliente'])
        self.modelo_original = {}
        for r, k in zip(registros, indice.claves):
            if k['modelo'] in indice.modelos:
                self.modelo_original.setdefault(k['modelo'], r['modelo'])
        self.series = {k['serie']: r['n_serie'] for r, k in zip(registros, indice.claves) if len(k['serie']) >= 5}

    def puntuar(self, tokens):
        puntos = {}
        for t in set(tokens):
            post = self.postings.get(t)
            if not post:
                continue
            idf = math.log(1 + (self.n - len(post) + 0.5) / (len(post) + 0.5))
            for i, tf in post:
                norm = tf + self.K1 * (1 - self.B + self.B * self.longitudes[i] / self.media)
                puntos[i] = puntos.get(i, 0.0) + idf * tf * (self.K1 + 1) / norm
        return puntos


def _rag(indice):
    if getattr(indice, '_rag', None) is None:
        indice._rag = _IndiceRAG(indice)
    return indice._rag


def detectar_entidades(indice, texto):
    """Cliente, modelo y nº de serie mencionados en una pregunta libre."""
    rag = _rag(indice)
    palabras = set(re.findall(r'[A-Z0-9]+', _sin_acentos(texto)))
    candidatos = {}
    for p in palabras:
        clientes = rag.clientes_por_palabra.get(p, ())
        if 0 < len(clientes) <= 3:
            for c in clientes:
                candidatos[c] = candidatos.get(c, 0) + 1
    cliente = max(candidatos, key=lambda c: (candidatos[c], -len(c))) if candidatos else ''

    texto_alf = _alfanum(texto)
    modelos = [m for m in rag.modelo_original
               if m in texto_alf and (len(m) >= 6 or (len(m) >= 4 and re.search(r'\d', m) and re.search(r'[A-Z]', m)))]
    series = [s for s in rag.series if s in texto_alf]
    return {
        'cliente': cliente,
        'modelo': rag.modelo_original[max(modelos, key=len)] if modelos else '',
        'n_serie': rag.series[max(series, key=len)] if series else '',
    }


def similares(indice, consulta, cliente='', modelo='', n_serie='', desde='', hasta='', k=8, recientes=4):
    """Partes del histórico más relevantes para 'consulta' dentro del ámbito dado.

    Devuelve {'similares': [...], 'recientes': [...]} donde 'recientes' son los
    últimos partes del ámbito (unidad > modelo > cliente) aunque no coincidan en texto.
    """
    rag = _rag(indice)
    registros, claves = indice.registros, indice.claves
    claves_cli = {clave_cliente(p) for p in [cliente, *_texto(cliente).split('/')]} - {''}
    serie = clave_serie(n_serie)
    mod = clave_modelo(modelo)
    if mod in indice.modelos_genericos:
        mod = ''

    def en_fechas(i):
        f = registros[i]['fecha']
        return (not desde or f >= desde) and (not hasta or f <= hasta)

    def coincidencias(i):
        k_ = claves[i]
        motivos = []
        if serie and k_['serie'] == serie:
            motivos.append('misma unidad')
        elif mod and k_['modelo'] == mod:
            motivos.append('mismo modelo')
        if claves_cli and k_['clientes'] & claves_cli:
            motivos.append('mismo cliente')
        return motivos

    factores = {'misma unidad': 3.0, 'mismo modelo': 2.5, 'mismo cliente': 1.3}
    MIN_PUNTOS = 2.5  # por debajo, la coincidencia es solo de palabras muy comunes
    puntos = rag.puntuar(tokens_busqueda(consulta))
    ranking = []
    for i, p in puntos.items():
        if p < MIN_PUNTOS or not en_fechas(i):
            continue
        for m in coincidencias(i):
            p *= factores[m]
        ranking.append((p, i))
    ranking.sort(reverse=True)

    def mejores(lista, n):
        if not lista:
            return []
        corte = lista[0][0] * 0.25  # descarta coincidencias residuales
        return [i for p, i in lista if p >= corte][:n]

    # Hasta la mitad de los resultados se reservan para el ámbito de la pantalla
    # (unidad / modelo / cliente): si no, partes de otras marcas con más palabras
    # en común taparían a los de la propia máquina.
    elegidos = mejores([(p, i) for p, i in ranking if coincidencias(i)], k // 2)
    resto = mejores(ranking, k)
    elegidos += [i for i in resto if i not in elegidos][:k - len(elegidos)]
    puntos_finales = {i: p for p, i in ranking}
    elegidos.sort(key=lambda i: (not coincidencias(i), -puntos_finales[i]))

    # Últimos partes del ámbito más concreto disponible.
    # Se usa el primero que tenga partes: muchos partes no tienen modelo o serie rellenos.
    criterios = []
    if serie:
        criterios.append(lambda k_: k_['serie'] == serie)
    if mod and claves_cli:
        criterios.append(lambda k_: k_['modelo'] == mod and bool(k_['clientes'] & claves_cli))
    if claves_cli:
        criterios.append(lambda k_: bool(k_['clientes'] & claves_cli))
    if mod:
        criterios.append(lambda k_: k_['modelo'] == mod)
    ambito = []
    for criterio in criterios:
        ambito = [i for i, k_ in enumerate(claves) if criterio(k_)]
        if ambito:
            break
    ambito = sorted((i for i in ambito if en_fechas(i) and i not in elegidos),
                    key=lambda i: registros[i]['fecha'], reverse=True)[:recientes]

    def con_motivo(i):
        return {**registros[i], 'coincide': ', '.join(coincidencias(i))}

    return {
        'similares': [con_motivo(i) for i in elegidos],
        'recientes': [con_motivo(i) for i in ambito],
    }


def resumen_ambito(indice, cliente='', modelo='', n_serie=''):
    """Una o dos líneas con el tamaño del histórico del ámbito (cliente / modelo / unidad)."""
    registros, claves = indice.registros, indice.claves
    lineas = []
    claves_cli = {clave_cliente(p) for p in [cliente, *_texto(cliente).split('/')]} - {''}
    if claves_cli:
        pos = [i for i, k in enumerate(claves) if k['clientes'] & claves_cli]
        if pos:
            fechas = sorted(registros[i]['fecha'] for i in pos)
            maquinas = {}
            for i in pos:
                m = registros[i]['modelo'] or registros[i]['equipo'] or 'sin modelo'
                maquinas[m] = maquinas.get(m, 0) + 1
            top = ', '.join(f'{m} ({n})' for m, n in sorted(maquinas.items(), key=lambda x: -x[1])[:10])
            lineas.append(f"Cliente {registros[pos[0]]['cliente']}: {len(pos)} partes entre {fechas[0]} y {fechas[-1]}. "
                          f"Máquinas con más partes: {top}.")
        else:
            lineas.append(f"Cliente {cliente}: sin partes en el histórico.")
    serie = clave_serie(n_serie)
    if serie:
        pos = [i for i, k in enumerate(claves) if k['serie'] == serie]
        lineas.append(f"Unidad S/N {n_serie}: {len(pos)} partes en el histórico.")
    mod = clave_modelo(modelo)
    if mod:
        pos = [i for i, k in enumerate(claves) if k['modelo'] == mod]
        n_cli = len({registros[i]['cliente'] for i in pos})
        lineas.append(f"Modelo {modelo}: {len(pos)} partes en {n_cli} clientes.")
    return '\n'.join(lineas)


def texto_registro(r, max_txt=300):
    """Formato compacto de un parte para el contexto del modelo."""
    def corto(t):
        t = ' '.join(_texto(t).split())
        return t if len(t) <= max_txt else t[:max_txt] + '…'
    maquina = ' '.join(x for x in (r['equipo'], r['marca'], r['modelo']) if x)
    if r['n_serie']:
        maquina += f" S/N {r['n_serie']}"
    extra = f" [{r['coincide']}]" if r.get('coincide') else ''
    return (f"- Parte {r['n_parte']} · {r['fecha']} · {r['cliente']} · {maquina or 'sin máquina'} · "
            f"{r['tecnico']}{extra}\n  Anomalía: {corto(r['anomalia'])}\n  Solución: {corto(r['solucion'])}")


# ---------------------------------------------------------------------------
# API de la vista de Históricos
# ---------------------------------------------------------------------------

COLUMNAS_EXPORTAR = [
    ('n_parte', 'Nº Parte'), ('fecha_entrada', 'Fecha Entrada'), ('fecha', 'Fecha Realizado'),
    ('cliente', 'Cliente'), ('nombre_comercial', 'Nombre Comercial'), ('equipo', 'Equipo'),
    ('marca', 'Marca'), ('modelo', 'Modelo'), ('n_serie', 'Nº Serie'), ('tecnico', 'Técnico'),
    ('anomalia', 'Anomalía'), ('solucion', 'Solución'),
]


def _cargar_y_filtrar(cliente, modelo, n_serie, desde, hasta, orden):
    indice, _ = cargar_historico()
    posiciones = filtrar(indice, cliente=cliente, modelo=modelo, n_serie=n_serie,
                         desde=desde, hasta=hasta, orden=orden)
    return indice, posiciones


@router.get("/opciones")
def get_opciones():
    try:
        indice, fuentes = cargar_historico()
        resultado = opciones(indice)
        resultado['fuentes'] = fuentes
        return resultado
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo leer el histórico de SAT: {e}")


@router.get("/buscar")
def get_buscar(cliente: str = '', modelo: str = '', n_serie: str = '', desde: str = '', hasta: str = '',
               orden: str = 'desc', pagina: int = 1, por_pagina: int = 50):
    try:
        indice, posiciones = _cargar_y_filtrar(cliente, modelo, n_serie, desde, hasta, orden)
        por_pagina = max(1, min(por_pagina, MAX_POR_PAGINA))
        pagina = max(1, pagina)
        inicio = (pagina - 1) * por_pagina
        registros = [indice.registros[i] for i in posiciones[inicio:inicio + por_pagina]]
        return {
            'total': len(posiciones),
            'clientes': len({indice.registros[i]['cliente'] for i in posiciones}),
            'pagina': pagina,
            'por_pagina': por_pagina,
            'registros': registros,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo buscar en el histórico de SAT: {e}")


@router.get("/exportar")
def get_exportar(cliente: str = '', modelo: str = '', n_serie: str = '', desde: str = '', hasta: str = '',
                  orden: str = 'desc'):
    try:
        indice, posiciones = _cargar_y_filtrar(cliente, modelo, n_serie, desde, hasta, orden)
        df = pd.DataFrame(
            [[indice.registros[i][campo] for campo, _ in COLUMNAS_EXPORTAR] for i in posiciones],
            columns=[titulo for _, titulo in COLUMNAS_EXPORTAR],
        )
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Historico SAT')
        output.seek(0)
        nombre = f"Historico_SAT_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        return StreamingResponse(
            output,
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': f'attachment; filename="{nombre}"'},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo exportar el histórico de SAT: {e}")
