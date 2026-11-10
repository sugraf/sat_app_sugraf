# comercial/pedidos.py
"""Pedidos que mandan los comerciales desde la app (pestaña «Pedidos»): consumibles o maquinaria para un cliente.

Al enviarlo se manda un correo a la oficina (correo.DESTINOS_PEDIDOS: carolina@sugraf.es) con todo el detalle.
Si pasadas 24 h no aparece en Pirineos ninguna venta ni pedido de cliente que lo demuestre tramitado, se manda un
segundo correo para que lo compruebe (revisar(), que llama Vercel tres veces al día: ver vercel.json).
Además, si existe SAT-App/Comerciales/Pedidos_comerciales.json (creado a mano: la cuenta de servicio no puede
crear ficheros, solo actualizarlos; contenido inicial {"pedidos": []}), se guarda ahí para la lista de
«Pedidos enviados». Si no existe, el pedido se envía igual por correo, pero no queda en la lista.
"""
import secrets
import threading
from datetime import datetime

from comercial import almacen, pirineos

FICHERO = 'Pedidos_comerciales.json'
TIPOS = ('Consumibles', 'Maquinaria')
# Tipos de antes: los pedidos de piezas pasan a consumibles y los de máquinas a maquinaria (al leer y al guardar).
TIPOS_ANTIGUOS = {'Piezas': 'Consumibles', 'Máquinas': 'Maquinaria'}
MAX_GUARDADOS = 1500
HORAS_RECORDATORIO = 24      # tiempo que se da a la oficina para tramitarlo antes del segundo correo
DIAS_MAX_RECORDATORIO = 15   # los pedidos más antiguos ya no se revisan
_lock = threading.Lock()


def nuevo_id(momento):
    return f"PED-{momento.strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2)}"


def tipo_actual(tipo):
    return TIPOS_ANTIGUOS.get(tipo, tipo)


def _al_dia(lista):
    """Pasa los tipos antiguos a los nuevos (Piezas -> Consumibles, Máquinas -> Maquinaria)."""
    for p in lista:
        if p.get('tipo') in TIPOS_ANTIGUOS:
            p['tipo'] = TIPOS_ANTIGUOS[p['tipo']]
    return lista


def _leer():
    meta, d = almacen.leer_json_propio(FICHERO, {})
    lista = (d or {}).get('pedidos', []) if isinstance(d, dict) else []
    return meta, _al_dia([p for p in lista if isinstance(p, dict)])


def guardar(pedido):
    """True si ha quedado guardado en el histórico; False si falta el fichero. Al guardar se reescribe la lista
    con los tipos ya al día, así que los pedidos antiguos de piezas quedan guardados como consumibles."""
    with _lock:
        meta, lista = _leer()
        if meta is None:
            return False
        lista.append(pedido)
        almacen.guardar_json_propio(FICHERO, {'pedidos': lista[-MAX_GUARDADOS:]}, meta)
        return True


def actualizar(cambios_por_id):
    """Aplica {id: {campo: valor}} a los pedidos guardados (p. ej. marcar el recordatorio ya enviado)."""
    with _lock:
        meta, lista = _leer()
        if meta is None:
            return False
        for p in lista:
            if p.get('id') in cambios_por_id:
                p.update(cambios_por_id[p['id']])
        almacen.guardar_json_propio(FICHERO, {'pedidos': lista[-MAX_GUARDADOS:]}, meta)
        return True


def listar(nombres_comerciales=None):
    """Pedidos guardados (los más nuevos primero); con nombres, solo los de esos comerciales. (lista, hay_fichero)."""
    meta, lista = _leer()
    if meta is None:
        return [], False
    if nombres_comerciales is not None:
        lista = [p for p in lista if p.get('comercial') in nombres_comerciales]
    return sorted(lista, key=lambda p: p.get('creado', ''), reverse=True), True


# ---------------------------------------------------------------- ¿se ha tramitado?
def movimiento(p):
    """Primera venta o pedido de cliente de Pirineos que demuestra que el pedido se ha tramitado: del mismo cliente
    (cualquiera de sus códigos), desde el día del pedido y, si el pedido lleva referencias, de alguna de ellas (si no
    lleva ninguna, vale cualquier producto). None si no aparece nada o el cliente no tiene código."""
    cod = p.get('cod_cliente')
    if not cod:
        return None
    desde = (p.get('creado') or '')[:10]
    codigos = set(pirineos.codigos_hermanos(cod)) | {cod}
    refs = {l['referencia'] for l in p.get('lineas', []) if l.get('referencia')}
    vale = (lambda ref: ref in refs) if refs else (lambda ref: True)
    ventas, _ = pirineos.todas_las_ventas()
    lineas = next((ventas[c] for c in codigos if c in ventas), [])   # los códigos hermanos comparten la lista
    for l in sorted(lineas, key=lambda l: l['fecha']):
        if l['fecha'] >= desde and (l['cantidad'] or 0) > 0 and vale(l['referencia']):
            return {'tipo': 'venta', 'fecha': l['fecha'], 'documento': l['documento'], 'referencia': l['referencia'],
                    'descripcion': l['descripcion']}
    for x in sorted(pirineos.pedidos_pendientes(), key=lambda x: x['fecha']):
        if x.get('codigo') in codigos and x['fecha'] >= desde and vale(x['referencia']):
            return {'tipo': 'pedido de cliente', 'fecha': x['fecha'], 'documento': x['pedido'], 'referencia': x['referencia'],
                    'descripcion': x['descripcion']}
    return None


def revisar(momento):
    """Revisa los pedidos enviados hace más de 24 h (y menos de 15 días) que aún no se han dado por tramitados ni
    tienen recordatorio: si en Pirineos ya hay movimiento se apunta como tramitado; si no, se manda el segundo correo
    a la oficina (una sola vez por pedido). Si no se pueden leer los datos de Pirineos no se manda nada."""
    from comercial import correo
    meta, lista = _leer()
    if meta is None:
        return {'ok': False, 'error': f'Falta {FICHERO}', 'tramitados': [], 'avisados': []}
    ahora = momento.replace(tzinfo=None)
    sello = momento.strftime('%Y-%m-%d %H:%M')
    cambios, tramitados, avisados, errores = {}, [], [], []
    for p in lista:
        if p.get('recordatorio') or p.get('tramitado'):
            continue
        try:
            horas = (ahora - datetime.strptime(p.get('creado', ''), '%Y-%m-%d %H:%M')).total_seconds() / 3600
        except ValueError:
            continue
        if horas < HORAS_RECORDATORIO or horas > DIAS_MAX_RECORDATORIO * 24:
            continue
        try:
            mov = movimiento(p)
        except Exception as e:      # sin datos de Pirineos no se puede saber: mejor no avisar en falso
            errores.append(f"{p.get('id')}: {e}")
            continue
        if mov:
            cambios[p['id']] = {'tramitado': dict(mov, detectado=sello)}
            tramitados.append(p['id'])
            continue
        ok, error = correo.recordatorio_pedido(p)
        if ok:
            cambios[p['id']] = {'recordatorio': sello}
            avisados.append(p['id'])
        else:
            errores.append(f"{p.get('id')}: correo ({error})")
    if cambios:
        actualizar(cambios)
    return {'ok': not errores, 'tramitados': tramitados, 'avisados': avisados, 'errores': errores}
