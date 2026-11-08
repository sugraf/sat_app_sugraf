# comercial/notificaciones.py
"""Notificaciones para la app de escritorio (Digital Manager).

La app comercial deja aquí avisos para la oficina (p. ej. «Sergio ha dado precio a X: preparar
oferta»). La app de escritorio los lee del mismo fichero de Drive, los muestra en la campana de
arriba y con una «!» amarilla en el módulo que toca, y se quedan hasta que alguien los marca como leídos.
Fichero: SAT-App/Comerciales/Notificaciones.json  ->  {"notificaciones": [...]} (creado a mano).
"""
import secrets
import threading

from comercial import almacen

FICHERO = 'Notificaciones.json'
MAX_GUARDADAS = 400
_lock = threading.Lock()


def crear(tipo, modulo, titulo, texto, datos, momento):
    """modulo: módulo del escritorio al que va (ofertas_consumibles, ofertas_maquinaria, comerciales...)."""
    nueva = {'id': f"NOT-{momento.strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2)}", 'tipo': tipo, 'modulo': modulo,
             'titulo': titulo, 'texto': texto, 'datos': datos, 'creada': momento.strftime('%Y-%m-%d %H:%M'),
             'leida': False, 'leida_por': '', 'leida_en': ''}
    with _lock:
        meta, d = almacen.leer_json_propio(FICHERO, {})
        if meta is None:
            raise RuntimeError(f'Falta el fichero {FICHERO} en SAT-App/Comerciales.')
        lista = (d or {}).get('notificaciones', []) if isinstance(d, dict) else []
        lista.append(nueva)
        # Se conservan todas las no leídas y las últimas leídas.
        if len(lista) > MAX_GUARDADAS:
            leidas = [n for n in lista if n.get('leida')]
            quitar = {n['id'] for n in leidas[:len(lista) - MAX_GUARDADAS]}
            lista = [n for n in lista if n['id'] not in quitar]
        almacen.guardar_json_propio(FICHERO, {'notificaciones': lista}, meta)
    return nueva
