import base64
import datetime
import io
import json
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from pydub import AudioSegment
from google import genai
from elevenlabs import ElevenLabs

app = FastAPI()

# Configuración de credenciales y IDs
CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SHEET_ID = "1O5nwvczZ4i6NxQJtwCnwddfcz3pA5eg_evqiujDnMRU"
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

# Carga segura de keys de Gemini
raw_keys = os.getenv("GEMINI_API_KEYS", "")
GEMINI_KEYS = []
if raw_keys:
    if raw_keys.strip().startswith("["):
        try:
            parsed = json.loads(raw_keys)
            if isinstance(parsed, list):
                GEMINI_KEYS = [str(k).strip() for k in parsed if k]
        except Exception:
            GEMINI_KEYS = [raw_keys.strip()]
    else:
        GEMINI_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]

single_key = os.getenv("GEMINI_API_KEY")
if single_key and single_key.strip() not in GEMINI_KEYS:
    GEMINI_KEYS.insert(0, single_key.strip())

if not GEMINI_KEYS:
    print("⚠️ ¡Atención! No se encontraron claves de Gemini configuradas.")

# Inicializar ElevenLabs
eleven_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

class PromptRequest(BaseModel):
    text: str

def generar_respuesta_con_fallback(user_text: str) -> str:
    """Genera respuesta con Gemini usando gemini-3.6-flash y rotación segura de keys."""
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

