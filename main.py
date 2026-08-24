import base64
import datetime
import io
import json
import os
import urllib.parse
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pydub import AudioSegment
from google import genai
from elevenlabs import ElevenLabs
import requests

app = FastAPI()

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

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY) if ELEVENLABS_API_KEY else None

class PromptRequest(BaseModel):
    text: str

def obtener_clima_actual(ciudad: str) -> str:
    """Consulta el clima en tiempo real de cualquier ciudad usando wttr.in."""
    try:
        url = f"https://wttr.in/{urllib.parse.quote(ciudad)}?format=3&lang=es"
        headers = {"User-Agent": "curl"}
        response = requests.get(url, headers=headers, timeout=3)
        if response.status_code == 200:
            return response.text.strip()
    except Exception:
        pass
    return "No disponible en este momento."

def generar_respuesta_con_flash(user_text: str) -> str:
    if not GEMINI_KEYS:
        raise ValueError("No hay API keys de Gemini disponibles.")

    # Contexto temporal global usando ZoneInfo (nativo de Python)
    try:
        ba_tz = ZoneInfo("America/Argentina/Buenos_Aires")
    except Exception:
        ba_tz = ZoneInfo("UTC")
        
    hora_actual = datetime.datetime.now(ba_tz).strftime('%H:%M (%d-%m-%Y)')

    # Detección inteligente si el usuario pregunta por clima de una ciudad específica
    contexto_extra = ""
    texto_lower = user_text.lower()
    
    if "clima" in texto_lower or "tiempo" in texto_lower or "temperatura" in texto_lower:
        palabras = user_text.split()
        for i, p in enumerate(palabras):
            if p.lower() in ["en", "de", "para"] and i + 1 < len(palabras):
                ciudad_objetivo = palabras[i + 1].strip("?.,!")
                clima_info = obtener_clima_actual(ciudad_objetivo)
                contexto_extra += f"\n- Clima actual en {ciudad_objetivo}: {clima_info}"

    ultimo_error = None
    for api_key in GEMINI_KEYS:
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=user_text,
                config={
                    "system_instruction": (
                        f"Eres J.A.R.V.I.S., un asistente de inteligencia artificial avanzado, formal y eficiente. "
                        f"INFORMACIÓN TEMPORAL: La hora local actual de referencia es {hora_actual}. "
                        f"{contexto_extra}"
                        "\nREGLA CRÍTICA: Da respuestas extremadamente directas, conversacionales y breves para maximizar la velocidad. "
                        "NUNCA incluyas scripts de programación ni explicaciones de código a menos que se te pida explícitamente."
                    )
                }
            )
            if response and response.text:
                return response.text
        except Exception as e:
            ultimo_error = e
            continue

    raise Exception(f"Todas las API keys de Gemini fallaron. Último error: {ultimo_error}")

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>J.A.R.V.I.S. - Omni Chat</title>
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/styles/atom-one-dark.min.css">
        <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/highlight.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
        
        <style>
            :root {
                --bg-main: #0b131a;
                --bg-panel: #111b21;
                --bg-bubble-user: #005c4b;
                --bg-bubble-jarvis: #202c33;
                --text-main: #e9edef;
                --text-muted: #8696a0;
                --accent: #00a884;
                --accent-hover: #008f72;
            }
            * { box-sizing: border-box; }
            body { background: var(--bg-main); color: var(--text-main); font-family: 'Segoe UI', Roboto, sans-serif; margin: 0; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
            header { padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; background: var(--bg-panel); border-bottom: 1px solid #222d34; }
            #chat-container { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 15px; max-width: 900px; width: 100%; margin: 0 auto; }
            .message-wrapper { display: flex; flex-direction: column; max-width: 80%; }
            .message-wrapper.user { align-self: flex-end; }
            .message-wrapper.jarvis { align-self: flex-start; }
            .bubble { padding: 10px 14px; border-radius: 8px; font-size: 14px; line-height: 1.5; word-wrap: break-word; }
            .user .bubble { background: var(--bg-bubble-user); color: #fff; border-top-right-radius: 0; }
            .jarvis .bubble { background: var(--bg-bubble-jarvis); color: var(--text-main); border-top-left-radius: 0; border: 1px solid #2a3942; }
            pre { background: #0b141a !important; padding: 12px; border-radius: 6px; overflow-x: auto; border: 1px solid #222d34; }
            code { font-family: 'Courier New', Courier, monospace; }
            footer { padding: 12px 20px; background: var(--bg-panel); display: flex; align-items: center; justify-content: center; gap: 12px; border-top: 1px solid #222d34; }
            .input-box-container { background: #2a3942; border-radius: 8px; padding: 6px 12px; display: flex; align-items: center; gap: 10px; width: 100%; max-width: 900px; }
            textarea { flex: 1; background: none; border: none; color: white; font-size: 14px; outline: none; resize: none; max-height: 100px; font-family: inherit; }
            textarea::placeholder { color: var(--text-muted); }
            .action-buttons { display: flex; align-items: center; gap: 8px; }
            .icon-btn { background: none; border: none; color: var(--text-muted); font-size: 18px; cursor: pointer; padding: 6px; border-radius: 50%; display: flex; align-items: center; justify-content: center; }
            .icon-btn:hover { color: var(--text-main); }
            .icon-btn.active { color: #d9534f; }
            .send-btn { background: var(--accent); color: #111b21; border: none; padding: 6px 14px; border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 13px; }
            .send-btn:hover { background: var(--accent-hover); }
            #status { font-size: 12px; color: var(--text-muted); }
            .action-link-btn { margin-top: 8px; background: #182229; border: 1px solid var(--accent); color: var(--accent); padding: 4px 8px; border-radius: 4px; font-size: 11px; cursor: pointer; text-decoration: none; display: inline-block; margin-right: 6px; }
            .action-link-btn:hover { background: var(--accent); color: #111b21; }
        </style>
    </head>
    <body>
        <header>
            <span style="font-weight: bold; color: var(--accent); letter-spacing: 1px;">J.A.R.V.I.S.</span>
            <span id="status">Inactivo</span>
        </header>

        <div id="chat-container">
            <div class="message-wrapper jarvis">
                <div class="bubble">Sistemas globales y meteorológicos enlazados. A su servicio, señor.</div>
            </div>
        </div>

        <footer>
            <div class="input-box-container">
                <textarea id="user-input" rows="1" placeholder="Escribe un mensaje a J.A.R.V.I.S..." oninput="autoExpand(this)"></textarea>
                <div class="action-buttons">
                    <button class="icon-btn" id="mic-btn" onclick="toggleVoz()" title="Modo Voz Continua">🎙️</button>
                    <button class="send-btn" onclick="enviarMensaje()">Enviar</button>
                </div>
            </div>
        </footer>

        <script>
            const chatContainer = document.getElementById('chat-container');
            const statusSpan = document.getElementById('status');
            const micBtn = document.getElementById('mic-btn');
            let isConversing = false;
            let recognition = null;
            let currentAudio = null;

            function autoExpand(textarea) {
                textarea.style.height = 'auto';
                textarea.style.height = textarea.scrollHeight + 'px';
            }

            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            if (SpeechRecognition) {
                recognition = new SpeechRecognition();
                recognition.lang = 'es-ES';
                recognition.interimResults = false;
                recognition.continuous = false;

                recognition.onstart = () => { statusSpan.innerText = "Escuchando..."; micBtn.classList.add('active'); };
                recognition.onresult = async (event) => { await enviarTexto(event.results[0][0].transcript); };
                recognition.onerror = () => { if (isConversing) setTimeout(() => { try { recognition.start(); } catch(e){} }, 1000); };
                recognition.onend = () => {
                    if (isConversing && statusSpan.innerText === "Escuchando...") {
                        try { recognition.start(); } catch(e){}
                    } else if (!isConversing) { micBtn.classList.remove('active'); }
                };
            }

            function interrumpirJarvis() {
                if (currentAudio) { currentAudio.pause(); currentAudio = null; }
            }

            function toggleVoz() {
                if (!recognition) return;
                isConversing = !isConversing;
                if (isConversing) { interrumpirJarvis(); try { recognition.start(); } catch(e){} }
                else { micBtn.classList.remove('active'); statusSpan.innerText = "Inactivo"; try { recognition.stop(); } catch(e){} }
            }

            async function enviarMensaje() {
                const textarea = document.getElementById('user-input');
                const text = textarea.value.trim();
                if (!text) return;
                textarea.value = "";
                textarea.style.height = 'auto';
                await enviarTexto(text);
            }

            document.getElementById('user-input').onkeydown = (e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); enviarMensaje(); }
            };

            async function enviarTexto(text) {
                interrumpirJarvis();
                if (isConversing) { try { recognition.stop(); } catch(e){} }

                appendMessageUI(text, 'user');
                statusSpan.innerText = "Procesando...";

                try {
                    const response = await fetch('/procesar', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ text: text })
                    });
                    
                    if (!response.ok) throw new Error("Error en el servidor");
                    const data = await response.json();

                    if (data.status === "ok") {
                        const audioSrc = data.audio_base64 ? "data:audio/wav;base64," + data.audio_base64 : null;
                        appendMessageUI(data.respuesta_texto, 'jarvis', audioSrc, `jarvis_${Date.now()}.wav`);

                        if (audioSrc) {
                            currentAudio = new Audio(audioSrc);
                            statusSpan.innerText = "J.A.R.V.I.S. hablando...";
                            currentAudio.play().catch(e => console.log(e));
                            currentAudio.onended = () => {
                                currentAudio = null;
                                if (isConversing) try { recognition.start(); } catch(e){}
                                else statusSpan.innerText = "Inactivo";
                            };
                        } else {
                            statusSpan.innerText = "Inactivo";
                            if (isConversing) try { recognition.start(); } catch(e){}
                        }
                    }
                } catch (err) {
                    statusSpan.innerText = "Error";
                    appendMessageUI("⚠️ Error de conexión.", 'jarvis');
                    if (isConversing) setTimeout(() => { try { recognition.start(); } catch(e){} }, 2000);
                }
            }

            function appendMessageUI(text, sender, audioSrc = null, fileName = null) {
                const wrapper = document.createElement('div');
                wrapper.className = `message-wrapper ${sender}`;
                const bubble = document.createElement('div');
                bubble.className = 'bubble';

                if (sender === 'user') {
                    bubble.innerText = text;
                } else {
                    bubble.innerHTML = marked.parse(text);
                    const pdfBtn = document.createElement('button');
                    pdfBtn.className = 'action-link-btn';
                    pdfBtn.innerHTML = '📄 PDF';
                    pdfBtn.onclick = () => {
                        const { jsPDF } = window.jspdf;
                        const doc = new jsPDF();
                        doc.text(text.replace(/<[^>]*>?/gm, ''), 15, 20);
                        doc.save(`jarvis_${Date.now()}.pdf`);
                    };
                    bubble.appendChild(pdfBtn);

                    if (audioSrc) {
                        const wavBtn = document.createElement('a');
                        wavBtn.href = audioSrc;
                        wavBtn.download = fileName;
                        wavBtn.className = 'action-link-btn';
                        wavBtn.innerText = '📥 WAV';
                        bubble.appendChild(wavBtn);
                    }
                }
                wrapper.appendChild(bubble);
                chatContainer.appendChild(wrapper);
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
        respuesta_texto = generar_respuesta_con_flash(user_text)

        audio_b64 = ""
        if eleven_client:
            try:
                audio_stream = eleven_client.text_to_speech.convert(
                    text=respuesta_texto[:250], 
                    voice_id="OqoIeNOqjjjkwABBwfFl",
                    model_id="eleven_multilingual_v2",
                    output_format="mp3_44100_128"
                )
                audio_bytes = b"".join(chunk for chunk in audio_stream)
                audio_mp3 = AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")
                audio_wav = audio_mp3.set_frame_rate(44100).set_channels(1)
                wav_io = io.BytesIO()
                audio_wav.export(wav_io, format="wav")
                audio_b64 = base64.b64encode(wav_io.getvalue()).decode('utf-8')
            except Exception as audio_err:
                print(f"⚠️ Audio omitido por latencia: {audio_err}")

        return {
            "status": "ok",
            "respuesta_texto": respuesta_texto,
            "audio_base64": audio_b64
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
