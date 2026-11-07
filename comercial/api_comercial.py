# comercial/api_comercial.py
"""API del módulo Comercial (/api/comercial/...).

- Acceso con permisos; visitas (horas de entrada y salida a mano, se pueden ir guardando),
  llamadas y precios dados; agenda y resumen de visitas diarias y tiempos.
- Control de visitas: clientes que toca visitar y clientes visitados de más.
- Ficha de cliente con «Preparar visita»: lo hablado, seguimientos, avisos del SAT,
  pedidos pendientes, máquinas, productos que deja de comprar y sugerencias.
- Precios (tarifa, precio especial y, solo Master, coste y margen estimado).
- Oportunidades de venta que marcan los técnicos (dentro de «Preparar visita»).
- Clientes a recuperar (dormidos / en riesgo), rutas y asistente.
"""
import os
import sys
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from comercial import actividad, analisis, control, geo, pirineos, sat  # noqa: E402
from comercial.usuarios import (USUARIOS, Sesion, clave_por_nombre, comerciales, comprobar_clave,  # noqa: E402
                                crear_token, normalizar, sesion_actual)

router = APIRouter()

# Listas de opciones (instrucciones, apartados 4, 6 y 7). Se pueden cambiar aquí.
OPCIONES = {
    'temas': ['Reposición de consumibles', 'Revisión de precios', 'Presentación de productos',
              'Demostración de maquinaria', 'Venta de maquinaria', 'Servicio técnico', 'Incidencia o reclamación',
              'Negociación comercial', 'Seguimiento de oferta', 'Prospección', 'Recuperación de cliente', 'Otros'],
    'resultados': ['Pedido conseguido', 'Oferta solicitada', 'Oferta pendiente de preparar', 'Seguimiento pendiente',
                   'Nueva oportunidad detectada', 'Cliente sin necesidades actuales', 'Cliente no disponible',
                   'Visita aplazada', 'Incidencia detectada', 'Sin resultado comercial', 'Otro'],
    'tipos_llamada': ['Prospección', 'Seguimiento de oferta', 'Negociación de precios', 'Reposición de consumibles',
                      'Venta de maquinaria', 'Recuperación de cliente', 'Incidencia', 'Coordinación de visita', 'Otro'],
}

# Colores de las tarifas para la consulta discreta de precios (configurables).
COLORES = {
    '1': {'color': '#3b82f6', 'nombre': 'Azul'},
    '2': {'color': '#22c55e', 'nombre': 'Verde'},
    '3': {'color': '#f59e0b', 'nombre': 'Ámbar'},
    'especial': {'color': '#a855f7', 'nombre': 'Violeta'},
}


def _error(e):
    if isinstance(e, HTTPException):
        raise e
    raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------- sesión
class Login(BaseModel):
    usuario: str
    clave: str


@router.post('/login')
def login(datos: Login):
    clave = clave_por_nombre(datos.usuario)
    if not clave:
        raise HTTPException(status_code=404, detail='Usuario sin acceso al módulo comercial.')
    if not comprobar_clave(clave, datos.clave):
        raise HTTPException(status_code=401, detail='Contraseña incorrecta.')
    sesion = Sesion(clave)
    return {'token': crear_token(clave), **sesion.publico()}


@router.get('/yo')
def yo(s: Sesion = Depends(sesion_actual)):
    leyenda = {k: {**v, 'etiqueta': ('Precio especial' if k == 'especial' else f'Tarifa {k}')} for k, v in COLORES.items()}
    return {**s.publico(), 'opciones': OPCIONES, 'colores': leyenda, 'para_cerrar': [c for c, _ in CAMPOS_PARA_CERRAR],
            'comerciales': comerciales() if s.puede('ver_todos') else [], 'hoy': actividad.ahora().strftime('%Y-%m-%d')}


def _claves_visibles(s, comercial=''):
    if not s.puede('ver_todos'):
        return [s.clave]
    if comercial:
        clave = clave_por_nombre(comercial)
        if not clave:
            raise HTTPException(status_code=404, detail='Comercial no encontrado.')
        return [clave]
    return [k for k in USUARIOS if k != 'master'] + ['master']


# ---------------------------------------------------------------- actividad
@router.get('/actividad')
def listar_actividad(desde: str = '', hasta: str = '', comercial: str = '', cliente: str = '',
                     s: Sesion = Depends(sesion_actual)):
    try:
        registros = actividad.listar(_claves_visibles(s, comercial), desde, hasta)
        if cliente:
            registros = [r for r in registros if r['COD_CLIENTE'] == cliente]
        return {'registros': registros}
    except Exception as e:
        _error(e)


