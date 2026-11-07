# comercial/api_comercial.py
"""API del módulo Comercial (/api/comercial/...).

- Acceso con permisos; visitas (cronómetro o a mano) con ubicación al entrar y salir,
  llamadas e hitos (incluidas ventas de maquinaria); agenda.
- Ficha de cliente con «Preparar visita»: lo hablado, seguimientos, avisos del SAT,
  pedidos pendientes, máquinas, productos que deja de comprar y sugerencias.
- Precios (tarifa, precio especial y, solo Master, coste y margen estimado).
- Oportunidades de venta que marcan los técnicos en los partes del SAT.
- Clientes a recuperar (dormidos / en riesgo), rutas, resumen semanal (Master) y asistente.
"""
import os
import sys
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from comercial import actividad, analisis, geo, pirineos, sat  # noqa: E402
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
    'tipos_hito': ['Venta de maquinaria', 'Cierre de una operación importante', 'Pedido extraordinario',
                   'Primera compra de un nuevo cliente', 'Recuperación de un cliente dormido',
                   'Nueva oportunidad comercial', 'Demostración realizada', 'Negociación estratégica',
                   'Acuerdo de precios', 'Firma de contrato', 'Reclamación importante', 'Pérdida de una operación', 'Otro'],
    'estados_venta_maquinaria': ['Operación ganada', 'Pedido confirmado', 'Venta facturada'],
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
    return {**s.publico(), 'opciones': OPCIONES, 'colores': leyenda,
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
    """Visitas en curso y seguimientos pendientes (fecha de seguimiento sin marcar como hecho)."""
    try:
        registros = actividad.listar(_claves_visibles(s, comercial))
        en_curso = [r for r in registros if r['ESTADO'] == actividad.EN_CURSO]
        seguimientos = sorted(
            (r for r in registros if r['ESTADO'] == actividad.REGISTRADA and r['FECHA_SEGUIMIENTO']
             and r['SEGUIMIENTO_HECHO'] != 'SI'),
            key=lambda r: r['FECHA_SEGUIMIENTO'])
        return {'en_curso': en_curso, 'seguimientos': seguimientos}
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


class Ubicacion(BaseModel):
    lat: float | None = None
    lon: float | None = None
    precision: float | None = None
    error: str = ''   # motivo si el móvil no ha dado la ubicación (denegada, sin señal...)


def _registrar_ubicacion(ub, cod_cliente):
    """(texto para guardar, distancia aproximada al cliente en km o '')."""
    if ub is None:
        return 'SIN DATOS', ''
    if ub.lat is None or ub.lon is None:
        return f"NO DISPONIBLE ({(ub.error or 'sin motivo')[:60]})", ''
    texto = f"{ub.lat:.5f},{ub.lon:.5f},{round(ub.precision or 0)}"
    c = pirineos.clientes().get(cod_cliente) if cod_cliente else None
    dist = geo.distancia_recta_km((ub.lat, ub.lon), geo.coordenadas_cliente(c)) if c else None
    return texto, ('' if dist is None else str(dist))


class Iniciar(BaseModel):
    cod_cliente: str = ''
    cliente: str
    poblacion: str = ''
    ubicacion: Ubicacion | None = None


@router.post('/actividad/iniciar')
def iniciar_visita(datos: Iniciar, s: Sesion = Depends(sesion_actual)):
    """Cronómetro: crea la visita EN CURSO con la hora del servidor."""
    try:
        if not s.puede('registrar'):
            raise HTTPException(status_code=403, detail='Sin permiso para registrar actividad.')
        if not datos.cliente.strip():
            raise HTTPException(status_code=400, detail='Elige el cliente.')
        abiertas = [r for r in actividad.listar([s.clave]) if r['ESTADO'] == actividad.EN_CURSO]
        if abiertas:
            raise HTTPException(status_code=409, detail=f"Ya tienes una visita en curso en {abiertas[0]['CLIENTE']}. Finalízala antes de empezar otra.")
        momento = actividad.ahora()
        d = {c: '' for c in actividad.EDITABLES}
        d.update({'TIPO': 'VISITA', 'COD_CLIENTE': datos.cod_cliente.strip(), 'CLIENTE': datos.cliente.strip(),
                  'POBLACION': datos.poblacion.strip(), 'FECHA': momento.strftime('%Y-%m-%d'),
                  'HORA_INICIO': momento.strftime('%H:%M')})
        d['UBI_INICIO'], d['DIST_INICIO_KM'] = _registrar_ubicacion(datos.ubicacion, d['COD_CLIENTE'])
        return {'registro': actividad.crear(s.clave, s.nombre, d, estado=actividad.EN_CURSO, registro='CRONÓMETRO')}
    except Exception as e:
        _error(e)


@router.post('/actividad/{id_registro}/finalizar')
def finalizar_visita(id_registro: str, ub: Ubicacion | None = None, s: Sesion = Depends(sesion_actual)):
    """Cronómetro: pone la hora de fin (del servidor). La visita queda EN CURSO hasta que
    se completa el formulario con los temas y el resultado."""
    try:
        clave, r = _dueno(s, id_registro)
        if clave != s.clave:
            raise HTTPException(status_code=403, detail='Solo quien inició la visita puede finalizarla.')
        if r['ESTADO'] != actividad.EN_CURSO:
            raise HTTPException(status_code=409, detail='Esta visita ya no está en curso.')
        if r['HORA_FIN']:
            return {'registro': r}
        momento = actividad.ahora()
        hora_fin = momento.strftime('%H:%M')
        if momento.strftime('%Y-%m-%d') != r['FECHA']:
            hora_fin = '23:59'  # la visita no puede pasar de un día a otro
        if hora_fin <= r['HORA_INICIO']:  # menos de un minuto
            ini = datetime.strptime(r['HORA_INICIO'], '%H:%M')
            hora_fin = (ini + timedelta(minutes=1)).strftime('%H:%M')
        ubi, dist = _registrar_ubicacion(ub, r['COD_CLIENTE'])
        r = actividad.actualizar(clave, s.nombre, id_registro, {'HORA_FIN': hora_fin, 'UBI_FIN': ubi, 'DIST_FIN_KM': dist},
                                 accion='FIN CRONÓMETRO')
        return {'registro': r}
    except Exception as e:
        _error(e)


class Registro(BaseModel):
    datos: dict
    confirmar_solape: bool = False


def _comprobar_y_guardar(s, clave, d, id_registro=None, confirmar_solape=False):
    if not confirmar_solape:
        cruzadas = actividad.solapes(clave, d, excluir_id=id_registro or '')
        if cruzadas:
            lista = '; '.join(f"{r['HORA_INICIO']}-{r['HORA_FIN']} {r['CLIENTE']}" for r in cruzadas)
            raise HTTPException(status_code=409, detail=f'SOLAPE|Este horario se cruza con: {lista}. ¿Guardar igualmente para revisión?')
    if id_registro:
        cambios = dict(d)
        cambios['DURACION_MIN'] = d.get('DURACION_MIN', '')
        return actividad.actualizar(clave, s.nombre, id_registro, cambios)
    return actividad.crear(clave, s.nombre, d)


@router.post('/actividad')
def crear_actividad(reg: Registro, s: Sesion = Depends(sesion_actual)):
    try:
        if not s.puede('registrar'):
            raise HTTPException(status_code=403, detail='Sin permiso para registrar actividad.')
        d, errores = actividad.validar(reg.datos)
        if errores:
            raise HTTPException(status_code=400, detail=' '.join(errores))
        return {'registro': _comprobar_y_guardar(s, s.clave, d, confirmar_solape=reg.confirmar_solape)}
    except Exception as e:
        _error(e)


@router.put('/actividad/{id_registro}')
def editar_actividad(id_registro: str, reg: Registro, s: Sesion = Depends(sesion_actual)):
    """Guarda cambios. Si la visita estaba EN CURSO (cronómetro), queda REGISTRADA."""
    try:
        clave, r = _dueno(s, id_registro)
        if clave != s.clave and not s.puede('editar_otros'):
            raise HTTPException(status_code=403, detail='No puedes modificar la actividad de otra persona.')
        if r['ESTADO'] == actividad.ANULADA:
            raise HTTPException(status_code=409, detail='El registro está anulado.')
        d, errores = actividad.validar(reg.datos)
        if errores:
            raise HTTPException(status_code=400, detail=' '.join(errores))
        # Si se corrige a mano la hora que tomó el cronómetro, queda dicho en el propio registro
        # (además del historial de cambios).
        if r['REGISTRO'] == 'CRONÓMETRO' and (d['HORA_INICIO'] != r['HORA_INICIO'] or (r['HORA_FIN'] and d['HORA_FIN'] != r['HORA_FIN'])):
            nota = f"[Horario del cronómetro corregido a mano; original {r['HORA_INICIO']}-{r['HORA_FIN'] or '?'}]"
            if nota not in d['OBSERVACIONES']:
                d['OBSERVACIONES'] = (d['OBSERVACIONES'] + ' ' if d['OBSERVACIONES'] else '') + nota
        if r['ESTADO'] == actividad.EN_CURSO:
            d['ESTADO'] = actividad.REGISTRADA
        return {'registro': _comprobar_y_guardar(s, clave, d, id_registro, reg.confirmar_solape)}
    except Exception as e:
        _error(e)


@router.post('/actividad/{id_registro}/borrador')
def borrador_visita(id_registro: str, reg: Registro, s: Sesion = Depends(sesion_actual)):
    """Guarda lo escrito de una visita EN CURSO sin cerrarla (no toca fecha ni horas)."""
    try:
        clave, r = _dueno(s, id_registro)
        if clave != s.clave:
            raise HTTPException(status_code=403, detail='Solo quien inició la visita puede modificarla.')
        if r['ESTADO'] != actividad.EN_CURSO:
            raise HTTPException(status_code=409, detail='Esta visita ya no está en curso.')
        d, _ = actividad.validar({**reg.datos, 'FECHA': r['FECHA']}, en_curso=True)
        for campo in ('TIPO', 'FECHA', 'HORA_INICIO', 'HORA_FIN', 'DURACION_MIN', 'COD_CLIENTE', 'CLIENTE', 'POBLACION'):
            d.pop(campo, None)
        return {'registro': actividad.actualizar(clave, s.nombre, id_registro, d, accion='BORRADOR')}
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
        ultimo = next((r for r in reversed(propios) if r['ESTADO'] == actividad.REGISTRADA), None)
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
            'actividad': list(reversed(propios)),
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


# ---------------------------------------------------------------- ubicación: información al usuario
@router.post('/ubicacion/informado')
def ubicacion_informado(s: Sesion = Depends(sesion_actual)):
    """Deja constancia (historial del usuario) de que se le ha informado del registro de ubicación."""
    try:
        actividad._anotar(s.clave, s.nombre, '-', 'INFORMADO REGISTRO DE UBICACIÓN', [])
        return {'ok': True}
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- resumen semanal (Master)
def resumen_semana(desde):
    lunes = date.fromisoformat(desde)
    lunes -= timedelta(days=lunes.weekday())
    domingo = lunes + timedelta(days=6)
    d, h = lunes.isoformat(), domingo.isoformat()
    hoy = actividad.ahora().strftime('%Y-%m-%d')
    claves = [k for k in USUARIOS]
    registros = actividad.listar(claves, d, h)
    todos = actividad.listar(claves)
    personas = []
    for k in claves:
        nombre = USUARIOS[k]['nombre']
        regs = [r for r in registros if r['COMERCIAL'] == nombre and r['ESTADO'] == actividad.REGISTRADA]
        visitas = [r for r in regs if r['TIPO'] == 'VISITA']
        con_ubi = [r for r in visitas if r['UBI_INICIO'][:1].isdigit() or r['UBI_INICIO'][:1] == '-']
        lejos = [r for r in visitas if r['DIST_INICIO_KM'] and float(r['DIST_INICIO_KM']) > 5]
        vencidos = [r for r in todos if r['COMERCIAL'] == nombre and r['ESTADO'] == actividad.REGISTRADA and r['FECHA_SEGUIMIENTO']
                    and r['SEGUIMIENTO_HECHO'] != 'SI' and r['FECHA_SEGUIMIENTO'] < hoy]
        if not regs and not vencidos and USUARIOS[k]['rol'] == 'master':
            continue
        personas.append({
            'nombre': nombre, 'visitas': len(visitas), 'llamadas': sum(r['TIPO'] == 'LLAMADA' for r in regs),
            'hitos': sum(r['TIPO'] == 'HITO' for r in regs),
            'minutos_visitas': sum(int(r['DURACION_MIN'] or 0) for r in visitas),
            'clientes': len({r['COD_CLIENTE'] or r['CLIENTE'] for r in visitas}),
            'dias_con_actividad': len({r['FECHA'] for r in regs}),
            'visitas_con_ubicacion': len(con_ubi), 'visitas_a_mano': sum(r['REGISTRO'] == 'MANUAL' for r in visitas),
            'visitas_lejos': len(lejos), 'seguimientos_vencidos': len(vencidos),
        })
    hitos = [r for r in registros if r['TIPO'] == 'HITO' and r['ESTADO'] == actividad.REGISTRADA]
    ops = [o for o in sat.oportunidades() if d <= o['fecha'] <= h]
    clientes = pirineos.clientes()
    recuperados = []
    for x in analisis.recuperados(d, h):
        c = clientes.get(x['codigo'])
        if c:
            recuperados.append({**x, 'nombre': c['nombre'], 'comercial': c['comercial']})
    return {'desde': d, 'hasta': h, 'personas': personas, 'hitos': hitos, 'oportunidades_sat': ops,
            'recuperados': sorted(recuperados, key=lambda x: -x['dias_parado'])}


@router.get('/resumen_semana')
def ver_resumen_semana(desde: str = '', s: Sesion = Depends(sesion_actual)):
    try:
        if s.rol != 'master':
            raise HTTPException(status_code=403, detail='Solo para Master.')
        return resumen_semana(desde or actividad.ahora().strftime('%Y-%m-%d'))
    except ValueError:
        raise HTTPException(status_code=400, detail='Fecha no válida.')
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- asistente
class Mensaje(BaseModel):
    rol: str
    texto: str


class PeticionAsistente(BaseModel):
    mensaje: str
    historial: list[Mensaje] = []
    contexto: dict = {}


@router.post('/asistente')
def asistente(req: PeticionAsistente, s: Sesion = Depends(sesion_actual)):
    try:
        from comercial import asistente as ia
        return ia.responder(s, req.mensaje, [m.model_dump() for m in req.historial], req.contexto)
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
