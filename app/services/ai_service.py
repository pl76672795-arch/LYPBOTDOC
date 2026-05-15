import logging
import os
import httpx
import asyncio
import re
from google import genai
from app.core.config import config

logger = logging.getLogger(__name__)

# Instanciamos el cliente de la nueva librería google-genai
api_key = getattr(config, 'gemini_api_key', os.getenv("GEMINI_API_KEY"))
if not api_key:
    logger.error("CRÍTICO: No se encontró GEMINI_API_KEY en las variables de entorno.")
client = genai.Client(api_key=api_key) if api_key else None

async def _llamar_deepseek(prompt: str) -> str:
    """Llama a la API de DeepSeek como modelo de respaldo."""
    deepseek_key = getattr(config, 'deepseek_api_key', os.getenv("DEEPSEEK_API_KEY"))
    if not deepseek_key:
        raise Exception("DEEPSEEK_API_KEY no configurada.")
        
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {deepseek_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}]
    }
    
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        response = await http_client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

async def _generar_con_fallback(prompt: str, modelo_principal: str) -> str:
    """Maneja la generación de contenido con degradación automática a un modelo estable si falla."""
    if not client:
        raise Exception("GEMINI_API_KEY no configurada en el servidor.")
        
    modelo_respaldo = 'gemini-2.0-flash'
    
    # Intento 1: Modelo Principal (con reintentos si hay error 429)
    for intento in range(3):
        try:
            respuesta = await client.aio.models.generate_content(
                model=modelo_principal,
                contents=prompt
            )
            return respuesta.text
        except Exception as e:
            error_str = str(e)
            if "429" in error_str and intento < 2:
                match = re.search(r"Please retry in (\d+\.?\d*)s", error_str)
                espera = float(match.group(1)) + 1 if match else (intento + 1) * 15
                logger.warning(f"⏳ Límite de peticiones (429) en Gemini. Reintentando en {espera:.1f}s...")
                await asyncio.sleep(espera)
                continue
            logger.warning(f"⚠️ Fallo en {modelo_principal}: {e}. Activando fallback a DeepSeek...")
            break
            
    # Intento 2: DeepSeek
    for intento in range(3):
        try:
            return await _llamar_deepseek(prompt)
        except Exception as e2:
            error_str = str(e2)
            if "402" in error_str:
                logger.error("⚠️ Fallo en DeepSeek: 402 Payment Required (Sin saldo). Omitiendo reintentos.")
                break
            if "429" in error_str and intento < 2:
                espera = (intento + 1) * 4
                logger.warning(f"⏳ Límite de peticiones (429) en DeepSeek. Reintentando en {espera}s...")
                await asyncio.sleep(espera)
                continue
            logger.error(f"⚠️ Fallo en DeepSeek: {e2}. Intentando último recurso con {modelo_respaldo}...")
            break
            
    # Intento 3: Supervivencia
    for intento in range(3):
        try:
            respuesta_fallback = await client.aio.models.generate_content(
                model=modelo_respaldo,
                contents=prompt
            )
            return respuesta_fallback.text
        except Exception as e3:
            error_str = str(e3)
            if "429" in error_str and intento < 2:
                match = re.search(r"Please retry in (\d+\.?\d*)s", error_str)
                espera = float(match.group(1)) + 1 if match else (intento + 1) * 15
                logger.warning(f"⏳ Límite de peticiones (429) en Fallback. Reintentando en {espera:.1f}s...")
                await asyncio.sleep(espera)
                continue
            raise e3

async def consultar_gemini(consulta: str) -> str:
    """
    Envía una consulta a Gemini con un rol legal estricto y devuelve la respuesta.
    """
    prompt = (
        "Eres el experto legal de LYP PRO. Usas los datos JSON de RENIEC/SUNAT para redactar contratos de alquiler bajo la Ley 30933.\n\n"
        f"Consulta del cliente: {consulta}"
    )
    
    try:
        # Utiliza el modelo configurado en Railway
        modelo = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        return await _generar_con_fallback(prompt, modelo)
    except Exception as e:
        logger.error(f"Error en la comunicación con Gemini: {e}")
        return (
            "❌ <b>El servicio de Inteligencia Artificial falló.</b>\n\n"
            f"🛠️ <b>Detalle técnico:</b> <code>{str(e)}</code>\n\n"
            "<i>Sugerencia: Verifique que su GEMINI_API_KEY en el archivo .env sea correcta, tenga saldo/cuota disponible, y que disponga de conexión a internet.</i>"
        )

async def normalizar_direccion(direccion: str) -> str:
    """Limpia y estructura formalmente una dirección usando IA."""
    prompt = (
        "Eres un estructurador de datos legales. Limpia y normaliza la siguiente dirección, "
        "separando claramente la Vía, Departamento, Provincia y Distrito si es posible. "
        "Devuelve ÚNICAMENTE la dirección en un formato formal, elegante y continuo (sin asteriscos "
        f"ni saltos de línea), ideal para ser inyectada en un contrato legal:\n\n{direccion}"
    )
    try:
        # Utiliza el modelo configurado en Railway
        modelo = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        respuesta = await _generar_con_fallback(prompt, modelo)
        return respuesta.strip()
    except Exception as e:
        logger.warning(f"⚠️ Fallo al normalizar dirección (Límite IA). Usando texto original: {e}")
        return direccion.strip()  # FALLBACK: Devuelve lo que el usuario escribió sin romper el bot

async def redactar_clausula_compleja(instrucciones: str) -> str:
    """Motor Principal: Usa Flash (no Lite) para redacción legal pesada y condicionales."""
    try:
        respuesta = await _generar_con_fallback(instrucciones, 'gemini-2.0-flash')
        return respuesta.strip()
    except Exception as e:
        raise Exception("Fallo en la redacción avanzada.")

async def analizar_documento_contrato(file_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Lee una imagen o PDF de un contrato y extrae sus datos clave usando Gemini."""
    if not client:
        raise Exception("GEMINI_API_KEY no configurada.")
        
    prompt = (
        "Eres un analista legal experto y detallista. Revisa el documento adjunto (imagen o PDF). "
        "Tu tarea es EXTRAER ABSOLUTAMENTE TODOS LOS DATOS RELEVANTES de forma estructurada y exhaustiva. "
        "Incluye: 1) Partes involucradas (Nombres, DNIs/RUCs, estado civil). 2) Objeto del contrato. "
        "3) Montos, precios, formas de pago y cuentas bancarias. 4) Plazos, fechas y penalidades. "
        "5) Cláusulas especiales y garantías. 6) Firmas, notarios y fechas de suscripción. "
        "Presenta la información con viñetas, de forma clara, profesional y lista para un reporte legal."
    )
    
    modelo = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    
    for intento in range(3):
        try:
            parte_doc = genai.types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
            respuesta = await client.aio.models.generate_content(
                model=modelo,
                contents=[parte_doc, prompt]
            )
            return respuesta.text
        except Exception as e:
            error_str = str(e)
            if "429" in error_str and intento < 2:
                match = re.search(r"Please retry in (\d+\.?\d*)s", error_str)
                espera = float(match.group(1)) + 1 if match else (intento + 1) * 15
                logger.warning(f"⏳ Límite de peticiones (429) en OCR. Reintentando en {espera:.1f}s...")
                await asyncio.sleep(espera)
                continue
            logger.error(f"Fallo al analizar el documento: {e}")
            return f"❌ No pude analizar el documento por un error de IA. Detalle: {str(e)}"
            
    return "❌ No se pudo analizar el documento después de múltiples intentos."