@router.get('/actividad/pendientes')
def pendientes(comercial: str = '', s: Sesion = Depends(sesion_actual)):
    """Visitas sin terminar (falta la hora de salida u otro dato) y seguimientos pendientes."""
    try:
        registros = actividad.listar(_claves_visibles(s, comercial))
        sin_terminar = [r for r in registros if r['ESTADO'] == actividad.SIN_TERMINAR]
        seguimientos = sorted(
            (r for r in registros if r['ESTADO'] == actividad.REGISTRADA and r['FECHA_SEGUIMIENTO']
             and r['SEGUIMIENTO_HECHO'] != 'SI'),
            key=lambda r: r['FECHA_SEGUIMIENTO'])
        return {'sin_terminar': sin_terminar, 'seguimientos': seguimientos}
    except Exception as e:
        _error(e)


def _dueno(s, id_registro):
    """Clave del usuario dueño de un registro (el ID empieza por las 3 primeras letras)."""
    pref = id_registro.split('-', 1)[0].lower()
    candidatos = [k for k in USUARIOS if k[:3] == pref]
    if not candidatos:
        raise HTTPException(status_code=404, detail='Registro no encontrado.')
    for clave in candidatos:
        if clave != s.clave and not s.puede('ver_todos'):
            continue
        r = actividad.obtener(clave, id_registro)
        if r:
            return clave, r
    raise HTTPException(status_code=404, detail='Registro no encontrado.')


@router.get('/actividad/{id_registro}')
def ver_actividad(id_registro: str, s: Sesion = Depends(sesion_actual)):
    try:
        clave, r = _dueno(s, id_registro)
        puede_editar = clave == s.clave or s.puede('editar_otros')
        return {'registro': r, 'historial': actividad.historial(clave, id_registro), 'puede_editar': puede_editar}
    except Exception as e:
        _error(e)


class Registro(BaseModel):
    datos: dict
    confirmar_solape: bool = False
    cerrar: bool = False   # botón «Cerrar visita»: la da por terminada


# Lo que tiene que estar relleno para poder cerrar una visita (darla por bien hecha).
CAMPOS_PARA_CERRAR = [('CLIENTE', 'cliente'), ('FECHA', 'fecha'), ('HORA_INICIO', 'hora de entrada'),
                      ('HORA_FIN', 'hora de salida'), ('TEMAS', 'temas tratados'), ('DESCRIPCION', 'qué se ha hablado'),
                      ('RESULTADO', 'resultado')]


def _estado(d, cerrar, anterior=''):
    """Una visita queda SIN TERMINAR (se puede ir guardando) hasta que se pulsa «Cerrar visita»,
    que exige hora de entrada y de salida. Una visita ya cerrada sigue cerrada al editarla."""
    if d['TIPO'] != 'VISITA':
        return actividad.REGISTRADA
    if cerrar or anterior == actividad.REGISTRADA:
        faltan = [nombre for campo, nombre in CAMPOS_PARA_CERRAR if not d.get(campo)]
        if faltan:
            raise HTTPException(status_code=400, detail='Para cerrar la visita falta: ' + ', '.join(faltan) + '.')
        return actividad.REGISTRADA
    return actividad.SIN_TERMINAR


def _comprobar_y_guardar(s, clave, d, id_registro=None, confirmar_solape=False, cerrar=False, anterior=''):
    estado = _estado(d, cerrar, anterior)
    if not confirmar_solape:
        cruzadas = actividad.solapes(clave, d, excluir_id=id_registro or '')
        if cruzadas:
            lista = '; '.join(f"{r['HORA_INICIO']}-{r['HORA_FIN']} {r['CLIENTE']}" for r in cruzadas)
            raise HTTPException(status_code=409, detail=f'SOLAPE|Este horario se cruza con: {lista}. ¿Guardar igualmente?')
    if id_registro:
        return actividad.actualizar(clave, s.nombre, id_registro, {**d, 'ESTADO': estado})
    return actividad.crear(clave, s.nombre, d, estado=estado)


@router.post('/actividad')
def crear_actividad(reg: Registro, s: Sesion = Depends(sesion_actual)):
    try:
        if not s.puede('registrar'):
            raise HTTPException(status_code=403, detail='Sin permiso para registrar actividad.')
        d, errores = actividad.validar(reg.datos)
        if errores:
            raise HTTPException(status_code=400, detail=' '.join(errores))
        return {'registro': _comprobar_y_guardar(s, s.clave, d, confirmar_solape=reg.confirmar_solape, cerrar=reg.cerrar)}
    except Exception as e:
        _error(e)


@router.put('/actividad/{id_registro}')
def editar_actividad(id_registro: str, reg: Registro, s: Sesion = Depends(sesion_actual)):
    """Guarda cambios (se puede ir guardando una visita a medias y completarla después)."""
    try:
        clave, r = _dueno(s, id_registro)
        if clave != s.clave and not s.puede('editar_otros'):
            raise HTTPException(status_code=403, detail='No puedes modificar la actividad de otra persona.')
        if r['ESTADO'] == actividad.ANULADA:
            raise HTTPException(status_code=409, detail='El registro está anulado.')
        d, errores = actividad.validar(reg.datos)
        if errores:
            raise HTTPException(status_code=400, detail=' '.join(errores))
        return {'registro': _comprobar_y_guardar(s, clave, d, id_registro, reg.confirmar_solape, reg.cerrar, r['ESTADO'])}
    except Exception as e:
        _error(e)


