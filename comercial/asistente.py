# comercial/asistente.py
"""Asistente comercial (IA) del módulo Comercial.

Usa el mismo modelo y la misma API key que el asistente del SAT (OPENAI_API_KEY de
contraseñas.txt). En lugar de mandarle los Excel, se le dan herramientas que consultan
los datos reales con los permisos de quien pregunta (un comercial nunca recibe costes,
márgenes ni la actividad de otros comerciales):

  buscar_clientes, ficha_cliente (preparación de visita completa), compras_cliente,
  buscar_articulos (precios), precios_dados, actividad, control_visitas, resumen_visitas,
  clientes_a_recuperar, oportunidades_sat, pedidos_pendientes y clientes_cerca.

Además recibe en cada pregunta lo que el usuario tiene abierto en la app (pantalla, cliente,
visita que está rellenando, búsqueda de precios, filtros...), para entender «este cliente»,
«esta visita» o «este artículo» sin que haya que explicárselo.
"""
import json
import logging
import time
from datetime import date

from fastapi import HTTPException

from comercial import actividad, analisis, geo, pirineos, sat
from comercial.usuarios import normalizar

logger = logging.getLogger('asistente_comercial')

MODELO_POR_DEFECTO = 'gpt-6-luna'
MAX_TOKENS = 3000
MAX_VUELTAS = 6
MAX_HISTORIAL = 8
MAX_SALIDA = 24000

