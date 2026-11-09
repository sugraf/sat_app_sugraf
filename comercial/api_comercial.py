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

from comercial import actividad, analisis, control, geo, incidencias, notificaciones, ofertas, pirineos, sat  # noqa: E402
from comercial.usuarios import (USUARIOS, Sesion, clave_por_nombre, comerciales, comprobar_clave,  # noqa: E402
                                crear_token, normalizar, sesion_actual)

router = APIRouter()

# Listas de opciones (instrucciones, apartados 4, 6 y 7). Se pueden cambiar aquí.
OPCIONES = {
    'temas': ['Reposición de consumibles', 'Revisión de precios', 'Presentación de productos',
              'Demostración de maquinaria', 'Venta de maquinaria', 'Servicio técnico', 'Incidencia o reclamación',
              'Negociación comercial', 'Seguimiento de oferta', 'Prospección', 'Recuperación de cliente', 'Otros'],
    # Pocas opciones a propósito; la oferta va aparte («¿Se ha conseguido una oferta?»).
    'resultados': ['Pedido conseguido', 'Seguimiento pendiente', 'Sin necesidades ahora', 'Incidencia detectada'],
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
            mismos = set(pirineos.codigos_hermanos(cliente))
            registros = [r for r in registros if r['COD_CLIENTE'] in mismos]
        return {'registros': registros}
    except Exception as e:
        _error(e)


@router.get('/actividad/pendientes')
def pendientes(comercial: str = '', s: Sesion = Depends(sesion_actual)):
    """Visitas sin terminar (falta la hora de salida u otro dato) y seguimientos pendientes."""
    try:
        registros = actividad.listar(_claves_visibles(s, comercial))
        sin_terminar = [r for r in registros if r['ESTADO'] == actividad.SIN_TERMINAR]
        comprados = {p['ID'] for p in _con_seguimiento_precio([r for r in registros if r['TIPO'] == 'PRECIO']) if p['estado_precio'] != 'PENDIENTE'}
        seguimientos = sorted(
            (r for r in registros if r['ESTADO'] == actividad.REGISTRADA and r['FECHA_SEGUIMIENTO']
             and r['SEGUIMIENTO_HECHO'] != 'SI' and r['ID'] not in comprados),
            key=lambda r: r['FECHA_SEGUIMIENTO'])
        programadas = sorted((r for r in registros if r['ESTADO'] == actividad.PROGRAMADA),
                             key=lambda r: (r['FECHA'], r['HORA_INICIO'] or '99:99'))
        return {'sin_terminar': sin_terminar, 'seguimientos': seguimientos, 'programadas': programadas}
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
                      ('HORA_FIN', 'hora de salida'), ('DESCRIPCION', 'qué se ha hablado'),
                      ('RESULTADO', 'resultado'), ('OFERTA', 'si se ha conseguido una oferta')]
# «¿Se ha conseguido una oferta?» (OFERTA = SI / NO). Si es SI: PRODUCTOS = «referencia · artículo» elegido del
# buscador y/o NECESIDADES = descripción libre (al menos una de las dos). Avisa a la oficina en Generación de ofertas.
OFERTA_SI, OFERTA_NO = 'SI', 'NO'


def _faltan(d, anterior=''):
    # Las visitas cerradas antes de existir la pregunta de la oferta se pueden seguir editando sin contestarla.
    return [(c, n) for c, n in CAMPOS_PARA_CERRAR if not d.get(c) and not (c == 'OFERTA' and anterior == actividad.REGISTRADA)]


def _comprobar_oferta(d):
    d['OFERTA'] = d.get('OFERTA', '').upper()
    if d['OFERTA'] not in ('', OFERTA_SI, OFERTA_NO):
        raise HTTPException(status_code=400, detail='Oferta: indica Sí o No.')
    if d['OFERTA'] == OFERTA_SI and not d.get('PRODUCTOS') and not d.get('NECESIDADES'):
        raise HTTPException(status_code=400, detail='Has marcado que se ha conseguido una oferta: indica qué (artículo) o escribe una breve descripción.')
    if d['OFERTA'] != OFERTA_SI and d['TIPO'] in ('VISITA', 'LLAMADA'):
        d['PRODUCTOS'] = d['NECESIDADES'] = ''


PALABRAS_MAQUINA = ('MAQUIN', 'IMPRESORA', 'PLOTTER', 'PLOTER', 'EQUIPO', 'CORTADORA', 'LAMINADORA', 'GUILLOTINA', 'PRENSA',
                    'ROUTER', 'ESCANER', 'ESCÁNER', 'PLEGADORA', 'HENDIDORA', 'ENCUADERNADORA', 'INSOLADORA', 'PROCESADORA', 'CTP')


def _modulo_oferta(d):
    """Generación de ofertas de maquinaria o de consumibles: por la familia del artículo elegido o, si solo hay
    descripción, por sus palabras (máquina, impresora, plotter…)."""
    ref = (d.get('PRODUCTOS') or '').split(' · ')[0].strip()
    art = pirineos.articulos().get(ref) if ref else None
    if art:
        texto = art.get('grupo', '') + ' ' + art.get('familia', '')
    else:
        texto = (d.get('PRODUCTOS') or '') + ' ' + (d.get('NECESIDADES') or '')
    texto = normalizar(texto).upper()
    return 'ofertas_maquinaria' if any(normalizar(p).upper() in texto for p in PALABRAS_MAQUINA) else 'ofertas_consumibles'


