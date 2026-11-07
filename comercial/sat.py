# comercial/sat.py
"""Lo que el módulo Comercial necesita del SAT (solo lectura de sus Excel en SAT-App):

- Avisos abiertos de un cliente: "Avisos Sin Tratar.xlsx" (sin asignar) y los de
  "Avisos Tratados.xlsx" que no están cerrados.
- Partes recientes: "Partes Finalizados.xlsx".
- Oportunidades de venta que marcan los técnicos en el parte ("OPCIÓN A VENTA" = SI y
  "DETALLE VENTA"). El seguimiento comercial de cada oportunidad (contactado, oferta,
  ganada...) se guarda aparte en Comerciales/Oportunidades_SAT.json, sin tocar los
  Excel del SAT.
"""
import hashlib
import threading
from datetime import date, timedelta

from comercial import almacen, pirineos
from comercial.usuarios import USUARIOS, normalizar

FICHERO_ESTADOS = 'Oportunidades_SAT.json'
ESTADOS = ['NUEVA', 'CONTACTADO', 'OFERTA ENVIADA', 'GANADA', 'PERDIDA', 'DESCARTADA']
ABIERTOS = {'NUEVA', 'CONTACTADO', 'OFERTA ENVIADA'}

_lock = threading.Lock()


def _t(v):
    return pirineos._texto(v)


def _leer(nombre):
    return pirineos._cargado(f'sat:{nombre}', lambda: almacen.buscar_sat(nombre), lambda c: list(pirineos._filas(c))) or []


# En partes antiguos la columna DETALLE VENTA recogía por error el estado de las piezas.
NO_ES_DETALLE = {'NO NECESITA PIEZAS', 'SI NECESITA PIEZAS', 'PIEZAS RECIBIDAS', 'PENDIENTE', 'NO', 'SI'}


def _detalle(v):
    t = _t(v)
    return '' if normalizar(t) in NO_ES_DETALLE else t


def _aviso(d, origen):
    return {
        'origen': origen,
        'cliente_nombre': _t(d.get('CLIENTE')), 'poblacion': _t(d.get('POBLACIÓN')),
        'fecha_entrada': _t(d.get('F. ENTR.'))[:10], 'fecha_realizacion': _t(d.get('FECHA REALIZACIÓN'))[:10],
        'maquina': _t(d.get('MÁQUINA')), 'marca': _t(d.get('MARCA')), 'modelo': _t(d.get('MODELO')),
        'descripcion': _t(d.get('DESCRIPCIÓN')), 'asignado': _t(d.get('ASIGNADO A')), 'estado': _t(d.get('ESTADO')),
        'resuelto': _t(d.get('RESUELTO O PENDIENTE')), 'solucion': _t(d.get('SOLUCIÓN')),
        'urgente': _t(d.get('URGENTE')).upper() == 'SI', 'parado': _t(d.get('PARADO')).upper() == 'SI',
        'opcion_venta': _t(d.get('OPCIÓN A VENTA')).upper() == 'SI', 'detalle_venta': _detalle(d.get('DETALLE VENTA')),
    }


def _con_codigo(lista):
    for a in lista:
        a['codigo'] = pirineos.codigo_por_nombre(a['cliente_nombre'])
    return lista


def avisos_abiertos():
    sin = [_aviso(d, 'Sin asignar') for d in _leer('Avisos Sin Tratar.xlsx')]
    tratados = [_aviso(d, 'En curso') for d in _leer('Avisos Tratados.xlsx')]
    tratados = [a for a in tratados if a['estado'].upper() != 'CERRADO']
    return _con_codigo([a for a in sin + tratados if a['cliente_nombre']])


def partes_finalizados():
    return _con_codigo([_aviso(d, 'Finalizado') for d in _leer('Partes Finalizados.xlsx') if _t(d.get('CLIENTE'))])


def avisos_cliente(codigo):
    codigos = set(pirineos.codigos_hermanos(codigo))
    abiertos = [a for a in avisos_abiertos() if a['codigo'] in codigos]
    corte = (date.today() - timedelta(days=120)).isoformat()
    recientes = [a for a in partes_finalizados() if a['codigo'] in codigos and (a['fecha_realizacion'] or a['fecha_entrada']) >= corte]
    recientes.sort(key=lambda a: a['fecha_realizacion'] or a['fecha_entrada'], reverse=True)
    return abiertos, recientes[:10]


# ---------------------------------------------------------------- oportunidades
def _clave(a):
    base = '|'.join([pirineos.clave_nombre(a['cliente_nombre']), a['maquina'], a['fecha_entrada'], a['detalle_venta'][:80]])
    return hashlib.sha1(base.encode('utf-8')).hexdigest()[:12]


def _usuario_de_comercial(nombre_pirineos):
    n = normalizar(nombre_pirineos)
    for clave, u in USUARIOS.items():
        if u['pirineos'] and normalizar(u['pirineos']) == n:
            return clave
    return ''


def oportunidades():
    """Todas las oportunidades marcadas por los técnicos, con su seguimiento comercial."""
    candidatas = [a for a in partes_finalizados() if a['opcion_venta']]
    candidatas += [a for a in _con_codigo([_aviso(d, 'En curso') for d in _leer('Avisos Tratados.xlsx')]) if a['opcion_venta']]
    _, estados = almacen.leer_json_propio(FICHERO_ESTADOS, {})
    estados = estados or {}
    clientes = pirineos.clientes()
    vistas, resultado = set(), []
    for a in candidatas:
        k = _clave(a)
        if k in vistas:
            continue
        vistas.add(k)
        c = clientes.get(a['codigo']) if a['codigo'] else None
        e = estados.get(k, {})
        resultado.append({
            'id': k, 'fecha': a['fecha_realizacion'] or a['fecha_entrada'], 'tecnico': a['asignado'],
            'cliente_nombre': c['nombre'] if c else a['cliente_nombre'], 'codigo': a['codigo'],
            'poblacion': a['poblacion'], 'maquina': ' '.join(x for x in (a['marca'], a['maquina'] or a['modelo']) if x),
            'detalle': a['detalle_venta'], 'averia': a['descripcion'],
            'comercial': c['comercial'] if c else '', 'usuario': _usuario_de_comercial(c['comercial']) if c else '',
            'estado': e.get('estado', 'NUEVA'), 'nota': e.get('nota', ''),
            'actualizado': e.get('actualizado', ''), 'actualizado_por': e.get('por', ''),
        })
    resultado.sort(key=lambda o: o['fecha'], reverse=True)
    return resultado


def actualizar_oportunidad(id_op, estado, nota, usuario, momento):
    if estado not in ESTADOS:
        raise ValueError('Estado no válido.')
    with _lock:
        meta, estados = almacen.leer_json_propio(FICHERO_ESTADOS, {})
        estados = estados or {}
        anterior = estados.get(id_op, {})
        historial = anterior.get('historial', [])
        historial.append({'fecha': momento, 'por': usuario, 'estado': estado, 'nota': nota})
        estados[id_op] = {'estado': estado, 'nota': nota, 'actualizado': momento, 'por': usuario, 'historial': historial[-30:]}
        almacen.guardar_json_propio(FICHERO_ESTADOS, estados, meta)