class Motivo(BaseModel):
    motivo: str = ''


@router.post('/actividad/{id_registro}/anular')
def anular_actividad(id_registro: str, m: Motivo, s: Sesion = Depends(sesion_actual)):
    try:
        clave, r = _dueno(s, id_registro)
        if clave != s.clave and not s.puede('editar_otros'):
            raise HTTPException(status_code=403, detail='No puedes anular la actividad de otra persona.')
        if not m.motivo.strip():
            raise HTTPException(status_code=400, detail='Indica el motivo de la anulación.')
        cambios = {'ESTADO': actividad.ANULADA,
                   'OBSERVACIONES': (r['OBSERVACIONES'] + ' ' if r['OBSERVACIONES'] else '') + f'[Anulado: {m.motivo.strip()}]'}
        return {'registro': actividad.actualizar(clave, s.nombre, id_registro, cambios, accion='ANULACIÓN')}
    except Exception as e:
        _error(e)


@router.post('/actividad/{id_registro}/seguimiento_hecho')
def seguimiento_hecho(id_registro: str, s: Sesion = Depends(sesion_actual)):
    try:
        clave, r = _dueno(s, id_registro)
        if clave != s.clave and not s.puede('editar_otros'):
            raise HTTPException(status_code=403, detail='No puedes modificar la actividad de otra persona.')
        return {'registro': actividad.actualizar(clave, s.nombre, id_registro, {'SEGUIMIENTO_HECHO': 'SI'}, accion='SEGUIMIENTO HECHO')}
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- precios dados (seguimiento)
class PrecioDado(BaseModel):
    cod_cliente: str
    cliente: str
    referencia: str
    descripcion: str = ''
    precio: float
    nota: str = ''


@router.post('/precios_dados')
def apuntar_precio(p: PrecioDado, s: Sesion = Depends(sesion_actual)):
    """Apunta el precio que el comercial ha dado a un cliente para una referencia."""
    try:
        if p.precio <= 0:
            raise HTTPException(status_code=400, detail='Indica un precio mayor que cero.')
        momento = actividad.ahora()
        d, errores = actividad.validar({
            'TIPO': 'PRECIO', 'COD_CLIENTE': p.cod_cliente, 'CLIENTE': p.cliente, 'FECHA': momento.strftime('%Y-%m-%d'),
            'HORA_INICIO': momento.strftime('%H:%M'), 'PRODUCTOS': p.referencia, 'DESCRIPCION': p.descripcion,
            'IMPORTE': f'{p.precio:.4f}'.rstrip('0').rstrip('.'), 'OBSERVACIONES': p.nota})
        if errores:
            raise HTTPException(status_code=400, detail=' '.join(errores))
        return {'registro': actividad.crear(s.clave, s.nombre, d)}
    except Exception as e:
        _error(e)


def precios_dados(s, cliente='', comercial=''):
    regs = actividad.listar(_claves_visibles(s, comercial))
    return [r for r in reversed(regs) if r['TIPO'] == 'PRECIO' and r['ESTADO'] == actividad.REGISTRADA
            and (not cliente or r['COD_CLIENTE'] == cliente)]


@router.get('/precios_dados')
def listar_precios_dados(cliente: str = '', comercial: str = '', s: Sesion = Depends(sesion_actual)):
    try:
        return {'precios': precios_dados(s, cliente, comercial)}
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- clientes
@router.get('/clientes')
def listar_clientes(s: Sesion = Depends(sesion_actual)):
    """Listado ligero de clientes para el buscador del móvil."""
    try:
        mio = normalizar(s.pirineos)
        perfiles = analisis.perfiles_todos()
        lista = []
        for c in pirineos.clientes().values():
            if c['grupo'].upper() == 'PROVEEDORES':
                continue
            lista.append({
                'codigo': c['codigo'], 'nombre': c['nombre'], 'nombre_comercial': c['nombre_comercial'],
                'poblacion': c['poblacion'], 'provincia': c['provincia'], 'comercial': c['comercial'],
                'mio': bool(mio) and c['comercial_norm'] == mio,
                'ultima_compra': perfiles.get(c['codigo'], {}).get('ultima', ''),
                'estado': perfiles.get(c['codigo'], {}).get('estado', 'sin_compras'),
                'ubicado': geo.coordenadas_cliente(c) is not None,
            })
        lista.sort(key=lambda c: c['nombre'])
        return {'clientes': lista}
    except Exception as e:
        _error(e)


