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

client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_raw:
        raise ValueError("Missing GOOGLE_CREDENTIALS_JSON environment variable.")
    creds_dict = json.loads(creds_raw)
    return build("drive", "v3", credentials=service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    ))

def get_excel_df(drive, name):
    query = f"'{FOLDER_ID}' in parents and name = '{name}' and trashed = false"
    res = drive.files().list(q=query, fields="files(id, mimeType)").execute()
    archivos = res.get("files", [])
    
    if archivos:
        file_info = archivos[0]
        file_id = file_info['id']
        mime_type = file_info.get('mimeType', '')
        
        if mime_type == 'application/vnd.google-apps.spreadsheet':
            request = drive.files().export_media(fileId=file_id, mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        else:
            request = drive.files().get_media(fileId=file_id)
            
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: 
            _, done = downloader.next_chunk()
        fh.seek(0)
        
        return pd.read_excel(fh, engine='openpyxl', dtype=str).fillna("")
    return pd.DataFrame()

class ChatRequest(BaseModel):
    query: str
    mode: str

@router.post("/chat")
def chat_ia(req: ChatRequest):
    try:
        drive = get_drive_service()
        
        if req.mode == "abiertos":
            df_sin = get_excel_df(drive, 'Avisos Sin Tratar.xlsx')
            df_tra = get_excel_df(drive, 'Avisos Tratados.xlsx')
            df_combined = pd.concat([df_sin, df_tra], ignore_index=True)
            context_text = df_combined.to_csv(index=False)
        else:
            df_fin = get_excel_df(drive, 'Partes Finalizados.xlsx')
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
        error_msg = f"Type: {type(e).__name__} | Message: {str(e)} | Trace: {error_trace}"
        raise HTTPException(status_code=500, detail=error_msg)