def _avisar_oferta(s, d, registro, previo=None):
    """Aviso en la app de escritorio (campana del módulo de ofertas) cuando una visita o llamada pasa a tener
    oferta conseguida, o cambia lo que se ha ofertado. Si falla, la visita queda guardada igual."""
    previo = previo or {}
    if d.get('OFERTA') != OFERTA_SI:
        return
    if previo.get('OFERTA') == OFERTA_SI and previo.get('PRODUCTOS', '') == d.get('PRODUCTOS', '') \
            and previo.get('NECESIDADES', '') == d.get('NECESIDADES', ''):
        return
    try:
        que = ' — '.join(x for x in (d.get('PRODUCTOS'), d.get('NECESIDADES')) if x)
        tipo = 'la visita' if d['TIPO'] == 'VISITA' else 'la llamada'
        notificaciones.crear('OFERTA_CONSEGUIDA', _modulo_oferta(d), f'Oferta conseguida: {d["CLIENTE"]}',
                             f'{s.nombre} ha conseguido una oferta en {tipo} a {d["CLIENTE"]} del {d["FECHA"][8:10]}/{d["FECHA"][5:7]}: '
                             f'{que}. ¿Preparar la oferta?',
                             {'cliente': d['CLIENTE'], 'codigo': d.get('COD_CLIENTE', ''), 'articulo': d.get('PRODUCTOS', ''),
                              'descripcion': d.get('NECESIDADES', ''), 'comercial': s.nombre, 'registro': registro.get('ID', '')},
                             actividad.ahora())
    except Exception:
        pass


def _estado(d, cerrar, anterior=''):
    """Una visita queda SIN TERMINAR (se puede ir guardando) hasta que se pulsa «Cerrar visita»,
    que exige hora de entrada y de salida. Una visita ya cerrada sigue cerrada al editarla."""
    if d['TIPO'] == 'LLAMADA':
        # Las llamadas se guardan de una vez y llevan lo mismo que una visita terminada.
        faltan = [nombre.replace('entrada', 'inicio').replace('salida', 'fin') for campo, nombre in _faltan(d, anterior)]
        if faltan:
            raise HTTPException(status_code=400, detail='Para guardar la llamada falta: ' + ', '.join(faltan) + '.')
        return actividad.REGISTRADA
    if d['TIPO'] != 'VISITA':
        return actividad.REGISTRADA
    if cerrar or anterior == actividad.REGISTRADA:
        faltan = [nombre for campo, nombre in _faltan(d, '' if cerrar else anterior)]
        if faltan:
            raise HTTPException(status_code=400, detail='Para cerrar la visita falta: ' + ', '.join(faltan) + '.')
        return actividad.REGISTRADA
    return actividad.SIN_TERMINAR


def _comprobar_y_guardar(s, clave, d, id_registro=None, confirmar_solape=False, cerrar=False, anterior='', previo=None):
    _comprobar_oferta(d)
    estado = _estado(d, cerrar, anterior)
    if not confirmar_solape:
        cruzadas = actividad.solapes(clave, d, excluir_id=id_registro or '')
        if cruzadas:
            lista = '; '.join(f"{r['HORA_INICIO']}-{r['HORA_FIN']} {r['CLIENTE']}" for r in cruzadas)
            raise HTTPException(status_code=409, detail=f'SOLAPE|Este horario se cruza con: {lista}. ¿Guardar igualmente?')
    if id_registro:
        registro = actividad.actualizar(clave, s.nombre, id_registro, {**d, 'ESTADO': estado})
    else:
        registro = actividad.crear(clave, s.nombre, d, estado=estado)
    _avisar_oferta(s, d, registro, previo)
    return registro


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
        return {'registro': _comprobar_y_guardar(s, clave, d, id_registro, reg.confirmar_solape, reg.cerrar, r['ESTADO'], previo=r)}
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
    confirmar_precio_bajo: bool = False


# Margen mínimo (sobre el precio de venta, con el último coste de compra). Se puede fijar por grupo o familia de
# artículos de Pirineos (en mayúsculas, como salen en Pirineos); lo que no esté aquí usa el de por defecto.
MARGEN_MINIMO_DEFECTO = 15.0
MARGENES_MINIMOS = {}      # p. ej. {'MAQUINARIA': 10, 'PLANCHAS': 12}


def margen_minimo(art):
    for clave in (art.get('subfamilia', ''), art.get('familia', ''), art.get('grupo', '')):
        if clave and clave.upper() in MARGENES_MINIMOS:
            return float(MARGENES_MINIMOS[clave.upper()])
    return MARGEN_MINIMO_DEFECTO


def _precio_bajo(art, precio):
    """{margen, minimo} si el precio deja menos margen del mínimo; None si está bien o no hay coste."""
    if not art or not art.get('coste') or not precio:
        return None
    m = _margen(precio, art['coste'])
    minimo = margen_minimo(art)
    return {'margen': m['pct_venta'], 'minimo': minimo} if m and m['pct_venta'] < minimo else None


