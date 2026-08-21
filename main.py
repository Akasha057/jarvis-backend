import os
import json
import uuid
import random
import base64
import tempfile
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google import genai
from elevenlabs.client import ElevenLabs

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

FOLDER_ID = "10nJftGge_D1W_Ph7pyK1QiC_ZNSx5ivR"

# Configuración de ElevenLabs
eleven_api_key = os.getenv("ELEVENLABS_API_KEY")
eleven_voice_id = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
client_eleven = ElevenLabs(api_key=eleven_api_key) if eleven_api_key else None

@app.get("/")
async def get_index():
    return FileResponse("static/index.html")

@app.post("/procesar")
async def procesar(request: Request):
    try:
        if not client_eleven:
            return JSONResponse(status_code=500, content={"status": "error", "message": "Falta configurar la API Key de ElevenLabs."})

        raw_keys = os.getenv("GEMINI_API_KEYS", "")
        if not raw_keys:
            return JSONResponse(status_code=500, content={"status": "error", "message": "No se encontraron API Keys en Render."})

        try:
            if raw_keys.startswith("["):
                lista_gemini_keys = json.loads(raw_keys)
            else:
                lista_gemini_keys = [k.strip() for k in raw_keys.replace('"', '').replace("'", "").split(",") if k.strip()]
        except Exception:
            lista_gemini_keys = [k.strip() for k in raw_keys.replace('"', '').replace("'", "").split(",") if k.strip()]

        if not lista_gemini_keys:
            return JSONResponse(status_code=500, content={"status": "error", "message": "La lista de llaves está vacía."})

        # Seleccionamos llave e inicializamos Gemini
        api_key_actual = random.choice(lista_gemini_keys)
        client_gemini = genai.Client(api_key=api_key_actual)

        data = await request.json()
        texto_usuario = data.get("text", "")
        
        if not texto_usuario:
            return JSONResponse(status_code=400, content={"status": "error", "message": "No se recibió texto."})

        # 1. Respuesta de Gemini (gemini-3.6-flash)
        response_gemini = client_gemini.models.generate_content(
            model='gemini-3.6-flash',
            contents=texto_usuario,
        )
        texto_respuesta = response_gemini.text

        # 2. Generación de Audio con ElevenLabs en memoria (bytes)
        audio_generator = client_eleven.text_to_speech.convert(
            text=texto_respuesta,
            voice_id=eleven_voice_id,
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128",
        )

        audio_bytes = b"".join(chunk for chunk in audio_generator)
        
        # Convertimos el audio a Base64 para enviarlo seguro al frontend
        audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')

        # 3. Guardado opcional en Drive usando un archivo temporal rápido
        nombre_archivo = f"jarvis_{uuid.uuid4()}.mp3"
        ruta_temporal = os.path.join(tempfile.gettempdir(), nombre_archivo)
        with open(ruta_temporal, "wb") as f:
            f.write(audio_bytes)

        try:
            subir_audio_a_drive(ruta_temporal, nombre_archivo)
        except Exception as drive_err:
            print(f"⚠️ Aviso de Drive: {drive_err}")

        if os.path.exists(ruta_temporal):
            os.remove(ruta_temporal)

        return {
            "status": "ok", 
            "respuesta_texto": texto_respuesta,
            "audio_base64": audio_base64
        }

    except Exception as e:
        print(f"❌ Error crítico en /procesar: {str(e)}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

def subir_audio_a_drive(file_path: str, file_name: str):
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        return
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=['https://www.googleapis.com/auth/drive']
    )
    service = build('drive', 'v3', credentials=creds)
    file_metadata = {'name': file_name, 'parents': [FOLDER_ID]}
    media = MediaFileUpload(file_path, mimetype='audio/mpeg', resumable=True)
    service.files().create(body=file_metadata, media_body=media, supportsAllDrives=True, fields='id').execute()
