import base64
import io
import json
import os
import datetime
from zoneinfo import ZoneInfo
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from google import genai
from elevenlabs import ElevenLabs
import requests

app = FastAPI()

# Carga segura de keys
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

def obtener_ubicacion_por_ip(ip: str) -> dict:
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

def generar_respuesta_gemini(user_text: str, image_bytes: bytes = None, client_ip: str = "127.0.0.1") -> tuple[str, str]:
    if not GEMINI_KEYS:
        raise ValueError("No hay API keys de Gemini disponibles.")

    info_geo = obtener_ubicacion_por_ip(client_ip)
    ciudad_actual, provincia_actual, pais_actual = info_geo["ciudad"], info_geo["provincia"], info_geo["pais"]
    
    try:
        user_tz = ZoneInfo(info_geo["timezone"])
    except Exception:
        user_tz = ZoneInfo("UTC")
        
    hora_actual = datetime.datetime.now(user_tz).strftime('%H:%M (%d-%m-%Y)')

    contents = []
    if image_bytes:
        contents.append({
            "mime_type": "image/png",
            "data": image_bytes
        })
    contents.append(user_text)

    ultimo_error = None
    for api_key in GEMINI_KEYS:
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config={
                    "system_instruction": (
                        f"Eres J.A.R.V.I.S., un asistente de inteligencia artificial avanzado, formal y eficiente. "
                        f"INFORMACIÓN EN TIEMPO REAL: El usuario se encuentra en {ciudad_actual}, {provincia_actual}, {pais_actual}. "
                        f"La hora local exacta es {hora_actual}. "
                        "REGLA CRÍTICA 1: Da respuestas extremadamente directas, conversacionales y breves (ideales para ser leídas por voz). "
                        "REGLA CRÍTICA 2: Utiliza SIEMPRE el sistema métrico (grados Celsius °C). "
                        "Si el usuario te envía una captura de pantalla, analízala con precisión para guiarlo paso a paso en su PC."
                    )
                }
            )
            if response and response.text:
                respuesta_texto = response.text
                
                audio_b64 = ""
                if eleven_client:
                    try:
                        from pydub import AudioSegment
                        audio_stream = eleven_client.text_to_speech.convert(
                            text=respuesta_texto[:300], 
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
                        print(f"⚠️ Error generando audio: {audio_err}")

                return respuesta_texto, audio_b64
        except Exception as e:
            ultimo_error = e
            continue

    raise Exception(f"Todas las API keys fallaron. Error: {ultimo_error}")

@app.get("/")
def home():
    return {"status": "online", "system": "J.A.R.V.I.S. WebSocket Gateway activo"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_ip = websocket.headers.get("x-forwarded-for")
    if client_ip:
        client_ip = client_ip.split(",")[0].strip()
    else:
        client_ip = websocket.client.host

    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            user_text = payload.get("text", "")
            image_b64 = payload.get("image_base64", None)
            
            image_bytes = base64.b64decode(image_b64) if image_b64 else None
            
            respuesta_texto, audio_b64 = generar_respuesta_gemini(user_text, image_bytes, client_ip)
            
            await websocket.send_text(json.dumps({
                "status": "ok",
                "respuesta_texto": respuesta_texto,
                "audio_base64": audio_b64
            }))
    except WebSocketDisconnect:
        print("Cliente desconectado del WebSocket.")
