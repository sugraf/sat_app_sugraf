# comercial/actividad.py
"""Registro de actividad comercial: visitas (horas de entrada y salida escritas a mano),
llamadas y precios dados a clientes (para el seguimiento de precios).

Se guarda en la carpeta "Comerciales" de Drive (dentro de SAT-App), un Excel por persona:
  Actividad_<usuario>.xlsx   una fila por visita / llamada / precio dado
  Historial_<usuario>.xlsx   cada cambio: quién, cuándo, campo, valor anterior y nuevo

Un fichero por persona evita que dos comerciales que guardan a la vez se pisen. Nada se
borra: anular un registro lo marca como ANULADO y queda en el historial.
"""
import io
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd

from comercial import almacen
from comercial.usuarios import USUARIOS

try:
    from zoneinfo import ZoneInfo
    ZONA = ZoneInfo('Europe/Madrid')
except Exception:  # sin base de datos de zonas horarias
    ZONA = None

COLUMNAS = [
    'ID', 'TIPO', 'SUBTIPO', 'COMERCIAL', 'COD_CLIENTE', 'CLIENTE', 'POBLACION',
    'FECHA', 'HORA_INICIO', 'HORA_FIN', 'DURACION_MIN', 'REGISTRO',
    'MOTIVO', 'TEMAS', 'DESCRIPCION', 'RESULTADO', 'PROXIMA_ACCION', 'FECHA_SEGUIMIENTO', 'SEGUIMIENTO_HECHO',
    'CONTACTO', 'PRODUCTOS', 'NECESIDADES', 'OFERTA', 'IMPORTE', 'MARCA', 'MODELO', 'ESTADO_OPERACION',
    'OBSERVACIONES', 'ESTADO', 'CREADO_POR', 'CREADO_EN', 'MODIFICADO_POR', 'MODIFICADO_EN',
]
# Para un precio dado (TIPO = PRECIO): PRODUCTOS = referencia, DESCRIPCION = artículo,
# IMPORTE = precio por unidad dado al cliente, OBSERVACIONES = nota.
COLUMNAS_HISTORIAL = ['FECHA_CAMBIO', 'USUARIO', 'ID', 'ACCION', 'CAMPO', 'VALOR_ANTERIOR', 'VALOR_NUEVO']

# Campos que puede rellenar el usuario (el resto los pone el servidor).
EDITABLES = [
    'TIPO', 'SUBTIPO', 'COD_CLIENTE', 'CLIENTE', 'POBLACION', 'FECHA', 'HORA_INICIO', 'HORA_FIN',
    'MOTIVO', 'TEMAS', 'DESCRIPCION', 'RESULTADO', 'PROXIMA_ACCION', 'FECHA_SEGUIMIENTO', 'SEGUIMIENTO_HECHO',
    'CONTACTO', 'PRODUCTOS', 'NECESIDADES', 'OFERTA', 'IMPORTE', 'MARCA', 'MODELO', 'ESTADO_OPERACION',
    'OBSERVACIONES',
]

TIPOS = ['VISITA', 'LLAMADA', 'PRECIO']
# Una visita se puede ir guardando: queda SIN TERMINAR hasta que tiene hora de entrada y de salida.
SIN_TERMINAR, REGISTRADA, ANULADA = 'SIN TERMINAR', 'REGISTRADA', 'ANULADA'

_locks = {clave: threading.Lock() for clave in USUARIOS}
_cache = {}  # nombre fichero -> (modifiedTime, DataFrame)


def ahora():
    return datetime.now(ZONA) if ZONA else datetime.utcnow() + timedelta(hours=1)


def _nombre(clave):
    return f'Actividad_{clave}.xlsx'


def _nombre_historial(clave):
    return f'Historial_{clave}.xlsx'


def _leer(nombre, columnas, usar_cache=True):
    meta = almacen.buscar_propio(nombre)
    if meta is None:
        return None, pd.DataFrame(columns=columnas, dtype=object)
    clave = meta.get('modifiedTime')
    if usar_cache and nombre in _cache and _cache[nombre][0] == clave:
        return meta, _cache[nombre][1].copy()
    try:
        df = pd.read_excel(io.BytesIO(almacen.descargar(meta)), dtype=str, keep_default_na=False)
    except Exception as e:
        raise RuntimeError(f"No se pudo leer '{nombre}' de Drive ({e}). No se ha modificado nada; inténtalo de nuevo.") from e
    for c in columnas:
        if c not in df.columns:
            df[c] = ''
    _cache[nombre] = (clave, df)
    return meta, df.copy()