@router.get('/clientes/{codigo}')
def ficha_cliente(codigo: str, s: Sesion = Depends(sesion_actual)):
    try:
        c = pirineos.clientes().get(codigo)
        if not c:
            raise HTTPException(status_code=404, detail='Cliente no encontrado en el maestro de Pirineos.')
        datos = {k: v for k, v in c.items() if k not in ('comercial_norm', 'tarifa')}

        lineas, leidos, faltan = pirineos.ventas_cliente(codigo)
        # Histórico agrupado por referencia: última compra y último precio facturado.
        por_ref = {}
        for l in lineas:  # ya vienen de más reciente a más antigua
            if l['tipo'] == 'SAT':
                continue
            p = por_ref.setdefault(l['referencia'] or l['descripcion'], {
                'referencia': l['referencia'], 'descripcion': l['descripcion'], 'familia': l['familia'],
                'ultima_fecha': l['fecha'], 'ultimo_precio': l['precio'], 'ultimo_dto': l['dto'],
                'veces': 0, 'cantidad_total': 0.0, 'importe_total': 0.0, 'precios': []})
            p['veces'] += 1
            p['cantidad_total'] += l['cantidad']
            p['importe_total'] += l['importe']
            if len(p['precios']) < 12 and l['precio']:
                p['precios'].append({'fecha': l['fecha'], 'precio': l['precio'], 'dto': l['dto']})
        compras = sorted(por_ref.values(), key=lambda p: p['ultima_fecha'], reverse=True)
        arts = pirineos.articulos()
        for p in compras:
            a = arts.get(p['referencia'])
            p['precio_actual'] = _precio_aplicable(c, a, codigo) if a else None

        especiales = pirineos.precios_especiales(codigo)
        for e in especiales:
            a = arts.get(e['referencia'])
            e['descripcion'] = a['descripcion'] if a else ''

        registros = actividad.listar(_claves_visibles(s))
        propios = [r for r in registros if r['COD_CLIENTE'] == codigo]
        visitas = [r for r in propios if r['TIPO'] == 'VISITA' and r['ESTADO'] == actividad.REGISTRADA]
        maquinas = pirineos.maquinas_cliente(codigo)
        avisos_abiertos, partes_recientes = sat.avisos_cliente(codigo)
        ops = [o for o in sat.oportunidades() if o['codigo'] == codigo]
        seguimientos = [r for r in propios if r['ESTADO'] == actividad.REGISTRADA and r['FECHA_SEGUIMIENTO']
                        and r['SEGUIMIENTO_HECHO'] != 'SI']
        ultimo = next((r for r in reversed(propios) if r['ESTADO'] == actividad.REGISTRADA and r['TIPO'] != 'PRECIO'), None)
        # Control de visitas de este cliente (visitas cerradas de cualquier comercial).
        todas = [r for r in actividad.listar(list(USUARIOS)) if r['TIPO'] == 'VISITA' and r['ESTADO'] == actividad.REGISTRADA]
        del_cliente = [r['FECHA'] for r in todas if r['COD_CLIENTE'] == codigo]
        de_su_comercial = [r for r in todas if normalizar(USUARIOS.get(clave_por_nombre(r['COMERCIAL']) or '', {}).get('pirineos', '')) == c['comercial_norm']]
        estado_visitas = control.estado_cliente(c, del_cliente, control.compras_12_meses(), control.cadencias(), inicio_de(de_su_comercial) or date.today())
        return {
            'cliente': datos,
            'resumen': {
                'ultima_compra': lineas[0]['fecha'] if lineas else '',
                'ultima_visita': max((r['FECHA'] for r in visitas), default=''),
                'ultimo_contacto': max((r['FECHA'] for r in propios if r['ESTADO'] == actividad.REGISTRADA), default=''),
                'facturado_periodo': round(sum(l['importe'] for l in lineas), 2),
            },
            'compras': compras,
            'especiales': especiales,
            'actividad': [r for r in reversed(propios) if r['TIPO'] != 'PRECIO'],
            'precios_dados': [r for r in reversed(propios) if r['TIPO'] == 'PRECIO'],
            'control': estado_visitas,
            'frecuencias': control.OPCIONES_FRECUENCIA,
            'periodo_ventas': {'leidos': leidos, 'faltan': faltan},
            'preparar': {
                'perfil': analisis.perfil(lineas),
                'ultimo_contacto': ultimo,
                'seguimientos': seguimientos,
                'avisos_sat': avisos_abiertos,
                'partes_recientes': partes_recientes,
                'pedidos': pirineos.pedidos_cliente(codigo),
                'oportunidades_sat': ops,
                'dejando_de_comprar': analisis.dejando_de_comprar(lineas)[:12],
                'familias_en_caida': analisis.familias_en_caida(lineas)[:6],
                'sugerencias': analisis.sugerencias(codigo),
                'avisos_maquinas': analisis.avisos_maquinas(maquinas),
            },
            'maquinas': sorted(maquinas, key=lambda m: (m['baja'], m['instalacion'] or '0'), reverse=False),
            'ubicado': geo.coordenadas_cliente(c) is not None,
        }
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- precios
def _precio_aplicable(cliente, art, codigo_cliente):
    """Precio que se puede ofrecer: especial vigente si existe; si no, la tarifa del cliente."""
    especial = pirineos.precio_especial_vigente(codigo_cliente, art['referencia']) if codigo_cliente else None
    if especial:
        return {'precio': especial['precio'], 'clase': 'especial', 'hasta': especial['hasta']}
    tarifa = cliente['tarifa'] if cliente else '1'
    precio = art['tarifas'].get(tarifa) or 0
    if not precio and tarifa != '1':
        precio, tarifa = art['tarifas'].get('1') or 0, '1'
    return {'precio': precio, 'clase': tarifa} if precio else None


