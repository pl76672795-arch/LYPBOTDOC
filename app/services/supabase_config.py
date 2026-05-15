import os
import asyncio
import logging
from supabase import create_client, Client
from dotenv import load_dotenv

# Forzar la carga de tu archivo .env local
load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
# Usaremos la service_role_key configurada en Railway para permisos de superusuario
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("🚨 CRÍTICO: SUPABASE_URL o SUPABASE_KEY no existen en el archivo .env. La base de datos fallará.")

# Inicialización del cliente
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

async def guardar_texto_contrato_async(user_uuid: str, tipo_documento: str, texto_generado: str) -> dict:
    """
    Guarda asíncronamente el texto del contrato generado por Gemini en la base de datos.
    Al usar supabase-py (que es síncrono), lo envolvemos en asyncio.to_thread 
    para no bloquear las consultas simultáneas de otros usuarios en Telegram.
    """
    if not supabase:
        raise ValueError("El cliente Supabase no está inicializado. Faltan variables de entorno.")
        
    def _insertar():
        return supabase.table("documentos").insert({
            "user_id": user_uuid,
            "tipo_documento": tipo_documento,
            "storage_path": "texto_ia", # Etiqueta para saber que no es un archivo .docx sino texto
            "metadata": {"texto_completo": texto_generado}
        }).execute()
        
    return await asyncio.to_thread(_insertar)