PROMPT = """Eres el asistente comercial de Sugraf, distribuidor de maquinaria de impresión digital, consumibles de artes gráficas (tintas offset, planchas, vinilos, lonas, DTF, gran formato...), láser y servicio técnico, en Aragón, Navarra, La Rioja y alrededores. Te usan los comerciales desde el móvil (a menudo justo antes o durante una visita) y Master (dirección).

La app tiene: «Hoy» (registrar visitas con hora de entrada y salida escritas a mano; se pueden ir guardando con «Visita sin terminar» y se cierran con «Visita finalizada» cuando están completas: cliente, horas, qué se habló, resultado y «¿Se ha conseguido una oferta?» Sí/No; con Sí hay que indicar qué artículo (buscador) o una breve descripción, y la oficina recibe el aviso en Generación de ofertas; también llamadas, que se guardan de una vez con lo mismo), «Clientes» (control de visitas: clientes a los que toca ir y clientes visitados de más según su frecuencia; clientes a recuperar; ficha con «Preparar visita»), «Precios» (precio al que se le cobra cada referencia a un cliente, stock, unidades comprometidas con pedidos de clientes y cuándo llega lo pedido al proveedor; busca también por el diccionario de sinónimos; apuntar el precio dado: si queda por debajo del margen mínimo —15 % salvo que se fije otro— pide confirmación y avisa a la oficina sin enseñar el coste), «Pedidos» (enviar un pedido de consumibles o de maquinaria para un cliente: cliente, artículos con cantidad, nota y, si se quiere, una foto del artículo que va adjunta al correo; si pasadas 24 h no aparece en Pirineos ninguna venta ni pedido de cliente que lo demuestre tramitado, se manda un recordatorio a la oficina; comentarios y persona de contacto; al enviarlo llega un correo a la oficina, carolina@sugraf.es; pestaña «Enviados» con los ya enviados y «Repetir pedido»; también desde la ficha con «Hacer pedido»), «Agenda» (visitas por día/semana/mes y resumen de visitas diarias y tiempos), «Ofertas» (seguimiento de ofertas y precios dados por estados: Pendiente de elaborar —pedida en visita y aún sin hacer en Generación de ofertas; pasa sola a Enviada cuando la oficina la hace—, Enviada —se puede filtrar por enviadas esta semana, este mes o caducadas—, Compra, Perdida y Anulada —pedida por error—; se pueden cambiar a mano en cualquier momento, aunque se hayan puesto solos; la compra va por producto: si el cliente compra solo algunos productos de una oferta según Pirineos —o se marcan a mano—, solo esos pasan a Compra y el resto sigue en su estado; una sola lista de lo pendiente de cerrar con cada cliente: precios dados, ofertas pedidas en visitas —pendientes de la oficina hasta que aparecen en Generación de ofertas— y ofertas de la oficina, y oportunidades de venta del SAT) y «Ruta» (clientes cercanos para planificar el día).
Cada pregunta trae PANTALLA: lo que el usuario está viendo ahora mismo. Úsalo: «este cliente», «esta visita», «este artículo» se refieren a lo que hay en pantalla. Si está rellenando una visita, puedes ayudarle a redactar «qué se ha hablado», recordarle lo que falta para cerrarla o decirle qué preguntar.

Cómo trabajar:
- Usa SIEMPRE las herramientas para cualquier dato de clientes, compras, precios, actividad, máquinas, pedidos, avisos del SAT u oportunidades. No inventes nada: si un dato no está, dilo.
- Si te nombran un cliente, búscalo con buscar_clientes; si hay varios parecidos, pregunta cuál. Si el usuario está viendo la ficha de un cliente (CONTEXTO), entiende que habla de ese.
- Para preparar una visita usa ficha_cliente: resume en pocas líneas lo importante (último contacto y lo que quedó pendiente, avisos del SAT abiertos, pedidos pendientes, productos que está dejando de comprar, sugerencias con su motivo, máquinas a renovar) y termina con 2-3 temas concretos para sacar en la visita.
- «Oportunidades de venta»: posibles ventas que los técnicos han detectado en los partes del SAT (llámalas así, no «oportunidades del SAT»).
- Precios: distingue precio de tarifa, precio especial vigente y precio histórico facturado. Nunca nombres la tarifa (1, 2, 3) delante del cliente salvo que el usuario lo pida expresamente. Costes y márgenes solo si las herramientas los devuelven (solo Master); son estimaciones con el último coste de compra, no margen neto.
- «Dejando de comprar», «dormido» y las sugerencias son estimaciones a partir del histórico de ventas cargado (año actual y dos anteriores): preséntalas como indicios, con su motivo, no como hechos.
- Visitas programadas (estado PROGRAMADA en actividad) son planes, no visitas hechas. Precios dados: hay que seguirlos hasta saber si compra.
- Si la ficha trae incidencias abiertas (problemas de pago, SAT no resuelto o no pagado, quejas), avisos del SAT abiertos o el cliente está marcado «No visitar», dilo SIEMPRE al principio al hablar de ese cliente o preparar la visita.
- Un mismo cliente puede tener varios códigos en Pirineos (departamentos, código antiguo y nuevo...): la app ya los trata como uno solo (suma compras, visitas y precios dados). Si una herramienta devuelve otros_codigos, menciónalo cuando ayude.
- Frecuencia de visita: la fija Master o se propone según lo que compra el cliente; «toca visitar» y «visitado de más» salen de control_visitas.
- Responde en español, breve y directo, pensado para leer en el móvil: frases cortas, listas cuando ayuden, cifras con € y fechas dd/mm/aaaa. Nada de rodeos.
"""