def _guardar(nombre, meta, df, filas_antes):
    if meta is not None and len(df) < filas_antes:
        raise RuntimeError(f"Guardado cancelado: '{nombre}' iba a perder filas. No se ha modificado nada.")
    salida = io.BytesIO()
    with pd.ExcelWriter(salida, engine='openpyxl') as w:
        df.to_excel(w, index=False, sheet_name='Actividad' if nombre.startswith('Actividad') else 'Historial')
        hoja = w.sheets['Actividad' if nombre.startswith('Actividad') else 'Historial']
        for i, col in enumerate(df.columns, start=1):
            ancho = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)]) + 2
            hoja.column_dimensions[hoja.cell(row=1, column=i).column_letter].width = min(ancho, 45)
        hoja.freeze_panes = 'A2'
    almacen.guardar_propio(nombre, salida.getvalue(), meta)
    _cache.pop(nombre, None)


def _registros(df):
    return [{k: (v if v is not None else '') for k, v in fila.items()} for fila in df[COLUMNAS].to_dict('records')]


# ---------------------------------------------------------------- lectura
def listar(claves, desde='', hasta='', incluir_anuladas=False):
    """Registros de los usuarios indicados entre dos fechas (YYYY-MM-DD, incluidas)."""
    def uno(clave):
        _, df = _leer(_nombre(clave), COLUMNAS)
        return _registros(df)

    with ThreadPoolExecutor(max_workers=max(1, len(claves))) as ex:
        todos = [r for lista in ex.map(uno, claves) for r in lista]
    resultado = []
    for r in todos:
        if not incluir_anuladas and r['ESTADO'] == ANULADA:
            continue
        if desde and r['FECHA'] < desde and r['ESTADO'] != SIN_TERMINAR:
            continue
        if hasta and r['FECHA'] > hasta:
            continue
        resultado.append(r)
    resultado.sort(key=lambda r: (r['FECHA'], r['HORA_INICIO'] or '99:99', r['CREADO_EN']))
    return resultado


def obtener(clave, id_registro):
    _, df = _leer(_nombre(clave), COLUMNAS)
    filas = df[df['ID'] == id_registro]
    return _registros(filas)[0] if len(filas) else None


def historial(clave, id_registro):
    _, df = _leer(_nombre_historial(clave), COLUMNAS_HISTORIAL)
    return df[df['ID'] == id_registro].to_dict('records')


# ---------------------------------------------------------------- validación
def _minutos(hora):
    try:
        h, m = str(hora).strip()[:5].split(':')
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except Exception:
        pass
    return None


def validar(datos):
    """Normaliza y comprueba un registro. Devuelve (datos, errores)."""
    d = {k: str(datos.get(k, '') if datos.get(k) is not None else '').strip() for k in EDITABLES}
    errores = []
    d['TIPO'] = d['TIPO'].upper()
    if d['TIPO'] not in TIPOS:
        errores.append('Tipo de actividad no válido.')
    if not d['CLIENTE']:
        errores.append('Indica el cliente.')
    try:
        datetime.strptime(d['FECHA'], '%Y-%m-%d')
    except ValueError:
        errores.append('Fecha no válida.')
    if d['FECHA_SEGUIMIENTO']:
        try:
            datetime.strptime(d['FECHA_SEGUIMIENTO'], '%Y-%m-%d')
        except ValueError:
            errores.append('Fecha de seguimiento no válida.')
    ini, fin = _minutos(d['HORA_INICIO']), _minutos(d['HORA_FIN'])
    if d['HORA_INICIO'] and ini is None:
        errores.append('Hora de inicio no válida.')
    if d['HORA_FIN'] and fin is None:
        errores.append('Hora de finalización no válida.')
    if ini is not None and fin is not None and fin <= ini:
        errores.append('La hora de finalización debe ser posterior a la de inicio (no se admiten duraciones negativas ni nulas).')
    d['HORA_INICIO'] = d['HORA_INICIO'][:5] if ini is not None else ''
    d['HORA_FIN'] = d['HORA_FIN'][:5] if fin is not None else ''
    d['DURACION_MIN'] = str(fin - ini) if ini is not None and fin is not None and fin > ini else ''
    if d['TIPO'] == 'PRECIO' and not d['IMPORTE']:
        errores.append('Indica el precio.')
    if d['IMPORTE']:
        try:
            d['IMPORTE'] = f"{float(d['IMPORTE'].replace('.', '').replace(',', '.') if ',' in d['IMPORTE'] else d['IMPORTE']):.2f}"
        except ValueError:
            errores.append('Importe no válido.')
    if d['FECHA'] and d['FECHA'] > ahora().strftime('%Y-%m-%d'):
        errores.append('No se puede registrar actividad realizada en una fecha futura.')
    return d, errores