def _margen(precio, coste):
    if not precio or not coste:
        return None
    return {'euros': round(precio - coste, 2), 'pct_venta': round((precio - coste) / precio * 100, 1),
            'recargo_coste': round((precio - coste) / coste * 100, 1)}


@router.get('/articulos')
def buscar_articulos(q: str = '', cliente: str = '', limite: int = 40, s: Sesion = Depends(sesion_actual)):
    try:
        palabras = normalizar(q).split()
        if not palabras:
            return {'articulos': [], 'total': 0}
        arts = pirineos.articulos()
        c = pirineos.clientes().get(cliente) if cliente else None
        def orden(a):
            exacto = a['referencia'] == q.strip() or normalizar(a['ref_proveedor']) == normalizar(q)
            desc = normalizar(a['descripcion']).split()
            # primero las que tienen todas las palabras en la descripción (palabra completa > trozo)
            completas = sum(p in desc for p in palabras)
            en_desc = sum(any(p in w for w in desc) for p in palabras)
            return (not exacto, -completas, -en_desc, a['stock'] <= 0, a['descripcion'])

        encontrados = sorted((a for a in arts.values() if all(p in a['_buscar'] for p in palabras)), key=orden)
        ver_costes = s.puede('ver_costes')
        resultado = []
        for a in encontrados[:max(1, min(limite, 100))]:
            fila = {
                'referencia': a['referencia'], 'ref_proveedor': a['ref_proveedor'], 'descripcion': a['descripcion'],
                'familia': a['familia'], 'subfamilia': a['subfamilia'], 'marca': a['marca'],
                'hay_stock': a['stock'] > 0,
                'tarifas': [{'clase': k, 'precio': v} for k, v in a['tarifas'].items() if v],
                'aplicable': _precio_aplicable(c, a, cliente) if c else None,
            }
            if ver_costes:
                fila['coste'] = a['coste'] or None
                fila['stock'] = a['stock']
                fila['margenes'] = {t['clase']: _margen(t['precio'], a['coste']) for t in fila['tarifas']}
                if fila['aplicable']:
                    fila['margen_aplicable'] = _margen(fila['aplicable']['precio'], a['coste'])
            resultado.append(fila)
        return {'articulos': resultado, 'total': len(encontrados)}
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- clientes a recuperar
def _es_mio(s, c):
    return bool(s.pirineos) and c['comercial_norm'] == normalizar(s.pirineos)


def _filtro_cartera(s, comercial=''):
    """Función que dice si un cliente entra en la cartera que se está mirando."""
    if not s.puede('ver_todos'):
        return lambda c: _es_mio(s, c)
    if comercial:
        clave = clave_por_nombre(comercial)
        objetivo = normalizar(USUARIOS[clave]['pirineos']) if clave else ''
        return lambda c: c['comercial_norm'] == objetivo
    return lambda c: True


def clientes_a_recuperar(s, comercial='', incluir_sin_compras=False):
    perfiles = analisis.perfiles_todos()
    en_cartera = _filtro_cartera(s, comercial)
    lista = []
    for c in pirineos.clientes().values():
        if c['grupo'].upper() == 'PROVEEDORES' or not en_cartera(c):
            continue
        p = perfiles.get(c['codigo'])
        if p and p['estado'] in ('dormido', 'en_riesgo'):
            lista.append({'codigo': c['codigo'], 'nombre': c['nombre'], 'poblacion': c['poblacion'], 'comercial': c['comercial'],
                          'estado': p['estado'], 'motivo': p['motivo'], 'ultima': p['ultima'], 'dias': p['dias'],
                          'compraba_al_anio': round(p['prev12'] or p['ult12'], 2)})
        elif incluir_sin_compras and not p and c['comercial']:
            lista.append({'codigo': c['codigo'], 'nombre': c['nombre'], 'poblacion': c['poblacion'], 'comercial': c['comercial'],
                          'estado': 'sin_compras', 'motivo': 'Sin compras en el histórico cargado (puede ser un cliente antiguo).',
                          'ultima': '', 'dias': None, 'compraba_al_anio': 0})
    orden = {'dormido': 0, 'en_riesgo': 1, 'sin_compras': 2}
    lista.sort(key=lambda x: (orden[x['estado']], -x['compraba_al_anio']))
    return lista