def _herramientas(s):
    h = [
        {'type': 'function', 'name': 'buscar_clientes', 'strict': False,
         'description': 'Busca clientes por nombre, nombre comercial, población o código. Devuelve hasta 10 con su código.',
         'parameters': {'type': 'object', 'required': ['texto'], 'properties': {
             'texto': {'type': 'string'}, 'solo_mios': {'type': 'boolean', 'description': 'Solo la cartera del usuario'}}}},
        {'type': 'function', 'name': 'ficha_cliente', 'strict': False,
         'description': 'Todo lo de un cliente para preparar una visita: datos, perfil de compra, último contacto, seguimientos, '
                        'avisos y partes del SAT, pedidos pendientes, máquinas, productos que deja de comprar, familias en caída, '
                        'sugerencias de venta cruzada, oportunidades de venta (detectadas por los técnicos) y precios especiales.',
         'parameters': {'type': 'object', 'required': ['codigo'], 'properties': {'codigo': {'type': 'string'}}}},
        {'type': 'function', 'name': 'compras_cliente', 'strict': False,
         'description': 'Líneas de venta de un cliente (año actual y dos anteriores), filtrables por texto del producto y fechas.',
         'parameters': {'type': 'object', 'required': ['codigo'], 'properties': {
             'codigo': {'type': 'string'}, 'texto': {'type': 'string'}, 'desde': {'type': 'string', 'description': 'AAAA-MM-DD'},
             'hasta': {'type': 'string', 'description': 'AAAA-MM-DD'}}}},
        {'type': 'function', 'name': 'buscar_articulos', 'strict': False,
         'description': 'Busca artículos por referencia o descripción con sus precios; si se da el cliente, el precio aplicable a ese cliente.',
         'parameters': {'type': 'object', 'required': ['texto'], 'properties': {
             'texto': {'type': 'string'}, 'cliente': {'type': 'string', 'description': 'Código de cliente (opcional)'}}}},
        {'type': 'function', 'name': 'actividad', 'strict': False,
         'description': 'Visitas, llamadas y precios dados registrados en la app entre dos fechas (por defecto los últimos 30 días).',
         'parameters': {'type': 'object', 'properties': {
             'desde': {'type': 'string'}, 'hasta': {'type': 'string'}, 'cliente': {'type': 'string', 'description': 'Código de cliente'},
             'comercial': {'type': 'string', 'description': 'Solo Master'}, 'tipo': {'type': 'string', 'enum': ['VISITA', 'LLAMADA', 'PRECIO']}}}},
        {'type': 'function', 'name': 'seguimiento_ofertas', 'strict': False,
         'description': 'Precios dados (con su estado: pendiente, aceptado, ha comprado -lo detecta en Pirineos-, no le interesa) y '
                        'ofertas de maquinaria y consumibles: pedidas en visitas (pendiente de elaborar, enviada, compra, perdida, anulada) y '
                        'preparadas en la oficina (enviada, perdida, anulada; cada producto con «comprada» si el cliente lo ha comprado: '
                        'si solo compra algunos, la compra es parcial).',
         'parameters': {'type': 'object', 'properties': {'solo_pendientes': {'type': 'boolean'},
                                                         'comercial': {'type': 'string', 'description': 'Solo Master'}}}},
        {'type': 'function', 'name': 'control_visitas', 'strict': False,
         'description': 'Clientes de la cartera con su frecuencia de visita, última visita y estado: toca visitar, pronto, visitado de más o al día.',
         'parameters': {'type': 'object', 'properties': {'comercial': {'type': 'string', 'description': 'Solo Master'},
                                                         'estado': {'type': 'string', 'enum': ['toca', 'pronto', 'demasiado', 'ok']}}}},
        {'type': 'function', 'name': 'resumen_visitas', 'strict': False,
         'description': 'Visitas diarias y tiempos por comercial entre dos fechas: nº de visitas, tiempo total y medio, primera entrada y última salida.',
         'parameters': {'type': 'object', 'required': ['desde', 'hasta'], 'properties': {
             'desde': {'type': 'string'}, 'hasta': {'type': 'string'}, 'comercial': {'type': 'string', 'description': 'Solo Master'}}}},
        {'type': 'function', 'name': 'precios_dados', 'strict': False,
         'description': 'Precios que los comerciales han dado a clientes (apuntados en la app), opcionalmente de un cliente.',
         'parameters': {'type': 'object', 'properties': {'cliente': {'type': 'string', 'description': 'Código de cliente'}}}},
        {'type': 'function', 'name': 'clientes_a_recuperar', 'strict': False,
         'description': 'Clientes dormidos o en riesgo (dejan de comprar respecto a su ritmo habitual) de la cartera.',
         'parameters': {'type': 'object', 'properties': {'comercial': {'type': 'string', 'description': 'Solo Master'}}}},
        {'type': 'function', 'name': 'oportunidades_sat', 'strict': False,
         'description': 'Oportunidades de venta que los técnicos han marcado en los partes del SAT, con su estado.',
         'parameters': {'type': 'object', 'properties': {'solo_abiertas': {'type': 'boolean'}}}},
        {'type': 'function', 'name': 'pedidos_pendientes', 'strict': False,
         'description': 'Pedidos de cliente pendientes de servir (todos los de la cartera o de un cliente).',
         'parameters': {'type': 'object', 'properties': {'cliente': {'type': 'string', 'description': 'Código de cliente'}}}},
        {'type': 'function', 'name': 'clientes_cerca', 'strict': False,
         'description': 'Clientes cerca de otro cliente o de una población, ordenados por interés para visitar (dormidos, seguimientos, oportunidades).',
         'parameters': {'type': 'object', 'properties': {
             'cliente': {'type': 'string', 'description': 'Código del cliente de referencia'},
             'poblacion': {'type': 'string'}, 'radio_km': {'type': 'number'}}}},
    ]
    return h


