# comercial/ofertas.py
"""Ofertas generadas en la oficina (app de escritorio) y su seguimiento.

Se leen los Excel de los módulos de ofertas del escritorio (solo lectura):
  Generacion Ofertas Maquinaria/Datos/Datos_maquinas.xlsx  (hoja «Formulario»: una fila por oferta)
  Generacion Ofertas Consumibles/Datos/Ofertas_*.xlsx      (offset, flex, rigidos, vin_corte: una fila por producto)
La cuenta de la app tiene que poder verlos: hay que compartir esas carpetas «Datos» con ella.
El seguimiento (abierta / ganada / perdida, nota, próxima fecha) se guarda en
SAT-App/Comerciales/Seguimiento_ofertas.json (creado a mano).
"""
import io
import threading
import time
from datetime import date, datetime

from openpyxl import load_workbook

from comercial import almacen, pirineos

FICHERO_SEGUIMIENTO = 'Seguimiento_ofertas.json'
FICHERO_MAQUINARIA = 'Datos_maquinas.xlsx'
FICHEROS_CONSUMIBLES = {'Ofertas_offset.xlsx': 'Offset', 'Ofertas_flex.xlsx': 'Flexibles', 'Ofertas_rigidos.xlsx': 'Rígidos',
                        'Ofertas_vin_corte.xlsx': 'Vinilo de corte'}
ESTADOS = ['ABIERTA', 'GANADA', 'PERDIDA']
VALIDEZ_DIAS = 30

_lock = threading.Lock()
_cache = {}


def _txt(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return ' '.join(str(v).split())


def _fecha(v):
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d')
    if isinstance(v, date):
        return v.isoformat()
    return ''


def _num(v):
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _filas(nombre, hoja=None):
    meta = almacen.buscar_compartido(nombre)
    if meta is None:
        return None, []
    clave = f"{meta['id']}:{meta.get('modifiedTime')}"
    if _cache.get(nombre, (None,))[0] == clave and time.time() - _cache[nombre][2] < 600:
        return meta, _cache[nombre][1]
    wb = load_workbook(io.BytesIO(almacen.descargar(meta)), read_only=True, data_only=True)
    try:
        ws = wb[hoja] if hoja and hoja in wb.sheetnames else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        cab = [_txt(c) for c in next(it, [])]
        filas = [dict(zip(cab, f)) for f in it if any(v is not None for v in f)]
    finally:
        wb.close()
    _cache[nombre] = (clave, filas, time.time())
    return meta, filas


def leer_ofertas():
    """(ofertas, avisos). Cada oferta: numero, tipo, fecha, cliente, codigo, lineas [{texto, precio}], total."""
    ofertas, avisos = {}, []
    meta, filas = _filas(FICHERO_MAQUINARIA, 'Formulario')
    if meta is None:
        avisos.append('No se ve Datos_maquinas.xlsx: comparte la carpeta «Generacion Ofertas Maquinaria/Datos» con la cuenta de la app.')
    for d in filas:
        num, fecha = _txt(d.get('Nº Oferta')), _fecha(d.get('Fecha'))
        if not num or not fecha:
            continue
        precio = _num(d.get('Precio especial')) or _num(d.get('Precio tarifa'))
        ofertas[f'M{num}'] = {'id': f'M{num}', 'numero': num, 'tipo': 'Maquinaria', 'categoria': _txt(d.get('Categoría')),
                              'fecha': fecha, 'cliente': _txt(d.get('Cliente')), 'validez': _txt(d.get('Validez oferta')),
                              'lineas': [{'texto': _txt(d.get('Maquina')), 'precio': precio}], 'total': precio}
    vistos = 0
    for nombre, familia in FICHEROS_CONSUMIBLES.items():
        meta, filas = _filas(nombre)
        if meta is None:
            continue
        vistos += 1
        for d in filas:
            num, fecha = _txt(d.get('Nº Oferta')), _fecha(d.get('Fecha'))
            if not num or not fecha or not num.isdigit():
                continue
            precios = [(_num(d.get(f'{k} {i}')), k, _txt(d.get(f'Formato {i}'))) for i in (1, 2, 3)
                       for k in ('Precio unitario', 'Precio m²', 'Precio Litro', 'Precio Kg')]
            precios = [p for p in precios if p[0]]
            linea = {'texto': _txt(d.get('Producto')) + (f" · {precios[0][2]}" if precios and precios[0][2] else ''),
                     'precio': precios[0][0] if precios else None,
                     'unidad': {'Precio unitario': '€/ud', 'Precio m²': '€/m²', 'Precio Litro': '€/l', 'Precio Kg': '€/kg'}[precios[0][1]] if precios else ''}
            k = f'C{num}|{_txt(d.get("Cliente")).upper()}'
            o = ofertas.setdefault(k, {'id': k, 'numero': num, 'tipo': 'Consumibles', 'categoria': familia, 'fecha': fecha,
                                       'cliente': _txt(d.get('Cliente')), 'validez': '', 'lineas': [], 'total': None})
            o['fecha'] = max(o['fecha'], fecha)
            o['lineas'].append(linea)
    if not vistos:
        avisos.append('No se ven los Excel de ofertas de consumibles: comparte la carpeta «Generacion Ofertas Consumibles/Datos» con la cuenta de la app.')
    # Las ofertas genéricas («OFERTA ESPECIAL», sin cliente) no son para seguir con nadie.
    ofertas = {k: o for k, o in ofertas.items() if o['cliente'] and not o['cliente'].upper().startswith('OFERTA')}
    for o in ofertas.values():
        o['codigo'] = pirineos.codigo_por_nombre(o['cliente'])
        o['dias'] = (date.today() - date.fromisoformat(o['fecha'])).days
    return list(ofertas.values()), avisos


def seguimiento():
    _, d = almacen.leer_json_propio(FICHERO_SEGUIMIENTO, {})
    return d if isinstance(d, dict) else {}


def fijar_seguimiento(id_oferta, estado, nota, proximo, usuario, momento):
    if estado not in ESTADOS:
        raise ValueError('Estado no válido.')
    with _lock:
        meta, d = almacen.leer_json_propio(FICHERO_SEGUIMIENTO, {})
        if meta is None:
            raise RuntimeError(f'Falta el fichero {FICHERO_SEGUIMIENTO} en SAT-App/Comerciales.')
        d = d if isinstance(d, dict) else {}
        anterior = d.get(id_oferta, {})
        historial = anterior.get('historial', [])
        historial.append({'fecha': momento.strftime('%Y-%m-%d %H:%M'), 'por': usuario, 'estado': estado, 'nota': nota})
        d[id_oferta] = {'estado': estado, 'nota': nota, 'proximo': proximo, 'por': usuario,
                        'fecha': momento.strftime('%Y-%m-%d %H:%M'), 'historial': historial[-20:]}
        almacen.guardar_json_propio(FICHERO_SEGUIMIENTO, d, meta)
        return d[id_oferta]
