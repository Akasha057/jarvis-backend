import os
import json
from fastapi import FastAPI, Request
from google import genai
from google.genai import types

class JarvisUnifiedBrain:
    def __init__(self, api_keys: list[str], elevenlabs_key: str, voice_id: str):
        self.api_keys = api_keys
        self.key_index = 0
        self.elevenlabs_key = elevenlabs_key
        self.voice_id = voice_id

    def _get_next_client(self):
        key = self.api_keys[self.key_index]
        self.key_index = (self.key_index + 1) % len(self.api_keys)
        return genai.Client(api_key=key)

    def pensar(self, prompt: str, usar_pro: bool = False) -> str:
        client = self._get_next_client()
        modelo = 'gemini-2.5-flash'

        system_instruction = (
            "Eres J.A.R.V.I.S., el asistente virtual de inteligencia artificial. "
            "Tu tono es formal, eficiente, educado y con un toque sutil de ironía refinada. "
            "Respondes siempre brevemente en español latino."
        )

        try:
            response = client.models.generate_content(
                model=modelo,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.7,
                )
            )
            return response.text
        except Exception as e:
            return f"Error en el núcleo de procesamiento: {str(e)}"

# Obtenemos las claves de forma segura desde las variables de entorno del servidor
# (Render te dejará configurarlas de manera privada)
raw_keys = os.getenv("GEMINI_API_KEYS", "[]")
mis_claves_gemini = json.loads(raw_keys)

eleven_key = os.getenv("ELEVENLABS_API_KEY", "")
voice_id_jarvis = os.getenv("ELEVENLABS_VOICE_ID", "OqoIeNOqjjjkwABBwfFl")

cerebro = JarvisUnifiedBrain(
    api_keys=mis_claves_gemini,
    elevenlabs_key=eleven_key,
    voice_id=voice_id_jarvis
)

app = FastAPI()

@app.post("/jarvis")
async def handle_jarvis(request: Request):
    data = await request.json()
    pregunta = data.get("prompt", "Hola")
    respuesta = cerebro.pensar(pregunta)
    return {"respuesta": respuesta}

@app.get("/")
def read_root():
    return {"status": "J.A.R.V.I.S. online"}
