import base64
import datetime
import io
import json
import os
import sqlite3
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

# ==================== CONFIGURACIÓN DE BASE DE DATOS (HISTORIAL) ====================
DB_FILE = "jarvis_chats.db"

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                sender TEXT,
                text TEXT,
                audio_b64 TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES sessions (id)
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Error inicializando base de datos: {e}")

init_db()

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

# Inicializar ElevenLabs con manejo de errores defensivo
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY) if ELEVENLABS_API_KEY else None

class PromptRequest(BaseModel):
    text: str
    session_id: int = None

def generar_respuesta_con_fallback(user_text: str) -> str:
    if not GEMINI_KEYS:
        return "⚠️ Error: No hay API keys de Gemini configuradas en el entorno."

    ultimo_error = None
    for api_key in GEMINI_KEYS:
        try:
            client = genai.Client(api_key=api_key)
            # Usando Gemini 3.6 Flash
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=user_text,
                config={
                    "system_instruction": "Eres JARVIS, un asistente de inteligencia artificial avanzado, formal, técnico, eficiente y de respuestas directas. Si generas código (como Python), utiliza bloques de código limpios."
                }
            )
            if response and response.text:
                return response.text
        except Exception as e:
            print(f"⚠️ API Key falló: {e}")
            ultimo_error = e
            continue

    return f"⚠️ Error generando respuesta con Gemini: {str(ultimo_error)}"

def subir_a_drive_y_registrar(texto: str, wav_bytes: bytes, filename: str):
    """Sube el audio WAV a Google Drive y registra en Google Sheets de forma segura sin bloquear."""
    try:
        if not CREDENTIALS_JSON:
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
        except Exception:
            pass

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
        print(f"⚠️ Aviso no crítico (Drive/Sheets): {e}")
        return "Error al guardar en Drive"

# ==================== ENDPOINTS ====================

@app.get("/sessions")
def get_sessions():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, created_at FROM sessions ORDER BY id DESC")
        rows = cursor.fetchall()
        conn.close()
        return [{"id": r[0], "title": r[1], "created_at": r[2]} for r in rows]
    except Exception as e:
        print(f"Error en /sessions: {e}")
        return []

@app.get("/sessions/{session_id}")
def get_session_messages(session_id: int):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT sender, text, audio_b64 FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,))
        rows = cursor.fetchall()
        conn.close()
        return [{"sender": r[0], "text": r[1], "audio_b64": r[2]} for r in rows]
    except Exception as e:
        print(f"Error en /sessions/{session_id}: {e}")
        return []

