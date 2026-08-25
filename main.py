import json
import os
import socket
import requests
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
import google.generativeai as genai

# ---------------------------------------------------------
# CONFIGURACIÓN SECRETA DESDE RENDER ENVIRONMENT
# ---------------------------------------------------------
# Se eliminaron todos los datos personales por defecto.
# Todo debe estar configurado en el panel "Environment" de Render.
DUCKDNS_DOMAIN = os.environ.get("DUCKDNS_DOMAIN", "")
DUCKDNS_TOKEN = os.environ.get("DUCKDNS_TOKEN", "")
TARGET_MAC = os.environ.get("TARGET_MAC", "")
WOL_PORT = int(os.environ.get("WOL_PORT", "9")) # 9 es el puerto estándar WoL

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")

# Configurar cliente de Google Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Instancia de FastAPI
app = FastAPI()

# ---------------------------------------------------------
# ESTADO GLOBAL DE LA APLICACIÓN
# ---------------------------------------------------------
class AppState:
    def __init__(self):
        self.pc_status = "offline"  # offline, waking, online
        self.pc_agent_ws: WebSocket | None = None

state = AppState()

# ---------------------------------------------------------
# MODELOS DE PETICIÓN
# ---------------------------------------------------------
class PromptRequest(BaseModel):
    text: str

# ---------------------------------------------------------
# FUNCIONES AUXILIARES
# ---------------------------------------------------------
def enviar_magic_packet():
    """Actualiza la IP pública en DuckDNS y manda el paquete WoL UDP directo al router."""
    if not DUCKDNS_TOKEN or not DUCKDNS_DOMAIN or not TARGET_MAC:
        print("⚠️ Faltan credenciales o la MAC en las variables de entorno.")
        return

    # 1. Actualizar IP pública en DuckDNS
    try:
        requests.get(
            f"https://www.duckdns.org/update?domains={DUCKDNS_DOMAIN}&token={DUCKDNS_TOKEN}",
            timeout=3
        )
        print("✅ IP pública actualizada en DuckDNS.")
    except Exception as e:
        print(f"⚠️ Error actualizando DuckDNS: {e}")

    # 2. Construir y enviar el Magic Packet por UDP
    try:
        # Limpiar la MAC (por si viene con ':' o '-')
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


def generar_respuesta_con_flash(prompt: str, client_ip: str) -> str:
    """Genera la respuesta usando Gemini 3.6 Flash."""
    try:
        # Aquí se especifica la versión exacta que solicitaste
        model = genai.GenerativeModel("gemini-3.6-flash")
        
        system_instruction = (
            "Eres JARVIS, una inteligencia artificial sofisticada, formal, eficiente y cortés. "
            "Responde de manera concisa y clara."
        )
        # Se estructura el prompt
        response = model.generate_content(
            f"{system_instruction}\n\nCliente IP: {client_ip}\nUsuario: {prompt}"
        )
        return response.text
    except Exception as e:
        print(f"⚠️ Error al llamar a Gemini 3.6 Flash: {e}")
        return "Disculpe, señor. Ocurrió un error al procesar su solicitud con el sistema Gemini."


def texto_a_voz_elevenlabs(texto: str) -> bytes | None:
    """Sintetiza texto a voz utilizando ElevenLabs."""
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        print("⚠️ Credenciales de ElevenLabs no configuradas.")
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
        res = requests.post(url, json=data, headers=headers, timeout=10)
        if res.status_code == 200:
            return res.content
        print(f"⚠️ Error ElevenLabs status code: {res.status_code}")
    except Exception as e:
        print(f"⚠️ Error en ElevenLabs: {e}")
    
    return None

# ---------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------
@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "JARVIS Cloud Backend",
        "pc_status": state.pc_status
    }


@app.post("/procesar")
async def procesar(payload: PromptRequest, request: Request):
    user_text = payload.text
    texto_lower = user_text.lower()
    respuesta_texto = ""

    # Lógica de encendido de PC
    if any(cmd in texto_lower for cmd in ["prende la pc", "encender la pc", "prender la pc"]):
        if state.pc_status == "online":
            respuesta_texto = "Señor, la PC ya se encuentra encendida y en línea."
        else:
            enviar_magic_packet()
            state.pc_status = "waking"
            respuesta_texto = "Enviando orden de encendido por paquete WoL a su red local..."

    # Lógica de apagado de PC
    elif any(cmd in texto_lower for cmd in ["apaga la pc", "apagar la pc"]):
        if state.pc_agent_ws and state.pc_status == "online":
            await state.pc_agent_ws.send_text(json.dumps({"action": "shutdown"}))
            respuesta_texto = "Enviando orden de apagado seguro a la PC..."
        else:
            respuesta_texto = "La PC no está conectada o ya se encuentra apagada."

    # Conversación general con Gemini 3.6 Flash
    else:
        # Intentamos obtener la IP del cliente (útil para el prompt)
        client_ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
        respuesta_texto = generar_respuesta_con_flash(user_text, client_ip)

    # Convertir respuesta a audio 
    audio_bytes = texto_a_voz_elevenlabs(respuesta_texto)

    return {
        "respuesta": respuesta_texto,
        "pc_status": state.pc_status,
        "audio_disponible": audio_bytes is not None
    }


@app.websocket("/ws/agent")
async def websocket_agent(websocket: WebSocket):
    """WebSocket utilizado por el agente que corre en la PC local para reportar estado."""
    await websocket.accept()
    state.pc_agent_ws = websocket
    state.pc_status = "online"
    print("🔌 Agente PC conectado.")

    try:
        while True:
            data = await websocket.receive_text()
            print(f"📩 Mensaje del agente PC: {data}")
    except WebSocketDisconnect:
        print("🔌 Agente PC desconectado.")
        state.pc_agent_ws = None
        state.pc_status = "offline"