@router.post('/precios_dados')
def apuntar_precio(p: PrecioDado, s: Sesion = Depends(sesion_actual)):
    """Apunta el precio que el comercial ha dado a un cliente para una referencia."""
    try:
        if p.precio <= 0:
            raise HTTPException(status_code=400, detail='Indica un precio mayor que cero.')
        art_pb = pirineos.articulos().get(p.referencia)
        bajo = _precio_bajo(art_pb, p.precio)
        if bajo and not p.confirmar_precio_bajo:
            # No se enseña el coste: solo que está por debajo del margen mínimo y que se avisará a la oficina.
            raise HTTPException(status_code=409, detail='PRECIO_BAJO|Este precio queda por debajo del margen mínimo '
                                f"({bajo['minimo']:g} %). Si lo apuntas, se avisará a la oficina. ¿Apuntarlo igualmente?")
        momento = actividad.ahora()
        d, errores = actividad.validar({
            'TIPO': 'PRECIO', 'COD_CLIENTE': p.cod_cliente, 'CLIENTE': p.cliente, 'FECHA': momento.strftime('%Y-%m-%d'),
            'HORA_INICIO': momento.strftime('%H:%M'), 'PRODUCTOS': p.referencia, 'DESCRIPCION': p.descripcion,
            'IMPORTE': f'{p.precio:.4f}'.rstrip('0').rstrip('.'), 'OBSERVACIONES': p.nota,
            # Recordatorio para saber si al final compra (sale en «Seguimientos pendientes» de Hoy).
            'FECHA_SEGUIMIENTO': (momento + timedelta(days=DIAS_SEGUIMIENTO_PRECIO)).strftime('%Y-%m-%d'),
            'PROXIMA_ACCION': f'¿Ha comprado {p.descripcion or p.referencia}? (precio dado {p.precio:.2f} €)'.replace('.', ',', 1)})
        if errores:
            raise HTTPException(status_code=400, detail=' '.join(errores))
        anteriores = precios_dados(s, p.cod_cliente) if p.cod_cliente else []
        registro = actividad.crear(s.clave, s.nombre, d)
        # Aviso por correo a Lucía y Carolina (si falla, el precio queda apuntado igual).
        correo_ok, correo_error = True, None
        try:
            from comercial import correo
            c = pirineos.clientes().get(p.cod_cliente)
            art = pirineos.articulos().get(p.referencia)
            ap = _precio_aplicable(c, art, p.cod_cliente) if (c and art) else None
            if ap:
                ap['texto'] = (f"precio especial hasta {ap['hasta']}" if ap.get('hasta') else 'precio especial') if ap['clase'] == 'especial' else f"tarifa {ap['clase']}"
            antes = [f"{r['FECHA']} · {r['COMERCIAL']} · {r['IMPORTE']} €" + (f" · {r['OBSERVACIONES']}" if r['OBSERVACIONES'] else '')
                     for r in anteriores if r['PRODUCTOS'] == p.referencia][:5]
            correo_ok, correo_error = correo.aviso_precio_dado(s.nombre, p.cliente, p.cod_cliente, p.referencia, p.descripcion, p.precio,
                                                               p.nota, momento.strftime('%d/%m/%Y %H:%M'), ap, antes, bajo)
        except Exception as e:
            correo_ok, correo_error = False, str(e)
        # Aviso en la app de escritorio (campana y «!» en el módulo de ofertas) para preparar la oferta.
        try:
            art = pirineos.articulos().get(p.referencia) or {}
            maquina = linea_articulo(art) == 'Maquinaria'
            notificaciones.crear('PRECIO_DADO', 'ofertas_maquinaria' if maquina else 'ofertas_consumibles',
                                 ('⚠ PRECIO BAJO · ' if bajo else '') + f'Precio dado a {p.cliente}',
                                 f"{s.nombre} ha dado precio a {p.cliente}: {p.descripcion or ''} (ref. {p.referencia}) a "
                                 + f"{p.precio:.2f} €".replace('.', ',')
                                 + (f" — margen {bajo['margen']:g} %, por debajo del mínimo del {bajo['minimo']:g} %".replace('.', ',') if bajo else '')
                                 + (f'. Nota: {p.nota}' if p.nota else '') + '. ¿Preparar oferta?',
                                 {'cliente': p.cliente, 'codigo': p.cod_cliente, 'referencia': p.referencia, 'descripcion': p.descripcion,
                                  'precio': p.precio, 'comercial': s.nombre, 'registro': registro['ID'], 'precio_bajo': bajo}, momento)
        except Exception:
            pass
        return {'registro': registro, 'correo': correo_ok, 'correo_error': correo_error, 'precio_bajo': bool(bajo)}
    except Exception as e:
        _error(e)


DIAS_SEGUIMIENTO_PRECIO = 7
ESTADOS_PRECIO = {'PENDIENTE': 'Pendiente de respuesta', 'ACEPTADO': 'Aceptado (pendiente de pedido)', 'COMPRADO': 'Ha comprado',
                  'NO_INTERESA': 'No le interesa'}


def linea_articulo(art):
    """«Maquinaria» o «Consumibles» según el grupo o la familia del artículo en Pirineos ('' si no se conoce)."""
    if not art:
        return ''
    return 'Maquinaria' if 'MAQUIN' in (art.get('grupo', '') + ' ' + art.get('familia', '')).upper() else 'Consumibles'


def _con_seguimiento_precio(regs):
    """Añade a cada precio dado su estado: el que se haya marcado a mano o «Ha comprado» si en las ventas de
    Pirineos aparece esa referencia facturada a ese cliente (cualquiera de sus códigos) desde ese día.
    También su línea (Maquinaria / Consumibles), por el artículo de Pirineos."""
    try:
        ventas, _ = pirineos.todas_las_ventas()
    except Exception:
        ventas = {}
    try:
        arts = pirineos.articulos()
    except Exception:
        arts = {}
    salida = []
    for r in regs:
        r = dict(r)
        compra = None
        if r['COD_CLIENTE'] and r['PRODUCTOS']:
            lineas = [l for l in ventas.get(r['COD_CLIENTE'], []) if l['referencia'] == r['PRODUCTOS'] and l['fecha'] >= r['FECHA']
                      and l['cantidad'] > 0]
            if lineas:
                l = min(lineas, key=lambda x: x['fecha'])
                compra = {'fecha': l['fecha'], 'precio': l['precio'], 'cantidad': l['cantidad'], 'documento': l['documento'],
                          'veces': len(lineas)}
        manual = r['ESTADO_OPERACION'] if r['ESTADO_OPERACION'] in ESTADOS_PRECIO else ''
        r['compra_pirineos'] = compra
        r['linea'] = linea_articulo(arts.get(r['PRODUCTOS'])) if r['PRODUCTOS'] else ''
        r['estado_precio'] = 'COMPRADO' if compra else (manual or 'PENDIENTE')
        r['dias'] = (actividad.ahora().date() - date.fromisoformat(r['FECHA'])).days if r['FECHA'] else None
        salida.append(r)
    return salida


def _ofertas_cliente(codigos):
    try:
        lista, _ = ofertas.leer_ofertas()
        res = ofertas.con_estado([dict(o) for o in lista if o['codigo'] and o['codigo'] in set(codigos)])
    except Exception:
        return []
    return sorted(res, key=lambda o: o['fecha'], reverse=True)


