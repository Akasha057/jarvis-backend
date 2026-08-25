import base64
import json
import os
import socket
import asyncio
import requests
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ---------------------------------------------------------
# CONFIGURACIÓN SECRETA DESDE RENDER ENVIRONMENT
# ---------------------------------------------------------
DUCKDNS_DOMAIN = os.environ.get("DUCKDNS_DOMAIN", "")
DUCKDNS_TOKEN = os.environ.get("DUCKDNS_TOKEN", "")
TARGET_MAC = os.environ.get("TARGET_MAC", "")
WOL_PORT = int(os.environ.get("WOL_PORT", "9"))

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")

# Instancia de FastAPI
app = FastAPI()

# Executor global para evitar congelamiento del Event Loop
executor = ThreadPoolExecutor(max_workers=5)

# ---------------------------------------------------------
# ESTADO GLOBAL DE LA APLICACIÓN
# ---------------------------------------------------------
class AppState:
    def __init__(self):
        self.pc_status = "offline"  # offline, waking, online
        self.pc_agent_ws: WebSocket | None = None
        self.relay_ws: WebSocket | None = None  # WebSocket del iPhone 7 (Puente local)

state = AppState()
key_index = 0

# ---------------------------------------------------------
# MODELOS DE PETICIÓN
# ---------------------------------------------------------
class PromptRequest(BaseModel):
    text: str

# ---------------------------------------------------------
# FUNCIONES AUXILIARES
# ---------------------------------------------------------
def obtener_lista_api_keys() -> list[str]:
    """Carga todas las claves individuales GEMINI_API_KEY_1..10 o GEMINI_API_KEY."""
    keys = []
    for i in range(1, 11):
        key = os.environ.get(f"GEMINI_API_KEY_{i}", "").strip()
        if key:
            keys.append(key)
            
    if not keys:
        raw_keys = os.environ.get("GEMINI_API_KEY", "")
        keys = [k.strip().strip('"').strip("'") for k in raw_keys.split(",") if k.strip()]
        
    return keys


def enviar_magic_packet():
    """Fallback: Actualiza la IP pública en DuckDNS y manda el paquete WoL UDP directo al router."""
    if not DUCKDNS_TOKEN or not DUCKDNS_DOMAIN or not TARGET_MAC:
        print("⚠️ Faltan credenciales o la MAC en las variables de entorno.")
        return

    try:
        requests.get(
            f"https://www.duckdns.org/update?domains={DUCKDNS_DOMAIN}&token={DUCKDNS_TOKEN}",
            timeout=3
        )
        print("✅ IP pública actualizada en DuckDNS.")
    except Exception as e:
        print(f"⚠️ Error actualizando DuckDNS: {e}")

    try:
        mac_limpia = TARGET_MAC.replace(":", "").replace("-", "")
        if len(mac_limpia) != 12:
            print("⚠️ Formato de MAC incorrecto en las variables de entorno.")
            return
            
        mac_bytes = bytes.fromhex(mac_limpia)
        magic_packet = b"\xff" * 6 + mac_bytes * 16

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic_packet, (f"{DUCKDNS_DOMAIN}.duckdns.org", WOL_PORT))
        print(f"📡 Magic Packet enviado exitosamente al puerto {WOL_PORT}")
    except Exception as e:
        print(f"⚠️ Error enviando Magic Packet: {e}")


