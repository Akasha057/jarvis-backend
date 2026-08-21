import os
import json
import tempfile
from fastapi import FastAPI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = FastAPI()

FOLDER_ID = "10nJftGge_D1W_Ph7pyK1QiC_ZNSx5ivR"

def subir_audio_a_drive(file_path: str, file_name: str):
    """Sube el archivo .wav generado a la carpeta de Google Drive usando la cuenta de servicio."""
    try:
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            print("⚠️ Falta la variable de entorno GOOGLE_CREDENTIALS_JSON")
            return

        # Cargamos las credenciales desde la variable de entorno de Render
        creds_dict = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=['https://www.googleapis.com/auth/drive']
        )
        
        service = build('drive', 'v3', credentials=creds)

        file_metadata = {
            'name': file_name,
            'parents': [FOLDER_ID]
        }
        
        media = MediaFileUpload(file_path, mimetype='audio/wav', resumable=True)
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        print(f"✅ Audio subido exitosamente a Google Drive. ID: {file.get('id')}")
    except Exception as e:
        print(f"❌ Error al subir el audio a Google Drive: {str(e)}")

@app.get("/")
def read_root():
    return {"status": "JARVIS Backend Operativo"}
