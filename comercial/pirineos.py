# comercial/pirineos.py
"""Lectura (solo lectura) de los datos de Pirineos que usa el módulo Comercial.

  Pirineos/Clientes/Clientes.xlsx               maestro de clientes (tarifa y comercial)
  Pirineos/Clientes/PreciosCliente.xlsx         precios especiales por cliente y referencia
  Pirineos/Articulos/Articulos.xlsx             artículos con Tarifa1/2/3 y último coste
  Pirineos/Movimientos de venta/MovimientosVentaActual.xlsx (+ años anteriores)

Cada fichero se guarda en memoria mientras la instancia siga viva y solo se vuelve a
descargar si ha cambiado en Drive (se comprueba como mucho cada 10 minutos).
"""
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from openpyxl import load_workbook

from comercial import almacen
from comercial.usuarios import normalizar

SEGUNDOS_COMPROBACION = 600
ANIOS_VENTAS_ANTERIORES = 2   # además del año en curso
GRUPOS_EXCLUIDOS = {'CONTABILIDAD'}
# Comerciales que ya no están: sus clientes se tratan como «sin comercial» (no entran en
# ninguna cartera; Master los ve todos). Se compara sin tildes ni mayúsculas.
COMERCIALES_DE_BAJA = {'PEREZ DE PETINTO FERNANDEZ, CELIA'}

_lock = threading.Lock()
_cache = {}  # nombre lógico -> {'clave', 'datos', 'comprobado'}