def solapes(clave, d, excluir_id=''):
    """Actividades del mismo usuario y día cuyo horario se cruza con el de d."""
    ini, fin = _minutos(d['HORA_INICIO']), _minutos(d['HORA_FIN'])
    if ini is None or fin is None:
        return []
    resultado = []
    for r in listar([clave], d['FECHA'], d['FECHA']):
        if r['ID'] == excluir_id or r['ESTADO'] != REGISTRADA:
            continue
        a, b = _minutos(r['HORA_INICIO']), _minutos(r['HORA_FIN'])
        if a is not None and b is not None and a < fin and ini < b:
            resultado.append(r)
    return resultado


# ---------------------------------------------------------------- escritura
def _anotar(clave, usuario, id_registro, accion, cambios):
    meta, df = _leer(_nombre_historial(clave), COLUMNAS_HISTORIAL, usar_cache=False)
    antes = len(df)
    marca = ahora().strftime('%Y-%m-%d %H:%M:%S')
    filas = [{'FECHA_CAMBIO': marca, 'USUARIO': usuario, 'ID': id_registro, 'ACCION': accion,
              'CAMPO': campo, 'VALOR_ANTERIOR': a, 'VALOR_NUEVO': n} for campo, a, n in cambios] or \
            [{'FECHA_CAMBIO': marca, 'USUARIO': usuario, 'ID': id_registro, 'ACCION': accion,
              'CAMPO': '', 'VALOR_ANTERIOR': '', 'VALOR_NUEVO': ''}]
    df = pd.concat([df, pd.DataFrame(filas, columns=COLUMNAS_HISTORIAL)], ignore_index=True)
    _guardar(_nombre_historial(clave), meta, df, antes)


def crear(clave, usuario, d, estado=REGISTRADA, registro='MANUAL'):
    with _locks[clave]:
        meta, df = _leer(_nombre(clave), COLUMNAS, usar_cache=False)
        antes = len(df)
        momento = ahora()
        fila = {c: '' for c in COLUMNAS}
        fila.update(d)
        fila.update({
            'ID': f"{clave[:3].upper()}-{momento.strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2)}",
            'COMERCIAL': USUARIOS[clave]['nombre'], 'REGISTRO': registro, 'ESTADO': estado,
            'CREADO_POR': usuario, 'CREADO_EN': momento.strftime('%Y-%m-%d %H:%M:%S'),
        })
        df = pd.concat([df, pd.DataFrame([fila], columns=COLUMNAS)], ignore_index=True)
        _guardar(_nombre(clave), meta, df, antes)
        _anotar(clave, usuario, fila['ID'], 'ALTA', [])
        return fila


def actualizar(clave, usuario, id_registro, cambios, accion='MODIFICACIÓN'):
    """Aplica cambios {campo: valor} a un registro y anota en el historial cada campo cambiado."""
    with _locks[clave]:
        meta, df = _leer(_nombre(clave), COLUMNAS, usar_cache=False)
        antes = len(df)
        idx = df.index[df['ID'] == id_registro]
        if not len(idx):
            raise KeyError('Registro no encontrado.')
        i = idx[0]
        diferencias = []
        for campo, valor in cambios.items():
            anterior = str(df.at[i, campo] or '')
            if anterior != str(valor):
                diferencias.append((campo, anterior, str(valor)))
                df.at[i, campo] = str(valor)
        if not diferencias:
            return _registros(df.loc[[i]])[0]
        df.at[i, 'MODIFICADO_POR'] = usuario
        df.at[i, 'MODIFICADO_EN'] = ahora().strftime('%Y-%m-%d %H:%M:%S')
        _guardar(_nombre(clave), meta, df, antes)
        _anotar(clave, usuario, id_registro, accion, diferencias)
        return _registros(df.loc[[i]])[0]
