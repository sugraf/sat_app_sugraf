# control/api_control.py
import io
import json
import os
import traceback
from fastapi import APIRouter, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from pydantic import BaseModel
import pandas as pd
from openai import OpenAI

router = APIRouter()
FOLDER_ID = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"
GROQ_API_KEY = "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"

COLS_SIN = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'REALIZADO POR', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR']
COLS_TRA = COLS_SIN + ['FECHA REALIZACIÓN', 'HORA ENTRADA', 'HORA SALIDA', 'HORAS TOTALES', 'RESUELTO O PENDIENTE', 'PIEZAS NECESARIAS', 'SOLUCIÓN', 'ESTADO', 'TIPO ASISTENCIA']

client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

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
        return df
    else:
        return pd.DataFrame(columns=cols, dtype=object)

class ChatRequest(BaseModel):
    query: str
    mode: str

@router.post("/chat")
def chat_ia(req: ChatRequest):
    try:
        drive = get_drive_service()
        
        if req.mode == "abiertos":
            df_sin = get_excel(drive, 'Avisos Sin Tratar.xlsx', COLS_SIN)
            df_tra = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
            df_combined = pd.concat([df_sin, df_tra], ignore_index=True)
            df_combined = df_combined.fillna("")
            context_text = df_combined.to_csv(index=False)
        else:
            df_fin = get_excel(drive, 'Partes Finalizados.xlsx', COLS_TRA)
            df_fin = df_fin.fillna("")
            context_text = df_fin.to_csv(index=False)
        
        if len(context_text) > 20000:
            context_text = context_text[:20000] + "\n...[TRUNCATED]"

        system_prompt = (
            "You are an expert technical service manager assistant. "
            "Answer the user's question using ONLY the provided context data. "
            "When routing or assigning technicians to locations, NEVER prioritize older tickets or give preference based on antiquity. "
            "Answer in the same language the user asks in."
        )
        
        user_prompt = f"Context Data (CSV Format):\n{context_text}\n\nQuestion: {req.query}"
        
        response = client.chat.completions.create(
            model="llama3-8b-8192", 
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1
        )
        
        return {"reply": response.choices[0].message.content}
    except Exception as e:
        error_trace = traceback.format_exc()
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}\n\nTraceback:\n{error_trace}")
