import io
import json
import os
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_raw:
        raise HTTPException(
            status_code=500,
            detail="Variable GOOGLE_CREDENTIALS_JSON no configurada en Vercel."
        )
    try:
        creds_dict = json.loads(creds_raw)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error con credenciales: {str(e)}")

class ParteTrabajo(BaseModel):
    cliente: str
    tecnico: str
    telefono: str
    estado: str
    averia: str
    solucion: str
    folder_id: str

@app.post("/api/guardar-parte")
@app.post("/guardar-parte")
def guardar_parte(parte: ParteTrabajo):
    try:
        drive = get_drive_service()
        fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        contenido = (
            f"=========================================\n"
            f"       PARTE DE SERVICIO TÉCNICO         \n"
            f"=========================================\n"
            f"Fecha de registro: {fecha_actual}\n"
            f"Estado:            {parte.estado.upper()}\n"
            f"Técnico asignado:  {parte.tecnico}\n"
            f"-----------------------------------------\n"
            f"DATOS DEL CLIENTE\n"
            f"Nombre / Empresa:  {parte.cliente}\n"
            f"Teléfono contacto: {parte.telefono}\n"
            f"-----------------------------------------\n"
            f"DESCRIPCIÓN DE LA AVERÍA:\n"
            f"{parte.averia}\n"
            f"-----------------------------------------\n"
            f"TRABAJO REALIZADO / SOLUCIÓN:\n"
            f"{parte.solucion}\n"
            f"=========================================\n"
        )

        nombre_archivo = f"Parte_{parte.cliente.strip().replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

        file_metadata = {
            "name": nombre_archivo,
            "parents": [parte.folder_id]
        }

        media = MediaIoBaseUpload(
            io.BytesIO(contenido.encode("utf-8")),
            mimetype="text/plain"
        )

        archivo = drive.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name"
        ).execute()

        return {"status": "ok", "archivo_id": archivo.get("id"), "nombre": archivo.get("name")}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/listar-partes")
@app.get("/listar-partes")
def listar_partes(folder_id: str):
    try:
        drive = get_drive_service()
        query = f"'{folder_id}' in parents and trashed = false"
        results = drive.files().list(
            q=query,
            fields="files(id, name, createdTime)",
            orderBy="createdTime desc",
            pageSize=20
        ).execute()

        return {"status": "ok", "archivos": results.get("files", [])}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