def _texto(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return ' '.join(str(v).split())


def _num(v):
    try:
        return float(v) if v not in (None, '') else 0.0
    except (TypeError, ValueError):
        return 0.0


def _fecha(v):
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d')
    if isinstance(v, date):
        return v.isoformat()
    return _texto(v)[:10]


def _filas(contenido):
    wb = load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
    try:
        filas = wb.worksheets[0].iter_rows(values_only=True)
        cabecera = [_texto(c) for c in next(filas, [])]
        for fila in filas:
            yield dict(zip(cabecera, fila))
    finally:
        wb.close()


def _cargado(nombre_logico, buscar, procesar):
    """Devuelve los datos procesados de un fichero, usando la caché si no ha cambiado."""
    with _lock:
        entrada = _cache.get(nombre_logico)
        if entrada and time.time() - entrada['comprobado'] < SEGUNDOS_COMPROBACION:
            return entrada['datos']
    try:
        meta = buscar()
        if meta is None:
            datos, clave = None, None
        else:
            clave = f"{meta['id']}:{meta.get('modifiedTime')}"
            if entrada and entrada['clave'] == clave:
                datos = entrada['datos']
            else:
                datos = procesar(almacen.descargar(meta))
    except Exception:
        if entrada:  # Drive falla pero ya teníamos una copia: seguimos con ella
            entrada['comprobado'] = time.time()
            return entrada['datos']
        raise
    with _lock:
        _cache[nombre_logico] = {'clave': clave, 'datos': datos, 'comprobado': time.time()}
    return datos


def _falta(nombre, ruta):
    correo = almacen.correo_cuenta_servicio()
    return RuntimeError(
        f"No se encuentra {nombre} ({ruta}). Comprueba que la carpeta está compartida con la cuenta de la app"
        + (f" ({correo})." if correo else '.')
    )


# ---------------------------------------------------------------- clientes
_DE_BAJA = {normalizar(n) for n in COMERCIALES_DE_BAJA}


def _procesar_clientes(contenido):
    clientes = {}
    for d in _filas(contenido):
        codigo = _texto(d.get('Codigo'))
        if not codigo:
            continue
        tarifa = _texto(d.get('Tarifa')) or '1'
        clientes[codigo] = {
            'codigo': codigo,
            'nif': _texto(d.get('NIF')),
            'nombre': _texto(d.get('Nombre')),
            'nombre_comercial': _texto(d.get('NombreComercial')),
            'direccion': _texto(d.get('Direccion')),
            'poblacion': _texto(d.get('Poblacion')),
            'cp': _texto(d.get('C_P')),
            'provincia': _texto(d.get('Provincia')),
            'telefono': _texto(d.get('Telefono_1')),
            'telefono2': _texto(d.get('Telefono_2')),
            'movil': _texto(d.get('Movil')),
            'email': _texto(d.get('E_mail')),
            'forma_pago': _texto(d.get('FormaPago')),
            'vencimiento': _texto(d.get('Vencimiento')),
            'grupo': _texto(d.get('GrupoCliente')),
            'actividad': _texto(d.get('Actividad')),
            'tarifa': tarifa if tarifa in ('1', '2', '3') else '1',
            'comercial': '' if normalizar(d.get('Comercial')) in _DE_BAJA else _texto(d.get('Comercial')),
            'comercial_norm': '' if normalizar(d.get('Comercial')) in _DE_BAJA else normalizar(d.get('Comercial')),
        }
    return clientes


def clientes():
    datos = _cargado('clientes', lambda: almacen.buscar_pirineos('Clientes.xlsx', 'Clientes'), _procesar_clientes)
    if datos is None:
        raise _falta('el maestro de clientes', 'Pirineos/Clientes/Clientes.xlsx')
    return datos


# ---------------------------------------------------------------- precios especiales
def _procesar_precios(contenido):
    por_cliente = {}
    for d in _filas(contenido):
        cliente, ref = _texto(d.get('Cliente')), _texto(d.get('Referencia'))
        if not cliente or not ref:
            continue
        por_cliente.setdefault(cliente, []).append({
            'referencia': ref, 'precio': _num(d.get('Precio')),
            'desde': _fecha(d.get('Fecha_Ini')), 'hasta': _fecha(d.get('Fecha_Fin')),
        })
    return por_cliente


def precios_especiales(codigo_cliente):
    datos = _cargado('precios', lambda: almacen.buscar_pirineos('PreciosCliente.xlsx', 'Clientes'), _procesar_precios) or {}
    hoy = date.today().isoformat()
    lista = []
    for p in datos.get(str(codigo_cliente), []):
        vigente = (not p['desde'] or p['desde'] <= hoy) and (not p['hasta'] or p['hasta'] >= hoy)
        lista.append({**p, 'vigente': vigente})
    return lista


def precio_especial_vigente(codigo_cliente, referencia):
    candidatos = [p for p in precios_especiales(codigo_cliente) if p['vigente'] and p['referencia'] == str(referencia)]
    return max(candidatos, key=lambda p: p['desde'] or '') if candidatos else None


# ---------------------------------------------------------------- artículos
def _procesar_articulos(contenido):
    articulos = {}
    for d in _filas(contenido):
        ref = _texto(d.get('Referencia'))
        desc = _texto(d.get('Descripcion'))
        if not ref or not desc or ref in articulos:
            continue
        articulos[ref] = {
            'referencia': ref,
            'ref_proveedor': _texto(d.get('ReferenciaProv')),
            'descripcion': desc,
            'grupo': _texto(d.get('NombreGrupo')),
            'familia': _texto(d.get('NombreFamilia')),
            'subfamilia': _texto(d.get('NombreSubFamilia')),
            'marca': _texto(d.get('NombreMarca')),
            'tarifas': {'1': _num(d.get('Tarifa1')), '2': _num(d.get('Tarifa2')), '3': _num(d.get('Tarifa3'))},
            'coste': _num(d.get('UltimoPrecioCompra')),
            'stock': _num(d.get('Stock')),
            '_buscar': normalizar(' '.join([ref, _texto(d.get('ReferenciaProv')), desc, _texto(d.get('NombreFamilia')),
                                            _texto(d.get('NombreSubFamilia')), _texto(d.get('NombreMarca'))])),
        }
    return articulos


def articulos():
    datos = _cargado('articulos', lambda: almacen.buscar_pirineos('Articulos.xlsx', 'Articulos'), _procesar_articulos)
    if datos is None:
        raise _falta('el maestro de artículos', 'Pirineos/Articulos/Articulos.xlsx')
    return datos


# ---------------------------------------------------------------- ventas
def _procesar_ventas(contenido):
    por_cliente = {}
    for d in _filas(contenido):
        cliente = _texto(d.get('CodigoCliente'))
        grupo = _texto(d.get('GrupoDescripcion'))
        if not cliente or grupo.upper() in GRUPOS_EXCLUIDOS:
            continue
        cantidad, importe = _num(d.get('Cantidad')), _num(d.get('Base') if d.get('Base') is not None else d.get('Importe'))
        if cantidad == 0 and importe == 0:
            continue
        por_cliente.setdefault(cliente, []).append({
            'fecha': _fecha(d.get('Fecha')),
            'tipo': _texto(d.get('Tipo')),
            'documento': _texto(d.get('NumeroDocumento')),
            'referencia': _texto(d.get('Referencia')),
            'descripcion': _texto(d.get('Descripcion')),
            'grupo': grupo,
            'familia': _texto(d.get('FamiliaDescripcion')),
            'subfamilia': _texto(d.get('SubFamiliaDescripcion')),
            'cantidad': cantidad,
            'precio': _num(d.get('Precio')),
            'dto': _num(d.get('Dto')),
            'importe': importe,
        })
    return por_cliente


def _ficheros_ventas():
    anio = date.today().year
    nombres = [f'MovimientosVenta{anio - i}.xlsx' for i in range(ANIOS_VENTAS_ANTERIORES, 0, -1)]
    return nombres + ['MovimientosVentaActual.xlsx']


def ventas_cliente(codigo_cliente):
    """(lineas, ficheros_leidos, ficheros_que_faltan) de las ventas del cliente."""
    nombres = _ficheros_ventas()

    def cargar(nombre):
        return nombre, _cargado(f'ventas:{nombre}',
                                lambda: almacen.buscar_pirineos(nombre, 'Movimientos de venta'), _procesar_ventas)

    with ThreadPoolExecutor(max_workers=len(nombres)) as ex:
        resultados = list(ex.map(cargar, nombres))
    lineas, leidos, faltan = [], [], []
    for nombre, datos in resultados:
        if datos is None:
            faltan.append(nombre)
            continue
        leidos.append(nombre)
        for cod in codigos_hermanos(codigo_cliente):
            lineas.extend(dict(l, codigo=cod) for l in datos.get(cod, []))
    # Si un documento aparece en dos ficheros (cambio de año), nos quedamos con uno.
    vistos, unicas = set(), []
    for l in sorted(lineas, key=lambda l: l['fecha'], reverse=True):
        k = (l['tipo'], l['documento'], l['referencia'], l['cantidad'], l['importe'], l['fecha'])
        if k in vistos:
            continue
        vistos.add(k)
        unicas.append(l)
    return unicas, leidos, faltan


def ultima_compra_por_cliente():
    """{codigo_cliente: fecha} con la última venta de los ficheros ya cargados (rápido)."""
    resultado = {}
    for nombre in _ficheros_ventas():
        entrada = _cache.get(f'ventas:{nombre}')
        if not entrada or not entrada['datos']:
            continue
        for cliente, lineas in entrada['datos'].items():
            f = max((l['fecha'] for l in lineas), default='')
            if f > resultado.get(cliente, ''):
                resultado[cliente] = f
    for cod in list(resultado):  # la del cliente es la última de cualquiera de sus códigos
        for h in codigos_hermanos(cod):
            if resultado[cod] > resultado.get(h, ''):
                resultado[h] = resultado[cod]
    return resultado


# ---------------------------------------------------------------- nombres de cliente -> código
# Las máquinas, los pedidos pendientes y los avisos del SAT identifican al cliente por su
# nombre (no por el código), así que se cruzan por nombre normalizado.
FORMAS_JURIDICAS = {'SA', 'SL', 'SLU', 'SAU', 'SLL', 'SLNE', 'SC', 'SCP', 'SCOOP', 'SCL', 'CB', 'COOP', 'SLP', 'S'}


def clave_nombre(nombre):
    t = normalizar(nombre)
    t = ''.join(c if c.isalnum() else ' ' for c in t)
    # "S.L." -> "S L" -> se quitan; también formas pegadas tipo "SL"
    palabras = [p for p in t.split() if p not in FORMAS_JURIDICAS and len(p) > 1 or p.isdigit()]
    return ' '.join(palabras)


# ---------------------------------------------------------------- grupos: varios códigos, un cliente
# En Pirineos una misma empresa puede tener varios códigos (uno por departamento o tipo de
# consumo -tintas, planchas, maquinaria...-, un código antiguo y otro nuevo, altas duplicadas).
# Se tratan como UN cliente: se suman sus ventas, sus visitas y su control de visitas.
#   - comercial/grupos_clientes.json: grupos revisados a mano (con motivo). Los códigos de
#     "revisados" ya se estudiaron, así que con ellos no se aplica ninguna regla automática.
#   - Códigos nuevos (no revisados): misma razón social y misma población -> mismo cliente.
_FICHERO_GRUPOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'grupos_clientes.json')