@router.get('/recuperar')
def recuperar(comercial: str = '', sin_compras: bool = False, s: Sesion = Depends(sesion_actual)):
    try:
        return {'clientes': clientes_a_recuperar(s, comercial, sin_compras),
                'criterio': (f'Dormido: lleva más de 3 veces su intervalo habitual de compra (mínimo {analisis.DORMIDO_MIN_DIAS} días) '
                             f'o, si compra poco, más de un año. En riesgo: más de 2 veces su intervalo (mínimo {analisis.RIESGO_MIN_DIAS} días).')}
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- oportunidades del SAT
def oportunidades_visibles(s, comercial=''):
    ops = sat.oportunidades()
    if not s.puede('ver_todos'):
        return [o for o in ops if o['usuario'] == s.clave]
    if comercial:
        clave = clave_por_nombre(comercial)
        return [o for o in ops if o['usuario'] == clave]
    return ops


@router.get('/oportunidades_sat')
def listar_oportunidades(comercial: str = '', s: Sesion = Depends(sesion_actual)):
    try:
        return {'oportunidades': oportunidades_visibles(s, comercial), 'estados': sat.ESTADOS}
    except Exception as e:
        _error(e)


class EstadoOportunidad(BaseModel):
    estado: str
    nota: str = ''


@router.post('/oportunidades_sat/{id_op}')
def cambiar_oportunidad(id_op: str, datos: EstadoOportunidad, s: Sesion = Depends(sesion_actual)):
    try:
        op = next((o for o in sat.oportunidades() if o['id'] == id_op), None)
        if not op:
            raise HTTPException(status_code=404, detail='Oportunidad no encontrada.')
        if not s.puede('ver_todos') and op['usuario'] != s.clave:
            raise HTTPException(status_code=403, detail='Esta oportunidad es de otro comercial.')
        sat.actualizar_oportunidad(id_op, datos.estado, datos.nota.strip(), s.nombre,
                                   actividad.ahora().strftime('%Y-%m-%d %H:%M'))
        return {'ok': True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- rutas
@router.get('/geo/estado')
def geo_estado(s: Sesion = Depends(sesion_actual)):
    try:
        faltan = geo.pendientes()
        return {'faltan': len(faltan), 'localizados': len(geo._cargar())}
    except Exception as e:
        _error(e)


@router.post('/geo/preparar')
def geo_preparar(s: Sesion = Depends(sesion_actual)):
    """Localiza códigos postales pendientes (unos pocos por llamada; la pantalla repite hasta terminar)."""
    try:
        return geo.preparar(8)  # poco tiempo por llamada (límite de Vercel); la pantalla va repitiendo
    except Exception as e:
        _error(e)


def candidatos_ruta(s, origen, radio_km=30, comercial='', solo_cartera=True):
    hoy = actividad.ahora().strftime('%Y-%m-%d')
    perfiles = analisis.perfiles_todos()
    en_cartera = _filtro_cartera(s, comercial) if solo_cartera else (lambda c: True)
    registros = actividad.listar(_claves_visibles(s, comercial if s.puede('ver_todos') else ''))
    por_cliente = {}
    for r in registros:
        if r['COD_CLIENTE'] and r['ESTADO'] == actividad.REGISTRADA:
            por_cliente.setdefault(r['COD_CLIENTE'], []).append(r)
    ops = {}
    for o in oportunidades_visibles(s, comercial):
        if o['codigo'] and o['estado'] in sat.ABIERTOS:
            ops.setdefault(o['codigo'], []).append(o)
    pedidos = {}
    for p in pirineos.pedidos_pendientes():
        if p['codigo']:
            pedidos.setdefault(p['codigo'], []).append(p)
    averias = {}
    for a in sat.avisos_abiertos():
        if a['codigo']:
            averias.setdefault(a['codigo'], []).append(a)

    resultado = []
    for c in pirineos.clientes().values():
        if c['grupo'].upper() == 'PROVEEDORES' or not en_cartera(c):
            continue
        pos = geo.coordenadas_cliente(c)
        dist = geo.distancia_km(origen, pos) if pos else None
        if dist is None or dist > radio_km:
            continue
        motivos, puntos = [], 0.0
        p = perfiles.get(c['codigo'])
        if p and p['estado'] == 'dormido':
            puntos += 3; motivos.append('Dormido: ' + p['motivo'])
        elif p and p['estado'] == 'en_riesgo':
            puntos += 2; motivos.append('En riesgo: ' + p['motivo'])
        regs = por_cliente.get(c['codigo'], [])
        pend = [r for r in regs if r['FECHA_SEGUIMIENTO'] and r['SEGUIMIENTO_HECHO'] != 'SI']
        if any(r['FECHA_SEGUIMIENTO'] <= hoy for r in pend):
            puntos += 3; motivos.append('Seguimiento vencido: ' + (pend[0]['PROXIMA_ACCION'] or 'pendiente'))
        elif pend:
            puntos += 1; motivos.append(f"Seguimiento el {pend[0]['FECHA_SEGUIMIENTO']}")
        if ops.get(c['codigo']):
            puntos += 3; motivos.append('Oportunidad del SAT: ' + (ops[c['codigo']][0]['detalle'] or 'ver detalle')[:80])
        if pedidos.get(c['codigo']):
            puntos += 0.5; motivos.append(f"{len(pedidos[c['codigo']])} línea(s) de pedido pendientes")
        if averias.get(c['codigo']):
            puntos += 0.5; motivos.append('Tiene un aviso del SAT abierto')
        visitas = [r['FECHA'] for r in regs if r['TIPO'] == 'VISITA']
        if visitas and (date.fromisoformat(hoy) - date.fromisoformat(max(visitas))).days > 60:
            puntos += 1; motivos.append(f"Sin visitar desde el {max(visitas)}")
        importe = (p['ult12'] if p else 0) or 0
        puntos += min(importe, 50000) / 25000  # un poco más de peso a los que más compran
        resultado.append({
            'codigo': c['codigo'], 'nombre': c['nombre'], 'poblacion': c['poblacion'], 'comercial': c['comercial'],
            'direccion': ', '.join(x for x in (c['direccion'], f"{c['cp']} {c['poblacion']}".strip(), c['provincia']) if x),
            'lat': pos[0], 'lon': pos[1], 'km': dist, 'puntos': round(puntos, 2), 'motivos': motivos,
            'compras_12m': round(importe, 2),
        })
    resultado.sort(key=lambda x: (-x['puntos'], x['km']))
    return resultado


@router.get('/ruta/candidatos')
def ruta_candidatos(lat: float | None = None, lon: float | None = None, cliente: str = '', radio: float = 30,
                    comercial: str = '', todos: bool = False, s: Sesion = Depends(sesion_actual)):
    """Clientes cerca de un punto (mi ubicación, o la de un cliente) ordenados por interés."""
    try:
        if lat is None or lon is None:
            c = pirineos.clientes().get(cliente)
            pos = geo.coordenadas_cliente(c) if c else None
            if not pos:
                raise HTTPException(status_code=400, detail='No hay punto de partida: activa la ubicación o elige un cliente ya localizado.')
            lat, lon = pos
        radio = max(2, min(radio, 300))
        return {'origen': {'lat': lat, 'lon': lon}, 'candidatos': candidatos_ruta(s, (lat, lon), radio, comercial, not todos)[:80],
                'pendientes_geo': len(geo.pendientes())}
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- control de visitas
def control_visitas(s, comercial=''):
    """Clientes de la cartera con su frecuencia de visita y si toca visitarlos o se visitan de más."""
    visitas = [r for r in actividad.listar(list(USUARIOS)) if r['TIPO'] == 'VISITA' and r['ESTADO'] == actividad.REGISTRADA]
    if s.rol == 'master' and not comercial:
        usuarios = [k for k, u in USUARIOS.items() if u['rol'] == 'comercial']
    elif s.puede('ver_todos'):
        k = clave_por_nombre(comercial)
        if not k:
            raise HTTPException(status_code=404, detail='Comercial no encontrado.')
        usuarios = [k]
    else:
        usuarios = [s.clave]
    resultado = []
    for k in usuarios:
        u = USUARIOS[k]
        propias = [r for r in visitas if r['COMERCIAL'] == u['nombre']]
        resultado += [{**x, 'usuario': u['nombre']} for x in control.control_cartera(u['pirineos'], visitas, inicio_de(propias))]
    orden = {'toca': 0, 'demasiado': 1, 'pronto': 2, 'ok': 3}
    resultado.sort(key=lambda x: (orden[x['estado']], -(x['dias_sin_visita'] or 0), -x['compras_12m']))
    return resultado


def inicio_de(visitas_propias):
    """Desde cuándo usa la app el comercial (su primera visita), para no marcar en rojo a toda
    la cartera el primer día."""
    fechas = [r['FECHA'] for r in visitas_propias if r['FECHA']]
    return date.fromisoformat(min(fechas)) if fechas else None


@router.get('/control')
def ver_control(comercial: str = '', s: Sesion = Depends(sesion_actual)):
    try:
        return {'clientes': control_visitas(s, comercial), 'frecuencias': control.OPCIONES_FRECUENCIA,
                'criterio': ('Frecuencia propuesta según lo que compra al año (15.000 € o más: mensual; 5.000-15.000: bimestral; '
                             '1.000-5.000: trimestral; menos: semestral; sin compras: no se exige). Master puede fijarla en la ficha. '
                             '«Visitado de más»: más del doble de las visitas previstas en 90 días, o 3+ visitas a un cliente que no compra.')}
    except Exception as e:
        _error(e)


class Frecuencia(BaseModel):
    dias: int | None = None   # None = volver a la propuesta automática


@router.put('/frecuencia/{codigo}')
def fijar_frecuencia(codigo: str, f: Frecuencia, s: Sesion = Depends(sesion_actual)):
    try:
        if s.rol != 'master':
            raise HTTPException(status_code=403, detail='Solo Master puede cambiar la frecuencia de visita.')
        if not pirineos.clientes().get(codigo):
            raise HTTPException(status_code=404, detail='Cliente no encontrado.')
        control.fijar_cadencia(codigo, f.dias, s.nombre, actividad.ahora().strftime('%Y-%m-%d %H:%M'))
        return {'ok': True}
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- resumen: visitas diarias y tiempos
def resumen(s, desde, hasta, comercial=''):
    """Por comercial y día: nº de visitas, tiempo total y medio, primera entrada y última salida."""
    regs = [r for r in actividad.listar(_claves_visibles(s, comercial), desde, hasta)
            if r['TIPO'] == 'VISITA' and r['ESTADO'] == actividad.REGISTRADA and desde <= r['FECHA'] <= hasta]
    dias = {}
    for r in regs:
        dias.setdefault((r['COMERCIAL'], r['FECHA']), []).append(r)
    filas = []
    for (comercial_nombre, fecha), vs in sorted(dias.items(), key=lambda x: (x[0][1], x[0][0]), reverse=True):
        minutos = [int(v['DURACION_MIN'] or 0) for v in vs]
        filas.append({'comercial': comercial_nombre, 'fecha': fecha, 'visitas': len(vs), 'minutos': sum(minutos),
                      'media': round(sum(minutos) / len(vs)) if vs else 0,
                      'entrada': min(v['HORA_INICIO'] for v in vs), 'salida': max(v['HORA_FIN'] for v in vs),
                      'clientes': len({v['COD_CLIENTE'] or v['CLIENTE'] for v in vs})})
    por_comercial = {}
    for f in filas:
        t = por_comercial.setdefault(f['comercial'], {'comercial': f['comercial'], 'dias': 0, 'visitas': 0, 'minutos': 0})
        t['dias'] += 1; t['visitas'] += f['visitas']; t['minutos'] += f['minutos']
    for t in por_comercial.values():
        t['visitas_por_dia'] = round(t['visitas'] / t['dias'], 1) if t['dias'] else 0
        t['media'] = round(t['minutos'] / t['visitas']) if t['visitas'] else 0
    return {'desde': desde, 'hasta': hasta, 'dias': filas, 'totales': list(por_comercial.values())}


@router.get('/resumen')
def ver_resumen(desde: str, hasta: str, comercial: str = '', s: Sesion = Depends(sesion_actual)):
    try:
        return resumen(s, desde, hasta, comercial)
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- asistente
class Mensaje(BaseModel):
    rol: str
    texto: str


class AccionPantalla(BaseModel):
    nombre: str
    descripcion: str = ''
    parametros: dict = {}


class ResultadoAccion(BaseModel):
    call_id: str
    salida: str = ''


class PeticionAsistente(BaseModel):
    mensaje: str = ''
    historial: list[Mensaje] = []
    contexto: dict = {}
    web: bool = False                      # búsqueda en internet (bola del mundo)
    control: bool = False                  # el robot puede usar la app
    acciones: list[AccionPantalla] = []    # lo que admite la pantalla
    continuar: str = ''                    # id de la respuesta cuyas acciones ya se han ejecutado
    resultados: list[ResultadoAccion] = []


@router.post('/asistente')
def asistente(req: PeticionAsistente, s: Sesion = Depends(sesion_actual)):
    try:
        from comercial import asistente as ia
        return ia.responder(s, req.mensaje, [m.model_dump() for m in req.historial], req.contexto,
                            web=req.web, control=req.control, acciones=[a.model_dump() for a in req.acciones],
                            continuar=req.continuar, resultados=[r.model_dump() for r in req.resultados])
    except HTTPException:
        raise
    except Exception as e:
        nombre = type(e).__name__
        if nombre == 'AuthenticationError':
            detalle = 'la clave de OpenAI no es válida o ha caducado (OPENAI_API_KEY en contraseñas.txt).'
        elif nombre == 'RateLimitError':
            detalle = 'se ha alcanzado el límite de uso de OpenAI; prueba en unos minutos.'
        elif nombre in ('APIConnectionError', 'APITimeoutError'):
            detalle = 'no hay conexión con el servicio de IA; prueba de nuevo.'
        else:
            detalle = str(e)
        raise HTTPException(status_code=503, detail=f'El asistente no está disponible ahora: {detalle}')
