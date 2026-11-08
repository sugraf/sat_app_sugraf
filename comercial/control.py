# comercial/control.py
"""Control de visitas por cliente: cuáles no se visitan y cuáles se ven demasiado.

Cada cliente de la cartera de un comercial tiene una frecuencia de visita (cada cuántos días):
- la que haya fijado Master (Comerciales/Cadencias.json en Drive), o
- una propuesta automática según lo que compra (últimos 12 meses), para no tener que
  configurar uno a uno los ~100 clientes de cada cartera:
      15.000 € o más  -> cada 30 días (mensual)
      5.000 - 15.000  -> cada 60 días (bimestral)
      1.000 - 5.000   -> cada 90 días (trimestral)
      menos de 1.000  -> cada 180 días (semestral)
      sin compras     -> sin frecuencia (no se exige visitarlo)

Estados:
- «Toca visitar»: han pasado más días que su frecuencia desde la última visita.
- «Pronto»: faltan pocos días (una cuarta parte de su frecuencia, como mucho 10).
- «Visitado de más»: en los últimos 90 días tiene muchas más visitas de las previstas
  (más del doble y al menos 2 de más), o 3 o más visitas sin que compre nada.
- Clientes sin ninguna visita registrada: se cuentan desde el día en que el comercial
  empezó a usar la app (su primera visita), para no marcar a todos en rojo el primer día.
"""
import threading
from datetime import date, timedelta

from comercial import almacen, pirineos
from comercial.usuarios import normalizar

FICHERO = 'Cadencias.json'
OPCIONES_FRECUENCIA = [(7, 'Semanal'), (15, 'Quincenal'), (30, 'Mensual'), (60, 'Bimestral'), (90, 'Trimestral'),
                       (180, 'Semestral'), (0, 'No visitar / sin frecuencia')]
VENTANA_DIAS = 90
AVISO_PRONTO_DIAS = 10

_lock = threading.Lock()
_cache = {'datos': None, 'leido': 0.0}


def nombre_frecuencia(dias):
    for d, n in OPCIONES_FRECUENCIA:
        if d == dias:
            return n
    return f'Cada {dias} días' if dias else 'Sin frecuencia'


def cadencias(refrescar=False):
    import time
    if refrescar or _cache['datos'] is None or time.time() - _cache['leido'] > 120:
        _, datos = almacen.leer_json_propio(FICHERO, {})
        _cache.update(datos=datos or {}, leido=time.time())
    return _cache['datos']


def fijar_cadencia(codigo, dias, usuario, momento):
    with _lock:
        meta, datos = almacen.leer_json_propio(FICHERO, {})
        datos = datos or {}
        if dias is None:
            datos.pop(str(codigo), None)
        else:
            datos[str(codigo)] = {'dias': int(dias), 'por': usuario, 'fecha': momento}
        almacen.guardar_json_propio(FICHERO, datos, meta)
        _cache.update(datos=datos, leido=0.0)


def compras_12_meses():
    """{codigo: importe facturado en los últimos 365 días}."""
    ventas, _ = pirineos.todas_las_ventas()
    corte = (date.today() - timedelta(days=365)).isoformat()
    excluir = {'CONTABILIDAD', 'SERVICIO TECNICO', 'SERVICIOS'}
    return {cod: round(sum(l['importe'] for l in lineas if l['fecha'] > corte and l['tipo'] != 'SAT'
                           and l['grupo'].upper() not in excluir), 2)
            for cod, lineas in ventas.items()}


def propuesta(importe):
    if importe >= 15000:
        return 30, f'compra {importe:,.0f} € al año'.replace(',', '.')
    if importe >= 5000:
        return 60, f'compra {importe:,.0f} € al año'.replace(',', '.')
    if importe >= 1000:
        return 90, f'compra {importe:,.0f} € al año'.replace(',', '.')
    if importe > 0:
        return 180, f'compra {importe:,.0f} € al año'.replace(',', '.')
    return 0, 'no ha comprado en el último año'