def _grupos_revisados():
    entrada = _cache.get('grupos_revisados')
    if entrada is None:
        try:
            with open(_FICHERO_GRUPOS, encoding='utf-8') as f:
                datos = json.load(f)
        except (OSError, ValueError):
            datos = {}
        entrada = {'grupos': datos.get('grupos', []), 'revisados': set(datos.get('revisados', []))}
        _cache['grupos_revisados'] = entrada
    return entrada


def _indice_nombres():
    cl = clientes()
    entrada = _cache.get('indice_nombres')
    version = (_cache.get('clientes', {}).get('clave'), (_cache.get('ventas_todas') or {}).get('version'))
    if entrada and entrada.get('version') == version:
        return entrada['datos']
    rev = _grupos_revisados()
    # Grupos: los revisados + misma razón social y población entre códigos sin revisar.
    grupo, principal_de = {}, {}
    for g in rev['grupos']:
        cods = [c for c in g['codigos'] if c in cl]
        if len(cods) > 1:
            pr = g['principal'] if g['principal'] in cods else cods[0]
            for c in cods:
                grupo[c], principal_de[c] = cods, pr
    auto = {}
    for c in cl.values():
        if c['codigo'] in rev['revisados'] or c['codigo'] in grupo:
            continue
        k = clave_nombre(c['nombre'])
        if k:
            auto.setdefault((k, normalizar(c['poblacion'])), []).append(c['codigo'])
    ventas_raw = _ventas_por_codigo_crudas()
    for cods in auto.values():
        if len(cods) > 1:
            cods = sorted(cods, key=lambda y: int(y) if y.isdigit() else 0)
            pr = max(cods, key=lambda x: (len(ventas_raw.get(x, [])), -int(x) if x.isdigit() else 0))
            for c in cods:
                grupo[c], principal_de[c] = cods, pr
    hermanos = {c: sorted(v, key=lambda y: int(y) if y.isdigit() else 0) for c, v in grupo.items()}
    # Cruce por nombre (máquinas, pedidos y avisos del SAT vienen por nombre, no por código).
    por_razon = {}
    for c in cl.values():
        k = clave_nombre(c['nombre'])
        if k:
            por_razon.setdefault(k, set()).add(c['codigo'])
    def principal(codigos):
        prs = {principal_de.get(x, x) for x in codigos}
        return max(prs, key=lambda x: (len(ventas_raw.get(x, [])), -int(x) if x.isdigit() else 0))
    exactos = {k: principal(v) for k, v in por_razon.items()}
    for c in cl.values():  # nombre comercial, solo si no lleva a dos empresas distintas
        k = clave_nombre(c['nombre_comercial'])
        if k and k not in exactos:
            exactos[k] = principal_de.get(c['codigo'], c['codigo'])
    indice = {'exactos': exactos, 'hermanos': hermanos, 'principal': principal_de}
    _cache['indice_nombres'] = {'version': version, 'datos': indice, 'clave': version, 'comprobado': time.time()}
    return indice


