import os
import json
import datetime
import io
import base64
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from google.oauth2 import service_account
from googleapiclient.discovery import build
from pydub import AudioSegment
from google import genai
from supabase import create_client, Client
from elevenlabs import ElevenLabs

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

class PromptRequest(BaseModel):
    text: str

def generar_respuesta_con_fallback(user_text: str) -> str:
    """Genera respuesta con Gemini usando gemini-3.6-flash y rotación de keys."""
    if not GEMINI_KEYS:
        raise ValueError("No hay API keys de Gemini disponibles.")

    ultimo_error = None
    for api_key in GEMINI_KEYS:
        try:
            client = genai.Client(api_key=api_key)
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

def subir_a_supabase(wav_bytes: bytes, filename: str) -> str:
    """Sube el audio al bucket de Supabase de forma ultra limpia sin metadatos que rompan el JSON."""
    try:
        if not supabase:
            print("⚠️ Supabase no está configurado correctamente.")
            return "No disponible (Sin credenciales de Supabase)"

        # Subida directa evitando diccionarios complejos de file_options que causaban el error 400
        supabase.storage.from_("jarvis-audios").upload(
            path=filename,
            file=wav_bytes
        )

        public_url = supabase.storage.from_("jarvis-audios").get_public_url(filename)
        return public_url
    except Exception as e:
        print(f"❌ Error subiendo a Supabase: {e}")
        return f"Error al subir: {e}"

def registrar_en_sheets(texto: str, audio_link: str):
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
        valores = [[datetime.datetime.now().isoformat(), texto, audio_link]]
        sheet_service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, 
            range="A1", 
            valueInputOption="RAW",
            body={"values": valores}
        ).execute()

    except Exception as e:
        print(f"❌ Error crítico al registrar en Sheets: {e}")

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
                            
                            if(data.audio_base64) {
                                const audioSrc = "data:audio/mp3;base64," + data.audio_base64;
                                const audio = new Audio(audioSrc);
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
def procesar(payload: PromptRequest):
    try:
        user_text = payload.text

        # 1. Generar respuesta con Gemini 3.6 Flash
        respuesta_texto = generar_respuesta_con_fallback(user_text)

        # 2. Generar audio con ElevenLabs
        audio_stream = eleven_client.text_to_speech.convert(
            text=respuesta_texto,
            voice_id="OqoIeNOqjjjkwABBwfFl",
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128"
        )
        audio_bytes = b"".join(chunk for chunk in audio_stream)

        # 3. Convertir MP3 a WAV
        audio_mp3 = AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")
        audio_wav = audio_mp3.set_frame_rate(44100).set_channels(1)
        wav_io = io.BytesIO()
        audio_wav.export(wav_io, format="wav")

        # 4. Subir a Supabase Storage (sin opciones JSON conflictivas) y registrar en Sheets
        filename = f'audio_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.wav'
        audio_link = subir_a_supabase(wav_io.getvalue(), filename)
        registrar_en_sheets(user_text, audio_link)

        # 5. Retornar respuesta al cliente
        audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')

        return {
            "status": "ok",
            "respuesta_texto": respuesta_texto,
            "audio_base64": audio_b64
        }

    except Exception as e:
        print(f"❌ Error en /procesar: {e}")
        raise HTTPException(status_code=500, detail=str(e))
