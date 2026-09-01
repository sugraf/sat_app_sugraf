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
import pandas as pd

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FOLDER_ID_DEFAULT = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

# --- ENTREGAR LA INTERFAZ ---
@app.get("/", response_class=HTMLResponse)
def home():
    ruta = os.path.join(os.path.dirname(__file__), "..", "index.html")
    if os.path.exists(ruta):
        with open(ruta, "r", encoding="utf-8") as f:
            return f.read()
    return "Error: index.html no encontrado."

@app.get("/{filename}.jpg")
def get_jpg(filename: str):
    ruta = os.path.join(os.path.dirname(__file__), "..", f"{filename}.jpg")
    if os.path.exists(ruta): return FileResponse(ruta, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Imagen no encontrada")

@app.get("/{filename}.png")
def get_png(filename: str):
    ruta = os.path.join(os.path.dirname(__file__), "..", f"{filename}.png")
    if os.path.exists(ruta): return FileResponse(ruta, media_type="image/png")
    raise HTTPException(status_code=404, detail="Imagen no encontrada")

@app.get("/manifest.json")
def get_manifest():
    return JSONResponse(content={
        "name": "Sugraf Digital Manager",
        "short_name": "Sugraf Hub",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#070e1c",
        "theme_color": "#ffffff",
        "icons": [{"src": "/logo_ejecutable.png", "sizes": "512x512", "type": "image/png"}]
    })

# --- CONEXIÓN GOOGLE DRIVE Y EXCEL ---
def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_raw)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def procesar_excel_avisos(drive, nuevo_aviso=None, borrar_n_parte=None):
    query = f"'{FOLDER_ID_DEFAULT}' in parents and name = 'Avisos Sin Tratar.xlsx' and trashed = false"
    res = drive.files().list(q=query, fields="files(id)").execute()
    archivos = res.get("files", [])
    
    cols = ['Nº PARTE', 'F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'TOTAL', 'REALIZADO POR', 'URG', 'GAR', 'MAN', 'INS']
    
    if archivos:
        file_id = archivos[0]['id']
        request = drive.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: downloader.next_chunk()
        fh.seek(0)
        df = pd.read_excel(fh)
    else:
        file_id = None
        df = pd.DataFrame(columns=cols)

    if nuevo_aviso:
        nuevo_df = pd.DataFrame([nuevo_aviso])
        df = pd.concat([df, nuevo_df], ignore_index=True)
        
    if borrar_n_parte:
        df = df[df['Nº PARTE'].astype(str) != str(borrar_n_parte)]
        
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
        
    media = MediaIoBaseUpload(io.BytesIO(output.getvalue()), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    
    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
    else:
        meta = {'name': 'Avisos Sin Tratar.xlsx', 'parents': [FOLDER_ID_DEFAULT]}
        drive.files().create(body=meta, media_body=media, fields='id').execute()
        
    return df.to_dict(orient="records")

# --- MODELOS ---
class NuevoAviso(BaseModel):
    n_parte: str
    fecha_entrada: str
    hora_entrada: str
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
@app.get("/api/clientes-maquinas")
def listar_clientes_maquinas():
    try:
        drive = get_drive_service()
        # Búsqueda con el nombre exacto sin espacios extra
        query = f"'{FOLDER_ID_DEFAULT}' in parents and name = 'EQUIPOS-FECHAS.xlsx' and trashed = false"
        res = drive.files().list(q=query, fields="files(id, name)").execute()
        archivos = res.get("files", [])
        
        if not archivos:
            return {"error": "No se encontró el archivo EQUIPOS-FECHAS.xlsx en Drive."}

        file_id = archivos[0]['id']
        request = drive.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: downloader.next_chunk()
        fh.seek(0)

        df = pd.read_excel(fh, header=4)
        clientes_map = {}
        
        for _, row in df.iterrows():
            c = str(row.get('CLIENTE', '')).strip()
            if not c or c.lower() == 'nan': continue
            
            n = str(row.get('NOMBRE', '')).strip()
            if not n or n.lower() == 'nan':
                e = str(row.get('EQUIPO', '')).strip()
                m = str(row.get('MARCA', '')).strip()
                mod = str(row.get('MODELO', '')).strip()
                n = f"{e} {m} {mod}".replace('nan', '').strip()
                
            if c not in clientes_map:
                clientes_map[c] = []
            if n and n not in clientes_map[c]:
                clientes_map[c].append(n)
                
        return clientes_map
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/avisos")
def listar_avisos():
    try:
        drive = get_drive_service()
        return {"status": "ok", "avisos": procesar_excel_avisos(drive)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/avisos")
def crear_aviso(aviso: NuevoAviso):
    try:
        drive = get_drive_service()
        fila_excel = {
            'Nº PARTE': aviso.n_parte,
            'F. ENTR.': f"{aviso.fecha_entrada} {aviso.hora_entrada}",
            'CLIENTE': aviso.cliente,
            'POBLACIÓN': aviso.poblacion,
            'MÁQUINA': aviso.maquina,
            'EQUIPO': '', 
            'MARCA': aviso.marca,
            'TOTAL': 0,
            'REALIZADO POR': aviso.realizado_por,
            'URG': 'SI' if aviso.urgente else 'NO',
            'GAR': 'SI' if aviso.garantia else 'NO',
            'MAN': 'SI' if aviso.mantenimiento else 'NO',
            'INS': 'SI' if aviso.instalacion else 'NO'
        }
        procesar_excel_avisos(drive, nuevo_aviso=fila_excel)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/guardar-parte")
def resolver_aviso(parte: ParteResolucion):
    try:
        drive = get_drive_service()
        contenido = (
            f"=== PARTE DE TRABAJO FINALIZADO ===\n"
            f"Nº AVISO ASOCIADO: {parte.n_parte}\n"
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
        procesar_excel_avisos(drive, borrar_n_parte=parte.n_parte)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
