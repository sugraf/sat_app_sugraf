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

COLS_SIN = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'REALIZADO POR', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR']
COLS_TRA = COLS_SIN + ['FECHA REALIZACIÓN', 'HORA ENTRADA', 'HORA SALIDA', 'HORAS TOTALES', 'RESUELTO O PENDIENTE', 'PIEZAS NECESARIAS', 'SOLUCIÓN', 'ESTADO']

def get_drive_service():
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    return build("drive", "v3", credentials=service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    ))

def get_excel(drive, name, cols):
    query = f"'{FOLDER_ID}' in parents and name = '{name}' and trashed = false"
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
        
        for c in cols:
            if c not in df.columns: df[c] = ''
        return file_id, df
    else:
        return None, pd.DataFrame(columns=cols)

def save_excel(drive, file_id, name, df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
    media = MediaIoBaseUpload(io.BytesIO(output.getvalue()), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
    else:
        drive.files().create(body={'name': name, 'parents': [FOLDER_ID]}, media_body=media).execute()

class UpdateIndex(BaseModel):
    aviso_index: int
    fuente: str
    valor: str

@router.get("/avisos")
def listar():
    try:
        drive = get_drive_service()
        fid_sin, df_sin = get_excel(drive, 'Avisos Sin Tratar.xlsx', COLS_SIN)
        fid_tra, df_tra = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
        
        avisos_sin = df_sin.fillna("").to_dict(orient="records")
        for i, a in enumerate(avisos_sin):
            a['fuente'] = 'sin_tratar'
            a['original_index'] = i
            
        avisos_tra = df_tra.fillna("").to_dict(orient="records")
        for i, a in enumerate(avisos_tra):
            a['fuente'] = 'tratados'
            a['original_index'] = i

        # El organizador ve todo
        return {"avisos": avisos_sin + avisos_tra}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/actualizar-pirineos")
def update_pirineos(data: UpdateIndex):
    try:
        drive = get_drive_service()
        filename = 'Avisos Sin Tratar.xlsx' if data.fuente == 'sin_tratar' else 'Avisos Tratados.xlsx'
        cols = COLS_SIN if data.fuente == 'sin_tratar' else COLS_TRA
        
        fid, df = get_excel(drive, filename, cols)
        if 0 <= data.aviso_index < len(df):
            df.at[data.aviso_index, 'PIRINEOS'] = data.valor
            save_excel(drive, fid, filename, df)
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/actualizar-tecnico")
def update_tecnico(data: UpdateIndex):
    try:
        drive = get_drive_service()
        # Lógica de salto entre Excels
        if data.fuente == 'sin_tratar':
            fid_sin, df_sin = get_excel(drive, 'Avisos Sin Tratar.xlsx', COLS_SIN)
            if data.valor != 'Pendiente':
                # Mover a Tratados
                row = df_sin.iloc[data.aviso_index].copy()
                df_sin = df_sin.drop(index=data.aviso_index).reset_index(drop=True)
                save_excel(drive, fid_sin, 'Avisos Sin Tratar.xlsx', df_sin)
                
                fid_tra, df_tra = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
                row['REALIZADO POR'] = data.valor
                for c in COLS_TRA:
                    if c not in row: row[c] = ''
                row['ESTADO'] = 'Vacío'
                df_tra = pd.concat([df_tra, pd.DataFrame([row])], ignore_index=True)
                save_excel(drive, fid_tra, 'Avisos Tratados.xlsx', df_tra)
            else:
                df_sin.at[data.aviso_index, 'REALIZADO POR'] = data.valor
                save_excel(drive, fid_sin, 'Avisos Sin Tratar.xlsx', df_sin)
                
        else: # Viene de tratados
            fid_tra, df_tra = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
            if data.valor == 'Pendiente':
                # Devolver a Sin Tratar
                row = df_tra.iloc[data.aviso_index].copy()
                df_tra = df_tra.drop(index=data.aviso_index).reset_index(drop=True)
                save_excel(drive, fid_tra, 'Avisos Tratados.xlsx', df_tra)
                
                fid_sin, df_sin = get_excel(drive, 'Avisos Sin Tratar.xlsx', COLS_SIN)
                row_sin = {k: row.get(k, '') for k in COLS_SIN}
                row_sin['REALIZADO POR'] = 'Pendiente'
                df_sin = pd.concat([df_sin, pd.DataFrame([row_sin])], ignore_index=True)
                save_excel(drive, fid_sin, 'Avisos Sin Tratar.xlsx', df_sin)
            else:
                df_tra.at[data.aviso_index, 'REALIZADO POR'] = data.valor
                save_excel(drive, fid_tra, 'Avisos Tratados.xlsx', df_tra)

        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/eliminar-aviso")
def eliminar_aviso(data: UpdateIndex):
    try:
        drive = get_drive_service()
        filename = 'Avisos Sin Tratar.xlsx' if data.fuente == 'sin_tratar' else 'Avisos Tratados.xlsx'
        cols = COLS_SIN if data.fuente == 'sin_tratar' else COLS_TRA
        
        fid, df = get_excel(drive, filename, cols)
        if 0 <= data.aviso_index < len(df):
            df = df.drop(index=data.aviso_index).reset_index(drop=True)
            save_excel(drive, fid, filename, df)
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