def _cliente_valido(codigo):
    c = pirineos.clientes().get(str(codigo or '').strip())
    if not c:
        raise ValueError(f'No existe el cliente con código {codigo}. Búscalo antes con buscar_clientes.')
    return c


def _ejecutar(s, nombre, args):
    from comercial import api_comercial as api
    if nombre == 'buscar_clientes':
        palabras = normalizar(args.get('texto', '')).split()
        mio = normalizar(s.pirineos)
        perfiles = analisis.perfiles_todos()
        res = []
        for c in pirineos.clientes().values():
            t = normalizar(f"{c['nombre']} {c['nombre_comercial']} {c['poblacion']} {c['codigo']}")
            if palabras and all(p in t for p in palabras) and (not args.get('solo_mios') or c['comercial_norm'] == mio):
                p = perfiles.get(c['codigo'], {})
                res.append({'codigo': c['codigo'], 'nombre': c['nombre'], 'nombre_comercial': c['nombre_comercial'],
                            'poblacion': c['poblacion'], 'comercial': c['comercial'], 'estado_compra': p.get('estado', 'sin_compras'),
                            'ultima_compra': p.get('ultima', ''),
                            'otros_codigos': [h for h in pirineos.codigos_hermanos(c['codigo']) if h != c['codigo']]})
        return {'total': len(res), 'clientes': res[:10]}
    if nombre == 'ficha_cliente':
        c = _cliente_valido(args.get('codigo'))
        f = api.ficha_cliente(c['codigo'], s=s)
        # Versión compacta (el detalle de compras está en compras_cliente).
        f['compras'] = [{k: p[k] for k in ('referencia', 'descripcion', 'familia', 'ultima_fecha', 'ultimo_precio', 'veces', 'cantidad_total')}
                        | {'precio_hoy': (p['precio_actual'] or {}).get('precio')} for p in f['compras'][:15]]
        f['actividad'] = [{k: r[k] for k in ('TIPO', 'FECHA', 'COMERCIAL', 'TEMAS', 'DESCRIPCION', 'RESULTADO', 'PROXIMA_ACCION', 'FECHA_SEGUIMIENTO') if r.get(k)}
                          for r in f['actividad'][:8]]
        f['especiales'] = [{k: e[k] for k in ('referencia', 'descripcion', 'precio', 'hasta')} for e in f['especiales'] if e['vigente']][:12]
        f['maquinas'] = [{k: m[k] for k in ('equipo', 'marca', 'modelo', 'maquina', 'instalacion', 'garantia') if m.get(k)}
                         for m in f['maquinas'] if not m['baja']][:15]
        P = f['preparar']
        P['partes_recientes'] = [{k: a[k] for k in ('fecha_realizacion', 'maquina', 'descripcion', 'solucion')} for a in P['partes_recientes'][:5]]
        P['avisos_sat'] = [{k: a[k] for k in ('origen', 'fecha_entrada', 'maquina', 'descripcion', 'asignado', 'urgente', 'parado')} for a in P['avisos_sat'][:8]]
        P['dejando_de_comprar'] = [{k: d[k] for k in ('referencia', 'descripcion', 'cada_dias', 'ultima', 'dias_sin_comprar', 'cantidad_habitual', 'ultimo_precio')}
                                   | {'posible_sustituto': (d['posible_sustituto'] or {}).get('descripcion', '')} for d in P['dejando_de_comprar'][:10]]
        P['pedidos'] = [{k: x[k] for k in ('pedido', 'fecha', 'descripcion', 'cantidad', 'importe', 'entrega')} for x in P['pedidos'][:15]]
        P['seguimientos'] = [{k: r[k] for k in ('FECHA', 'PROXIMA_ACCION', 'FECHA_SEGUIMIENTO')} for r in P['seguimientos']]
        if P['ultimo_contacto']:
            P['ultimo_contacto'] = {k: v for k, v in P['ultimo_contacto'].items() if v and not k.startswith(('CREADO', 'MODIF'))}
        return f
    if nombre == 'compras_cliente':
        c = _cliente_valido(args.get('codigo'))
        lineas, leidos, faltan = pirineos.ventas_cliente(c['codigo'])
        palabras = normalizar(args.get('texto', '')).split()
        d, h = args.get('desde', ''), args.get('hasta', '')
        sel = [l for l in lineas if (not palabras or all(p in normalizar(l['descripcion'] + ' ' + l['familia'] + ' ' + l['referencia']) for p in palabras))
               and (not d or l['fecha'] >= d) and (not h or l['fecha'] <= h)]
        campos = ('fecha', 'referencia', 'descripcion', 'cantidad', 'precio', 'dto', 'importe')
        por_producto = {}
        for l in sel:
            p = por_producto.setdefault(l['referencia'], {'referencia': l['referencia'], 'descripcion': l['descripcion'], 'veces': 0,
                                                          'cantidad': 0.0, 'importe': 0.0, 'ultima': l['fecha'], 'ultimo_precio': l['precio']})
            p['veces'] += 1; p['cantidad'] += l['cantidad']; p['importe'] = round(p['importe'] + l['importe'], 2)
        return {'cliente': c['nombre'], 'ficheros': leidos, 'faltan': faltan, 'lineas_encontradas': len(sel),
                'importe_total': round(sum(l['importe'] for l in sel), 2),
                'por_producto': sorted(por_producto.values(), key=lambda p: -p['importe'])[:40],
                'ultimas_lineas': [{k: l[k] for k in campos} for l in sel[:40]]}
    if nombre == 'buscar_articulos':
        return api.buscar_articulos(args.get('texto', ''), str(args.get('cliente', '') or ''), 15, s=s)
    if nombre == 'actividad':
        hoy = actividad.ahora().date()
        d = args.get('desde') or (date.fromordinal(hoy.toordinal() - 30)).isoformat()
        r = api.listar_actividad(d, args.get('hasta', ''), args.get('comercial', '') if s.puede('ver_todos') else '',
                                 str(args.get('cliente', '') or ''), s=s)['registros']
        if args.get('tipo'):
            r = [x for x in r if x['TIPO'] == args['tipo']]
        campos = ['ID', 'TIPO', 'SUBTIPO', 'COMERCIAL', 'CLIENTE', 'FECHA', 'HORA_INICIO', 'HORA_FIN', 'DURACION_MIN', 'TEMAS',
                  'DESCRIPCION', 'RESULTADO', 'PROXIMA_ACCION', 'FECHA_SEGUIMIENTO', 'SEGUIMIENTO_HECHO', 'IMPORTE', 'MARCA',
                  'MODELO', 'ESTADO_OPERACION', 'ESTADO', 'REGISTRO']
        return {'total': len(r), 'registros': [{k: x[k] for k in campos if x.get(k)} for x in r[-100:]]}
    if nombre == 'clientes_a_recuperar':
        lista = api.clientes_a_recuperar(s, args.get('comercial', '') if s.puede('ver_todos') else '')
        return {'total': len(lista), 'clientes': lista[:40]}
    if nombre == 'oportunidades_sat':
        ops = api.oportunidades_visibles(s)
        if args.get('solo_abiertas'):
            ops = [o for o in ops if o['estado'] in sat.ABIERTOS]
        return {'total': len(ops), 'oportunidades': ops[:40]}
    if nombre == 'pedidos_pendientes':
        if args.get('cliente'):
            c = _cliente_valido(args['cliente'])
            return {'cliente': c['nombre'], 'pedidos': pirineos.pedidos_cliente(c['codigo'])}
        en_cartera = api._filtro_cartera(s)
        cl = pirineos.clientes()
        ped = [p for p in pirineos.pedidos_pendientes() if p['codigo'] and cl.get(p['codigo']) and en_cartera(cl[p['codigo']])]
        campos = ('cliente_nombre', 'pedido', 'fecha', 'descripcion', 'cantidad', 'importe', 'entrega', 'urgente')
        return {'total': len(ped), 'pedidos': [{k: p[k] for k in campos} for p in ped[:70]]}
    if nombre == 'clientes_cerca':
        origen = None
        if args.get('cliente'):
            origen = geo.coordenadas_cliente(_cliente_valido(args['cliente']))
        elif args.get('poblacion'):
            n = normalizar(args['poblacion'])
            c = next((c for c in pirineos.clientes().values() if normalizar(c['poblacion']) == n and geo.coordenadas_cliente(c)), None)
            origen = geo.coordenadas_cliente(c) if c else None
        if not origen:
            return {'error': 'No tengo la ubicación de ese punto (puede que falte preparar el mapa en Rutas).'}
        lista = api.candidatos_ruta(s, origen, float(args.get('radio_km') or 25))
        return {'total': len(lista), 'clientes': [{k: x[k] for k in ('codigo', 'nombre', 'poblacion', 'km', 'motivos', 'compras_12m')}
                                                 for x in lista[:25]]}
    if nombre == 'seguimiento_ofertas':
        d = api.seguimiento_comercial(s, args.get('comercial', '') if s.puede('ver_todos') else '')
        precios = [{k: p[k] for k in ('FECHA', 'COMERCIAL', 'CLIENTE', 'PRODUCTOS', 'DESCRIPCION', 'IMPORTE', 'estado_precio', 'compra_pirineos', 'dias')}
                   for p in d['precios'] if not args.get('solo_pendientes') or p['estado_precio'] in ('PENDIENTE', 'ACEPTADO')][:40]
        ofs = [{k: o[k] for k in ('id', 'tipo', 'numero', 'fecha', 'dias', 'cliente', 'codigo', 'lineas', 'estado', 'n_compradas', 'seguimiento', 'comercial')}
               for o in d['ofertas'] if not args.get('solo_pendientes') or o['estado'] == 'ENVIADA'][:40]
        pedidas = [{k: r[k] for k in ('ID', 'FECHA', 'COMERCIAL', 'CLIENTE', 'PRODUCTOS', 'NECESIDADES', 'estado_pedida', 'oferta_oficina', 'dias')}
                   for r in d.get('pedidas', []) if not args.get('solo_pendientes') or r['estado_pedida'] in ('ELABORAR', 'ENVIADA')][:40]
        return {'precios_dados': precios, 'ofertas_pedidas_en_visitas': pedidas, 'ofertas': ofs, 'avisos': d['avisos_ofertas']}
    if nombre == 'control_visitas':
        lista = api.control_visitas(s, args.get('comercial', '') if s.puede('ver_todos') else '')
        if args.get('estado'):
            lista = [x for x in lista if x['estado'] == args['estado']]
        return {'total': len(lista), 'clientes': lista[:60]}
    if nombre == 'resumen_visitas':
        return api.resumen(s, args.get('desde', ''), args.get('hasta', ''), args.get('comercial', '') if s.puede('ver_todos') else '')
    if nombre == 'precios_dados':
        p = api.precios_dados(s, str(args.get('cliente', '') or ''))
        return {'total': len(p), 'precios': [{k: r[k] for k in ('FECHA', 'COMERCIAL', 'CLIENTE', 'PRODUCTOS', 'DESCRIPCION', 'IMPORTE', 'OBSERVACIONES') if r.get(k)} for r in p[:60]]}
    return {'error': f'Herramienta desconocida: {nombre}'}


