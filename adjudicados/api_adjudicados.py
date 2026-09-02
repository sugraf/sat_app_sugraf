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
FOLDER_ID = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

def get_drive_service():
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    return build("drive", "v3", credentials=service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    ))

class ParteResolucion(BaseModel):
    aviso_index: int
    fecha: str
    hora: str
    tipo_visita: str
    cliente: str
    poblacion: str
    maquina: str
    tecnico: str
    solucion: str

@router.get("/mis-avisos")
def get_mis_avisos(tecnico: str):
    try:
        drive = get_drive_service()
        query = f"'{FOLDER_ID}' in parents and name = 'Avisos Sin Tratar.xlsx' and trashed = false"
        res = drive.files().list(q=query, fields="files(id)").execute()
        if not res.get("files"): return {"avisos": []}
        
        req = drive.files().get_media(fileId=res.get("files")[0]['id'])
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done: _, done = downloader.next_chunk()
        fh.seek(0)
        df = pd.read_excel(fh)
        
        if 'REALIZADO POR' not in df.columns: return {"avisos": []}
        df['REALIZADO POR'] = df['REALIZADO POR'].fillna('Pendiente')
        
        avisos_mios = []
        for index, row in df.iterrows():
            asignado = str(row['REALIZADO POR'])
            # Si el usuario es master, se trae a todos los que ya estén asignados a algún técnico
            if tecnico == 'master':
                if asignado != 'Pendiente' and asignado != 'nan' and asignado != '':
                    dic = row.fillna("").to_dict()
                    dic['aviso_index'] = index
                    avisos_mios.append(dic)
            else:
                if asignado == tecnico:
                    dic = row.fillna("").to_dict()
                    dic['aviso_index'] = index
                    avisos_mios.append(dic)
                
        return {"avisos": avisos_mios}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/resolver")
def resolver_aviso(parte: ParteResolucion):
    try:
        drive = get_drive_service()
        
        # Generar TXT
        contenido = (
            f"=== PARTE DE TRABAJO FINALIZADO ===\n"
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
        nombre_txt = f"ParteResuelto_{parte.cliente.replace(' ', '_')}.txt"
        media_txt = MediaIoBaseUpload(io.BytesIO(contenido.encode("utf-8")), mimetype="text/plain")
        drive.files().create(body={"name": nombre_txt, "parents": [FOLDER_ID]}, media_body=media_txt).execute()
        
        # Borrar fila Excel
        query = f"'{FOLDER_ID}' in parents and name = 'Avisos Sin Tratar.xlsx' and trashed = false"
        res = drive.files().list(q=query, fields="files(id)").execute()
        if res.get("files"):
            file_id = res.get("files")[0]['id']
            req = drive.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, req)
            done = False
            while not done: _, done = downloader.next_chunk()
            fh.seek(0)
            df = pd.read_excel(fh)
            
            df = df.drop(index=parte.aviso_index).reset_index(drop=True)
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
            media_xls = MediaIoBaseUpload(io.BytesIO(output.getvalue()), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            drive.files().update(fileId=file_id, media_body=media_xls).execute()
            
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
