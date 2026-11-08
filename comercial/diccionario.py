# comercial/diccionario.py
"""Diccionario de sinónimos (el mismo que se edita en el módulo Diccionario del escritorio).

Fichero: «Mi unidad/Diccionario y Sinonimos/Diccionario.xlsx» (compartido con la cuenta de la app).
Columnas: Origen, Cliente, Producto, Sinonimo, Referencia, Referencia cliente, Proveedor.

Sirve para que en los buscadores de artículos se encuentre un producto aunque se escriba como lo llama
un cliente o con otro nombre: «agua de fuente» -> la referencia que diga el diccionario.
"""
from comercial import almacen, pirineos
from comercial.usuarios import normalizar

FICHERO = 'Diccionario.xlsx'


def _procesar(contenido):
    entradas = []
    for d in pirineos._filas(contenido):
        e = {k: pirineos._texto(d.get(c)) for k, c in (('cliente', 'Cliente'), ('producto', 'Producto'), ('sinonimo', 'Sinonimo'),
                                                         ('referencia', 'Referencia'), ('ref_cliente', 'Referencia cliente'),
                                                         ('proveedor', 'Proveedor'))}
        if not (e['referencia'] or e['producto']):
            continue
        e['_buscar'] = normalizar(' '.join([e['sinonimo'], e['ref_cliente'], e['producto']]))
        e['_cliente'] = pirineos.clave_nombre(e['cliente']) if e['cliente'] else ''
        entradas.append(e)
    return entradas


def entradas():
    try:
        return pirineos._cargado('diccionario', lambda: almacen.buscar_compartido(FICHERO), _procesar) or []
    except Exception:
        return []


def coincidencias(q, nombre_cliente=''):
    """Entradas del diccionario que encajan con lo buscado (todas las palabras en el sinónimo, la referencia
    del cliente o el producto). Las del cliente elegido van primero; las de otros clientes no se usan."""
    palabras = normalizar(q).split()
    if not palabras:
        return []
    cli = pirineos.clave_nombre(nombre_cliente) if nombre_cliente else ''
    res = [e for e in entradas() if all(p in e['_buscar'] for p in palabras) and (not e['_cliente'] or not cli or e['_cliente'] == cli)]
    return sorted(res, key=lambda e: not (cli and e['_cliente'] == cli))


def referencias(q, nombre_cliente=''):
    """{referencia: texto del sinónimo que la ha traído} para lo buscado."""
    arts = pirineos.articulos()
    res = {}
    for e in coincidencias(q, nombre_cliente):
        motivo = e['sinonimo'] or e['ref_cliente'] or e['producto']
        if e['referencia'] and e['referencia'] in arts:
            res.setdefault(e['referencia'], motivo)
        elif e['producto']:      # sin referencia: los artículos cuyo nombre lleva todas las palabras del producto
            pal = normalizar(e['producto']).split()
            for a in arts.values():
                if pal and all(p in a['_buscar'] for p in pal):
                    res.setdefault(a['referencia'], motivo)
    return res