def _salida(s, nombre, args):
    try:
        datos = _ejecutar(s, nombre, args)
    except HTTPException as e:
        datos = {'error': e.detail}
    except ValueError as e:
        datos = {'error': str(e)}
    except Exception as e:
        logger.exception('Error en %s', nombre)
        datos = {'error': str(e)}
    texto = json.dumps(datos, ensure_ascii=False, default=str)
    return texto if len(texto) <= MAX_SALIDA else texto[:MAX_SALIDA] + '… (recortado)'


def _cliente_openai():
    try:
        from Control_IA.api_control import MODELO_IA, cliente_ia
        return cliente_ia(), MODELO_IA
    except ImportError:
        from openai import OpenAI
        from secretos import obtener
        return OpenAI(api_key=obtener('OPENAI_API_KEY')), MODELO_POR_DEFECTO


BUSQUEDA_WEB = {'type': 'web_search', 'search_context_size': 'medium',
                'user_location': {'type': 'approximate', 'country': 'ES', 'timezone': 'Europe/Madrid'}}
MAX_ACCIONES = 30
MAX_FUENTES = 8

PROMPT_CONTROL = """
MODO CONTROL: si en «Herramientas activadas» aparece «control de la app», además de consultar datos puedes USAR la app por el usuario con las funciones de pantalla (ir_a, abrir_ficha_cliente, nueva_visita, rellenar_visita, guardar_visita, cerrar_visita, buscar_precios...). Se ejecutan en su móvil, a la vista, y te llega el resultado y cómo queda la pantalla.
- Hazlo cuando te pidan hacer algo en la app («ábreme la ficha de X», «apunta una visita a X de 10:00 a 11:00...», «busca el precio de...»). Para consultas basta con las herramientas de datos.
- Antes de rellenar una visita, abre el formulario (nueva_visita o abrir_visita). Usa los códigos de cliente reales (buscar_clientes).
- Puedes guardar una visita sin terminar. Para cerrarla deben estar todos los campos importantes; si falta algo que no sabes (qué se habló, resultado...), no lo inventes: guarda y pregunta.
- Nunca anules registros ni cambies frecuencias si el usuario no lo ha pedido expresamente.
Si el control NO está activado y te piden hacer algo en la app, explica cómo hacerlo y recuerda que pueden activar el interruptor bajo el robot para que lo hagas tú.
Búsqueda en internet: solo si está activada; cita lo que saques de internet."""


