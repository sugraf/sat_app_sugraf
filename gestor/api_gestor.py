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

COLS_SIN = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'ASIGNADO A', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR', 'OBSERVACIONES']
COLS_TRA = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'ASIGNADO A', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR', 'TIPO ASISTENCIA', 'FECHA REALIZACIÓN', 'HORA ENTRADA', 'HORA SALIDA', 'HORAS TOTALES', 'RESUELTO O PENDIENTE', 'PIEZAS NECESARIAS', 'SOLUCIÓN', 'ESTADO', 'OPCIÓN A VENTA', 'DETALLE VENTA', 'ESTADO PIEZAS', 'OBSERVACIONES']

def get_drive_service():
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    return build("drive", "v3", credentials=service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    ))

def get_excel(drive, name, cols):
    query = f"'{FOLDER_ID}' in parents and name = '{name}' and trashed = false"
    res = drive.files().list(q=query, fields="files(id, mimeType)").execute()
    if res.get("files"):
        file_info = res.get("files")[0]
        file_id = file_info['id']
        mime_type = file_info.get('mimeType', '')
        
        try:
            if mime_type == 'application/vnd.google-apps.spreadsheet':
                req = drive.files().export_media(fileId=file_id, mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            else:
                req = drive.files().get_media(fileId=file_id)
            
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, req)
            done = False
            while not done: _, done = downloader.next_chunk()
            fh.seek(0)
            df = pd.read_excel(fh, dtype=object)
        except Exception:
            df = pd.DataFrame(columns=cols, dtype=object)
            
        for c in cols:
            if c not in df.columns: df[c] = ''
        return file_id, df
    else:
        return None, pd.DataFrame(columns=cols, dtype=object)

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

