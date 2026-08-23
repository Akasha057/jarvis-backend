import os
import json
import datetime
import io
import base64
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from google.oauth2 import service_account
from googleapiclient.discovery import build
from pydub import AudioSegment
from google import genai
from elevenlabs import ElevenLabs
from supabase import create_client, Client

app = FastAPI()

# Configuración de credenciales y IDs
CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SHEET_ID = "1O5nwvczZ4i6NxQJtwCnwddfcz3pA5eg_evqiujDnMRU"

# Configuración de Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# Cargar la lista de keys de Gemini
raw_keys = os.getenv("GEMINI_API_KEYS", "[]")
try:
    if raw_keys.startswith("["):
        GEMINI_KEYS = json.loads(raw_keys)
    else:
        GEMINI_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
except Exception:
    GEMINI_KEYS = []

single_key = os.getenv("GEMINI_API_KEY")
if single_key and single_key not in GEMINI_KEYS:
    GEMINI_KEYS.insert(0, single_key)

if not GEMINI_KEYS:
    print("⚠️ ¡Atención! No se encontraron claves de Gemini configuradas en GEMINI_API_KEYS.")

# Inicializar ElevenLabs
eleven_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
VOICE_ID = "OqoIeNOqjjjkwABBwfFl"  # Tu Voice ID configurado

class PromptRequest(BaseModel):
    text: str

def generar_respuesta_gemini(user_text: str) -> str:
    """Genera respuesta con Gemini aplicando rotación de keys y usando gemini-3.6-flash."""
    if not GEMINI_KEYS:
        raise ValueError("No hay API keys de Gemini disponibles.")

    ultimo_error = None
    for api_key in GEMINI_KEYS:
        try:
            client = genai.Client(api_key=api_key, http_options={'api_version': 'v1beta'})
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=user_text,
                config={
                    "system_instruction": "Eres JARVIS, un asistente de inteligencia artificial avanzado, formal, técnico, eficiente y de respuestas directas."
                }
            )
            if response and response.text:
                return response.text
        except Exception as e:
            print(f"⚠️ API Key falló: {e}")
            ultimo_error = e
            continue
            
    raise Exception(f"Todas las API keys de Gemini fallaron. Último error: {ultimo_error}")

def generar_audio_elevenlabs(text: str) -> bytes:
    """Genera audio en formato WAV usando ElevenLabs."""
    try:
        audio_generator = eleven_client.text_to_speech.convert(
            voice_id=VOICE_ID,
            optimize_streaming_latency="0",
            output_format="pcm_22050",
            text=text,
            model_id="eleven_multilingual_v2"
        )
        audio_bytes_raw = b"".join(chunk for chunk in audio_generator)
        
        # Convertir PCM raw a WAV usando pydub
        segment = AudioSegment(
            data=audio_bytes_raw,
            sample_width=2,
            frame_rate=22050,
            channels=1
        )
        wav_io = io.BytesIO()
        segment.export(wav_io, format="wav")
        return wav_io.getvalue()
    except Exception as e:
        print(f"❌ Error generando audio con ElevenLabs: {e}")
        raise e

def registrar_en_sheets(timestamp_str: str, texto: str, audio_link: str):
    """Registra el texto y el enlace de Supabase del audio en Google Sheets."""
    try:
        if not CREDENTIALS_JSON:
            print("⚠️ Aviso: No se encontró GOOGLE_CREDENTIALS_JSON")
            return

        creds_dict = json.loads(CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, 
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        
        sheet_service = build('sheets', 'v4', credentials=creds)
        valores = [[timestamp_str, texto, audio_link]]
        sheet_service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, 
            range="A1", 
            valueInputOption="RAW",
            body={"values": valores}
        ).execute()

    except Exception as e:
        print(f"❌ Error crítico al registrar en Sheets: {e}")

