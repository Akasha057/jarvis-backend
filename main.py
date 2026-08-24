import base64
import datetime
import io
import json
import os
import secrets
import time
import urllib.parse
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pydub import AudioSegment
from google import genai
from elevenlabs import ElevenLabs
import requests

app = FastAPI()

# ============================================================
# Carga segura de keys de Gemini (sin cambios respecto al original)
# ============================================================
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

# ============================================================
# NUEVO: Configuración de seguridad para el agente y el centinela
# ============================================================
# Estos secrets se definen como variables de entorno en Render.
# NUNCA se hardcodean acá. Si no existen, el sistema arranca "cerrado"
# (rechaza cualquier conexión de agente/centinela) en vez de "abierto".
AGENT_SECRET = os.getenv("JARVIS_AGENT_SECRET")
SENTINEL_SECRET = os.getenv("JARVIS_SENTINEL_SECRET")
CLIENT_SECRET = os.getenv("JARVIS_CLIENT_SECRET")  # token para el iPhone 13 / frontend

if not AGENT_SECRET:
    print("⚠️ JARVIS_AGENT_SECRET no configurado. El endpoint /ws/agent rechazará todas las conexiones.")
if not SENTINEL_SECRET:
    print("⚠️ JARVIS_SENTINEL_SECRET no configurado. El endpoint /ws/sentinel rechazará todas las conexiones.")
if not CLIENT_SECRET:
    print("⚠️ JARVIS_CLIENT_SECRET no configurado. El endpoint /ws/client rechazará todas las conexiones.")


def _tokens_match(a: Optional[str], b: Optional[str]) -> bool:
    """Comparación de tokens resistente a timing attacks."""
    if not a or not b:
        return False
    return secrets.compare_digest(a, b)


# ============================================================
# NUEVO: Estado del sistema (PC + centinela)
# ============================================================
class PCStatus(str, Enum):
    OFFLINE = "offline"    # sin conexión del agente, no sabemos si está prendida o no
    WAKING = "waking"      # se disparó WoL, esperando que el agente se conecte
    ONLINE = "online"      # el agente está conectado y responde
    BUSY = "busy"          # el agente está conectado pero ejecutando un comando largo


class SystemState:
    """
    Estado en memoria del sistema. Vive mientras el proceso de Render esté
    arriba. Si Render reinicia el dyno, este estado se resetea a OFFLINE,
    lo cual es el comportamiento correcto (asumimos lo peor hasta que el
    agente se reconecte y confirme que está online).
    """
    def __init__(self):
        self.pc_status: PCStatus = PCStatus.OFFLINE
        self.agent_ws: Optional[WebSocket] = None
        self.sentinel_ws: Optional[WebSocket] = None
        self.last_agent_heartbeat: Optional[float] = None
        self.last_sentinel_heartbeat: Optional[float] = None
        self.waking_since: Optional[float] = None

    def agent_connected(self, ws: WebSocket):
        self.agent_ws = ws
        self.pc_status = PCStatus.ONLINE
        self.last_agent_heartbeat = time.time()
        self.waking_since = None

    def agent_disconnected(self):
        self.agent_ws = None
        self.pc_status = PCStatus.OFFLINE

    def sentinel_connected(self, ws: WebSocket):
        self.sentinel_ws = ws
        self.last_sentinel_heartbeat = time.time()

    def sentinel_disconnected(self):
        self.sentinel_ws = None

    def mark_waking(self):
        self.pc_status = PCStatus.WAKING
        self.waking_since = time.time()

    def snapshot(self) -> dict:
        return {
            "pc_status": self.pc_status.value,
            "agent_connected": self.agent_ws is not None,
            "sentinel_connected": self.sentinel_ws is not None,
            "last_agent_heartbeat": self.last_agent_heartbeat,
            "last_sentinel_heartbeat": self.last_sentinel_heartbeat,
            "waking_since": self.waking_since,
        }


state = SystemState()

# Timeout: si el agente no manda heartbeat en este intervalo, lo consideramos
# offline aunque el socket TCP siga técnicamente abierto (conexiones zombie).
AGENT_HEARTBEAT_TIMEOUT_SECONDS = 60
# Si llevamos más de esto en estado WAKING sin que el agente aparezca,
# asumimos que el WoL falló.
WAKE_TIMEOUT_SECONDS = 90


class PromptRequest(BaseModel):
    text: str
    client_ip: str = None