def codigos_hermanos(codigo):
    """Todos los códigos de Pirineos que son el mismo cliente, incluido este."""
    return _indice_nombres()['hermanos'].get(str(codigo), [str(codigo)])


def principal(codigo):
    """Código que representa al cliente (el propio si no tiene más códigos)."""
    codigo = str(codigo or '')
    return _indice_nombres()['principal'].get(codigo, codigo) if codigo else ''


def codigo_por_nombre(nombre):
    """Código de cliente de Pirineos para un nombre escrito en otro fichero, o '' si no es seguro."""
    k = clave_nombre(nombre)
    if not k:
        return ''
    indice = _indice_nombres()['exactos']
    if k in indice:
        return indice[k]
    # Coincidencia por palabras (p. ej. "VICNILO / TABUENCA PEREZ, VICTOR" -> "TABUENCA PEREZ, VICTOR"):
    # todas las palabras de uno están en el otro, con al menos dos palabras, y es un único cliente.
    palabras = set(k.split())
    if len(palabras) < 2:
        return ''
    candidatos = {cod for clave, cod in indice.items()
                  if palabras <= set(clave.split()) or (len(clave.split()) >= 2 and set(clave.split()) <= palabras)}
    return next(iter(candidatos)) if len(candidatos) == 1 else ''


