# comercial/incidencias.py
"""Incidencias de clientes: problemas de pago, SAT no resuelto o no pagado, quejas...

Las apuntan los comerciales y Master desde la app comercial, Master del SAT desde la SAT App
y la oficina desde la app de escritorio (módulo Comerciales). Todos ven las mismas.
Fichero: SAT-App/Comerciales/Incidencias_clientes.json  ->  {"incidencias": [ {...}, ... ]}
(creado a mano: la cuenta de servicio no puede crear ficheros en Drive).

Cada incidencia: id, codigo, cliente, tipo, detalle, estado (ABIERTA / RESUELTA), origen
(Comercial / SAT / Escritorio), creado_por, creado_en, resuelto_por, resuelto_en, resolucion.
Un cliente con varios códigos de Pirineos ve las incidencias de todos ellos.
"""
import secrets
import threading
import time

from comercial import almacen, pirineos

FICHERO = 'Incidencias_clientes.json'
TIPOS = {
    'PAGOS': 'Problemas de pago',
    'SAT_NO_RESUELTO': 'SAT no resuelto',
    'SAT_NO_PAGADO': 'SAT no pagado',
    'QUEJA': 'Queja / descontento',
    'OTRA': 'Otra',
}
ABIERTA, RESUELTA = 'ABIERTA', 'RESUELTA'

_lock = threading.Lock()
_cache = {'datos': None, 'leido': 0.0}


def _leer(refrescar=False):
    if refrescar or _cache['datos'] is None or time.time() - _cache['leido'] > 60:
        meta, datos = almacen.leer_json_propio(FICHERO, {})
        if meta is None:
            raise RuntimeError(f'Falta el fichero {FICHERO} en la carpeta SAT-App/Comerciales de Drive.')
        lista = (datos or {}).get('incidencias', []) if isinstance(datos, dict) else []
        _cache.update(datos=lista, leido=time.time(), meta=meta)
    return _cache['datos']


def listar(codigo='', estado=''):
    lista = _leer()
    if codigo:
        mismos = set(pirineos.codigos_hermanos(codigo))
        lista = [i for i in lista if i.get('codigo') in mismos]
    if estado:
        lista = [i for i in lista if i.get('estado') == estado]
    lista = sorted(lista, key=lambda i: i.get('creado_en', ''), reverse=True)   # más recientes primero
    return sorted(lista, key=lambda i: i.get('estado') != ABIERTA)               # y las abiertas arriba


def abiertas_por_cliente():
    """{codigo_principal: nº de incidencias abiertas}."""
    res = {}
    for i in _leer():
        if i.get('estado') == ABIERTA and i.get('codigo'):
            pr = pirineos.principal(i['codigo'])
            res[pr] = res.get(pr, 0) + 1
    return res


def _guardar(cambiar):
    with _lock:
        meta, datos = almacen.leer_json_propio(FICHERO, {})
        if meta is None:
            raise RuntimeError(f'Falta el fichero {FICHERO} en la carpeta SAT-App/Comerciales de Drive.')
        lista = (datos or {}).get('incidencias', []) if isinstance(datos, dict) else []
        resultado = cambiar(lista)
        almacen.guardar_json_propio(FICHERO, {'incidencias': lista}, meta)
        _cache.update(datos=lista, leido=time.time())
        return resultado


def crear(codigo, cliente, tipo, detalle, usuario, origen, momento):
    tipo = tipo if tipo in TIPOS else 'OTRA'
    detalle = ' '.join(str(detalle or '').split())[:1000]
    if not detalle:
        raise ValueError('Explica la incidencia.')
    c = pirineos.clientes().get(str(codigo or ''))
    if not c and not cliente:
        raise ValueError('Elige el cliente.')
    nueva = {'id': f"INC-{momento.strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2)}", 'codigo': c['codigo'] if c else '',
             'cliente': c['nombre'] if c else str(cliente)[:120], 'tipo': tipo, 'detalle': detalle, 'estado': ABIERTA,
             'origen': origen, 'creado_por': usuario, 'creado_en': momento.strftime('%Y-%m-%d %H:%M'),
             'resuelto_por': '', 'resuelto_en': '', 'resolucion': ''}
    _guardar(lambda lista: lista.append(nueva))
    return nueva


def resolver(id_inc, usuario, nota, momento):
    def cambiar(lista):
        for i in lista:
            if i.get('id') == id_inc:
                if i.get('estado') == RESUELTA:
                    raise ValueError('Esta incidencia ya estaba resuelta.')
                i.update(estado=RESUELTA, resuelto_por=usuario, resuelto_en=momento.strftime('%Y-%m-%d %H:%M'),
                         resolucion=' '.join(str(nota or '').split())[:500])
                return i
        raise LookupError('Incidencia no encontrada.')
    return _guardar(cambiar)
