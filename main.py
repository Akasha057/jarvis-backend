import os
import json
import uuid
import random
import base64
import datetime
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google import genai
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# ID de tu Google Sheet para los logs de audio
SHEET_ID = "1O5nwvczZ4i6NxQJtwCnwddfcz3pA5eg_evqiujDnMRU" 

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

        # Prompt de sistema integrado para obligar a Gemini a responder con el tono estricto de JARVIS
        prompt_sistema = (
            "Eres JARVIS, el asistente de inteligencia artificial avanzado. "
            "Responde de manera estricta, formal, sobria, fría, sumamente técnica y sin expresiones alegres o exageradas. "
            "Evita usar signos de exclamación excesivos o tonos joviales. Mantén la compostura propia de un sistema de alta tecnología."
        )

        # 1. Respuesta de Gemini con directiva de tono
        response_gemini = client_gemini.models.generate_content(
            model='gemini-3.6-flash',
            contents=f"{prompt_sistema}\n\nUsuario: {texto_usuario}",
        )
        texto_respuesta = response_gemini.text

        # 2. Generación de Audio con ElevenLabs aplicando estabilidad alta
        audio_generator = client_eleven.text_to_speech.convert(
            text=texto_respuesta,
            voice_id=eleven_voice_id,
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128",
            voice_settings=VoiceSettings(
                stability=0.92,
                similarity_boost=0.75,
                style=0.0,
                use_speaker_boost=True
            )
        )

        audio_bytes = b"".join(chunk for chunk in audio_generator)
        audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')

        # 3. Guardado seguro en Google Sheets (evita errores de cuota)
        try:
            guardar_en_sheets(texto_respuesta, audio_base64)
        except Exception as sheet_err:
            print(f"⚠️ Aviso de Sheets: {sheet_err}")

        return {
            "status": "ok", 
            "respuesta_texto": texto_respuesta,
            "audio_base64": audio_base64
        }

    except Exception as e:
        print(f"❌ Error crítico en /procesar: {str(e)}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

def guardar_en_sheets(texto: str, audio_b64: str):
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        return
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    service = build('sheets', 'v4', credentials=creds)
    
    timestamp = datetime.datetime.now().isoformat()
    valores = [[timestamp, texto, audio_b64]]
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="A1",
        valueInputOption="RAW",
        body={"values": valores}
    ).execute()