class PCCommandRequest(BaseModel):
    action: str          # ej: "open_app", "shutdown", "wake"
    payload: dict = {}


# ============================================================
# Geolocalización y clima (sin cambios respecto al original)
# ============================================================
def obtener_ubicacion_por_ip(ip: str) -> dict:
    """Detecta de forma autónoma la ciudad, provincia y país basándose en la IP del cliente."""
    try:
        url = f"https://ipapi.co/{ip}/json/" if ip and ip not in ["127.0.0.1", "localhost", "::1"] else "https://ipapi.co/json/"
        headers = {"User-Agent": "curl"}
        response = requests.get(url, headers=headers, timeout=3)
        if response.status_code == 200:
            data = response.json()
            return {
                "ciudad": data.get("city", "Buenos Aires"),
                "provincia": data.get("region", "Buenos Aires"),
                "pais": data.get("country_name", "Argentina"),
                "timezone": data.get("timezone", "America/Argentina/Buenos_Aires")
            }
    except Exception:
        pass

    return {
        "ciudad": "Buenos Aires",
        "provincia": "Buenos Aires",
        "pais": "Argentina",
        "timezone": "America/Argentina/Buenos_Aires"
    }


def obtener_clima_actual(ciudad: str) -> str:
    """Consulta el clima en tiempo real forzando el sistema métrico (Celsius) con wttr.in."""
    try:
        url = f"https://wttr.in/{urllib.parse.quote(ciudad)}?format=3&lang=es&m"
        headers = {"User-Agent": "curl"}
        response = requests.get(url, headers=headers, timeout=3)
        if response.status_code == 200:
            return response.text.strip()
    except Exception:
        pass
    return "No disponible en este momento."


def generar_respuesta_con_flash(user_text: str, client_ip: str) -> str:
    if not GEMINI_KEYS:
        raise ValueError("No hay API keys de Gemini disponibles.")

    info_geo = obtener_ubicacion_por_ip(client_ip)
    ciudad_actual = info_geo["ciudad"]
    provincia_actual = info_geo["provincia"]
    pais_actual = info_geo["pais"]

    try:
        user_tz = ZoneInfo(info_geo["timezone"])
    except Exception:
        user_tz = ZoneInfo("UTC")

    hora_actual = datetime.datetime.now(user_tz).strftime('%H:%M (%d-%m-%Y)')

    contexto_extra = ""
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
            contexto_extra = f"\n- DATOS METEOROLÓGICOS (OBLIGATORIO GRADOS CELSIUS °C): Clima en {ciudad_objetivo}: {clima_info}"
        else:
            clima_info = obtener_clima_actual(ciudad_actual)
            contexto_extra = f"\n- DATOS METEOROLÓGICOS (OBLIGATORIO GRADOS CELSIUS °C): Clima en la ubicación actual del usuario ({ciudad_actual}, {provincia_actual}, {pais_actual}): {clima_info}"

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
                        f"INFORMACIÓN AUTÓNOMA EN TIEMPO REAL: El usuario se encuentra actualmente localizado en {ciudad_actual}, {provincia_actual}, {pais_actual}. "
                        f"La hora local exacta en su ubicación es {hora_actual}. "
                        f"{contexto_extra}"
                        "\nREGLA CRÍTICA 1: Da respuestas extremadamente directas, conversacionales y breves. "
                        "REGLA CRÍTICA 2: Utiliza SIEMPRE y exclusivamente el sistema métrico (grados Celsius °C). NUNCA menciones Fahrenheit. "
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


# ============================================================
# Frontend (sin cambios respecto al original — se omite acá por espacio,
# pegar tal cual el bloque HTML del main.py original)
# ============================================================
@app.get("/", response_class=HTMLResponse)
def home():
    return HTML_FRONTEND