def subir_a_supabase(wav_bytes: bytes, filename: str) -> str:
    """Sube el audio al bucket 'jarvis-audios' mediante HTTP POST con multipart/form-data."""
    try:
        if not SUPABASE_URL or not SUPABASE_KEY:
            print("⚠️ Supabase no está configurado correctamente.")
            return "No disponible (Sin credenciales de Supabase)"

        # Endpoint directo del bucket de Supabase Storage
        url = f"{SUPABASE_URL}/storage/v1/object/jarvis-audios/{filename}"
        
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "x-upsert": "true"
        }

        # Usar 'files' en lugar de 'data' evita problemas de parseo binario en la API de Supabase Storage
        files = {
            'file': (filename, wav_bytes, 'audio/wav')
        }

        response = requests.post(url, headers=headers, files=files)
        
        if response.status_code in [200, 201]:
            # Construir y retornar la URL pública oficial limpia
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/jarvis-audios/{filename}"
            return public_url
        else:
            print(f"❌ Error en Supabase HTTP {response.status_code}: {response.text}")
            return f"Error al subir: {response.status_code}"

    except Exception as e:
        print(f"❌ Error crítico subiendo a Supabase: {e}")
        return f"Error al subir: {e}"
        
@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>JARVIS Interface</title>
        <style>
            body { background: #000; color: #00ffcc; font-family: 'Courier New', monospace; text-align: center; padding-top: 50px; }
            button { background: #002222; border: 1px solid #00ffcc; color: #00ffcc; padding: 20px; font-size: 20px; cursor: pointer; border-radius: 8px; }
            button:hover { background: #004444; }
            #status { margin-top: 20px; font-size: 18px; }
        </style>
    </head>
    <body>
        <h1>JARVIS</h1>
        <button id="start-btn">🎙️ ACTIVAR MODO VOZ</button>
        <p id="status">Esperando comandos...</p>

        <script>
            const btn = document.getElementById('start-btn');
            const status = document.getElementById('status');
            
            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            if (!SpeechRecognition) {
                status.innerText = "Tu navegador no soporta reconocimiento de voz. Usa Google Chrome.";
            } else {
                const recognition = new SpeechRecognition();
                recognition.lang = 'es-ES';
                
                btn.onclick = () => {
                    recognition.start();
                    status.innerText = "Escuchando...";
                };

                recognition.onresult = async (event) => {
                    const transcript = event.results[0][0].transcript;
                    status.innerText = "Procesando: " + transcript;
                    
                    try {
                        const response = await fetch('/procesar', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({text: transcript})
                        });
                        
                        if (!response.ok) {
                            const errorData = await response.json();
                            throw new Error(errorData.detail || "Error desconocido en el servidor");
                        }
                        
                        const data = await response.json();
                        
                        if(data.status === "ok") {
                            status.innerText = "JARVIS: " + data.respuesta_texto;
                            
                            if(data.audio_url) {
                                const audio = new Audio(data.audio_url);
                                audio.play().catch(e => console.log("Error al reproducir audio:", e));
                            }
                        } else {
                            status.innerText = "Error: " + (data.message || "Respuesta inválida");
                        }
                    } catch (err) {
                        status.innerText = "Error: " + err.message;
                        console.error(err);
                    }
                };
            }
        </script>
    </body>
    </html>
    """

@app.post("/procesar")
async def procesar(data: dict):
    user_text = data.get("text", "")
    
    try:
        # 1. Generar respuesta con Gemini
        response_text = generar_respuesta_gemini(user_text)
        
        # 2. Generar audio con ElevenLabs
        audio_bytes = generar_audio_elevenlabs(response_text)
        
        # 3. Preparar nombre del archivo
        timestamp_str = datetime.datetime.now().isoformat()
        timestamp_file = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"audio_{timestamp_file}.wav"
        
        # 4. Subir a Supabase
        public_url = subir_a_supabase(audio_bytes, filename)
        
        # 5. Registrar en Google Sheets
        registrar_en_sheets(timestamp_str, user_text, public_url)
        
        # 6. Notificar a Pipedream para la subida a Google Drive
        try:
            webhook_url = "https://eowdtdj2mhqdehn.m.pipedream.net"
            payload = {
                "audio_url": public_url,
                "filename": filename
            }
            requests.post(webhook_url, json=payload)
            print(f"✅ Notificación enviada a Pipedream para {filename}")
        except Exception as e:
            print(f"❌ Error enviando notificación a Pipedream: {e}")

        # 7. Retornar respuesta compatible con el script de la interfaz
        return {
            "status": "ok",
            "respuesta_texto": response_text,
            "audio_url": public_url
        }
    except Exception as e:
        print(f"❌ Error en /procesar: {e}")
        raise HTTPException(status_code=500, detail=str(e))
