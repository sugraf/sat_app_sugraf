# secretos.py
"""Contraseñas de la aplicación, leídas de Google Drive.

Están en el fichero "contraseñas.txt" de la carpeta Media de Drive, con líneas
del tipo `NOMBRE = valor` (las que empiezan por # son comentarios). Así no
aparecen ni en el código de GitHub ni en Vercel; para cambiar una basta con
editar ese fichero. Se releen como mucho cada 10 minutos.

Si existe una variable de entorno con el mismo nombre, tiene prioridad (útil
para pruebas en local).
"""
import io
import json
import os
import threading
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

FOLDER_ID = "17qgeZik9VsH4hH0LxcAlZRTL8-80kL8h"  # Mi unidad/Media
NOMBRE_FICHERO = "contraseñas.txt"
SEGUNDOS_CACHE = 600

_lock = threading.Lock()
_cache = {'valores': None, 'leido': 0.0}


def _descargar_fichero():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON")), scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    archivos = drive.files().list(
        q=f"'{FOLDER_ID}' in parents and name = '{NOMBRE_FICHERO}' and trashed = false",
        fields="files(id, mimeType)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    if not archivos:
        raise RuntimeError(
            f"No se encuentra '{NOMBRE_FICHERO}' en la carpeta Media de Drive. "
            "Comprueba que la carpeta está compartida con la cuenta de servicio de la app."
        )
    archivo = archivos[0]
    if archivo.get('mimeType') == 'application/vnd.google-apps.document':
        peticion = drive.files().export_media(fileId=archivo['id'], mimeType='text/plain')
    else:
        peticion = drive.files().get_media(fileId=archivo['id'], supportsAllDrives=True)
    fh = io.BytesIO()
    descarga = MediaIoBaseDownload(fh, peticion)
    terminado = False
    while not terminado:
        _, terminado = descarga.next_chunk()
    return fh.getvalue().decode('utf-8-sig')


def _leer_valores(texto):
    valores = {}
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith('#') or '=' not in linea:
            continue
        nombre, valor = linea.split('=', 1)
        valores[nombre.strip().upper()] = valor.strip()
    return valores


def obtener(nombre):
    """Devuelve la contraseña `nombre` (p. ej. 'OPENAI_API_KEY')."""
    if os.environ.get(nombre):
        return os.environ[nombre]
    with _lock:
        if _cache['valores'] is None or time.time() - _cache['leido'] > SEGUNDOS_CACHE:
            _cache['valores'] = _leer_valores(_descargar_fichero())
            _cache['leido'] = time.time()
        valores = _cache['valores']
    if not valores.get(nombre):
        raise RuntimeError(f"Falta la línea '{nombre} = ...' en {NOMBRE_FICHERO} de la carpeta Media de Drive.")
    return valores[nombre]