def precios_dados(s, cliente='', comercial=''):
    regs = actividad.listar(_claves_visibles(s, comercial))
    return [r for r in reversed(regs) if r['TIPO'] == 'PRECIO' and r['ESTADO'] == actividad.REGISTRADA
            and (not cliente or r['COD_CLIENTE'] in set(pirineos.codigos_hermanos(cliente)))]


@router.get('/precios_dados')
def listar_precios_dados(cliente: str = '', comercial: str = '', s: Sesion = Depends(sesion_actual)):
    try:
        return {'precios': precios_dados(s, cliente, comercial)}
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- alertas de cliente
def alertas_por_cliente():
    """{codigo_principal: {'incidencias': n, 'sat': n}}: incidencias abiertas y avisos del SAT abiertos.
    Si algo falla (p. ej. falta un fichero) se devuelve lo que se pueda: no debe romper las listas."""
    res = {}
    try:
        for cod, n in incidencias.abiertas_por_cliente().items():
            res.setdefault(cod, {'incidencias': 0, 'sat': 0})['incidencias'] = n
    except Exception:
        pass
    try:
        for a in sat.avisos_abiertos():
            if a['codigo']:
                res.setdefault(pirineos.principal(a['codigo']), {'incidencias': 0, 'sat': 0})['sat'] += 1
    except Exception:
        pass
    return res


