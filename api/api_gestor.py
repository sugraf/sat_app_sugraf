import io
import json
import os
from fastapi import APIRouter, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from pydantic import BaseModel
import pandas as pd

router = APIRouter()

FOLDER_ID_DEFAULT = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_raw)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def procesar_excel_avisos_gestor(drive, borrar_n_parte=None, actualizar_pirineos_n_parte=None, nuevo_valor_pirineos=None, actualizar_tecnico_n_parte=None, nuevo_tecnico=None):
    query = f"'{FOLDER_ID_DEFAULT}' in parents and name = 'Avisos Sin Tratar.xlsx' and trashed = false"
    res = drive.files().list(q=query, fields="files(id)").execute()
    archivos = res.get("files", [])
    
    cols = ['Nº PARTE', 'F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'REALIZADO POR', 'URGENTE', 'PIRINEOS']
    
    if archivos:
        file_id = archivos[0]['id']
        request = drive.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: 
            status, done = downloader.next_chunk()
        fh.seek(0)
        df = pd.read_excel(fh)
    else:
        file_id = None
        df = pd.DataFrame(columns=cols)

    if 'PIRINEOS' not in df.columns:
        df['PIRINEOS'] = 'NO'
    df['PIRINEOS'] = df['PIRINEOS'].replace('', 'NO').fillna('NO')
    
    if 'REALIZADO POR' not in df.columns:
        df['REALIZADO POR'] = 'Pendiente'
    df['REALIZADO POR'] = df['REALIZADO POR'].replace('', 'Pendiente').fillna('Pendiente')
        
    if borrar_n_parte:
        df = df[df['Nº PARTE'].astype(str) != str(borrar_n_parte)]
        
    if actualizar_pirineos_n_parte:
        df.loc[df['Nº PARTE'].astype(str) == str(actualizar_pirineos_n_parte), 'PIRINEOS'] = nuevo_valor_pirineos

    if actualizar_tecnico_n_parte:
        df.loc[df['Nº PARTE'].astype(str) == str(actualizar_tecnico_n_parte), 'REALIZADO POR'] = nuevo_tecnico

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
        
    media = MediaIoBaseUpload(io.BytesIO(output.getvalue()), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    
    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
    else:
        meta = {'name': 'Avisos Sin Tratar.xlsx', 'parents': [FOLDER_ID_DEFAULT]}
        drive.files().create(body=meta, media_body=media, fields='id').execute()
        
    return df.fillna("").to_dict(orient="records")

class ActualizarPirineos(BaseModel):
    n_parte: str
    pirineos: str

class ActualizarTecnico(BaseModel):
    n_parte: str
    tecnico: str

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

@router.get("/avisos")
def listar_avisos():
    try:
        drive = get_drive_service()
        return {"status": "ok", "avisos": procesar_excel_avisos_gestor(drive)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/actualizar-pirineos")
def actualizar_pirineos(datos: ActualizarPirineos):
    try:
        drive = get_drive_service()
        procesar_excel_avisos_gestor(drive, actualizar_pirineos_n_parte=datos.n_parte, nuevo_valor_pirineos=datos.pirineos)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/actualizar-tecnico")
def actualizar_tecnico(datos: ActualizarTecnico):
    try:
        drive = get_drive_service()
        procesar_excel_avisos_gestor(drive, actualizar_tecnico_n_parte=datos.n_parte, nuevo_tecnico=datos.tecnico)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/guardar-parte")
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
        procesar_excel_avisos_gestor(drive, borrar_n_parte=parte.n_parte)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
