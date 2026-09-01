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

def get_excel_avisos(drive):
    query = f"'{FOLDER_ID}' in parents and name = 'Avisos Sin Tratar.xlsx' and trashed = false"
    res = drive.files().list(q=query, fields="files(id)").execute()
    archivos = res.get("files", [])
    if not archivos: return None, pd.DataFrame()
    
    file_id = archivos[0]['id']
    req = drive.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done: _, done = downloader.next_chunk()
    fh.seek(0)
    df = pd.read_excel(fh)
    
    if 'PIRINEOS' not in df.columns: df['PIRINEOS'] = 'NO'
    if 'REALIZADO POR' not in df.columns: df['REALIZADO POR'] = 'Pendiente'
    df['PIRINEOS'] = df['PIRINEOS'].replace('', 'NO').fillna('NO')
    df['REALIZADO POR'] = df['REALIZADO POR'].replace('', 'Pendiente').fillna('Pendiente')
    return file_id, df

def save_excel(drive, file_id, df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
    media = MediaIoBaseUpload(io.BytesIO(output.getvalue()), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    drive.files().update(fileId=file_id, media_body=media).execute()

class UpdateIndex(BaseModel):
    aviso_index: int
    valor: str

@router.get("/avisos")
def listar():
    try:
        drive = get_drive_service()
        _, df = get_excel_avisos(drive)
        return {"avisos": df.fillna("").to_dict(orient="records")}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/actualizar-pirineos")
def update_pirineos(data: UpdateIndex):
    try:
        drive = get_drive_service()
        file_id, df = get_excel_avisos(drive)
        if 0 <= data.aviso_index < len(df):
            df.at[data.aviso_index, 'PIRINEOS'] = data.valor
            save_excel(drive, file_id, df)
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/actualizar-tecnico")
def update_tecnico(data: UpdateIndex):
    try:
        drive = get_drive_service()
        file_id, df = get_excel_avisos(drive)
        if 0 <= data.aviso_index < len(df):
            df.at[data.aviso_index, 'REALIZADO POR'] = data.valor
            save_excel(drive, file_id, df)
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
