# comercial/almacen.py
"""Acceso a los ficheros del módulo Comercial en Google Drive.

- Datos de Pirineos (solo lectura): Clientes.xlsx, PreciosCliente.xlsx, Articulos.xlsx y
  MovimientosVenta*.xlsx. Se buscan por nombre entre lo que la cuenta de servicio de la
  app puede ver (primero dentro de la carpeta Pirineos).
- Datos propios del módulo (lectura y escritura): carpeta "Comerciales" dentro de la
  carpeta SAT-App de Drive (la misma que usa el SAT).

IMPORTANTE: la cuenta de servicio de la app no tiene espacio propio en Drive, así que NO
puede crear ficheros nuevos en «Mi unidad», solo modificar los que ya existen. Por eso los
ficheros de la carpeta Comerciales (Actividad_<usuario>.xlsx, Historial_<usuario>.xlsx,
Oportunidades_SAT.json, geo_codigos_postales.json) se crean a mano una vez (por una persona)
y la app solo los actualiza. Si se añade un usuario nuevo, hay que crear sus dos Excel.

Para pruebas en local se puede definir COMERCIAL_LOCAL_DIR: entonces todo se lee y se
escribe en esa carpeta del disco (Pirineos dentro de <dir>/Pirineos y los datos propios
en <dir>/Comerciales) y no se usa Drive.
"""
import io
import json
import os
import threading
import time

SAT_FOLDER_ID = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"        # Mi unidad/SAT-App
ARTICULOS_FOLDER_ID = "1uAaJfCxrumjT07fB6ezWHyronOokEl_r"  # Mi unidad/Pirineos/Articulos
NOMBRE_CARPETA_COMERCIAL = "Comerciales"
MIME_CARPETA = 'application/vnd.google-apps.folder'
MIME_SHEETS = 'application/vnd.google-apps.spreadsheet'
MIME_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

_lock = threading.Lock()
_ids = {}  # caché de ids de carpetas: nombre lógico -> id


def correo_cuenta_servicio():
    try:
        return json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON") or '{}').get('client_email', '')
    except Exception:
        return ''


def _local_dir():
    return os.environ.get("COMERCIAL_LOCAL_DIR") or ''


def _drive():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON")), scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _escapar(nombre):
    return nombre.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------- lectura Pirineos
def _carpeta_pirineos(drive):
    """Id de la carpeta Pirineos (padre de Pirineos/Articulos), o None si no se ve."""
    if 'pirineos' in _ids:
        return _ids['pirineos']
    try:
        meta = drive.files().get(fileId=ARTICULOS_FOLDER_ID, fields='parents', supportsAllDrives=True).execute()
        padre = (meta.get('parents') or [None])[0]
    except Exception:
        padre = None
    _ids['pirineos'] = padre
    return padre