def _t(valor, maximo=300):
    texto = ' '.join(str(valor or '').split())
    return texto if len(texto) <= maximo else texto[:maximo] + '…'


def _leer(respuesta):
    """(texto, llamadas a funciones, fuentes web)."""
    llamadas, fuentes = [], {}
    for item in respuesta.output or []:
        tipo = getattr(item, 'type', '')
        if tipo == 'function_call':
            try:
                args = json.loads(item.arguments or '{}')
            except Exception:
                args = {}
            llamadas.append({'call_id': item.call_id, 'nombre': item.name, 'args': args if isinstance(args, dict) else {}})
        elif tipo == 'message':
            for parte in getattr(item, 'content', None) or []:
                for ann in getattr(parte, 'annotations', None) or []:
                    url = getattr(ann, 'url', '')
                    if getattr(ann, 'type', '') == 'url_citation' and url and url not in fuentes:
                        fuentes[url] = getattr(ann, 'title', '') or url
    texto = (getattr(respuesta, 'output_text', '') or '').strip()
    return texto, llamadas, [{'titulo': t, 'url': u} for u, t in list(fuentes.items())[:MAX_FUENTES]]


def _contexto(s, contexto):
    ctx = [f"Pantalla: {contexto.get('vista', 'inicio')}."]
    if contexto.get('cliente'):
        c = pirineos.clientes().get(str(contexto['cliente']))
        if c:
            ctx.append(f"Cliente abierto: {c['nombre']} (código {c['codigo']}, {c['poblacion']}).")
    # Todo lo demás que la pantalla cuenta de sí misma (visita a medio rellenar, búsqueda de precios,
    # resultados, filtros, fecha de la agenda...), resumido.
    detalle = {k: v for k, v in contexto.items() if k not in ('vista', 'cliente', 'etiqueta') and v not in (None, '', [], {})}
    if detalle:
        ctx.append('PANTALLA: ' + json.dumps(detalle, ensure_ascii=False, default=str)[:6000])
    return '\n'.join(ctx)


