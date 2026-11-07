# comercial/geo.py
"""Ubicación aproximada de los clientes para rutas y para el registro de entrada/salida.

Cada cliente se sitúa en el centro de su código postal (precisión de pueblo o barrio,
suficiente para planificar rutas y para saber si un registro se hizo "cerca" del cliente,
pero NO para afirmar que alguien estuvo en una dirección concreta).

Las coordenadas de cada código postal se piden una sola vez a OpenStreetMap (Nominatim,
máximo 1 consulta por segundo) y se guardan en Drive: Comerciales/geo_codigos_postales.json.
"""
import json
import math
import threading
import time
import urllib.parse
import urllib.request

from comercial import almacen, pirineos

FICHERO = 'geo_codigos_postales.json'
AGENTE = 'SugrafApp-Comercial/1.0 (planificacion de rutas)'
FACTOR_CARRETERA = 1.3  # la distancia en línea recta se multiplica para estimar la de carretera

_lock = threading.Lock()
_estado = {'datos': None, 'meta': None, 'leido': 0.0, 'ultima_consulta': 0.0}


def _cargar():
    if _estado['datos'] is None or time.time() - _estado['leido'] > 600:
        meta, datos = almacen.leer_json_propio(FICHERO, {})
        _estado.update(datos=datos or {}, meta=meta, leido=time.time())
    return _estado['datos']


def clave(cp, poblacion, provincia):
    cp = ''.join(ch for ch in str(cp or '') if ch.isdigit())
    if len(cp) == 4:
        cp = '0' + cp
    return cp if len(cp) == 5 else f"{pirineos.normalizar(poblacion)}|{pirineos.normalizar(provincia)}"


def coordenadas_cliente(c):
    """(lat, lon) del cliente o None si aún no se ha localizado su código postal."""
    if not c:
        return None
    v = _cargar().get(clave(c['cp'], c['poblacion'], c['provincia']))
    return (v[0], v[1]) if v and v[0] is not None else None


def pendientes():
    """Claves (código postal o población) de clientes españoles que aún no tienen coordenadas."""
    datos = _cargar()
    faltan = {}
    for c in pirineos.clientes().values():
        if c['grupo'].upper() == 'PROVEEDORES' or not (c['cp'] or c['poblacion']):
            continue
        k = clave(c['cp'], c['poblacion'], c['provincia'])
        if k not in datos:
            faltan[k] = c
    return faltan


def _consultar(parametros):
    espera = 1.1 - (time.time() - _estado['ultima_consulta'])
    if espera > 0:
        time.sleep(espera)
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({**parametros, 'format': 'json', 'limit': 1})
    peticion = urllib.request.Request(url, headers={'User-Agent': AGENTE, 'Accept-Language': 'es'})
    try:
        with urllib.request.urlopen(peticion, timeout=10) as r:
            datos = json.loads(r.read().decode('utf-8'))
    finally:
        _estado['ultima_consulta'] = time.time()
    return (float(datos[0]['lat']), float(datos[0]['lon'])) if datos else None


def localizar(cp, poblacion, provincia):
    k = clave(cp, poblacion, provincia)
    resultado = None
    if len(k) == 5 and k.isdigit():
        resultado = _consultar({'postalcode': k, 'country': 'es'})
    if not resultado and poblacion:
        resultado = _consultar({'q': f'{poblacion}, {provincia}, España' if provincia else f'{poblacion}, España'})
    return k, resultado


def preparar(segundos=40):
    """Localiza códigos postales pendientes durante como mucho `segundos`. Devuelve cuántos faltan."""
    with _lock:
        datos = dict(_cargar())
        faltan = pendientes()
        inicio, hechos = time.time(), 0
        for k, c in list(faltan.items()):
            if time.time() - inicio > segundos:
                break
            try:
                _, coords = localizar(c['cp'], c['poblacion'], c['provincia'])
            except Exception:
                break  # sin conexión o límite del servicio: se reintenta en la próxima llamada
            datos[k] = [coords[0], coords[1]] if coords else [None, None]
            hechos += 1
        if hechos:
            meta, _ = almacen.leer_json_propio(FICHERO, {})
            almacen.guardar_json_propio(FICHERO, datos, meta)
            _estado.update(datos=datos, leido=time.time())
        return {'hechos': hechos, 'faltan': len(faltan) - hechos, 'total': len(datos) + len(faltan) - hechos}


def distancia_km(a, b):
    """Distancia aproximada por carretera entre dos puntos (lat, lon)."""
    if not a or not b:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(2 * 6371 * math.asin(math.sqrt(h)) * FACTOR_CARRETERA, 1)


def distancia_recta_km(a, b):
    return None if not a or not b else round(distancia_km(a, b) / FACTOR_CARRETERA, 1)