def _llamar_gemini_sync(key: str, prompt: str, client_ip: str, modelo: str) -> str | None:
    """Llamada HTTP REST directa a Gemini."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={key}"
    
    headers = {
        "Content-Type": "application/json"
    }
    
    payload = {
        "system_instruction": {
            "parts": [
                {
                    "text": "Eres JARVIS, una inteligencia artificial sofisticada, formal, eficiente y cortés. Responde de manera concisa y clara."
                }
            ]
        },
        "contents": [
            {
                "parts": [
                    {
                        "text": f"Cliente IP: {client_ip}\nUsuario: {prompt}"
                    }
                ]
            }
        ]
    }

    res = requests.post(url, json=payload, headers=headers, timeout=8)
    
    if res.status_code == 200:
        data = res.json()
        try:
            texto = data["candidates"][0]["content"]["parts"][0]["text"]
            if texto and texto.strip():
                return texto.strip()
        except (KeyError, IndexError):
            return None
    else:
        raise Exception(f"{res.status_code} {res.text}")
        
    return None


def generar_respuesta_con_flash(prompt: str, client_ip: str) -> str:
    """Intenta generar respuesta rotando claves usando únicamente gemini-3.6-flash vía HTTP directo."""
    global key_index
    keys = obtener_lista_api_keys()
    
    if not keys:
        return "Disculpe, señor. Las claves de API de Gemini no están configuradas en Render."

    total_keys = len(keys)
    modelo_unico = "gemini-3.6-flash"

    for intento in range(total_keys):
        current_key = keys[(key_index + intento) % total_keys]
        
        try:
            future = executor.submit(_llamar_gemini_sync, current_key, prompt, client_ip, modelo_unico)
            resultado = future.result(timeout=8)

            if resultado:
                key_index = (key_index + intento + 1) % total_keys
                return resultado

        except TimeoutError:
            print(f"⚠️ Timeout (8s) alcanzado con la Key ...{current_key[-6:]}")
        except Exception as e:
            print(f"⚠️ Error con Key ...{current_key[-6:]}: {e}")
            if "401" in str(e) or "UNAUTHENTICATED" in str(e):
                continue

    return "Disculpe, señor. Ocurrió un error o tiempo de espera agotado al conectar con el sistema Gemini."


def texto_a_voz_elevenlabs(texto: str) -> bytes | None:
    """Sintetiza texto a voz utilizando ElevenLabs con timeout estricto."""
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY
    }
    data = {
        "text": texto,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75
        }
    }

    try:
        res = requests.post(url, json=data, headers=headers, timeout=4)
        if res.status_code == 200:
            return res.content
    except Exception as e:
        print(f"⚠️ Error o timeout en ElevenLabs: {e}")
    
    return None

# ---------------------------------------------------------
# ENDPOINTS Y ENTORNO WEB
# ---------------------------------------------------------
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/estado")
def obtener_estado():
    """Endpoint que devuelve el estado actual de la PC para la UI web."""
    return {"pc_status": state.pc_status}


@app.get("/", response_class=HTMLResponse)
def read_root():
    """Retorna la interfaz web de JARVIS con sondeo automático de estado y botón de TeamViewer."""
    html_content = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>J.A.R.V.I.S. Control System</title>
        <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='45' fill='%23050a14' stroke='%2300f0ff' stroke-width='6'/><circle cx='50' cy='50' r='20' fill='%23ffffff'/><circle cx='50' cy='50' r='20' fill='%2300f0ff' opacity='0.7'/></svg>">
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;800&family=Rajdhani:wght@400;500;700&display=swap" rel="stylesheet">
        <style>
            :root {
                --cyan-glow: #00f0ff;
                --cyan-dim: #008b9b;
                --bg-dark: #050a14;
                --panel-bg: rgba(8, 20, 38, 0.7);
                --border-color: rgba(0, 240, 255, 0.3);
            }
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                background-color: var(--bg-dark);
                color: #e0f7fc;
                font-family: 'Rajdhani', sans-serif;
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: flex-start;
                padding: 20px;
                background-image: 
                    radial-gradient(circle at 50% 30%, rgba(0, 240, 255, 0.05) 0%, transparent 70%),
                    linear-gradient(rgba(0, 240, 255, 0.03) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(0, 240, 255, 0.03) 1px, transparent 1px);
                background-size: 100% 100%, 30px 30px, 30px 30px;
            }
            header { text-align: center; margin-bottom: 20px; width: 100%; max-width: 800px; }
            h1 {
                font-family: 'Orbitron', sans-serif;
                font-size: 2.2rem;
                letter-spacing: 4px;
                color: var(--cyan-glow);
                text-shadow: 0 0 10px rgba(0, 240, 255, 0.5);
            }
            .arc-container { display: flex; justify-content: center; align-items: center; margin: 15px 0; }
            .arc-reactor {
                position: relative;
                width: 120px; height: 120px;
                border-radius: 50%;
                border: 2px solid var(--border-color);
                box-shadow: 0 0 20px rgba(0, 240, 255, 0.2);
                display: flex; justify-content: center; align-items: center;
            }
            .core {
                width: 50px; height: 50px; border-radius: 50%;
                background: radial-gradient(circle, #ffffff 0%, var(--cyan-glow) 60%, var(--cyan-dim) 100%);
                box-shadow: 0 0 25px var(--cyan-glow), 0 0 50px var(--cyan-glow);
                transition: all 0.3s ease;
            }
            .arc-reactor.speaking .core { animation: pulse-speaking 0.8s infinite alternate; }
            @keyframes pulse-speaking {
                0% { transform: scale(0.9); box-shadow: 0 0 15px var(--cyan-glow); }
                100% { transform: scale(1.25); box-shadow: 0 0 40px var(--cyan-glow), 0 0 70px var(--cyan-glow); }
            }
            .status-badge {
                display: inline-block; padding: 6px 16px; border-radius: 20px;
                font-family: 'Orbitron', sans-serif; font-size: 0.85rem; letter-spacing: 1px;
                border: 1px solid var(--border-color); background: rgba(0, 0, 0, 0.4); margin-top: 10px;
            }
            .status-offline { color: #ff4d4d; border-color: #ff4d4d; }
            .status-waking { color: #ffaa00; border-color: #ffaa00; }
            .status-online { color: #00ff66; border-color: #00ff66; box-shadow: 0 0 10px rgba(0, 255, 102, 0.3); }
            
            .remote-access-bar {
                margin: 10px 0;
            }
            .btn-remote {
                background: rgba(37, 99, 235, 0.2);
                border-color: #2563eb;
                color: #60a5fa;
                text-decoration: none;
                display: inline-block;
                padding: 6px 16px;
                border-radius: 4px;
                font-family: 'Orbitron', sans-serif;
                font-size: 0.85rem;
                font-weight: 600;
                transition: all 0.2s;
            }
            .btn-remote:hover {
                background: #2563eb;
                color: #fff;
                box-shadow: 0 0 10px rgba(37, 99, 235, 0.6);
            }

            .main-panel {
                width: 100%; max-width: 800px; background: var(--panel-bg);
                border: 1px solid var(--border-color); box-shadow: 0 0 15px rgba(0, 0, 0, 0.5);
                backdrop-filter: blur(8px); border-radius: 8px; padding: 20px;
                display: flex; flex-direction: column; height: 500px;
            }
            .chat-log {
                flex: 1; overflow-y: auto; padding-right: 10px;
                display: flex; flex-direction: column; gap: 12px; margin-bottom: 15px;
            }
            .chat-log::-webkit-scrollbar { width: 6px; }
            .chat-log::-webkit-scrollbar-thumb { background: var(--cyan-dim); border-radius: 3px; }
            .msg { max-width: 80%; padding: 10px 14px; border-radius: 6px; font-size: 1.05rem; line-height: 1.4; }
            .msg.user { align-self: flex-end; background: rgba(0, 240, 255, 0.15); border: 1px solid rgba(0, 240, 255, 0.4); color: #ffffff; }
            .msg.jarvis { align-self: flex-start; background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.15); color: #e0f7fc; }
            .controls { display: flex; gap: 10px; }
            input[type="text"] {
                flex: 1; background: rgba(0, 0, 0, 0.5); border: 1px solid var(--border-color);
                padding: 12px 16px; border-radius: 4px; color: #fff;
                font-family: 'Rajdhani', sans-serif; font-size: 1.1rem; outline: none;
            }
            input[type="text"]:focus { border-color: var(--cyan-glow); box-shadow: 0 0 8px rgba(0, 240, 255, 0.4); }
            button {
                background: rgba(0, 240, 255, 0.1); border: 1px solid var(--cyan-glow);
                color: var(--cyan-glow); font-family: 'Orbitron', sans-serif;
                padding: 0 20px; border-radius: 4px; cursor: pointer; transition: all 0.2s; font-weight: 600;
            }
            button:hover { background: var(--cyan-glow); color: #000; box-shadow: 0 0 12px var(--cyan-glow); }
            .btn-mic.recording { background: #ff4d4d; border-color: #ff4d4d; color: #fff; animation: pulse-red 1s infinite alternate; }
            @keyframes pulse-red { 0% { box-shadow: 0 0 5px #ff4d4d; } 100% { box-shadow: 0 0 15px #ff4d4d; } }
        </style>
    </head>
    <body>
        <header>
            <h1>J.A.R.V.I.S.</h1>
            <div class="arc-container">
                <div class="arc-reactor" id="arcReactor"><div class="core"></div></div>
            </div>
            <div>PC STATUS: <span id="statusBadge" class="status-badge status-offline">OFFLINE</span></div>
            <div class="remote-access-bar">
                <a href="https://start.teamviewer.com" class="btn-remote" target="_blank">🖥️ ABRIR TEAMVIEWER WEB</a>
            </div>
        </header>

        <main class="main-panel">
            <div class="chat-log" id="chatLog">
                <div class="msg jarvis">Sistemas en línea, señor. ¿En qué puedo ayudarle hoy?</div>
            </div>

            <div class="controls">
                <input type="text" id="userInput" placeholder="Escriba un comando o pregunta..." onkeydown="if(event.key==='Enter') enviarMensaje()" />
                <button class="btn-mic" id="btnMic" onclick="toggleMic()">🎙️</button>
                <button onclick="enviarMensaje()">ENVIAR</button>
            </div>
        </main>

        <script>
            let pcStatus = "offline";
            let recognition = null;
            let isRecording = false;

            const chatLog = document.getElementById("chatLog");
            const userInput = document.getElementById("userInput");
            const statusBadge = document.getElementById("statusBadge");
            const arcReactor = document.getElementById("arcReactor");
            const btnMic = document.getElementById("btnMic");

            if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
                const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
                recognition = new SpeechRecognition();
                recognition.lang = 'es-ES';
                recognition.continuous = false;
                recognition.interimResults = false;

                recognition.onresult = (event) => {
                    userInput.value = event.results[0][0].transcript;
                    enviarMensaje();
                };
                recognition.onend = () => { isRecording = false; btnMic.classList.remove("recording"); };
                recognition.onerror = () => { isRecording = false; btnMic.classList.remove("recording"); };
            } else {
                btnMic.style.display = "none";
            }

            function toggleMic() {
                if (!recognition) return;
                if (isRecording) { recognition.stop(); } 
                else { recognition.start(); isRecording = true; btnMic.classList.add("recording"); }
            }

            function actualizarEstadoUI(status) {
                pcStatus = status;
                statusBadge.className = "status-badge status-" + status;
                statusBadge.innerText = status.toUpperCase();
            }

            // Sondeo periódico del estado de la PC cada 3 segundos
            async function verificarEstadoPC() {
                try {
                    const response = await fetch("/estado");
                    const data = await response.json();
                    if (data.pc_status) {
                        actualizarEstadoUI(data.pc_status);
                    }
                } catch (err) {
                    console.log("Error consultando estado");
                }
            }
            setInterval(verificarEstadoPC, 3000);

            function agregarMensaje(texto, sender) {
                const msgDiv = document.createElement("div");
                msgDiv.className = "msg " + sender;
                msgDiv.innerText = texto;
                chatLog.appendChild(msgDiv);
                chatLog.scrollTop = chatLog.scrollHeight;
            }

            function reproducirAudioBase64(base64Audio) {
                if (!base64Audio) return;
                const audio = new Audio("data:audio/mp3;base64," + base64Audio);
                arcReactor.classList.add("speaking");
                audio.play().catch(e => console.log("Audio autostart blocked", e));
                audio.onended = () => arcReactor.classList.remove("speaking");
            }

            async function enviarMensaje() {
                const text = userInput.value.trim();
                if (!text) return;

                agregarMensaje(text, "user");
                userInput.value = "";

                try {
                    const response = await fetch("/procesar", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ text: text })
                    });

                    const data = await response.json();
                    if (data.respuesta) { agregarMensaje(data.respuesta, "jarvis"); }
                    if (data.audio_b64) { reproducirAudioBase64(data.audio_b64); }
                    if (data.pc_status) { actualizarEstadoUI(data.pc_status); }

                } catch (err) {
                    agregarMensaje("Error de red al conectar con el servidor.", "jarvis");
                }
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.post("/procesar")
def procesar(payload: PromptRequest, request: Request):
    user_text = payload.text
    texto_lower = user_text.lower()
    respuesta_texto = ""

    if any(cmd in texto_lower for cmd in ["prende la pc", "encender la pc", "prender la pc", "enciende la pc"]):
        if state.pc_status == "online":
            respuesta_texto = "Señor, la PC ya se encuentra encendida y en línea."
        elif state.relay_ws is not None:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(state.relay_ws.send_text(json.dumps({"action": "wake_wol"})))
                loop.close()
                state.pc_status = "waking"
                respuesta_texto = "Orden enviada al iPhone 7 puente. Activando Magic Packet en la red local..."
            except Exception as e:
                print(f"⚠️ Error al comunicar con el relay: {e}")
                enviar_magic_packet()
                state.pc_status = "waking"
                respuesta_texto = "Enviando orden de encendido por paquete WoL tradicional..."
        else:
            enviar_magic_packet()
            state.pc_status = "waking"
            respuesta_texto = "Enviando orden de encendido por paquete WoL a su red local..."

    elif any(cmd in texto_lower for cmd in ["apaga la pc", "apagar la pc"]):
        if state.pc_agent_ws and state.pc_status == "online":
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(state.pc_agent_ws.send_text(json.dumps({"action": "shutdown"})))
            loop.close()
            respuesta_texto = "Enviando orden de apagado seguro a la PC..."
        else:
            respuesta_texto = "La PC não está conectada o ya se encuentra apagada."

    else:
        client_ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
        respuesta_texto = generar_respuesta_con_flash(user_text, client_ip)

    audio_bytes = texto_a_voz_elevenlabs(respuesta_texto)
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8") if audio_bytes else None

    return {
        "respuesta": respuesta_texto,
        "pc_status": state.pc_status,
        "audio_b64": audio_b64
    }


@app.websocket("/ws/agent")
async def websocket_agent(websocket: WebSocket):
    await websocket.accept()
    state.pc_agent_ws = websocket
    state.pc_status = "online"
    print("🔌 Agente PC conectado.")

    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        print("🔌 Agente PC desconectado.")
        state.pc_agent_ws = None
        state.pc_status = "offline"


@app.websocket("/ws/relay")
async def websocket_relay(websocket: WebSocket):
    await websocket.accept()
    state.relay_ws = websocket
    print("📱 iPhone 7 (Puente local WoL) conectado y escuchando.")

    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        print("📱 iPhone 7 desconectado.")
        state.relay_ws = None