def buscar_pirineos(nombre, subcarpeta=None):
    """Metadatos {id, name, mimeType, modifiedTime} de un fichero de Pirineos, o None.

    Busca primero en Pirineos/<subcarpeta> y, si la cuenta no ve esa carpeta, en todo lo
    que tenga compartido (por nombre exacto)."""
    base = _local_dir()
    if base:
        ruta = os.path.join(base, 'Pirineos', subcarpeta or '', nombre)
        if not os.path.exists(ruta):
            return None
        return {'id': ruta, 'name': nombre, 'mimeType': MIME_XLSX, 'modifiedTime': str(os.path.getmtime(ruta)), 'local': True}

    drive = _drive()
    campos = 'files(id, name, mimeType, modifiedTime, parents)'
    pirineos = _carpeta_pirineos(drive)
    if pirineos and subcarpeta:
        clave = f'pirineos/{subcarpeta}'
        if clave not in _ids:
            r = drive.files().list(
                q=f"'{pirineos}' in parents and name = '{_escapar(subcarpeta)}' and mimeType = '{MIME_CARPETA}' and trashed = false",
                fields='files(id)', supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute().get('files', [])
            _ids[clave] = r[0]['id'] if r else None
        if _ids[clave]:
            r = drive.files().list(
                q=f"'{_ids[clave]}' in parents and name = '{_escapar(nombre)}' and trashed = false",
                fields=campos, orderBy='modifiedTime desc', supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute().get('files', [])
            if r:
                return r[0]
    r = drive.files().list(
        q=f"name = '{_escapar(nombre)}' and trashed = false and mimeType != '{MIME_CARPETA}'",
        fields=campos, orderBy='modifiedTime desc', supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get('files', [])
    return r[0] if r else None


def descargar(meta):
    """Contenido (bytes) de un fichero devuelto por buscar_*()."""
    if meta.get('local'):
        with open(meta['id'], 'rb') as f:
            return f.read()
    from googleapiclient.http import MediaIoBaseDownload
    drive = _drive()
    if meta.get('mimeType') == MIME_SHEETS:
        peticion = drive.files().export_media(fileId=meta['id'], mimeType=MIME_XLSX)
    else:
        peticion = drive.files().get_media(fileId=meta['id'], supportsAllDrives=True)
    fh = io.BytesIO()
    descarga = MediaIoBaseDownload(fh, peticion, chunksize=20 * 1024 * 1024)
    terminado = False
    while not terminado:
        _, terminado = descarga.next_chunk()
    return fh.getvalue()


# ---------------------------------------------------------------- datos propios
def _carpeta_comercial(drive):
    """Id de la carpeta SAT-App/Comerciales. Si hay varias con ese nombre, la que tiene
    ficheros (y entre ellas la más antigua), para que siempre sea la misma."""
    with _lock:
        if _ids.get('comercial'):
            return _ids['comercial']
        carpetas = drive.files().list(
            q=f"'{SAT_FOLDER_ID}' in parents and name = '{NOMBRE_CARPETA_COMERCIAL}' and mimeType = '{MIME_CARPETA}' and trashed = false",
            fields='files(id, createdTime)', orderBy='createdTime', supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute().get('files', [])
        if not carpetas:
            raise RuntimeError("No existe la carpeta SAT-App/Comerciales en Drive. Créala (con sus ficheros) desde tu cuenta.")
        for c in carpetas:
            hay = drive.files().list(q=f"'{c['id']}' in parents and trashed = false", fields='files(id)', pageSize=1,
                                     supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get('files', [])
            if hay:
                _ids['comercial'] = c['id']
                return c['id']
        return carpetas[0]['id']  # todas vacías: no se guarda, para volver a mirar en la próxima llamada


def buscar_propio(nombre):
    """Metadatos de un fichero de la carpeta Comerciales, o None si aún no existe."""
    base = _local_dir()
    if base:
        ruta = os.path.join(base, NOMBRE_CARPETA_COMERCIAL, nombre)
        if not os.path.exists(ruta):
            return None
        return {'id': ruta, 'name': nombre, 'mimeType': MIME_XLSX, 'modifiedTime': str(os.path.getmtime(ruta)), 'local': True}
    drive = _drive()
    carpeta = _carpeta_comercial(drive)
    r = drive.files().list(
        q=f"'{carpeta}' in parents and name = '{_escapar(nombre)}' and trashed = false",
        fields='files(id, name, mimeType, modifiedTime)', orderBy='modifiedTime desc',
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get('files', [])
    return r[0] if r else None


def guardar_propio(nombre, contenido, meta=None, mimetype=MIME_XLSX):
    """Crea o reemplaza un fichero de la carpeta Comerciales."""
    base = _local_dir()
    if base:
        carpeta = os.path.join(base, NOMBRE_CARPETA_COMERCIAL)
        os.makedirs(carpeta, exist_ok=True)
        tmp = os.path.join(carpeta, f'.{nombre}.{time.time_ns()}.tmp')
        with open(tmp, 'wb') as f:
            f.write(contenido)
        os.replace(tmp, os.path.join(carpeta, nombre))
        return
    from googleapiclient.http import MediaIoBaseUpload
    drive = _drive()
    media = MediaIoBaseUpload(io.BytesIO(contenido), mimetype=mimetype, resumable=False)
    if meta and not meta.get('local'):
        drive.files().update(fileId=meta['id'], media_body=media, supportsAllDrives=True).execute()
    else:
        try:
            drive.files().create(
                body={'name': nombre, 'parents': [_carpeta_comercial(drive)]}, media_body=media, supportsAllDrives=True,
            ).execute()
        except Exception as e:
            if 'storageQuotaExceeded' in str(e) or 'storage quota' in str(e).lower():
                raise RuntimeError(
                    f"Falta el fichero '{nombre}' en la carpeta SAT-App/Comerciales de Drive y la app no puede crearlo "
                    "(la cuenta de la app no tiene espacio propio en Drive). Hay que crearlo una vez desde la cuenta de Sugraf."
                ) from e
            raise


# ---------------------------------------------------------------- ficheros del SAT (solo lectura)
def buscar_sat(nombre):
    """Metadatos de un fichero de la carpeta SAT-App (avisos y partes), o None."""
    base = _local_dir()
    if base:
        ruta = os.path.join(base, 'SAT-App', nombre)
        if not os.path.exists(ruta):
            return None
        return {'id': ruta, 'name': nombre, 'mimeType': MIME_XLSX, 'modifiedTime': str(os.path.getmtime(ruta)), 'local': True}
    drive = _drive()
    r = drive.files().list(
        q=f"'{SAT_FOLDER_ID}' in parents and name = '{_escapar(nombre)}' and trashed = false",
        fields='files(id, name, mimeType, modifiedTime)', orderBy='modifiedTime desc',
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get('files', [])
    return r[0] if r else None


# ---------------------------------------------------------------- ficheros compartidos (p. ej. ofertas del escritorio)
def buscar_compartido(nombre):
    """Metadatos de un fichero que la cuenta de la app ve compartido (por nombre exacto), o None.
    En pruebas locales: <COMERCIAL_LOCAL_DIR>/Ofertas/<nombre>."""
    base = _local_dir()
    if base:
        ruta = os.path.join(base, 'Ofertas', nombre)
        if not os.path.exists(ruta):
            return None
        return {'id': ruta, 'name': nombre, 'mimeType': MIME_XLSX, 'modifiedTime': str(os.path.getmtime(ruta)), 'local': True}
    drive = _drive()
    r = drive.files().list(
        q=f"name = '{_escapar(nombre)}' and trashed = false and mimeType != '{MIME_CARPETA}'",
        fields='files(id, name, mimeType, modifiedTime)', orderBy='modifiedTime desc',
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get('files', [])
    return r[0] if r else None


# ---------------------------------------------------------------- JSON propios (cachés, estados)
def leer_json_propio(nombre, defecto=None):
    meta = buscar_propio(nombre)
    if meta is None:
        return None, defecto
    try:
        return meta, json.loads(descargar(meta).decode('utf-8'))
    except Exception:
        return meta, defecto


def guardar_json_propio(nombre, datos, meta=None):
    guardar_propio(nombre, json.dumps(datos, ensure_ascii=False).encode('utf-8'), meta, mimetype='application/json')