class EditarAviso(BaseModel):
    aviso_index: int
    fuente: str
    datos: dict

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
            df.loc[data.aviso_index, 'PIRINEOS'] = data.valor
            save_excel(drive, fid, filename, df)
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/actualizar-tecnico")
def update_tecnico(data: UpdateIndex):
    try:
        drive = get_drive_service()
        if data.fuente == 'sin_tratar':
            fid_sin, df_sin = get_excel(drive, 'Avisos Sin Tratar.xlsx', COLS_SIN)
            
            if data.aviso_index >= len(df_sin) or data.aviso_index < 0:
                raise HTTPException(status_code=400, detail="El aviso ya se ha movido o no existe. Refresca la vista.")
                
            if data.valor != 'Pendiente':
                row = df_sin.iloc[data.aviso_index].copy()
                fid_tra, df_tra = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
                
                is_dup = False
                c = str(row.get('CLIENTE', ''))
                m = str(row.get('MÁQUINA', ''))
                f = str(row.get('F. ENTR.', ''))
                for _, t_row in df_tra.iterrows():
                    if str(t_row.get('CLIENTE', '')) == c and str(t_row.get('MÁQUINA', '')) == m and str(t_row.get('F. ENTR.', '')) == f:
                        is_dup = True
                        break
                        
                if not is_dup:
                    row_dict = row.to_dict()
                    for col in COLS_TRA:
                        if col not in row_dict: row_dict[col] = ''
                    row_dict['ASIGNADO A'] = data.valor
                    row_dict['ESTADO'] = 'Vacío'
                    df_tra = pd.concat([df_tra, pd.DataFrame([row_dict])], ignore_index=True)
                    save_excel(drive, fid_tra, 'Avisos Tratados.xlsx', df_tra)
                
                df_sin = df_sin.drop(index=data.aviso_index).reset_index(drop=True)
                save_excel(drive, fid_sin, 'Avisos Sin Tratar.xlsx', df_sin)
            else:
                df_sin.loc[data.aviso_index, 'ASIGNADO A'] = data.valor
                save_excel(drive, fid_sin, 'Avisos Sin Tratar.xlsx', df_sin)
                
        else: 
            fid_tra, df_tra = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
            
            if data.aviso_index >= len(df_tra) or data.aviso_index < 0:
                raise HTTPException(status_code=400, detail="El aviso ya se ha movido o no existe. Refresca la vista.")
                
            row = df_tra.iloc[data.aviso_index].copy()
            
            estado = str(row.get('ESTADO', '')).strip()
            if estado in ['Abierto', 'Cerrado'] and data.valor != row.get('ASIGNADO A', ''):
                raise HTTPException(status_code=400, detail="No se puede quitar ni reasignar un aviso que ya ha sido empezado o finalizado por un técnico.")

            if data.valor == 'Pendiente':
                fid_sin, df_sin = get_excel(drive, 'Avisos Sin Tratar.xlsx', COLS_SIN)
                row_sin = {k: row.get(k, '') for k in COLS_SIN}
                row_sin['ASIGNADO A'] = 'Pendiente'
                
                is_dup = False
                c = str(row_sin.get('CLIENTE', ''))
                m = str(row_sin.get('MÁQUINA', ''))
                f = str(row_sin.get('F. ENTR.', ''))
                for _, s_row in df_sin.iterrows():
                    if str(s_row.get('CLIENTE', '')) == c and str(s_row.get('MÁQUINA', '')) == m and str(s_row.get('F. ENTR.', '')) == f:
                        is_dup = True
                        break
                        
                if not is_dup:
                    df_sin = pd.concat([df_sin, pd.DataFrame([row_sin])], ignore_index=True)
                    save_excel(drive, fid_sin, 'Avisos Sin Tratar.xlsx', df_sin)

                df_tra = df_tra.drop(index=data.aviso_index).reset_index(drop=True)
                save_excel(drive, fid_tra, 'Avisos Tratados.xlsx', df_tra)
            else:
                df_tra.loc[data.aviso_index, 'ASIGNADO A'] = data.valor
                save_excel(drive, fid_tra, 'Avisos Tratados.xlsx', df_tra)

        return {"status": "ok"}
    except HTTPException as he:
        raise he
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/editar-aviso")
def editar_aviso(data: EditarAviso):
    try:
        drive = get_drive_service()
        filename = 'Avisos Sin Tratar.xlsx' if data.fuente == 'sin_tratar' else 'Avisos Tratados.xlsx'
        cols = COLS_SIN if data.fuente == 'sin_tratar' else COLS_TRA
        
        fid, df = get_excel(drive, filename, cols)
        if 0 <= data.aviso_index < len(df):
            if data.fuente == 'tratados':
                estado = str(df.loc[data.aviso_index].get('ESTADO', '')).strip()
                if estado in ['Abierto', 'Cerrado']:
                    raise HTTPException(status_code=400, detail="No se puede modificar un aviso que ya ha sido empezado o cerrado por un técnico.")
            
            for k, v in data.datos.items():
                if k in df.columns:
                    df.loc[data.aviso_index, k] = v
                    
            save_excel(drive, fid, filename, df)
        return {"status": "ok"}
    except HTTPException as he:
        raise he
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/eliminar-aviso")
def eliminar_aviso(data: UpdateIndex):
    try:
        drive = get_drive_service()
        filename = 'Avisos Sin Tratar.xlsx' if data.fuente == 'sin_tratar' else 'Avisos Tratados.xlsx'
        cols = COLS_SIN if data.fuente == 'sin_tratar' else COLS_TRA
        
        fid, df = get_excel(drive, filename, cols)
        if 0 <= data.aviso_index < len(df):
            if data.fuente == 'tratados':
                row = df.iloc[data.aviso_index]
                estado = str(row.get('ESTADO', '')).strip()
                if estado in ['Abierto', 'Cerrado']:
                    raise HTTPException(status_code=400, detail="No se puede eliminar un aviso que ya ha sido empezado o cerrado. Ciérralo y archívalo.")

            df = df.drop(index=data.aviso_index).reset_index(drop=True)
            save_excel(drive, fid, filename, df)
        return {"status": "ok"}
    except HTTPException as he:
        raise he
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))