def subir_a_drive_y_registrar(texto: str, wav_bytes: bytes, filename: str):
    """Sube el audio WAV directamente a la carpeta compartida de Google Drive y registra en Google Sheets."""
    try:
        if not CREDENTIALS_JSON:
            print("⚠️ Aviso: No se encontró GOOGLE_CREDENTIALS_JSON")
            return "No disponible"

        creds_dict = json.loads(CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, 
            scopes=[
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive.file'
            ]
        )

        drive_service = build('drive', 'v3', credentials=creds)
        file_metadata = {'name': filename}
        if DRIVE_FOLDER_ID:
            file_metadata['parents'] = [DRIVE_FOLDER_ID]

        media = MediaIoBaseUpload(io.BytesIO(wav_bytes), mimetype='audio/wav', resumable=True)
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink'
        ).execute()

        file_id = file.get('id')
        audio_link = file.get('webViewLink')

        try:
            drive_service.permissions().create(
                fileId=file_id,
                body={'role': 'reader', 'type': 'anyone'}
            ).execute()
        except Exception as perm_err:
            print(f"⚠️ No se pudo hacer público el archivo en Drive: {perm_err}")

        sheet_service = build('sheets', 'v4', credentials=creds)
        valores = [[datetime.datetime.now().isoformat(), texto, audio_link]]
        sheet_service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, 
            range="A1", 
            valueInputOption="RAW",
            body={"values": valores}
        ).execute()

        return audio_link

    except Exception as e:
        print(f"❌ Error al subir a Drive o Sheets: {e}")
        return "Error al guardar en Drive"

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>JARVIS - Chat Continuo</title>
        <style>
            body { background: #0b141a; color: #e9edef; font-family: 'Segoe UI', Courier, monospace; margin: 0; padding: 0; display: flex; flex-direction: column; height: 100vh; }
            header { background: #202c33; padding: 15px; text-align: center; border-bottom: 1px solid #2a3942; font-size: 20px; font-weight: bold; color: #00ffcc; }
            #chat-container { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 15px; }
            .message { max-width: 70%; padding: 12px 16px; border-radius: 8px; font-size: 15px; line-height: 1.4; word-wrap: break-word; }
            .user-msg { background: #005c4b; align-self: flex-end; border-top-right-radius: 0; }
            .jarvis-msg { background: #202c33; align-self: flex-start; border-top-left-radius: 0; border: 1px solid #2a3942; color: #00ffcc; }
            footer { background: #202c33; padding: 15px; display: flex; align-items: center; justify-content: center; gap: 10px; border-top: 1px solid #2a3942; }
            button { background: #00a884; border: none; color: white; padding: 12px 24px; font-size: 16px; cursor: pointer; border-radius: 24px; font-weight: bold; transition: 0.2s; }
            button:hover { background: #02906f; }
            button.active { background: #d9534f; }
            #status { font-size: 14px; color: #8696a0; margin-left: 10px; }
            a.download-link { display: block; margin-top: 6px; font-size: 12px; color: #53bdeb; text-decoration: underline; }
        </style>
    </head>
    <body>
        <header>J.A.R.V.I.S. // SISTEMA ACTIVO</header>
        
        <div id="chat-container">
            <div class="message jarvis-msg">Hola Santino. Sistema enlazado. Activa el modo conversación cuando gustes.</div>
        </div>

        <footer>
            <button id="toggle-btn">🎙️ INICIAR CONVERSACIÓN</button>
            <span id="status">Inactivo</span>
        </footer>

        <script>
            const toggleBtn = document.getElementById('toggle-btn');
            const statusSpan = document.getElementById('status');
            const chatContainer = document.getElementById('chat-container');

            let isConversing = false;
            let recognition = null;

            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            if (!SpeechRecognition) {
                statusSpan.innerText = "Tu navegador no soporta voz. Usa Google Chrome.";
                toggleBtn.disabled = true;
            } else {
                recognition = new SpeechRecognition();
                recognition.lang = 'es-ES';
                recognition.interimResults = false;
                recognition.continuous = false;

                recognition.onstart = () => {
                    statusSpan.innerText = "Escuchando...";
                };

                recognition.onresult = async (event) => {
                    const transcript = event.results[0][0].transcript;
                    statusSpan.innerText = "Procesando...";
                    
                    // Añadir burbuja del usuario a la derecha
                    appendMessage(transcript, 'user-msg');

                    try {
                        const response = await fetch('/procesar', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({text: transcript})
                        });
                        
                        if (!response.ok) {
                            const errorData = await response.json();
                            throw new Error(errorData.detail || "Error en el servidor");
                        }
                        
                        const data = await response.json();
                        
                        if(data.status === "ok") {
                            const audioSrc = "data:audio/wav;base64," + data.audio_base64;
                            const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
                            const fileName = `jarvis_${timestamp}.wav`;

                            // Descarga automática en segundo plano
                            const dl = document.createElement('a');
                            dl.href = audioSrc;
                            dl.download = fileName;
                            document.body.appendChild(dl);
                            dl.click();
                            document.body.removeChild(dl);

                            // Añadir burbuja de JARVIS a la izquierda con enlace de respaldo
                            const jarvisHtml = `${data.respuesta_texto}<br><a class="download-link" href="${audioSrc}" download="${fileName}">📥 Descargar WAV</a>`;
                            appendMessage(jarvisHtml, 'jarvis-msg', true);

                            // Reproducir audio y reabrir micrófono al terminar
                            const audio = new Audio(audioSrc);
                            statusSpan.innerText = "JARVIS hablando...";
                            
                            audio.play().catch(e => console.log("Error al reproducir:", e));

                            audio.onended = () => {
                                if (isConversing) {
                                    try {
                                        recognition.start();
                                    } catch (err) {
                                        console.log("El micrófono ya estaba activo");
                                    }
                                } else {
                                    statusSpan.innerText = "Pausado";
                                }
                            };
                        }
                    } catch (err) {
                        statusSpan.innerText = "Error: " + err.message;
                        console.error(err);
                        if (isConversing) {
                            setTimeout(() => { try { recognition.start(); } catch(e){} }, 2000);
                        }
                    }
                };

                recognition.onerror = (event) => {
                    console.log("Error de reconocimiento:", event.error);
                    if (isConversing && event.error !== 'aborted') {
                        setTimeout(() => { try { recognition.start(); } catch(e){} }, 1000);
                    }
                };

                recognition.onend = () => {
                    // Si sigue activo el modo conversación pero se cerró por silencio, reintentar
                    if (isConversing && statusSpan.innerText === "Escuchando...") {
                        try { recognition.start(); } catch(e){}
                    }
                };

                toggleBtn.onclick = () => {
                    isConversing = !isConversing;
                    if (isConversing) {
                        toggleBtn.innerText = "⏹️ DETENER CONVERSACIÓN";
                        toggleBtn.classList.add('active');
                        try {
                            recognition.start();
                        } catch(e) {
                            console.log(e);
                        }
                    } else {
                        toggleBtn.innerText = "🎙️ INICIAR CONVERSACIÓN";
                        toggleBtn.classList.remove('active');
                        try {
                            recognition.stop();
                        } catch(e){}
                        statusSpan.innerText = "Inactivo";
                    }
                };
            }

            function appendMessage(text, className, isHtml = false) {
                const div = document.createElement('div');
                div.className = `message ${className}`;
                if (isHtml) {
                    div.innerHTML = text;
                } else {
                    div.innerText = text;
                }
                chatContainer.appendChild(div);
                chatContainer.scrollTop = chatContainer.scrollHeight;
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
        wav_bytes = wav_io.getvalue()

        # 4. Subir a Google Drive y registrar en Sheets
        filename = f'audio_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.wav'
        subir_a_drive_y_registrar(user_text, wav_bytes, filename)

        # 5. Retornar Base64
        audio_b64 = base64.b64encode(wav_bytes).decode('utf-8')

        return {
            "status": "ok",
            "respuesta_texto": respuesta_texto,
            "audio_base64": audio_b64
        }

    except Exception as e:
        print(f"❌ Error crítico en /procesar: {e}")
        raise HTTPException(status_code=500, detail=str(e))