@app.post("/procesar")
def procesar(payload: PromptRequest, request: Request):
    try:
        user_text = payload.text

        client_ip = request.headers.get("x-forwarded-for")
        if client_ip:
            client_ip = client_ip.split(",")[0].strip()
        else:
            client_ip = request.client.host

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
                print(f"⚠️ Audio omitido por latencia: {audio_err}")

        return {
            "status": "ok",
            "respuesta_texto": respuesta_texto,
            "audio_base64": audio_b64
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# NUEVO: Endpoint de estado (para debugging y para que el cliente
# sepa si tiene sentido pedir un comando o hay que esperar)
# ============================================================
@app.get("/status")
def get_status():
    return state.snapshot()


# ============================================================
# NUEVO: WebSocket del agente (jarvis_pc_agent.py se conecta acá
# cuando la PC está prendida)
# ============================================================
@app.websocket("/ws/agent")
async def ws_agent(websocket: WebSocket):
    # Autenticación ANTES de aceptar la conexión.
    token = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not AGENT_SECRET or not _tokens_match(token, AGENT_SECRET):
        await websocket.close(code=4401)  # código custom: no autorizado
        return

    await websocket.accept()
    state.agent_connected(websocket)
    print("✅ Agente de PC conectado.")

    try:
        while True:
            # Esperamos mensajes del agente: heartbeats o resultados de comandos.
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")
            if msg_type == "heartbeat":
                state.last_agent_heartbeat = time.time()
            elif msg_type == "command_result":
                # Acá en el futuro reenviamos el resultado al cliente que
                # pidió el comando (via un mapeo request_id -> client_ws).
                print(f"📩 Resultado de comando: {msg.get('payload')}")
            else:
                print(f"⚠️ Mensaje desconocido del agente: {msg}")

    except WebSocketDisconnect:
        pass
    finally:
        state.agent_disconnected()
        print("🔌 Agente de PC desconectado.")


# ============================================================
# NUEVO: WebSocket del centinela (iPhone 7 Plus, cuando exista)
# ============================================================
@app.websocket("/ws/sentinel")
async def ws_sentinel(websocket: WebSocket):
    token = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not SENTINEL_SECRET or not _tokens_match(token, SENTINEL_SECRET):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    state.sentinel_connected(websocket)
    print("✅ Centinela conectado.")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "heartbeat":
                state.last_sentinel_heartbeat = time.time()
            elif msg.get("type") == "wol_sent":
                print("📡 Centinela confirmó envío de paquete WoL.")

    except WebSocketDisconnect:
        pass
    finally:
        state.sentinel_disconnected()
        print("🔌 Centinela desconectado.")


# ============================================================
# NUEVO: WebSocket del cliente (iPhone 13 / frontend)
# ============================================================
@app.websocket("/ws/client")
async def ws_client(websocket: WebSocket):
    token = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not CLIENT_SECRET or not _tokens_match(token, CLIENT_SECRET):
        await websocket.close(code=4401)
        return

    await websocket.accept()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "JSON inválido"})
                continue

            if msg.get("type") == "pc_command":
                await handle_pc_command(websocket, msg.get("action"), msg.get("payload", {}))
            else:
                await websocket.send_json({"type": "error", "detail": f"Tipo de mensaje desconocido: {msg.get('type')}"})

    except WebSocketDisconnect:
        pass


async def handle_pc_command(client_ws: WebSocket, action: str, payload: dict):
    """
    Punto central de decisión: ¿la PC está online? Reenviamos el comando.
    ¿Está offline? Si la acción es "wake", disparamos WoL vía centinela.
    Si no, avisamos que la PC está apagada.
    """
    if action == "wake":
        if state.pc_status == PCStatus.ONLINE:
            await client_ws.send_json({"type": "pc_status", "status": "already_online"})
            return

        if state.sentinel_ws is None:
            await client_ws.send_json({
                "type": "error",
                "detail": "El centinela (iPhone 7) no está conectado. No se puede despertar la PC."
            })
            return

        await state.sentinel_ws.send_json({"type": "send_wol"})
        state.mark_waking()
        await client_ws.send_json({"type": "pc_status", "status": "waking"})
        return

    # Cualquier otra acción requiere que el agente esté online.
    if state.pc_status != PCStatus.ONLINE or state.agent_ws is None:
        await client_ws.send_json({
            "type": "error",
            "detail": f"La PC no está online (estado actual: {state.pc_status.value}). Pedí 'wake' primero."
        })
        return

    await state.agent_ws.send_json({"type": "command", "action": action, "payload": payload})
    await client_ws.send_json({"type": "pc_status", "status": "command_sent"})


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
            }
            wrapper.appendChild(bubble);
            chatContainer.appendChild(wrapper);
            chatContainer.scrollTop = chatContainer.scrollHeight;
        }
    </script>
</body>
</html>
"""
    except Exception as e:

        raise HTTPException(status_code=500, detail=str(e))