# ---------------------------------------------------------------- clientes
@router.get('/clientes')
def listar_clientes(s: Sesion = Depends(sesion_actual)):
    """Listado ligero de clientes para el buscador del móvil."""
    try:
        mio = normalizar(s.pirineos)
        perfiles = analisis.perfiles_todos()
        cl = pirineos.clientes()
        alertas = alertas_por_cliente()
        cad = control.cadencias()
        lista = []
        for c in pirineos.clientes().values():
            if c['grupo'].upper() == 'PROVEEDORES':
                continue
            lista.append({
                'codigo': c['codigo'], 'nombre': c['nombre'], 'nombre_comercial': c['nombre_comercial'],
                'poblacion': c['poblacion'], 'provincia': c['provincia'], 'comercial': c['comercial'],
                'mio': bool(mio) and any(cl[h]['comercial_norm'] == mio for h in pirineos.codigos_hermanos(c['codigo']) if h in cl),
                'otros_codigos': [h for h in pirineos.codigos_hermanos(c['codigo']) if h != c['codigo']],
                'alertas': alertas.get(pirineos.principal(c['codigo'])),
                'no_visitar': bool((cad.get(pirineos.principal(c['codigo'])) or {}).get('no_visitar')),
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

        mismos = pirineos.codigos_hermanos(codigo)
        especiales = [dict(e, codigo=h) for h in mismos for e in pirineos.precios_especiales(h)]
        for e in especiales:
            a = arts.get(e['referencia'])
            e['descripcion'] = a['descripcion'] if a else ''

        registros = actividad.listar(_claves_visibles(s))
        propios = [r for r in registros if r['COD_CLIENTE'] in mismos]
        visitas = [r for r in propios if r['TIPO'] == 'VISITA' and r['ESTADO'] == actividad.REGISTRADA]
        maquinas = pirineos.maquinas_cliente(codigo)
        avisos_abiertos, partes_recientes = sat.avisos_cliente(codigo)
        ops = [o for o in sat.oportunidades() if o['codigo'] in mismos]
        seguimientos = [r for r in propios if r['ESTADO'] == actividad.REGISTRADA and r['FECHA_SEGUIMIENTO']
                        and r['SEGUIMIENTO_HECHO'] != 'SI']
        ultimo = next((r for r in reversed(propios) if r['ESTADO'] == actividad.REGISTRADA and r['TIPO'] != 'PRECIO'), None)
        # Control de visitas de este cliente (visitas cerradas de cualquier comercial).
        todas = [r for r in actividad.listar(list(USUARIOS)) if r['TIPO'] == 'VISITA' and r['ESTADO'] == actividad.REGISTRADA]
        del_cliente = [r['FECHA'] for r in todas if r['COD_CLIENTE'] in mismos]
        cl = pirineos.clientes()
        comerciales_cli = {cl[h]['comercial_norm'] for h in mismos if h in cl}
        de_su_comercial = [r for r in todas if normalizar(USUARIOS.get(clave_por_nombre(r['COMERCIAL']) or '', {}).get('pirineos', '')) in comerciales_cli]
        estado_visitas = control.estado_cliente(c, del_cliente, control.compras_12_meses(), control.cadencias(), inicio_de(de_su_comercial) or date.today())
        return {
            'cliente': datos,
            'otros_codigos': [{'codigo': h, 'nombre': cl[h]['nombre'], 'nombre_comercial': cl[h]['nombre_comercial'], 'comercial': cl[h]['comercial']}
                              for h in mismos if h != codigo and h in cl],
            'resumen': {
                'ultima_compra': lineas[0]['fecha'] if lineas else '',
                'ultima_visita': max((r['FECHA'] for r in visitas), default=''),
                'ultimo_contacto': max((r['FECHA'] for r in propios if r['ESTADO'] == actividad.REGISTRADA), default=''),
                'facturado_periodo': round(sum(l['importe'] for l in lineas), 2),
            },
            'compras': compras,
            'especiales': especiales,
            'actividad': [r for r in reversed(propios) if r['TIPO'] != 'PRECIO'],
            'precios_dados': _con_seguimiento_precio([r for r in reversed(propios) if r['TIPO'] == 'PRECIO']),
            'ofertas': _ofertas_cliente(mismos),
            'control': estado_visitas,
            'incidencias': _incidencias_seguras(codigo),
            'tipos_incidencia': incidencias.TIPOS,
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
        # Diccionario de sinónimos: lo que el diccionario dice que es «eso» va primero.
        try:
            from comercial import diccionario
            por_sinonimo = diccionario.referencias(q, c['nombre'] if c else '')
        except Exception:
            por_sinonimo = {}
        if por_sinonimo:
            ya = {a['referencia'] for a in encontrados}
            extra = [arts[r] for r in por_sinonimo if r in arts and r not in ya]
            encontrados = [a for a in encontrados if a['referencia'] in por_sinonimo] + extra + \
                          [a for a in encontrados if a['referencia'] not in por_sinonimo]
        ver_costes = s.puede('ver_costes')
        try:
            llegadas, comprometido = pirineos.llegadas_por_referencia(), pirineos.comprometido_por_referencia()
        except Exception:
            llegadas, comprometido = {}, {}
        resultado = []
        for a in encontrados[:max(1, min(limite, 100))]:
            fila = {
                'referencia': a['referencia'], 'ref_proveedor': a['ref_proveedor'], 'descripcion': a['descripcion'],
                'familia': a['familia'], 'subfamilia': a['subfamilia'], 'marca': a['marca'],
                'hay_stock': a['stock'] > 0,
                # Stock y pedidos a proveedor: lo que hay, lo ya comprometido con pedidos de clientes y lo que llega.
                'stock_uds': a['stock'], 'comprometido': comprometido.get(a['referencia'], 0),
                'llegadas': [{k: x[k] for k in ('prevista', 'cantidad', 'retrasada')} for x in llegadas.get(a['referencia'], [])[:3]],
                'sinonimo': por_sinonimo.get(a['referencia'], ''),
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
    cl = pirineos.clientes()
    def del_grupo(pred):
        return lambda c: any(pred(cl[h]) for h in pirineos.codigos_hermanos(c['codigo']) if h in cl)
    if not s.puede('ver_todos'):
        return del_grupo(lambda c: _es_mio(s, c))
    if comercial:
        clave = clave_por_nombre(comercial)
        objetivo = normalizar(USUARIOS[clave]['pirineos']) if clave else ''
        return del_grupo(lambda c: c['comercial_norm'] == objetivo)
    return lambda c: True


def clientes_a_recuperar(s, comercial='', incluir_sin_compras=False):
    perfiles = analisis.perfiles_todos()
    en_cartera = _filtro_cartera(s, comercial)
    lista = []
    for c in pirineos.clientes().values():
        if c['grupo'].upper() == 'PROVEEDORES' or pirineos.principal(c['codigo']) != c['codigo'] or not en_cartera(c):
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
            por_cliente.setdefault(pirineos.principal(r['COD_CLIENTE']), []).append(r)
    ops = {}
    for o in oportunidades_visibles(s, comercial):
        if o['codigo'] and o['estado'] in sat.ABIERTOS:
            ops.setdefault(pirineos.principal(o['codigo']), []).append(o)
    pedidos = {}
    for p in pirineos.pedidos_pendientes():
        if p['codigo']:
            pedidos.setdefault(pirineos.principal(p['codigo']), []).append(p)
    averias = {}
    for a in sat.avisos_abiertos():
        if a['codigo']:
            averias.setdefault(pirineos.principal(a['codigo']), []).append(a)

    resultado = []
    for c in pirineos.clientes().values():
        if c['grupo'].upper() == 'PROVEEDORES' or pirineos.principal(c['codigo']) != c['codigo'] or not en_cartera(c):
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
            puntos += 3; motivos.append('Oportunidad de venta: ' + (ops[c['codigo']][0]['detalle'] or 'ver detalle')[:80])
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
    orden = {'toca': 0, 'demasiado': 1, 'pronto': 2, 'ok': 3, 'no_visitar': 4}
    alertas = alertas_por_cliente()
    for x in resultado:
        x['alertas'] = alertas.get(x['codigo'])
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
        codigo = pirineos.principal(codigo)  # la frecuencia es del cliente, no de cada código
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


# ---------------------------------------------------------------- incidencias de clientes
def _incidencias_seguras(codigo):
    try:
        return incidencias.listar(codigo)
    except Exception as e:
        return [{'id': '', 'error': str(e)}]


class NuevaIncidencia(BaseModel):
    cod_cliente: str = ''
    cliente: str = ''
    tipo: str
    detalle: str


class Resolucion(BaseModel):
    nota: str = ''


@router.get('/incidencias')
def listar_incidencias(cliente: str = '', estado: str = '', s: Sesion = Depends(sesion_actual)):
    try:
        return {'incidencias': incidencias.listar(cliente, estado), 'tipos': incidencias.TIPOS}
    except Exception as e:
        _error(e)


@router.post('/incidencias')
def nueva_incidencia(d: NuevaIncidencia, s: Sesion = Depends(sesion_actual)):
    try:
        return {'incidencia': incidencias.crear(d.cod_cliente, d.cliente, d.tipo, d.detalle, s.nombre, 'Comercial', actividad.ahora())}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _error(e)


@router.post('/incidencias/{id_inc}/resolver')
def resolver_incidencia(id_inc: str, r: Resolucion, s: Sesion = Depends(sesion_actual)):
    try:
        return {'incidencia': incidencias.resolver(id_inc, s.nombre, r.nota, actividad.ahora())}
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- no visitar
class NoVisitar(BaseModel):
    no_visitar: bool
    motivo: str = ''


@router.put('/no_visitar/{codigo}')
def marcar_no_visitar(codigo: str, d: NoVisitar, s: Sesion = Depends(sesion_actual)):
    """El comercial (o Master) marca un cliente como «no visitar» con un motivo, o lo quita."""
    try:
        if not pirineos.clientes().get(codigo):
            raise HTTPException(status_code=404, detail='Cliente no encontrado.')
        if d.no_visitar and not d.motivo.strip():
            raise HTTPException(status_code=400, detail='Indica el motivo para no visitarlo.')
        control.marcar_no_visitar(pirineos.principal(codigo), d.no_visitar, d.motivo, s.nombre,
                                  actividad.ahora().strftime('%Y-%m-%d %H:%M'))
        return {'ok': True}
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- desde la SAT App (Master del SAT)
# La SAT App no tiene sesiones en el servidor (el usuario se elige en el navegador), así que
# esto se limita a lo imprescindible: buscar clientes por nombre y apuntar / ver / resolver
# incidencias. Solo responde si quien llama dice ser «Master» del SAT.
USUARIOS_SAT_GESTION = {'Master'}


def _usuario_sat(usuario):
    if usuario not in USUARIOS_SAT_GESTION:
        raise HTTPException(status_code=403, detail='Solo Master del SAT puede gestionar las incidencias para comerciales.')
    return f'{usuario} (SAT)'


class IncidenciaSAT(NuevaIncidencia):
    usuario: str


class ResolucionSAT(Resolucion):
    usuario: str


@router.get('/sat/clientes')
def sat_buscar_clientes(q: str = '', usuario: str = ''):
    try:
        _usuario_sat(usuario)
        palabras = normalizar(q).split()
        if not palabras:
            return {'clientes': []}
        res = []
        for c in pirineos.clientes().values():
            t = normalizar(f"{c['nombre']} {c['nombre_comercial']} {c['poblacion']} {c['codigo']}")
            if all(p in t for p in palabras) and c['grupo'].upper() != 'PROVEEDORES':
                res.append({'codigo': c['codigo'], 'nombre': c['nombre'], 'nombre_comercial': c['nombre_comercial'],
                            'poblacion': c['poblacion'], 'comercial': c['comercial']})
        return {'clientes': res[:25]}
    except Exception as e:
        _error(e)


class ClientesSAT(BaseModel):
    usuario: str
    clientes: list[str] = []


@router.post('/sat/alertas')
def sat_alertas(d: ClientesSAT):
    """Avisos antes de ir: para cada nombre de cliente de los avisos del SAT, sus incidencias abiertas (pagos,
    SAT no pagado, quejas...) apuntadas por comerciales, oficina o Master. Lo ven técnicos y Master del SAT."""
    try:
        if not d.usuario.strip():
            raise HTTPException(status_code=401, detail='Falta el usuario.')
        abiertas = incidencias.listar('', incidencias.ABIERTA)
        por_codigo = {}
        for i in abiertas:
            if i.get('codigo'):
                por_codigo.setdefault(pirineos.principal(i['codigo']), []).append(i)
        por_nombre = {pirineos.clave_nombre(i['cliente']): i for i in abiertas if not i.get('codigo')}
        res = {}
        for nombre in list(dict.fromkeys(x for x in d.clientes if x))[:400]:
            cod = pirineos.codigo_por_nombre(nombre)
            lista = por_codigo.get(pirineos.principal(cod), []) if cod else []
            if not lista and pirineos.clave_nombre(nombre) in por_nombre:
                lista = [por_nombre[pirineos.clave_nombre(nombre)]]
            if lista:
                res[nombre] = [{'tipo': i['tipo'], 'tipo_texto': incidencias.TIPOS.get(i['tipo'], i['tipo']), 'detalle': i['detalle'],
                                'creado_por': i['creado_por'], 'creado_en': i['creado_en']} for i in lista]
        return {'alertas': res}
    except Exception as e:
        _error(e)


@router.get('/sat/incidencias')
def sat_incidencias(usuario: str = '', estado: str = ''):
    try:
        _usuario_sat(usuario)
        return {'incidencias': incidencias.listar('', estado), 'tipos': incidencias.TIPOS}
    except Exception as e:
        _error(e)


@router.post('/sat/incidencias')
def sat_nueva_incidencia(d: IncidenciaSAT):
    try:
        quien = _usuario_sat(d.usuario)
        return {'incidencia': incidencias.crear(d.cod_cliente, d.cliente, d.tipo, d.detalle, quien, 'SAT', actividad.ahora())}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _error(e)


@router.post('/sat/incidencias/{id_inc}/resolver')
def sat_resolver_incidencia(id_inc: str, r: ResolucionSAT):
    try:
        quien = _usuario_sat(r.usuario)
        return {'incidencia': incidencias.resolver(id_inc, quien, r.nota, actividad.ahora())}
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- visitas y reuniones programadas
TIPOS_PROGRAMADA = {'VISITA': ('VISITA', 'Visita'), 'REUNION': ('VISITA', 'Reunión'), 'LLAMADA': ('LLAMADA', 'Llamada programada')}


class Programar(BaseModel):
    cod_cliente: str = ''
    cliente: str
    poblacion: str = ''
    fecha: str
    hora: str = ''
    tipo: str = 'VISITA'
    objetivo: str = ''


def _datos_programada(p):
    try:
        datetime.strptime(p.fecha, '%Y-%m-%d')
    except ValueError:
        raise HTTPException(status_code=400, detail='Fecha no válida.')
    if not p.cliente.strip():
        raise HTTPException(status_code=400, detail='Elige el cliente.')
    hora = p.hora.strip()[:5]
    if hora and actividad._minutos(hora) is None:
        raise HTTPException(status_code=400, detail='Hora no válida.')
    tipo, subtipo = TIPOS_PROGRAMADA.get(p.tipo.upper(), TIPOS_PROGRAMADA['VISITA'])
    return {'TIPO': tipo, 'SUBTIPO': subtipo, 'COD_CLIENTE': p.cod_cliente, 'CLIENTE': p.cliente.strip(), 'POBLACION': p.poblacion,
            'FECHA': p.fecha, 'HORA_INICIO': hora, 'MOTIVO': ' '.join(p.objetivo.split())[:500]}


@router.post('/programar')
def programar(p: Programar, s: Sesion = Depends(sesion_actual)):
    """Programa una visita, reunión o llamada para un día (para preparar la semana). No cuenta como
    hecha hasta que se confirma y se rellena (horas reales, qué se habló...)."""
    try:
        if not s.puede('registrar'):
            raise HTTPException(status_code=403, detail='Sin permiso para registrar actividad.')
        return {'registro': actividad.crear(s.clave, s.nombre, _datos_programada(p), estado=actividad.PROGRAMADA, registro='PROGRAMADA')}
    except Exception as e:
        _error(e)


class Mover(BaseModel):
    fecha: str
    hora: str = ''
    objetivo: str | None = None


@router.put('/programadas/{id_registro}')
def mover_programada(id_registro: str, m: Mover, s: Sesion = Depends(sesion_actual)):
    """Cambia el día (y la hora) de una visita programada."""
    try:
        clave, r = _dueno(s, id_registro)
        if clave != s.clave and not s.puede('editar_otros'):
            raise HTTPException(status_code=403, detail='No puedes cambiar la agenda de otra persona.')
        if r['ESTADO'] != actividad.PROGRAMADA:
            raise HTTPException(status_code=409, detail='Esta visita ya no está programada (se ha hecho o anulado).')
        datetime.strptime(m.fecha, '%Y-%m-%d')
        cambios = {'FECHA': m.fecha, 'HORA_INICIO': m.hora.strip()[:5] if actividad._minutos(m.hora) is not None else ''}
        if m.objetivo is not None:
            cambios['MOTIVO'] = ' '.join(m.objetivo.split())[:500]
        return {'registro': actividad.actualizar(clave, s.nombre, id_registro, cambios, accion='CAMBIO DE FECHA')}
    except ValueError:
        raise HTTPException(status_code=400, detail='Fecha no válida.')
    except Exception as e:
        _error(e)


@router.post('/programadas/{id_registro}/cancelar')
def cancelar_programada(id_registro: str, m: Motivo, s: Sesion = Depends(sesion_actual)):
    try:
        clave, r = _dueno(s, id_registro)
        if clave != s.clave and not s.puede('editar_otros'):
            raise HTTPException(status_code=403, detail='No puedes cambiar la agenda de otra persona.')
        if r['ESTADO'] != actividad.PROGRAMADA:
            raise HTTPException(status_code=409, detail='Esta visita ya no está programada.')
        nota = f"[Cancelada: {m.motivo.strip()}]" if m.motivo.strip() else '[Cancelada]'
        return {'registro': actividad.actualizar(clave, s.nombre, id_registro, {
            'ESTADO': actividad.ANULADA, 'OBSERVACIONES': (r['OBSERVACIONES'] + ' ' if r['OBSERVACIONES'] else '') + nota}, accion='CANCELACIÓN')}
    except Exception as e:
        _error(e)


# ---------------------------------------------------------------- seguimiento de precios dados y ofertas
class EstadoPrecio(BaseModel):
    estado: str
    nota: str = ''
    fecha_seguimiento: str = ''


@router.post('/precios_dados/{id_registro}/estado')
def estado_precio(id_registro: str, e: EstadoPrecio, s: Sesion = Depends(sesion_actual)):
    """Seguimiento de un precio dado: aceptado, ha comprado, no le interesa o volver a preguntar otro día."""
    try:
        clave, r = _dueno(s, id_registro)
        if clave != s.clave and not s.puede('editar_otros'):
            raise HTTPException(status_code=403, detail='No puedes modificar la actividad de otra persona.')
        if r['TIPO'] != 'PRECIO':
            raise HTTPException(status_code=400, detail='No es un precio dado.')
        if e.estado not in ESTADOS_PRECIO:
            raise HTTPException(status_code=400, detail='Estado no válido.')
        cambios = {'ESTADO_OPERACION': '' if e.estado == 'PENDIENTE' else e.estado}
        if e.estado in ('COMPRADO', 'NO_INTERESA'):
            cambios['SEGUIMIENTO_HECHO'] = 'SI'
        elif e.fecha_seguimiento:
            datetime.strptime(e.fecha_seguimiento, '%Y-%m-%d')
            cambios.update(FECHA_SEGUIMIENTO=e.fecha_seguimiento, SEGUIMIENTO_HECHO='')
        if e.nota.strip():
            cambios['OBSERVACIONES'] = (r['OBSERVACIONES'] + ' · ' if r['OBSERVACIONES'] else '') + \
                f"[{actividad.ahora().strftime('%d/%m')} {ESTADOS_PRECIO[e.estado]}: {e.nota.strip()}]"
        return {'registro': actividad.actualizar(clave, s.nombre, id_registro, cambios, accion='SEGUIMIENTO PRECIO')}
    except ValueError:
        raise HTTPException(status_code=400, detail='Fecha no válida.')
    except Exception as e2:
        _error(e2)


# Ofertas pedidas en una visita o llamada («¿Se ha conseguido una oferta?» = Sí). Estado en ESTADO_OPERACION:
#   (vacío) pendiente de la oficina -> ENVIADA (la oficina ya la ha hecho: se detecta sola en los Excel de ofertas,
#   o se marca a mano) -> GANADA / PERDIDA; o CANCELADA si ya no hace falta.
ESTADOS_PEDIDA = {'NUEVA': 'Nueva', 'ENVIADA': 'Oferta enviada', 'GANADA': 'Ganada', 'PERDIDA': 'Perdida',
                  'DESCARTADA': 'Descartada'}
PEDIDA_ANTIGUOS = {'PENDIENTE': 'NUEVA', 'CANCELADA': 'DESCARTADA'}


def _ofertas_pedidas(regs, lista_ofertas):
    """Une cada oferta pedida con la oferta que la oficina haya hecho después a ese cliente (cualquiera de sus códigos)."""
    por_codigo = {}
    for o in lista_ofertas:
        if o.get('codigo'):
            por_codigo.setdefault(pirineos.principal(o['codigo']), []).append(o)
    usadas, salida = set(), []
    for r in sorted(regs, key=lambda r: r['FECHA']):
        r = dict(r)
        candidatas = [o for o in por_codigo.get(pirineos.principal(r['COD_CLIENTE']), []) if r['COD_CLIENTE']
                      and o['fecha'] >= r['FECHA'] and o['id'] not in usadas]
        o = min(candidatas, key=lambda o: o['fecha']) if candidatas else None
        if o:
            usadas.add(o['id'])
            o.setdefault('pedida', {'id': r['ID'], 'fecha': r['FECHA'], 'comercial': r['COMERCIAL'], 'tipo': r['TIPO'],
                                    'que': ' — '.join(x for x in (r['PRODUCTOS'], r['NECESIDADES']) if x)})
        manual = PEDIDA_ANTIGUOS.get(r['ESTADO_OPERACION'], r['ESTADO_OPERACION'])
        manual = manual if manual in ESTADOS_PEDIDA else ''
        r['oferta_oficina'] = o['id'] if o else ''
        r['linea'] = o['tipo'] if o else ''      # se sabe por la oferta que haya hecho la oficina
        # Lo marcado a mano manda siempre; si no hay nada, «Oferta enviada» cuando la oficina ya la ha hecho y «Nueva» si no.
        r['estado_pedida'] = manual or ('ENVIADA' if o else 'NUEVA')
        r['dias'] = (date.today() - date.fromisoformat(r['FECHA'])).days
        salida.append(r)
    return sorted(salida, key=lambda r: r['FECHA'], reverse=True)


def seguimiento_comercial(s, comercial='', cliente=''):
    """Precios dados, ofertas pedidas en visitas, ofertas de la oficina y oportunidades de venta, con su estado.
    Con «cliente» (un código de Pirineos), solo lo de ese cliente (todos sus códigos) y todas sus ofertas."""
    todas = actividad.listar(_claves_visibles(s, comercial))
    mismos = set(pirineos.codigos_hermanos(cliente)) if cliente else None
    if mismos is not None:
        todas = [r for r in todas if r['COD_CLIENTE'] in mismos]
    regs = [r for r in todas if r['TIPO'] == 'PRECIO' and r['ESTADO'] == actividad.REGISTRADA]
    precios = sorted(_con_seguimiento_precio(regs), key=lambda r: r['FECHA'], reverse=True)
    pedidas_regs = [r for r in todas if r['TIPO'] in ('VISITA', 'LLAMADA') and r.get('OFERTA') == OFERTA_SI
                    and r['ESTADO'] in (actividad.REGISTRADA, actividad.SIN_TERMINAR)]
    try:
        lista, avisos = ofertas.leer_ofertas()
    except Exception as e:
        lista, avisos = [], [f'No se han podido leer las ofertas: {e}']
    if mismos is not None:
        lista = [o for o in lista if o['codigo'] and o['codigo'] in mismos]
    lista = ofertas.con_estado([dict(o) for o in lista])
    cl = pirineos.clientes()
    mio = normalizar(s.pirineos)
    for o in lista:
        c = cl.get(o['codigo']) if o['codigo'] else None
        o['comercial'] = c['comercial'] if c else ''
        o['mio'] = bool(c and mio and any(cl[h]['comercial_norm'] == mio for h in pirineos.codigos_hermanos(c['codigo']) if h in cl))
    lista.sort(key=lambda o: o['fecha'], reverse=True)    # más recientes primero
    lista.sort(key=lambda o: o['estado'] not in ofertas.ABIERTOS)     # y las abiertas arriba
    pedidas = _ofertas_pedidas(pedidas_regs, lista)
    try:
        oportunidades = oportunidades_visibles(s, comercial)
        if mismos is not None:
            oportunidades = [o for o in oportunidades if o['codigo'] in mismos]
    except Exception:
        oportunidades = []
    return {'precios': precios, 'estados_precio': ESTADOS_PRECIO, 'ofertas': lista, 'avisos_ofertas': avisos,
            'validez_dias': ofertas.VALIDEZ_DIAS, 'pedidas': pedidas, 'estados_pedida': ESTADOS_PEDIDA,
            'oportunidades': oportunidades, 'estados_oportunidad': sat.ESTADOS}


@router.get('/seguimiento')
def ver_seguimiento(comercial: str = '', cliente: str = '', s: Sesion = Depends(sesion_actual)):
    try:
        return seguimiento_comercial(s, comercial, cliente)
    except Exception as e:
        _error(e)


class EstadoPedida(BaseModel):
    estado: str
    nota: str = ''


@router.post('/actividad/{id_registro}/oferta_pedida')
def estado_oferta_pedida(id_registro: str, e: EstadoPedida, s: Sesion = Depends(sesion_actual)):
    """Estado de una oferta pedida en una visita (NUEVA, ENVIADA, GANADA, PERDIDA o DESCARTADA), siempre a mano."""
    try:
        clave, r = _dueno(s, id_registro)
        if clave != s.clave and not s.puede('editar_otros'):
            raise HTTPException(status_code=403, detail='No puedes modificar la actividad de otra persona.')
        if r.get('OFERTA') != OFERTA_SI:
            raise HTTPException(status_code=400, detail='Esta visita no tiene oferta pedida.')
        estado = PEDIDA_ANTIGUOS.get(e.estado, e.estado)
        if estado not in ESTADOS_PEDIDA:
            raise HTTPException(status_code=400, detail='Estado no válido.')
        cambios = {'ESTADO_OPERACION': estado}
        if e.nota.strip():
            cambios['OBSERVACIONES'] = (r['OBSERVACIONES'] + ' ' if r['OBSERVACIONES'] else '') + f'[Oferta: {ESTADOS_PEDIDA[estado]}. {e.nota.strip()}]'
        return {'registro': actividad.actualizar(clave, s.nombre, id_registro, cambios, accion='SEGUIMIENTO OFERTA')}
    except Exception as ex:
        _error(ex)


class SeguimientoOferta(BaseModel):
    estado: str
    nota: str = ''
    proximo: str = ''


@router.post('/ofertas/{id_oferta}/seguimiento')
def seguir_oferta(id_oferta: str, d: SeguimientoOferta, s: Sesion = Depends(sesion_actual)):
    try:
        return {'seguimiento': ofertas.fijar_seguimiento(id_oferta, d.estado, d.nota.strip(), d.proximo, s.nombre, actividad.ahora())}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _error(e)
