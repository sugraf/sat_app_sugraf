# logger_movimientos.py_prueba
import io
import json
import os
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

FOLDER_ID = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_raw)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def registrar_movimiento(tipo: str, datos: dict):
    try:
        drive = get_drive_service()
        
        query = f"'{FOLDER_ID}' in parents and name = 'movimientos.json' and trashed = false"
        res = drive.files().list(q=query, fields="files(id)").execute()
        archivos = res.get("files", [])

        registro = {"creacion": [], "borrado": [], "cierre": []}
        file_id = None

        if archivos:
            file_id = archivos[0]['id']
            request = drive.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            
            content = fh.getvalue().decode('utf-8').strip()
            if content:
                try:
                    data_leida = json.loads(content)
                    for k in registro:
                        if k in data_leida:
                            registro[k] = data_leida[k]
                except json.JSONDecodeError:
                    pass
        
        nuevo_movimiento = {
            "fecha_accion": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "datos": datos
        }
        
        if tipo in registro:
            registro[tipo].append(nuevo_movimiento)
            
            if len(registro[tipo]) > 50:
                registro[tipo] = registro[tipo][-50:]
        
        json_str = json.dumps(registro, indent=4, ensure_ascii=False)
        media = MediaIoBaseUpload(io.BytesIO(json_str.encode('utf-8')), mimetype='application/json')

        if file_id:
            drive.files().update(fileId=file_id, media_body=media).execute()
        else:
            drive.files().create(body={'name': 'movimientos.json', 'parents': [FOLDER_ID]}, media_body=media).execute()
            
    except Exception as e:
        print(f"Error logging to Drive: {e}")
