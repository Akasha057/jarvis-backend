import os
import io
import json
import base64
import datetime
import traceback
from zoneinfo import ZoneInfo
from typing import Dict, Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Header, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests
from pydub import AudioSegment
from elevenlabs.client import ElevenLabs
from google import genai
from google.genai import types

app = FastAPI(title="J.A.R.V.I.S. Cloud Brain")

# ---------------------------------------------------------
# CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ---------------------------------------------------------
GEMINI_KEYS_RAW = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_KEYS = [k.strip() for k in GEMINI_KEYS_RAW.split(",") if k.strip()]

ELEVEN_API_KEY = os.environ.get("ELEVEN_API_KEY", "")
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")

# Secrets de seguridad (Configúralos en el panel de Render)
JARVIS_AGENT_SECRET = os.environ.get("JARVIS_AGENT_SECRET", "default_agent_secret_change_me")
JARVIS_SENTINEL_SECRET = os.environ.get("JARVIS_SENTINEL_SECRET", "default_sentinel_secret_change_me")

eleven_client = ElevenLabs(api_key=ELEVEN_API_KEY) if ELEVEN_API_KEY else None

# ---------------------------------------------------------
# ESTADO GLOBAL Y ADMINISTRADOR DE WEBSOCKETS
# ---------------------------------------------------------
class EcosistemaState:
    def __init__(self):
        self.pc_status = "offline"  # "offline", "online", "waking"
        self.pc_last_seen = None
        self.sentinel_connected = False
        self.pc_agent_ws: WebSocket = None
        self.sentinel_ws: WebSocket = None

state = EcosistemaState()

# ---------------------------------------------------------
# HELPER FUNCTIONS (GEO, WEATHER, GEMINI)
# ---------------------------------------------------------
def obtener_ubicacion_por_ip(ip: str) -> dict:
    try:
        res = requests.get(f"http://ip-api.com/json/{ip}?lang=es", timeout=3).json()
        if res.get("status") == "success":
            return {
                "ciudad": res.get("city", "Desconocida"),
                "provincia": res.get("regionName", "Desconocida"),
                "pais": res.get("country", "Desconocida"),
                "timezone": res.get("timezone", "UTC")
            }
    except Exception:
        pass
    return {"ciudad": "Buenos Aires", "provincia": "Buenos Aires", "pais": "Argentina", "timezone": "America/Argentina/Buenos_Aires"}

def obtener_clima_actual(ciudad: str) -> str:
    if not WEATHER_API_KEY:
        return "Clave de WeatherAPI no configurada."
    try:
        url = f"http://api.weatherapi.com/v1/current.json?key={WEATHER_API_KEY}&q={ciudad}&lang=es"
        res = requests.get(url, timeout=3).json()
        if "current" in res:
            temp = res["current"]["temp_c"]
            cond = res["current"]["condition"]["text"]
            return f"{temp}°C, {cond}"
    except Exception:
        pass
    return "No se pudo obtener el clima."

def generar_respuesta_con_flash(user_text: str, client_ip: str) -> str:
    if not GEMINI_KEYS:
        raise ValueError("No hay API keys de Gemini configuradas en GEMINI_API_KEYS o GEMINI_API_KEY.")

    info_geo = obtener_ubicacion_por_ip(client_ip)
    ciudad_actual = info_geo["ciudad"]
    provincia_actual = info_geo["provincia"]
    pais_actual = info_geo["pais"]

    try:
        user_tz = ZoneInfo(info_geo["timezone"])
    except Exception:
        user_tz = ZoneInfo("UTC")

    hora_actual = datetime.datetime.now(user_tz).strftime('%H:%M (%d-%m-%Y)')

    contexto_extra = f"\n- ESTADO DEL HARDWARE: PC Local: {state.pc_status.upper()} | Centinela WoL: {'CONECTADO' if state.sentinel_connected else 'DESCONECTADO'}"
    texto_lower = user_text.lower()

    if any(palabra in texto_lower for palabra in ["clima", "tiempo", "temperatura", "hace frío", "hace calor"]):
        ciudad_objetivo = None
        palabras = user_text.split()
        for i, p in enumerate(palabras):
            if p.lower() in ["en", "de", "para"] and i + 1 < len(palabras):
                ciudad_objetivo = " ".join(palabras[i+1:]).strip("?.,!")
                break
        if not ciudad_objetivo and len(palabras) > 2:
            ciudad_objetivo = palabras[-1].strip("?.,!")

        if ciudad_objetivo and ciudad_objetivo not in ["clima", "tiempo", "temperatura", "el"]:
            clima_info = obtener_clima_actual(ciudad_objetivo)
            contexto_extra += f"\n- DATOS METEOROLÓGICOS (CELSIUS °C): Clima en {ciudad_objetivo}: {clima_info}"
        else:
            clima_info = obtener_clima_actual(ciudad_actual)
            contexto_extra += f"\n- DATOS METEOROLÓGICOS (CELSIUS °C): Clima actual ({ciudad_actual}): {clima_info}"

    system_instruction_text = (
        f"Eres J.A.R.V.I.S., un asistente de inteligencia artificial avanzado, formal y eficiente. "
        f"Ubicación del usuario: {ciudad_actual}, {provincia_actual}, {pais_actual}. Hora local: {hora_actual}. "
        f"{contexto_extra}\n"
        "REGLA 1: Da respuestas extremadamente directas, conversacionales y breves. "
        "REGLA 2: Usa siempre el sistema métrico (°C)."
    )

    config = types.GenerateContentConfig(
        system_instruction=system_instruction_text,
        temperature=0.7
    )

    ultimo_error = None
    for api_key in GEMINI_KEYS:
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=user_text,
                config=config
            )
            if response and response.text:
                return response.text
        except Exception as e:
            ultimo_error = e
            continue

    raise Exception(f"Fallo en llamadas a Gemini. Último error registrado: {ultimo_error}")

