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
            'comercial': _texto(d.get('Comercial')),
            'comercial_norm': normalizar(d.get('Comercial')),
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
        lineas.extend(datos.get(str(codigo_cliente), []))
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


def _indice_nombres():
    cl = clientes()
    entrada = _cache.get('indice_nombres')
    version = (_cache.get('clientes', {}).get('clave'), (_cache.get('ventas_todas') or {}).get('version'))
    if entrada and entrada.get('version') == version:
        return entrada['datos']
    # En Pirineos una misma empresa puede tener varios códigos (uno por departamento:
    # tintas, preimpresión, maquinaria...). Para cruzar por nombre se elige el código
    # "principal" (el de más ventas o, si no hay ventas, el más bajo) y se guardan todos
    # los códigos hermanos para mostrar en cada uno lo que llegue por nombre.
    por_razon = {}
    for c in cl.values():
        k = clave_nombre(c['nombre'])
        if k:
            por_razon.setdefault(k, set()).add(c['codigo'])
    ventas = (_cache.get('ventas_todas') or {}).get('datos') or {}
    def principal(codigos):
        return max(codigos, key=lambda x: (len(ventas.get(x, [])), -int(x) if x.isdigit() else 0))
    hermanos = {}
    for codigos in por_razon.values():
        for x in codigos:
            hermanos[x] = sorted(codigos, key=lambda y: int(y) if y.isdigit() else 0)
    exactos = {k: principal(v) for k, v in por_razon.items()}
    for c in cl.values():  # nombre comercial, solo si no lleva a dos empresas distintas
        k = clave_nombre(c['nombre_comercial'])
        if k and k not in exactos:
            exactos[k] = c['codigo']
    indice = {'exactos': exactos, 'hermanos': hermanos}
    _cache['indice_nombres'] = {'version': version, 'datos': indice, 'clave': version, 'comprobado': time.time()}
    return indice


def codigos_hermanos(codigo):
    """Todos los códigos de Pirineos de la misma empresa (misma razón social), incluido este."""
    return _indice_nombres()['hermanos'].get(str(codigo), [str(codigo)])


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
    codigos = set(codigos_hermanos(codigo))
    return sorted((p for p in pedidos_pendientes() if p['codigo'] in codigos), key=lambda p: p['fecha'], reverse=True)


# ---------------------------------------------------------------- todas las ventas (para análisis)
def todas_las_ventas():
    """({codigo_cliente: [lineas]}, version) de todos los ficheros de ventas cargados."""
    nombres = _ficheros_ventas()

    def cargar(nombre):
        return _cargado(f'ventas:{nombre}', lambda: almacen.buscar_pirineos(nombre, 'Movimientos de venta'), _procesar_ventas)

    with ThreadPoolExecutor(max_workers=len(nombres)) as ex:
        resultados = list(ex.map(cargar, nombres))
    version = '|'.join(str(_cache.get(f'ventas:{n}', {}).get('clave')) for n in nombres)
    entrada = _cache.get('ventas_todas')
    if entrada and entrada.get('version') == version:
        return entrada['datos'], version
    por_cliente = {}
    for datos in resultados:
        for cliente, lineas in (datos or {}).items():
            por_cliente.setdefault(cliente, []).extend(lineas)
    for cliente, lineas in por_cliente.items():
        vistos, unicas = set(), []
        for l in sorted(lineas, key=lambda l: l['fecha'], reverse=True):
            k = (l['tipo'], l['documento'], l['referencia'], l['cantidad'], l['importe'], l['fecha'])
            if k not in vistos:
                vistos.add(k)
                unicas.append(l)
        por_cliente[cliente] = unicas
    _cache['ventas_todas'] = {'version': version, 'datos': por_cliente, 'clave': version, 'comprobado': time.time()}
    return por_cliente, version
