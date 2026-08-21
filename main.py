import os
import json
import uuid
import tempfile
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Inicialización de FastAPI
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

FOLDER_ID = "10nJftGge_D1W_Ph7pyK1QiC_ZNSx5ivR"

# --- RUTA PRINCIPAL (Sirve la interfaz web) ---
@app.get("/")
async def get_index():
    return FileResponse("static/index.html")

# --- RUTA DE PROCESAMIENTO (Cerebro de JARVIS) ---
@app.post("/procesar")
async def procesar(request: Request):
    try:
        data = await request.json()
        texto_usuario = data.get("text", "")
        
        if not texto_usuario:
            return {"status": "error", "message": "No se recibió texto."}

        # 1. Procesamiento con Gemini usando la variable de entorno segura
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        # Nota: Ajusta esta llamada según la librería oficial que estés usando para Gemini
        from google import genai
        client_gemini = genai.Client(api_key=gemini_api_key)
        
        response_gemini = client_gemini.models.generate_content(
            model='gemini-2.5-flash', # O el modelo que prefieras usar
            contents=texto_usuario,
        )
        texto_respuesta = response_gemini.text

        # 2. Generación de Audio con ElevenLabs de forma segura
        eleven_api_key = os.getenv("ELEVENLABS_API_KEY")
        from elevenlabs.client import ElevenLabs
        client_eleven = ElevenLabs(api_key=eleven_api_key)

        audio_generator = client_eleven.text_to_speech.convert(
            text=texto_respuesta,
            voice_id="JBFqnCBsd6RMkjVDRZzb", # Puedes cambiar el ID de la voz si usas otra
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128",
        )

        # Guardamos el audio temporalmente para subirlo a Drive
        nombre_archivo = f"jarvis_{uuid.uuid4()}.mp3"
        ruta_temporal = os.path.join(tempfile.gettempdir(), nombre_archivo)
        
        with open(ruta_temporal, "wb") as f:
            for chunk in audio_generator:
                f.write(chunk)

        # 3. Subir automáticamente a Google Drive
        subir_audio_a_drive(ruta_temporal, nombre_archivo)
        
        # Limpieza del archivo temporal local
        if os.path.exists(ruta_temporal):
            os.remove(ruta_temporal)
        
        return {
            "status": "ok", 
            "respuesta_texto": texto_respuesta
        }

    except Exception as e:
        print(f"❌ Error en /procesar: {str(e)}")
        return {"status": "error", "message": str(e)}

# --- FUNCIÓN DE GOOGLE DRIVE ---
def subir_audio_a_drive(file_path: str, file_name: str):
    try:
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            print("⚠️ Falta GOOGLE_CREDENTIALS_JSON")
            return

        creds_dict = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=['https://www.googleapis.com/auth/drive']
        )
        service = build('drive', 'v3', credentials=creds)

        file_metadata = {
            'name': file_name,
            'parents': [FOLDER_ID]
        }
        
        media = MediaFileUpload(file_path, mimetype='audio/mpeg', resumable=True)
        
        service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        print(f"✅ Audio subido exitosamente a Google Drive: {file_name}")
    except Exception as e:
        print(f"❌ Error al subir a Google Drive: {str(e)}")