# ---------------------------------------------------------
# ENDPOINTS Y RUTAS
# ---------------------------------------------------------
class PromptRequest(BaseModel):
    text: str

@app.get("/status")
def get_status():
    return {
        "pc_status": state.pc_status,
        "pc_last_seen": state.pc_last_seen,
        "sentinel_connected": state.sentinel_connected
    }

@app.post("/procesar")
async def procesar(payload: PromptRequest, request: Request):
    try:
        user_text = payload.text
        texto_lower = user_text.lower()

        # Interceptación de comandos físicos
        if "prende la pc" in texto_lower or "encender la pc" in texto_lower:
            if state.pc_status == "online":
                respuesta_texto = "Señor, la PC ya se encuentra encendida y en línea."
            elif state.sentinel_ws:
                await state.sentinel_ws.send_text(json.dumps({"action": "wake_on_lan"}))
                state.pc_status = "waking"
                respuesta_texto = "Enviando orden de encendido vía el centinela local..."
            else:
                respuesta_texto = "No puedo encender la PC en este momento porque el centinela local (iPhone 7) está desconectado de la red."
        elif "apaga la pc" in texto_lower or "apagar la pc" in texto_lower:
            if state.pc_agent_ws and state.pc_status == "online":
                await state.pc_agent_ws.send_text(json.dumps({"action": "shutdown"}))
                respuesta_texto = "Enviando orden de apagado seguro a la PC..."
            else:
                respuesta_texto = "La PC no está conectada o ya se encuentra apagada."
        else:
            client_ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
            respuesta_texto = generar_respuesta_con_flash(user_text, client_ip)

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
                print(f"⚠️ Audio omitido: {audio_err}")

        return {
            "status": "ok",
            "respuesta_texto": respuesta_texto,
            "audio_base64": audio_b64
        }
    except Exception as e:
        print("❌ ERROR EN /procesar:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------------------------------------
# WEBSOCKETS PARA CONTROL REMOTO
# ---------------------------------------------------------
@app.websocket("/ws/agent")
async def websocket_agent(websocket: WebSocket):
    secret = websocket.query_params.get("secret")
    if secret != JARVIS_AGENT_SECRET:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    state.pc_agent_ws = websocket
    state.pc_status = "online"
    state.pc_last_seen = datetime.datetime.now().isoformat()
    print("💻 Agente de PC Windows Conectado.")

    try:
        while True:
            data = await websocket.receive_text()
            state.pc_last_seen = datetime.datetime.now().isoformat()
            # Procesar pings/heartbeats del agente
    except WebSocketDisconnect:
        state.pc_agent_ws = None
        state.pc_status = "offline"
        print("💻 Agente de PC Windows Desconectado.")

@app.websocket("/ws/sentinel")
async def websocket_sentinel(websocket: WebSocket):
    secret = websocket.query_params.get("secret")
    if secret != JARVIS_SENTINEL_SECRET:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    state.sentinel_ws = websocket
    state.sentinel_connected = True
    print("📱 Centinela iPhone 7 Conectado.")

    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        state.sentinel_ws = None
        state.sentinel_connected = False
        print("📱 Centinela iPhone 7 Desconectado.")

# ---------------------------------------------------------
# FRONTEND HTML
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return HTML_FRONTEND

HTML_FRONTEND = """
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
            <div class="bubble">Sistemas de geolocalización y telemetría autónoma enlazados. A su servicio, señor.</div>
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

                const actionsContainer = document.createElement('div');
                actionsContainer.style.marginTop = '8px';

                // Botón Exportar PDF
                const pdfBtn = document.createElement('button');
                pdfBtn.className = 'action-link-btn';
                pdfBtn.innerHTML = '📄 PDF';
                pdfBtn.onclick = () => {
                    const { jsPDF } = window.jspdf;
                    const doc = new jsPDF();
                    doc.text(text.replace(/<[^>]*>?/gm, ''), 15, 20);
                    doc.save(`jarvis_${Date.now()}.pdf`);
                };
                actionsContainer.appendChild(pdfBtn);

                // Botón Descargar Audio .WAV (solo si viene un audio generado)
                if (audioSrc) {
                    const wavBtn = document.createElement('a');
                    wavBtn.className = 'action-link-btn';
                    wavBtn.innerHTML = '🔊 Descargar WAV';
                    wavBtn.href = audioSrc;
                    wavBtn.download = fileName || `jarvis_${Date.now()}.wav`;
                    actionsContainer.appendChild(wavBtn);
                }

                bubble.appendChild(actionsContainer);
            }
            wrapper.appendChild(bubble);
            chatContainer.appendChild(wrapper);
            chatContainer.scrollTop = chatContainer.scrollHeight;
        }
    </script>
</body>
</html>
"""
