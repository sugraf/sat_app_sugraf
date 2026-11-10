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
# Estados de una oferta de la oficina en Seguimiento (se pueden cambiar siempre a mano):
#   ENVIADA (la oficina la ha hecho) · PERDIDA · ANULADA (pedida por error).
# «Compra» no es un estado de toda la oferta sino de cada producto: un producto está comprado si en las ventas de
# Pirineos aparece después de la oferta (solo) o si se marca a mano (también se puede desmarcar una detección).
# Si se compran todos los productos, la oferta entera queda en COMPRA; si solo algunos, esos van a «Compra» y el
# resto sigue con el estado de la oferta.
ESTADOS = ['ENVIADA', 'PERDIDA', 'ANULADA']
ABIERTOS = ('ENVIADA',)
# Estados guardados antes del cambio. GANADA se trata aparte: marca como comprados los productos detectados en
# Pirineos o, si no se detecta ninguno, todos.
ANTIGUOS = {'ABIERTA': 'ENVIADA', 'NUEVA': 'ENVIADA', 'DESCARTADA': 'ANULADA', 'GANADA': 'ENVIADA'}
COMPRA, NO_COMPRA = 'COMPRA', 'NO'
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
                              'lineas': [{'texto': _txt(d.get('Maquina')), 'producto': _txt(d.get('Maquina')), 'precio': precio}],
                              'total': precio}
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
                     'producto': _txt(d.get('Producto')),
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


# ---------------------------------------------------------------- ofertas que se ganan solas
# Si después de la fecha de la oferta el cliente compra (ventas de Pirineos) algo de lo ofertado, la oferta pasa
# sola a GANADA, igual que los precios dados. Las ofertas no llevan referencia: se compara el nombre del producto
# ofertado con la descripción de lo vendido (los códigos de modelo, como «HD7725CE» o «556», tienen que estar
# todos) y, si el diccionario de sinónimos dice qué referencia es ese producto, también por referencia.
VACIAS = {'de', 'del', 'la', 'el', 'los', 'las', 'para', 'con', 'sin', 'y', 'en', 'por', 'mm', 'cm', 'uds', 'ud', 'kg', 'gr',
          'litro', 'litros', 'garrafa', 'bidon', 'caja', 'cajas', 'rollo', 'rollos', 'formato', 'unidad', 'unidades'}


def _tokens(texto):
    from comercial.usuarios import normalizar
    t = ''.join(c if c.isalnum() else ' ' for c in normalizar(texto))
    return [w for w in t.split() if len(w) >= 3 and w not in VACIAS]