@app.post("/procesar")
def procesar(payload: PromptRequest):
    try:
        user_text = payload.text
        session_id = payload.session_id

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        if not session_id:
            title = user_text[:30] + "..." if len(user_text) > 30 else user_text
            cursor.execute("INSERT INTO sessions (title) VALUES (?)", (title,))
            session_id = cursor.lastrowid

        cursor.execute("INSERT INTO messages (session_id, sender, text, audio_b64) VALUES (?, ?, ?, ?)",
                       (session_id, 'user', user_text, None))
        conn.commit()
        conn.close()

        # 1. Generar respuesta con Gemini
        print(f"🤖 Consultando a Gemini para: '{user_text}'")
        respuesta_texto = generar_respuesta_con_fallback(user_text)

        # 2. Generar audio con ElevenLabs (con respaldo en caso de fallo de cuota/clave)
        audio_b64 = ""
        wav_bytes = b""
        try:
            if eleven_client:
                print("🎙️ Generando audio con ElevenLabs...")
                audio_stream = eleven_client.text_to_speech.convert(
                    text=respuesta_texto[:500],
                    voice_id="OqoIeNOqjjjkwABBwfFl",
                    model_id="eleven_multilingual_v2",
                    output_format="mp3_44100_128"
                )
                audio_bytes = b"".join(chunk for chunk in audio_stream)
                
                audio_mp3 = AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")
                audio_wav = audio_mp3.set_frame_rate(44100).set_channels(1)
                wav_io = io.BytesIO()
                audio_wav.export(wav_io, format="wav")
                wav_bytes = wav_io.getvalue()
                audio_b64 = base64.b64encode(wav_bytes).decode('utf-8')
        except Exception as audio_err:
            print(f"⚠️ Error generando audio con ElevenLabs (continuando solo con texto): {audio_err}")

        # 3. Subir a Drive (si hay audio)
        if wav_bytes:
            filename = f'audio_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.wav'
            subir_a_drive_y_registrar(user_text, wav_bytes, filename)

        # Guardar respuesta de JARVIS
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (session_id, sender, text, audio_b64) VALUES (?, ?, ?, ?)",
                       (session_id, 'jarvis', respuesta_texto, audio_b64))
        conn.commit()
        conn.close()

        return {
            "status": "ok",
            "session_id": session_id,
            "respuesta_texto": respuesta_texto,
            "audio_base64": audio_b64
        }

    except Exception as e:
        print(f"❌ Error crítico en /procesar: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JARVIS - Gemini Studio</title>
        <!-- Markdown Parser & Code Highlighter -->
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/styles/atom-one-dark.min.css">
        <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/highlight.min.js"></script>
        <!-- jsPDF para descarga de PDFs -->
        <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
        
        <style>
            :root {
                --bg-main: #131314;
                --bg-sidebar: #1e1f20;
                --bg-chat: #1e1f20;
                --bg-input: #2b2c2f;
                --text-main: #e3e3e3;
                --text-muted: #8e918f;
                --accent: #00ffcc;
                --accent-hover: #00b38f;
            }
            * { box-sizing: border-box; }
            body { background: var(--bg-main); color: var(--text-main); font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 0; display: flex; height: 100vh; overflow: hidden; }
            
            /* Sidebar */
            #sidebar { width: 260px; background: var(--bg-sidebar); display: flex; flex-direction: column; border-right: 1px solid #333; transition: transform 0.3s ease; z-index: 10; }
            #sidebar.collapsed { transform: translateX(-260px); position: absolute; height: 100%; }
            .sidebar-header { padding: 15px; display: flex; align-items: center; justify-content: space-between; }
            .new-chat-btn { background: var(--bg-input); border: 1px solid #444; color: var(--text-main); padding: 10px 15px; border-radius: 20px; cursor: pointer; font-size: 14px; width: 90%; margin: 0 auto; text-align: left; display: flex; align-items: center; gap: 8px; }
            .new-chat-btn:hover { background: #333; }
            
            .search-box { padding: 0 15px 10px 15px; }
            .search-box input { width: 100%; background: var(--bg-input); border: none; padding: 8px 12px; border-radius: 8px; color: white; font-size: 13px; outline: none; }
            
            .sessions-list { flex: 1; overflow-y: auto; padding: 0 10px; }
            .session-item { padding: 10px 12px; border-radius: 8px; cursor: pointer; font-size: 13px; color: var(--text-main); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; }
            .session-item:hover { background: #282a2c; }
            .session-item.active { background: #333538; color: var(--accent); }

            /* Main Content */
            #main { flex: 1; display: flex; flex-direction: column; height: 100vh; position: relative; }
            header { padding: 15px 20px; display: flex; align-items: center; justify-content: space-between; background: var(--bg-main); border-bottom: 1px solid #222; }
            .toggle-sidebar-btn { background: none; border: none; color: var(--text-main); font-size: 20px; cursor: pointer; }
            
            #chat-container { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 20px; max-width: 800px; width: 100%; margin: 0 auto; }
            .message-wrapper { display: flex; flex-direction: column; max-width: 85%; }
            .message-wrapper.user { align-self: flex-end; }
            .message-wrapper.jarvis { align-self: flex-start; width: 100%; }
            
            .bubble { padding: 14px 18px; border-radius: 12px; font-size: 14px; line-height: 1.5; word-wrap: break-word; }
            .user .bubble { background: #2b2c2f; color: #fff; border-bottom-right-radius: 4px; }
            .jarvis .bubble { background: transparent; color: var(--text-main); padding-left: 0; }
            
            /* Estilos para bloques de código */
            pre { background: #0f1115 !important; padding: 15px; border-radius: 8px; overflow-x: auto; border: 1px solid #333; }
            code { font-family: 'Courier New', Courier, monospace; }
            .code-header { background: #1a1d23; padding: 5px 10px; font-size: 11px; color: var(--text-muted); display: flex; justify-content: space-between; border-top-left-radius: 8px; border-top-right-radius: 8px; border: 1px solid #333; border-bottom: none; }
            .copy-btn { background: none; border: none; color: var(--accent); cursor: pointer; font-size: 11px; }

            /* Footer de entrada */
            footer { padding: 15px; background: var(--bg-main); display: flex; flex-direction: column; align-items: center; gap: 8px; }
            .input-box-container { background: var(--bg-input); border-radius: 24px; padding: 8px 15px; display: flex; align-items: center; gap: 10px; width: 100%; max-width: 800px; border: 1px solid #3a3b3f; }
            .input-box-container:focus-within { border-color: var(--accent); }
            
            textarea { flex: 1; background: none; border: none; color: white; font-size: 15px; outline: none; resize: none; max-height: 120px; font-family: inherit; }
            textarea::placeholder { color: var(--text-muted); }
            
            .action-buttons { display: flex; align-items: center; gap: 8px; }
            .icon-btn { background: none; border: none; color: var(--text-main); font-size: 18px; cursor: pointer; padding: 6px; border-radius: 50%; display: flex; align-items: center; justify-content: center; }
            .icon-btn:hover { background: rgba(255,255,255,0.1); }
            .icon-btn.active { color: #ff4d4d; background: rgba(255,77,77,0.1); }
            
            #status { font-size: 11px; color: var(--text-muted); text-align: center; }
            .pdf-btn { margin-top: 8px; background: #222; border: 1px solid var(--accent); color: var(--accent); padding: 5px 10px; border-radius: 6px; font-size: 12px; cursor: pointer; }
            .pdf-btn:hover { background: var(--accent); color: #000; }
        </style>
    </head>
    <body>
        
        <!-- BARRA LATERAL (HISTORIAL) -->
        <div id="sidebar">
            <div class="sidebar-header">
                <span style="font-weight: bold; color: var(--accent);">J.A.R.V.I.S. Studio</span>
            </div>
            <button class="new-chat-btn" onclick="nuevaConversacion()">➕ Nueva conversación</button>
            <div class="search-box" style="margin-top: 15px;">
                <input type="text" id="search-sessions" placeholder="Buscar en historial..." oninput="filtrarHistorial(this.value)" />
            </div>
            <div class="sessions-list" id="sessions-list"></div>
        </div>

        <!-- CONTENEDOR PRINCIPAL -->
        <div id="main">
            <header>
                <div style="display: flex; align-items: center; gap: 12px;">
                    <button class="toggle-sidebar-btn" onclick="toggleSidebar()">☰</button>
                    <span id="current-title" style="font-weight: 500; font-size: 16px;">Nueva Conversación</span>
                </div>
                <div id="status">Sistema en espera</div>
            </header>

            <div id="chat-container">
                <div class="message-wrapper jarvis">
                    <div class="bubble">Hola Santino. Panel central de JARVIS enlazado y listo. ¿Qué desarrollamos hoy?</div>
                </div>
            </div>

            <footer>
                <div class="input-box-container">
                    <textarea id="user-input" rows="1" placeholder="Pregúntale algo a JARVIS o escribe código..." oninput="autoExpand(this)"></textarea>
                    <div class="action-buttons">
                        <button class="icon-btn" id="mic-btn" onclick="toggleVoz()" title="Modo Voz Continua">🎙️</button>
                        <button class="icon-btn" onclick="enviarMensaje()" title="Enviar">🚀</button>
                    </div>
                </div>
                <div style="font-size: 11px; color: var(--text-muted);">JARVIS v3.6 - Cloud & Local Bridge</div>
            </footer>
        </div>

        <script>
            let currentSessionId = null;
            let isConversing = false;
            let recognition = null;
            let currentAudio = null;
            let allSessions = [];

            window.onload = async () => {
                await cargarHistorial();
            };

            async function cargarHistorial() {
                try {
                    const res = await fetch('/sessions');
                    allSessions = await res.json();
                    renderizarHistorial(allSessions);
                } catch (e) {
                    console.error("Error al cargar historial:", e);
                }
            }

            function renderizarHistorial(sessions) {
                const list = document.getElementById('sessions-list');
                list.innerHTML = '';
                sessions.forEach(s => {
                    const div = document.createElement('div');
                    div.className = `session-item ${s.id === currentSessionId ? 'active' : ''}`;
                    div.innerText = s.title;
                    div.onclick = () => seleccionarSesion(s.id, s.title);
                    list.appendChild(div);
                });
            }

            function filtrarHistorial(query) {
                const filtradas = allSessions.filter(s => s.title.toLowerCase().includes(query.toLowerCase()));
                renderizarHistorial(filtradas);
            }

            async function seleccionarSesion(id, title) {
                currentSessionId = id;
                document.getElementById('current-title').innerText = title;
                renderizarHistorial(allSessions);

                const res = await fetch(`/sessions/${id}`);
                const messages = await res.json();

                const chatContainer = document.getElementById('chat-container');
                chatContainer.innerHTML = '';

                messages.forEach(m => {
                    appendMessageUI(m.text, m.sender === 'user' ? 'user' : 'jarvis', m.audio_b64);
                });
            }

            function nuevaConversacion() {
                currentSessionId = null;
                document.getElementById('current-title').innerText = "Nueva Conversación";
                document.getElementById('chat-container').innerHTML = `
                    <div class="message-wrapper jarvis">
                        <div class="bubble">Nueva sesión iniciada. ¿En qué te asisto?</div>
                    </div>
                `;
                renderizarHistorial(allSessions);
            }

            function toggleSidebar() {
                document.getElementById('sidebar').classList.toggle('collapsed');
            }

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

                recognition.onstart = () => { document.getElementById('status').innerText = "Escuchando..."; };
                recognition.onresult = async (event) => {
                    const transcript = event.results[0][0].transcript;
                    await enviarTexto(transcript);
                };
                recognition.onend = () => {
                    if (isConversing) {
                        try { recognition.start(); } catch(e){}
                    } else {
                        document.getElementById('status').innerText = "En espera";
                    }
                };
            }

            function toggleVoz() {
                if (!recognition) {
                    alert("Tu navegador no soporta reconocimiento de voz.");
                    return;
                }
                isConversing = !isConversing;
                const micBtn = document.getElementById('mic-btn');
                if (isConversing) {
                    micBtn.classList.add('active');
                    if(currentAudio) currentAudio.pause();
                    try { recognition.start(); } catch(e){}
                } else {
                    micBtn.classList.remove('active');
                    try { recognition.stop(); } catch(e){}
                    document.getElementById('status').innerText = "En espera";
                }
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
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    enviarMensaje();
                }
            };

            async function enviarTexto(text) {
                if (currentAudio) { currentAudio.pause(); currentAudio = null; }
                if (isConversing) { try { recognition.stop(); } catch(e){} }

                appendMessageUI(text, 'user');
                document.getElementById('status').innerText = "Procesando...";

                try {
                    const res = await fetch('/procesar', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ text: text, session_id: currentSessionId })
                    });
                    
                    if (!res.ok) {
                        throw new Error("Error en el servidor backend.");
                    }

                    const data = await res.json();

                    if (data.status === "ok") {
                        currentSessionId = data.session_id;
                        await cargarHistorial();
                        const activeSes = allSessions.find(s => s.id === currentSessionId);
                        if(activeSes) {
                            document.getElementById('current-title').innerText = activeSes.title;
                        }

                        appendMessageUI(data.respuesta_texto, 'jarvis', data.audio_base64);

                        if (data.audio_base64) {
                            const audioSrc = "data:audio/wav;base64," + data.audio_base64;
                            currentAudio = new Audio(audioSrc);
                            document.getElementById('status').innerText = "JARVIS hablando...";
                            currentAudio.play().catch(e => console.log(e));

                            currentAudio.onended = () => {
                                currentAudio = null;
                                if (isConversing) {
                                    try { recognition.start(); } catch(e){}
                                } else {
                                    document.getElementById('status').innerText = "En espera";
                                }
                            };
                        } else {
                            document.getElementById('status').innerText = "En espera";
                            if (isConversing) { try { recognition.start(); } catch(e){} }
                        }
                    }
                } catch (e) {
                    document.getElementById('status').innerText = "Error en procesamiento";
                    appendMessageUI("⚠️ Ocurrió un error al procesar tu solicitud en el servidor.", 'jarvis');
                    console.error(e);
                    if (isConversing) { setTimeout(() => { try { recognition.start(); } catch(e){} }, 2000); }
                }
            }

            function appendMessageUI(text, sender, audioB64 = null) {
                const container = document.getElementById('chat-container');
                const wrapper = document.createElement('div');
                wrapper.className = `message-wrapper ${sender}`;

                const bubble = document.createElement('div');
                bubble.className = 'bubble';

                if (sender === 'user') {
                    bubble.innerText = text;
                } else {
                    bubble.innerHTML = marked.parse(text);
                    
                    bubble.querySelectorAll('pre').forEach(pre => {
                        const header = document.createElement('div');
                        header.className = 'code-header';
                        header.innerHTML = `<span>código</span> <button class="copy-btn" onclick="copiarCodigo(this)">Copiar</button>`;
                        pre.parentNode.insertBefore(header, pre);
                    });

                    const pdfBtn = document.createElement('button');
                    pdfBtn.className = 'pdf-btn';
                    pdfBtn.innerHTML = '📄 Descargar respuesta en PDF';
                    pdfBtn.onclick = () => generarPDF(text);
                    bubble.appendChild(pdfBtn);

                    if (audioB64) {
                        const audioSrc = "data:audio/wav;base64," + audioB64;
                        const dl = document.createElement('a');
                        dl.href = audioSrc;
                        dl.download = `jarvis_${Date.now()}.wav`;
                        dl.className = 'pdf-btn';
                        dl.style.marginLeft = '8px';
                        dl.style.textDecoration = 'none';
                        dl.style.display = 'inline-block';
                        dl.innerText = '📥 Descargar WAV';
                        bubble.appendChild(dl);
                    }
                }

                wrapper.appendChild(bubble);
                container.appendChild(wrapper);
                container.scrollTop = container.scrollHeight;
                hljs.highlightAll();
            }

            function copiarCodigo(btn) {
                const code = btn.closest('.message-wrapper').querySelector('code').innerText;
                navigator.clipboard.writeText(code);
                btn.innerText = '¡Copiado!';
                setTimeout(() => btn.innerText = 'Copiar', 2000);
            }

            function generarPDF(text) {
                const { jsPDF } = window.jspdf;
                const doc = new jsPDF();
                doc.setFont("Helvetica", "normal");
                doc.setFontSize(12);
                
                const splitText = doc.splitTextToSize(text.replace(/<[^>]*>?/gm, ''), 180);
                doc.text(splitText, 15, 20);
                doc.save(`jarvis_respuesta_${Date.now()}.pdf`);
            }
        </script>
    </body>
    </html>
    """