def estado_cliente(c, visitas_cliente, compras, cad_fijadas, inicio_uso, hoy=None):
    """Situación de visitas de un cliente. visitas_cliente: fechas (AAAA-MM-DD) de visitas registradas."""
    hoy = hoy or date.today()
    fijada = cad_fijadas.get(pirineos.principal(c['codigo'])) or cad_fijadas.get(c['codigo'])
    importe = compras.get(c['codigo'], 0.0)
    if fijada:
        dias_freq, origen = fijada['dias'], f"fijada por {fijada.get('por', 'Master')}"
    else:
        dias_freq, motivo = propuesta(importe)
        origen = f'propuesta: {motivo}'
    fechas = sorted(visitas_cliente)
    ultima = fechas[-1] if fechas else ''
    corte = (hoy - timedelta(days=VENTANA_DIAS)).isoformat()
    recientes = sum(1 for f in fechas if f > corte)
    desde = date.fromisoformat(ultima) if ultima else inicio_uso
    dias = (hoy - desde).days if desde else None

    estado, texto = 'ok', ''
    if dias_freq:
        previstas = VENTANA_DIAS / dias_freq
        if recientes > max(2 * previstas, previstas + 2):
            estado, texto = 'demasiado', f'{recientes} visitas en 90 días (lo previsto son unas {round(previstas) or 1})'
        elif dias is not None and dias > dias_freq:
            estado = 'toca'
            texto = f'{dias} días sin visita (frecuencia: cada {dias_freq})' if ultima else f'Sin visitas registradas (frecuencia: cada {dias_freq} días)'
        elif dias is not None and dias > dias_freq - min(AVISO_PRONTO_DIAS, max(1, round(dias_freq / 4))):
            estado, texto = 'pronto', f'Toca visitar en {max(dias_freq - dias, 0)} días'
    elif recientes >= 3:
        estado, texto = 'demasiado', f'{recientes} visitas en 90 días y no ha comprado en el último año'
    return {
        'frecuencia_dias': dias_freq, 'frecuencia_origen': origen,
        'frecuencia': nombre_frecuencia(dias_freq) if (dias_freq or fijada) else 'Sin frecuencia',
        'fijada': bool(fijada), 'compras_12m': importe, 'ultima_visita': ultima, 'dias_sin_visita': dias if ultima else None,
        'visitas_90d': recientes, 'estado': estado, 'texto': texto,
    }


def control_cartera(nombre_pirineos_comercial, visitas, inicio_uso=None, hoy=None):
    """Situación de todos los clientes de la cartera de un comercial.
    visitas: registros de visita cerrados (de cualquiera) · inicio_uso: primera visita del comercial."""
    hoy = hoy or date.today()
    inicio_uso = inicio_uso or hoy
    objetivo = normalizar(nombre_pirineos_comercial)
    compras = compras_12_meses()
    cad = cadencias()
    # Un cliente con varios códigos de Pirineos cuenta una vez (con su código principal) y
    # suma las visitas hechas a cualquiera de sus códigos.
    por_cliente = {}
    for r in visitas:
        if r['COD_CLIENTE']:
            por_cliente.setdefault(pirineos.principal(r['COD_CLIENTE']), []).append(r['FECHA'])
    cl = pirineos.clientes()
    lista, vistos = [], set()
    for c in cl.values():
        if c['comercial_norm'] != objetivo or c['grupo'].upper() == 'PROVEEDORES':
            continue
        pr = pirineos.principal(c['codigo'])
        if pr in vistos:
            continue
        vistos.add(pr)
        c = cl.get(pr, c)
        e = estado_cliente(c, por_cliente.get(pr, []), compras, cad, inicio_uso, hoy)
        lista.append({'codigo': c['codigo'], 'nombre': c['nombre'], 'poblacion': c['poblacion'],
                      'comercial': c['comercial'], 'otros_codigos': [h for h in pirineos.codigos_hermanos(pr) if h != pr], **e})
    orden = {'toca': 0, 'demasiado': 1, 'pronto': 2, 'ok': 3}
    lista.sort(key=lambda x: (orden[x['estado']], -(x['dias_sin_visita'] or 0), -x['compras_12m']))
    return lista
