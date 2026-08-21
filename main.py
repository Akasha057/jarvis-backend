import os
import json
import datetime
import io
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from pydub import AudioSegment
import google.generativeai as genai
from elevenlabs import ElevenLabs

app = FastAPI()

# Configuración de credenciales y APIs
# Configuración de credenciales y IDs
CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SHEET_ID = "1O5nwvczZ4i6NxQJtwCnwddfcz3pA5eg_evqiujDnMRU"
FOLDER_ID = "10nJftGge_D1W_Ph7pyK1QiC_ZNSx5ivR"

# Inicializar Gemini y ElevenLabs (asegurate de tener tus API keys en las variables de entorno de Render)
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
eleven_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

class PromptRequest(BaseModel):
    text: str

def guardar_en_sheets_y_drive(texto: str, audio_bytes: bytes):
    try:
        if not CREDENTIALS_JSON:
            print("⚠️ Aviso: No se encontró GOOGLE_CREDENTIALS_JSON")
            return

        creds_dict = json.loads(CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, 
            scopes=[
                'https://www.googleapis.com/auth/spreadsheets', 
                'https://www.googleapis.com/auth/drive.file'
            ]
        )
        
        # 1. Convertir MP3 de ElevenLabs a WAV (44.1kHz, Mono) para Applio
        audio_mp3 = AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")
        audio_wav = audio_mp3.set_frame_rate(44100).set_channels(1)
        
        wav_io = io.BytesIO()
        audio_wav.export(wav_io, format="wav")
        wav_io.seek(0)
        
        # 2. Subir el archivo WAV a Google Drive usando MediaInMemoryUpload
        drive_service = build('drive', 'v3', credentials=creds)
        file_metadata = {
            'name': f'audio_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.wav',
            'parents': [FOLDER_ID]
        }
        
        wav_bytes_data = wav_io.getvalue()
        media = MediaInMemoryUpload(wav_bytes_data, mimetype='audio/wav')
        
        file = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        
        # 3. Registrar los datos en Google Sheets
        sheet_service = build('sheets', 'v4', credentials=creds)
        valores = [[datetime.datetime.now().isoformat(), texto, file.get('webViewLink')]]
        sheet_service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, 
            range="A1", 
            valueInputOption="RAW",
            body={"values": valores}
        ).execute()

    except Exception as e:
        print(f"❌ Error crítico al guardar en Sheets/Drive: {e}")

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
                        const data = await response.json();
                        
                        if(data.status === "ok") {
                            status.innerText = "JARVIS: " + data.respuesta_texto;
                            
                            if(data.audio_base64) {
                                const audioSrc = "data:audio/mp3;base64," + data.audio_base64;
                                const audio = new Audio(audioSrc);
                                audio.play().catch(e => console.log("Error al reproducir audio:", e));
                            }
                        } else {
                            status.innerText = "Error: " + data.message;
                        }
                    } catch (err) {
                        status.innerText = "Error de conexión con el servidor.";
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
        
        # 1. Generar respuesta con Gemini (manteniendo el rol técnico de JARVIS)
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction="Eres JARVIS, un asistente de inteligencia artificial avanzado, formal, técnico, eficiente y de respuestas directas."
        )
        response = model.generate_content(user_text)
        respuesta_texto = response.text

        # 2. Generar audio con ElevenLabs
        audio_stream = eleven_client.text_to_speech.convert(
            text=respuesta_texto,
            voice_id="JBFqnCBsd6RMkjVDRZzb", # Reemplazá con tu voice ID si usas una específica
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128"
        )
        
        audio_bytes = b"".join(chunk for chunk in audio_stream)
        
        # 3. Guardar en Google Drive (.wav) y registrar en Google Sheets de forma asíncrona o directa
        guardar_en_sheets_y_drive(user_text, audio_bytes)
        
        # 4. Retornar respuesta a la interfaz en base64 para reproducción inmediata
        import base64
        audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
        
        return {
            "status": "ok",
            "respuesta_texto": respuesta_texto,
            "audio_base64": audio_b64
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