# ---------------------------------------------------------------- máquinas instaladas
def _fecha_o_vacio(v):
    f = _fecha(v)
    return f if len(f) == 10 and f[4] == '-' and f > '1990' else ''


def _procesar_maquinas(contenido):
    lista = []
    for d in _filas(contenido):
        nombre_cliente = _texto(d.get('Nombre'))
        modelo, equipo, marca = _texto(d.get('Modelo')), _texto(d.get('Equipo')), _texto(d.get('Marca'))
        maquina = _texto(d.get('NombreMaquina'))
        if not nombre_cliente or not (modelo or maquina or equipo):
            continue
        lista.append({
            'cliente_nombre': nombre_cliente,
            'maquina': maquina, 'equipo': equipo, 'marca': marca, 'modelo': modelo,
            'n_serie': _texto(d.get('N_Serie')), 'familia': _texto(d.get('Familia')),
            'instalacion': _fecha_o_vacio(d.get('FechaInstalacion')),
            'garantia': _fecha_o_vacio(d.get('Fecha_Garantia')),
            'baja': _texto(d.get('Baja')).upper().startswith('S'),
        })
    return lista


def maquinas():
    """Todas las máquinas de Maquinas.xlsx con el código de cliente cruzado por nombre."""
    datos = _cargado('maquinas', lambda: almacen.buscar_pirineos('Maquinas.xlsx', 'Maquinas por cliente'), _procesar_maquinas)
    if datos is None:
        return []
    version = _cache['indice_nombres']['version'] if 'indice_nombres' in _cache else None
    for m in datos:
        if m.get('_v') != version or version is None:
            m['codigo'] = codigo_por_nombre(m['cliente_nombre'])
            m['_v'] = _cache['indice_nombres']['version']
    return datos


def maquinas_cliente(codigo):
    codigos = set(codigos_hermanos(codigo))
    return [m for m in maquinas() if m['codigo'] in codigos]


def clave_modelo(m):
    """Modelo comparable entre clientes (el modelo; si no hay, el nombre de la máquina)."""
    return clave_nombre(m['modelo'] or m['maquina'])


# ---------------------------------------------------------------- pedidos pendientes de clientes
def _procesar_pedidos(contenido):
    lista = []
    for d in _filas(contenido):
        cliente = _texto(d.get('NombreCliente'))
        if not cliente:
            continue
        lista.append({
            'cliente_nombre': cliente, 'pedido': _texto(d.get('N_Pedido')), 'fecha': _fecha(d.get('Fecha')),
            'referencia': _texto(d.get('Referencia')), 'descripcion': _texto(d.get('Descripcion')),
            'cantidad': _num(d.get('Cantidad')), 'importe': _num(d.get('ImporteFinal') or d.get('Importe')),
            'entrega': _fecha_o_vacio(d.get('Fecha_Entrega')), 'urgente': _texto(d.get('Urgente')).upper().startswith('S'),
            'observaciones': _texto(d.get('Observaciones')),
        })
    return lista