def responder(s, mensaje, historial, contexto, cliente=None, web=False, control=False, acciones=None,
              continuar='', resultados=None):
    """Conversa con el modelo. Las herramientas de datos se ejecutan aquí; las acciones de pantalla
    (modo control) se devuelven al navegador, que las ejecuta y vuelve con `continuar` y sus resultados.
    `cliente` permite pruebas."""
    continuando = bool(continuar)
    if not continuando and not (mensaje or '').strip():
        raise HTTPException(status_code=400, detail='Escribe una pregunta.')
    inicio = time.time()
    if cliente is None:
        cliente, modelo = _cliente_openai()
    else:
        modelo = MODELO_POR_DEFECTO
    tools = _herramientas(s)
    nombres_servidor = {h['name'] for h in tools}
    if web:
        tools.append(dict(BUSQUEDA_WEB))
    if control:
        import re
        for a in (acciones or [])[:MAX_ACCIONES]:
            nombre = str(a.get('nombre', ''))
            if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', nombre) or nombre in nombres_servidor:
                continue
            tools.append({'type': 'function', 'name': nombre, 'strict': False, 'description': _t(a.get('descripcion'), 600),
                          'parameters': a.get('parametros') or {'type': 'object', 'properties': {}}})

    if continuando:
        entrada = [{'type': 'function_call_output', 'call_id': str(r.get('call_id', '')), 'output': _t(r.get('salida'), 6000)}
                   for r in (resultados or [])]
        if entrada:
            entrada[-1]['output'] += '\n\nPANTALLA AHORA:\n' + _contexto(s, contexto)
    else:
        modos = [m for m, activo in (('búsqueda en internet', web), ('control de la app', control)) if activo]
        ctx = (f"Usuario: {s.nombre} (rol {s.rol}{'; puede ver costes y márgenes' if s.puede('ver_costes') else '; NO ve costes ni márgenes'}"
               f"{'; ve la actividad de todos' if s.puede('ver_todos') else '; solo ve su propia actividad'}).\n"
               f"Hoy es {actividad.ahora().strftime('%A %d/%m/%Y %H:%M')}.\n"
               f"Herramientas activadas: {', '.join(modos) or 'ninguna'}.\n" + _contexto(s, contexto))
        entrada = [{'role': 'assistant' if m.get('rol') == 'assistant' else 'user', 'content': str(m.get('texto', ''))[:1500]}
                   for m in historial[-MAX_HISTORIAL:]]
        entrada.append({'role': 'user', 'content': 'CONTEXTO:\n' + ctx + '\n\nPREGUNTA:\n' + mensaje.strip()})
    parametros = dict(model=modelo, instructions=PROMPT + PROMPT_CONTROL, input=entrada, tools=tools,
                      max_output_tokens=MAX_TOKENS, reasoning={'effort': 'low'})
    if continuando:
        parametros['previous_response_id'] = continuar

    usadas, texto, fuentes, de_pantalla, resultados_servidor = [], '', [], [], []
    for vuelta in range(MAX_VUELTAS):
        respuesta = cliente.responses.create(**parametros)
        texto, llamadas, f = _leer(respuesta)
        fuentes += [x for x in f if x not in fuentes]
        en_servidor = [c for c in llamadas if c['nombre'] in nombres_servidor]
        de_pantalla = [c for c in llamadas if c['nombre'] not in nombres_servidor]
        if not en_servidor:
            break
        salidas = []
        for c in en_servidor:
            usadas.append(c['nombre'])
            salidas.append({'type': 'function_call_output', 'call_id': c['call_id'], 'output': _salida(s, c['nombre'], c['args'])})
        if de_pantalla:
            # También hay acciones para el navegador: estas salidas vuelven junto con las suyas.
            resultados_servidor = [{'call_id': x['call_id'], 'salida': x['output']} for x in salidas]
            break
        if vuelta == MAX_VUELTAS - 1:
            texto = texto or 'He necesitado demasiadas consultas. Prueba a concretar más la pregunta.'
            break
        parametros.update(previous_response_id=respuesta.id, input=salidas)
    logger.info('asistente %s | %.1fs | herramientas=%s | pantalla=%s', s.clave, time.time() - inicio, usadas,
                [c['nombre'] for c in de_pantalla])
    if not texto and not de_pantalla:
        texto = 'No he podido generar una respuesta. Prueba a reformular la pregunta.'
    return {'respuesta': texto, 'acciones': de_pantalla, 'respuesta_id': respuesta.id,
            'resultados_servidor': resultados_servidor, 'fuentes': fuentes[:MAX_FUENTES], 'herramientas': usadas}