def _encaja(producto, linea_venta, refs_diccionario):
    if linea_venta['referencia'] and linea_venta['referencia'] in refs_diccionario:
        return True
    toks = _tokens(producto)
    if not toks:
        return False
    venta = set(_tokens(linea_venta['descripcion'] + ' ' + linea_venta['referencia']))
    fuertes = [w for w in toks if any(c.isdigit() for c in w)]
    debiles = [w for w in toks if w not in fuertes]
    if fuertes:
        return all(w in venta for w in fuertes) and (not debiles or any(w in venta for w in debiles))
    return len(toks) >= 2 and sum(w in venta for w in toks) >= max(2, -(-len(toks) * 3 // 4))


def _lineas_venta(o, ventas):
    """Ventas del cliente desde la fecha de la oferta que pueden ser de lo ofertado."""
    if not o.get('codigo'):
        return []
    lineas = [l for l in ventas.get(o['codigo'], []) if l['fecha'] >= o['fecha'] and l['cantidad'] > 0]
    if o.get('tipo') == 'Maquinaria':
        # Que compre tóner o consumibles de esa máquina no es comprar la máquina: tiene que ser una venta de
        # maquinaria (por grupo o familia) o de un importe parecido al ofertado.
        total = o.get('total') or 0
        lineas = [l for l in lineas if 'MAQUIN' in (l.get('grupo', '') + ' ' + l.get('familia', '')).upper()
                  or (total and abs(l.get('importe') or 0) >= 0.3 * total)]
    return sorted(lineas, key=lambda l: l['fecha'])


def compras_por_linea(o, ventas):
    """Para cada producto ofertado (en el orden de o['lineas']), su primera compra en Pirineos o None."""
    lineas = _lineas_venta(o, ventas)
    if not lineas:
        return [None] * len(o['lineas'])
    try:
        from comercial import diccionario
    except Exception:
        diccionario = None
    salida = []
    for ofertado in o['lineas']:
        try:
            refs = diccionario.referencias(ofertado['producto']) if diccionario and ofertado.get('producto') else {}
        except Exception:
            refs = {}
        l = next((l for l in lineas if _encaja(ofertado.get('producto') or ofertado['texto'], l, refs)), None)
        salida.append({'fecha': l['fecha'], 'documento': l['documento'], 'referencia': l['referencia'], 'descripcion': l['descripcion'],
                       'cantidad': l['cantidad'], 'precio': l['precio'], 'ofertado': ofertado.get('producto') or ofertado['texto']} if l else None)
    return salida


def compra_de_oferta(o, ventas):
    """Primera compra del cliente, desde la fecha de la oferta, de algo ofertado; o None."""
    compras = [c for c in compras_por_linea(o, ventas) if c]
    return min(compras, key=lambda c: c['fecha']) if compras else None


def clave_linea(linea):
    """Clave de un producto de la oferta para guardar su marca a mano (el nombre, sin tildes ni mayúsculas)."""
    from comercial.usuarios import normalizar
    return ' '.join(normalizar(linea.get('producto') or linea.get('texto') or '').upper().split())


def con_estado(lista):
    """Añade a cada oferta su seguimiento, si cada producto está comprado (detectado en Pirineos o marcado a mano)
    y su estado: COMPRA si están comprados todos; si no, el marcado a mano para el resto (ENVIADA, PERDIDA o
    ANULADA) o ENVIADA (si la oficina la ha hecho, se ha enviado)."""
    seg = seguimiento()
    try:
        ventas, _ = pirineos.todas_las_ventas()
    except Exception:
        ventas = {}
    for o in lista:
        sg = seg.get(o['id'], {})
        o['seguimiento'] = sg
        guardado = sg.get('estado') or ''
        manual = ANTIGUOS.get(guardado, guardado) if guardado in ESTADOS or guardado in ANTIGUOS else ''
        autos = compras_por_linea(o, ventas)
        marcas = sg.get('lineas') if isinstance(sg.get('lineas'), dict) else {}
        ganada_antes = guardado == 'GANADA' and not any(autos)
        for l, auto in zip(o['lineas'], autos):
            l['clave'] = clave_linea(l)
            marca = marcas.get(l['clave']) or {}
            l['compra_pirineos'] = auto
            l['marca'] = marca                      # {'estado': COMPRA | NO, 'por', 'fecha'} si se ha tocado a mano
            if marca.get('estado') == COMPRA:
                l['comprada'] = True
            elif marca.get('estado') == NO_COMPRA:
                l['comprada'] = False
            else:
                l['comprada'] = bool(auto) or ganada_antes
            l['comprada_sola'] = l['comprada'] and not marca.get('estado') and bool(auto)
        o['n_compradas'] = sum(1 for l in o['lineas'] if l['comprada'])
        o['estado_resto'] = manual or 'ENVIADA'
        o['estado'] = COMPRA if o['lineas'] and o['n_compradas'] == len(o['lineas']) else o['estado_resto']
        o['compra_pirineos'] = min((a for a in autos if a), key=lambda c: c['fecha'], default=None)
        o['ganada_sola'] = o['estado'] == COMPRA and all(l['comprada_sola'] for l in o['lineas'])
    return lista


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
        d[id_oferta] = dict(anterior, estado=estado, nota=nota, proximo=proximo, por=usuario,
                            fecha=momento.strftime('%Y-%m-%d %H:%M'), historial=historial[-30:])
        almacen.guardar_json_propio(FICHERO_SEGUIMIENTO, d, meta)
        return d[id_oferta]


def marcar_lineas(id_oferta, claves, estado, usuario, momento):
    """Marca a mano productos de una oferta: estado COMPRA (comprado), NO (no comprado, aunque Pirineos parezca
    decir que sí) o '' (quitar la marca: vuelve a mandar lo detectado en Pirineos)."""
    if estado not in (COMPRA, NO_COMPRA, ''):
        raise ValueError('Estado de producto no válido.')
    if not claves:
        raise ValueError('Indica qué productos.')
    with _lock:
        meta, d = almacen.leer_json_propio(FICHERO_SEGUIMIENTO, {})
        if meta is None:
            raise RuntimeError(f'Falta el fichero {FICHERO_SEGUIMIENTO} en SAT-App/Comerciales.')
        d = d if isinstance(d, dict) else {}
        o = dict(d.get(id_oferta, {}))
        marcas = dict(o.get('lineas') or {})
        sello = momento.strftime('%Y-%m-%d %H:%M')
        for k in claves:
            if estado:
                marcas[k] = {'estado': estado, 'por': usuario, 'fecha': sello}
            else:
                marcas.pop(k, None)
        o['lineas'] = marcas
        historial = o.get('historial', [])
        historial.append({'fecha': sello, 'por': usuario, 'estado': f"PRODUCTOS {estado or 'AUTOMÁTICO'}", 'nota': ', '.join(claves)[:300]})
        o['historial'] = historial[-30:]
        d[id_oferta] = o
        almacen.guardar_json_propio(FICHERO_SEGUIMIENTO, d, meta)
        return o