def pedidos_pendientes():
    datos = _cargado('pedidos', lambda: almacen.buscar_pirineos('PedidosPendientesCliente.xlsx', 'Pedidos pendientes'), _procesar_pedidos)
    if datos is None:
        return []
    version = _cache['indice_nombres']['version'] if 'indice_nombres' in _cache else None
    for p in datos:
        if p.get('_v') != version or version is None:
            p['codigo'] = codigo_por_nombre(p['cliente_nombre'])
            p['_v'] = _cache['indice_nombres']['version']
    return datos


def pedidos_cliente(codigo):
    """Pedidos pendientes de servir de un cliente (todos sus códigos), con retraso, llegada prevista del
    proveedor y si se ha reclamado (lo que se marca en Pedidos pendientes del escritorio)."""
    codigos = set(codigos_hermanos(codigo))
    hoy = date.today().isoformat()
    lleg = llegadas_por_referencia()
    recl = reclamaciones()
    salida = []
    for p in pedidos_pendientes():
        if p['codigo'] not in codigos:
            continue
        p = dict(p)
        p['retraso'] = (date.today() - date.fromisoformat(p['entrega'])).days if p['entrega'] and p['entrega'] < hoy else 0
        prox = lleg.get(p['referencia'], [])
        p['llegada'] = prox[0] if prox else None
        p['reclamado'] = recl.get((p['pedido'], p['referencia'])) or recl.get(('', p['referencia'], clave_nombre(p['cliente_nombre'])))
        salida.append(p)
    return sorted(salida, key=lambda p: (-p['retraso'], p['fecha']), reverse=False)


def comprometido_por_referencia():
    """{referencia: unidades pendientes de servir a clientes}."""
    res = {}
    for p in pedidos_pendientes():
        if p['referencia']:
            res[p['referencia']] = res.get(p['referencia'], 0) + (p['cantidad'] or 0)
    return res


# ---------------------------------------------------------------- pedidos pendientes a proveedores
def _col(d, *nombres, excluir=()):
    """Valor de la primera columna cuyo nombre (sin tildes, espacios ni guiones) coincide o empieza por uno de
    los dados. Los Excel de Pirineos cambian a veces el nombre exacto de la columna."""
    claves = {''.join(ch for ch in normalizar(k) if ch.isalnum()).lower(): k for k in d if k}
    for n in nombres:
        if n in claves and n not in excluir:
            return d[claves[n]]
    for n in nombres:
        for k, orig in claves.items():
            if k.startswith(n) and k not in excluir:
                return d[orig]
    return None


def _procesar_pedidos_proveedor(contenido):
    lista = []
    for d in _filas(contenido):
        ref = _texto(_col(d, 'referencia'))
        if not ref:
            continue
        prevista = _fecha_o_vacio(_col(d, 'fechaprevistarecepcion', 'fechaprevista', 'fecharecepcion', 'fechaentrega',
                                       excluir=('fecharecepcion2', 'fechaprevistarecepcion3')))
        lista.append({'proveedor': _texto(_col(d, 'nombreproveedor', 'proveedor')), 'pedido': _texto(_col(d, 'npedido', 'pedido')),
                      'fecha': _fecha(_col(d, 'fecha')), 'prevista': prevista, 'referencia': ref,
                      'descripcion': _texto(_col(d, 'descripcion')), 'cantidad': _num(_col(d, 'cantidad'))})
    return lista


def pedidos_proveedor():
    datos = _cargado('pedidos_proveedor', lambda: almacen.buscar_pirineos('PedidosPendientesProveedor.xlsx', 'Pedidos pendientes'),
                     _procesar_pedidos_proveedor)
    return datos or []


def llegadas_por_referencia():
    """{referencia: [{prevista, cantidad, proveedor, pedido, retrasada}]} de lo pedido a proveedores, por fecha."""
    hoy = date.today().isoformat()
    res = {}
    for p in pedidos_proveedor():
        res.setdefault(p['referencia'], []).append({'prevista': p['prevista'], 'cantidad': p['cantidad'], 'proveedor': p['proveedor'],
                                                    'pedido': p['pedido'], 'retrasada': bool(p['prevista'] and p['prevista'] < hoy)})
    for v in res.values():
        v.sort(key=lambda x: x['prevista'] or '9999')
    return res


