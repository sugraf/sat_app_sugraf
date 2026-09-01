import io
import json
import os
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FOLDER_ID_DEFAULT = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

# --- ENTREGAR LA INTERFAZ HTML Y LAS IMÁGENES ---

@app.get("/", response_class=HTMLResponse)
def home():
    # Sirve el archivo index.html desde la raíz
    ruta = os.path.join(os.path.dirname(__file__), "..", "index.html")
    if os.path.exists(ruta):
        with open(ruta, "r", encoding="utf-8") as f:
            return f.read()
    return "Error: index.html no encontrado."

@app.get("/{filename}.jpg")
def get_jpg(filename: str):
    ruta = os.path.join(os.path.dirname(__file__), "..", f"{filename}.jpg")
    if os.path.exists(ruta):
        return FileResponse(ruta, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Imagen no encontrada")

@app.get("/{filename}.png")
def get_png(filename: str):
    ruta = os.path.join(os.path.dirname(__file__), "..", f"{filename}.png")
    if os.path.exists(ruta):
        return FileResponse(ruta, media_type="image/png")
    raise HTTPException(status_code=404, detail="Imagen no encontrada")

@app.get("/manifest.json")
def get_manifest():
    manifest_data = {
        "name": "Sugraf Digital Manager",
        "short_name": "Sugraf Hub",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#070e1c",
        "theme_color": "#ffffff",
        "icons": [{"src": "/logo_ejecutable.png", "sizes": "512x512", "type": "image/png"}]
    }
    return JSONResponse(content=manifest_data)

# --- LÓGICA DE GOOGLE DRIVE Y BASE DE DATOS JSON ---

def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_raw)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def get_db_file_id(drive):
    # Busca el archivo de base de datos en Drive
    query = f"'{FOLDER_ID_DEFAULT}' in parents and name = 'db_incidencias.json' and trashed = false"
    res = drive.files().list(q=query, fields="files(id, name)").execute()
    archivos = res.get("files", [])
    return archivos[0]["id"] if archivos else None

def read_db(drive):
    file_id = get_db_file_id(drive)
    if not file_id: return []
    request = drive.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done: downloader.next_chunk()
    fh.seek(0)
    try:
        return json.loads(fh.read().decode('utf-8'))
    except:
        return []

def write_db(drive, data):
    file_id = get_db_file_id(drive)
    media = MediaIoBaseUpload(io.BytesIO(json.dumps(data).encode('utf-8')), mimetype='application/json')
    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
    else:
        meta = {'name': 'db_incidencias.json', 'parents': [FOLDER_ID_DEFAULT]}
        drive.files().create(body=meta, media_body=media, fields='id').execute()

# --- MODELOS DE DATOS ---

class NuevaIncidencia(BaseModel):
    n_parte: str
    fecha_entrada: str
    cliente: str
    poblacion: str
    maquina: str
    marca: str
    realizado_por: str
    urgente: bool
    garantia: bool
    mantenimiento: bool
    instalacion: bool

class ParteResolucion(BaseModel):
    n_parte: str
    fecha: str
    hora: str
    tipo_visita: str
    cliente: str
    poblacion: str
    maquina: str
    tecnico: str
    solucion: str

# --- ENDPOINTS ---

@app.get("/api/incidencias")
def listar_incidencias():
    try:
        drive = get_drive_service()
        data = read_db(drive)
        return {"status": "ok", "incidencias": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/incidencias")
def crear_incidencia(inc: NuevaIncidencia):
    try:
        drive = get_drive_service()
        data = read_db(drive)
        data.append(inc.dict())
        write_db(drive, data)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/guardar-parte")
def resolver_incidencia(parte: ParteResolucion):
    try:
        drive = get_drive_service()
        
        # 1. Crear el Parte Final en TXT
        contenido = (
            f"=== PARTE DE TRABAJO FINALIZADO ===\n"
            f"Nº PARTE ASOCIADO: {parte.n_parte}\n"
            f"TÉCNICO:           {parte.tecnico}\n"
            f"FECHA INTERVENCIÓN:{parte.fecha} a las {parte.hora}\n"
            f"TIPO DE VISITA:    {parte.tipo_visita.upper()}\n"
            f"-----------------------------------\n"
            f"CLIENTE:   {parte.cliente}\n"
            f"DIRECCIÓN: {parte.poblacion}\n"
            f"MÁQUINA:   {parte.maquina}\n"
            f"-----------------------------------\n"
            f"TRABAJOS REALIZADOS / SOLUCIÓN:\n{parte.solucion}\n"
        )
        
        nombre_txt = f"ParteResuelto_{parte.cliente.replace(' ', '_')}_{parte.n_parte}.txt"
        meta = {"name": nombre_txt, "parents": [FOLDER_ID_DEFAULT]}
        media = MediaIoBaseUpload(io.BytesIO(contenido.encode("utf-8")), mimetype="text/plain")
        drive.files().create(body=meta, media_body=media).execute()

        # 2. Borrar la incidencia de la Base de Datos (Ya está resuelta)
        data = read_db(drive)
        data = [i for i in data if i.get("n_parte") != parte.n_parte]
        write_db(drive, data)

        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
