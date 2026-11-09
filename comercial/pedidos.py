# comercial/pedidos.py
"""Pedidos que mandan los comerciales desde la app (pestaña «Pedidos»): piezas o máquinas para un cliente.

Al enviarlo se manda un correo a la oficina (correo.DESTINOS_PEDIDOS: carolina@sugraf.es) con todo el detalle.
Además, si existe SAT-App/Comerciales/Pedidos_comerciales.json (creado a mano: la cuenta de servicio no puede
crear ficheros, solo actualizarlos; contenido inicial {"pedidos": []}), se guarda ahí para la lista de
«Pedidos enviados». Si no existe, el pedido se envía igual por correo, pero no queda en la lista.
"""
import secrets
import threading

from comercial import almacen

FICHERO = 'Pedidos_comerciales.json'
TIPOS = ('Piezas', 'Máquinas')
MAX_GUARDADOS = 1500
_lock = threading.Lock()


def nuevo_id(momento):
    return f"PED-{momento.strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2)}"


def guardar(pedido):
    """True si ha quedado guardado en el histórico; False si falta el fichero."""
    with _lock:
        meta, d = almacen.leer_json_propio(FICHERO, {})
        if meta is None:
            return False
        lista = (d or {}).get('pedidos', []) if isinstance(d, dict) else []
        lista.append(pedido)
        almacen.guardar_json_propio(FICHERO, {'pedidos': lista[-MAX_GUARDADOS:]}, meta)
        return True


def listar(nombres_comerciales=None):
    """Pedidos guardados (los más nuevos primero); con nombres, solo los de esos comerciales. (lista, hay_fichero)."""
    meta, d = almacen.leer_json_propio(FICHERO, {})
    if meta is None:
        return [], False
    lista = (d or {}).get('pedidos', []) if isinstance(d, dict) else []
    if nombres_comerciales is not None:
        lista = [p for p in lista if p.get('comercial') in nombres_comerciales]
    return sorted(lista, key=lambda p: p.get('creado', ''), reverse=True), True