# ---------------------------------------------------------------- reclamaciones (Pedidos pendientes del escritorio)
def _procesar_reclamados(contenido):
    try:
        regs = json.loads(contenido.decode('utf-8')).get('reclamaciones', [])
    except Exception:
        return []
    return [r for r in regs if isinstance(r, dict) and r.get('fecha')]


def reclamaciones():
    """{(pedido cliente, referencia): registro} y {('', referencia, clave cliente): registro}. Lo marca la oficina
    en «Mi unidad/Pedidos Pendientes/Reclamados.json» (compartido con la cuenta de la app)."""
    try:
        regs = _cargado('reclamados', lambda: almacen.buscar_compartido('Reclamados.json'), _procesar_reclamados) or []
    except Exception:
        regs = []
    res = {}
    for r in regs:
        info = {'fecha': r.get('fecha', ''), 'veces': len(r.get('historial') or [r.get('fecha')]), 'proveedor': r.get('proveedor', '')}
        if r.get('pedido_cliente') not in (None, '', '-'):
            res[(str(r['pedido_cliente']), r.get('referencia', ''))] = info
        if r.get('cliente'):
            res[('', r.get('referencia', ''), clave_nombre(r['cliente']))] = info
    return res


# ---------------------------------------------------------------- todas las ventas (para análisis)
def _ventas_por_codigo_crudas():
    """{codigo: [lineas]} sin agrupar, con lo que ya esté en caché (no descarga nada)."""
    resultado = {}
    for nombre in _ficheros_ventas():
        for cliente, lineas in ((_cache.get(f'ventas:{nombre}') or {}).get('datos') or {}).items():
            resultado.setdefault(cliente, []).extend(lineas)
    return resultado


def todas_las_ventas():
    """({codigo_cliente: [lineas]}, version) de todos los ficheros de ventas cargados.

    Los códigos de un mismo cliente comparten la MISMA lista (todas las ventas del cliente,
    cada línea con su 'codigo'), así que cualquier cálculo por código ve el cliente entero.
    Para recorrer clientes sin repetirlos, usar clientes_unicos()."""
    nombres = _ficheros_ventas()

    def cargar(nombre):
        return _cargado(f'ventas:{nombre}', lambda: almacen.buscar_pirineos(nombre, 'Movimientos de venta'), _procesar_ventas)

    with ThreadPoolExecutor(max_workers=len(nombres)) as ex:
        resultados = list(ex.map(cargar, nombres))
    version = '|'.join(str(_cache.get(f'ventas:{n}', {}).get('clave')) for n in nombres) + '|' + str(_cache.get('clientes', {}).get('clave'))
    entrada = _cache.get('ventas_todas')
    if entrada and entrada.get('version') == version:
        return entrada['datos'], version
    crudas = {}
    for datos in resultados:
        for cliente, lineas in (datos or {}).items():
            crudas.setdefault(cliente, []).extend(dict(l, codigo=cliente) for l in lineas)
    por_cliente = {}
    for cliente in crudas:
        if cliente in por_cliente:
            continue
        cods = codigos_hermanos(cliente)
        lineas = [l for c in cods for l in crudas.get(c, [])]
        vistos, unicas = set(), []
        for l in sorted(lineas, key=lambda l: l['fecha'], reverse=True):
            k = (l['codigo'], l['tipo'], l['documento'], l['referencia'], l['cantidad'], l['importe'], l['fecha'])
            if k not in vistos:
                vistos.add(k)
                unicas.append(l)
        for c in cods:
            por_cliente[c] = unicas
    _cache['ventas_todas'] = {'version': version, 'datos': por_cliente, 'clave': version, 'comprobado': time.time()}
    return por_cliente, version


def clientes_unicos(codigos):
    """Deja un solo código (el principal) por cliente."""
    vistos, lista = set(), []
    for c in codigos:
        pr = principal(c)
        if pr not in vistos:
            vistos.add(pr)
            lista.append(pr)
    return lista
