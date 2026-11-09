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


def _sustituidas(lista, tipo, registro, momento, motivo):
    """Da por leídas (para todos) las pendientes de ese tipo que vienen del mismo registro de la web: así, si el
    comercial cambia la oferta (p. ej. de consumibles a maquinaria), no se queda el aviso viejo en el otro módulo."""
    for n in lista:
        if registro and not n.get('leida') and n.get('tipo') == tipo and (n.get('datos') or {}).get('registro') == registro:
            n.update(leida=True, leida_por=motivo, leida_en=momento.strftime('%Y-%m-%d %H:%M'))


def crear(tipo, modulo, titulo, texto, datos, momento, sustituir=False):
    """modulo: módulo del escritorio al que va (ofertas_consumibles, ofertas_maquinaria, comerciales...).
    sustituir: retira antes las pendientes del mismo tipo y del mismo registro (datos['registro'])."""
    nueva = {'id': f"NOT-{momento.strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2)}", 'tipo': tipo, 'modulo': modulo,
             'titulo': titulo, 'texto': texto, 'datos': datos, 'creada': momento.strftime('%Y-%m-%d %H:%M'),
             'leida': False, 'leida_por': '', 'leida_en': ''}
    with _lock:
        meta, d = almacen.leer_json_propio(FICHERO, {})
        if meta is None:
            raise RuntimeError(f'Falta el fichero {FICHERO} en SAT-App/Comerciales.')
        lista = (d or {}).get('notificaciones', []) if isinstance(d, dict) else []
        if sustituir:
            _sustituidas(lista, tipo, (datos or {}).get('registro'), momento, 'Sustituida (la web cambió la oferta)')
        lista.append(nueva)
        # Se conservan todas las no leídas y las últimas leídas.
        if len(lista) > MAX_GUARDADAS:
            leidas = [n for n in lista if n.get('leida')]
            quitar = {n['id'] for n in leidas[:len(lista) - MAX_GUARDADAS]}
            lista = [n for n in lista if n['id'] not in quitar]
        almacen.guardar_json_propio(FICHERO, {'notificaciones': lista}, meta)
    return nueva


def retirar(tipo, registro, momento, motivo='Retirada (la web quitó la oferta)'):
    """Retira las pendientes de ese tipo que vienen de ese registro (p. ej. la visita ya no tiene oferta)."""
    with _lock:
        meta, d = almacen.leer_json_propio(FICHERO, {})
        if meta is None or not isinstance(d, dict):
            return
        lista = d.get('notificaciones', [])
        antes = sum(1 for n in lista if not n.get('leida'))
        _sustituidas(lista, tipo, registro, momento, motivo)
        if sum(1 for n in lista if not n.get('leida')) != antes:
            almacen.guardar_json_propio(FICHERO, {'notificaciones': lista}, meta)
