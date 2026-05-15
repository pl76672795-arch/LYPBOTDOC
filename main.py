import logging
from pathlib import Path
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.helpers import escape_markdown
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from app.core.config import config
from app.utils.formatters import monto_a_letras, fecha_legal
from app.generators.word_engine import GeneradorWord
from app.services.ai_service import consultar_gemini, normalizar_direccion, analizar_documento_contrato
from app.core.schemas import UserSession, APIError, AIError, StorageError, DocumentError
from pydantic import ValidationError
import httpx
from app.services.supabase_config import supabase, guardar_texto_contrato_async
import html
import json
import asyncio
import base64
import os
from datetime import datetime
import re
try:
    from fpdf import FPDF
except ImportError:
    FPDF = None

# Configuración de logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- MIDDLEWARES Y SERVICIOS DE BASE DE DATOS ---

async def db_get_or_create_user(telegram_id: int, username: str, referral_id: int = None) -> dict:
    """Verifica si el usuario existe en Supabase; si no, lo registra con 5 créditos."""
    def _db_op():
        # Búsqueda de usuario existente
        res = supabase.table("profiles").select("*").eq("telegram_id", telegram_id).execute()
        if res.data:
            return res.data[0]
        
        # Creación de nuevo usuario (El trigger en Supabase registrará el bono en credit_transactions)
        new_user = {
            "telegram_id": telegram_id,
            "nombre": username,
            "credits": 5
            # "plan_id": 'FREE' # Omitido a nivel DB si el schema de SQL actual no tiene la columna,
            # pero lo manejaremos lógicamente en la Interfaz de Usuario.
        }
        res_insert = supabase.table("profiles").insert(new_user).execute()
        user_data = res_insert.data[0] if res_insert.data else {"credits": 5, "nombre": username, "id": None}
        
        # Lógica de Growth Hacking (Recompensa por Referido)
        if referral_id and referral_id != telegram_id and user_data.get("id"):
            ref_res = supabase.table("profiles").select("id, credits").eq("telegram_id", referral_id).execute()
            if ref_res.data:
                referrer_uuid = ref_res.data[0]["id"]
                current_credits = ref_res.data[0]["credits"]
                supabase.table("profiles").update({"credits": current_credits + 2}).eq("id", referrer_uuid).execute()
                supabase.table("credit_transactions").insert({
                    "user_id": referrer_uuid, "monto": 2, "descripcion": "Recompensa de referido (Nuevo Usuario)"
                }).execute()
                
        return user_data
    
    # Ejecuta la llamada bloqueante en un hilo de fondo
    return await asyncio.to_thread(_db_op)

def require_credits(func):
    """Middleware: Verifica que el usuario tenga saldo (credits > 0) antes de procesar el comando."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs) -> int | None:
        user_id = update.effective_user.id
        
        def _get_credits() -> int:
            res = supabase.table("profiles").select("credits").eq("telegram_id", user_id).execute()
            return res.data[0]["credits"] if res.data else 0
            
        saldo_actual = await asyncio.to_thread(_get_credits)
        
        if saldo_actual <= 0:
            keyboard = [[InlineKeyboardButton("💰 Recargar Créditos", callback_data="buy_credits")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg = "⚠️ *Ups, saldo insuficiente*\n\nNo dispones de créditos para realizar esta acción\\. Por favor, recarga tu saldo para continuar usando los servicios de LYP PRO\\."
            
            if update.callback_query:
                await update.callback_query.answer("Saldo insuficiente", show_alert=True)
                await update.callback_query.edit_message_text(msg, parse_mode="MarkdownV2", reply_markup=reply_markup)
            elif update.message:
                await update.message.reply_text(msg, parse_mode="MarkdownV2", reply_markup=reply_markup)
            
            return ConversationHandler.END
            
        return await func(update, context, *args, **kwargs)
    return wrapper

async def guardar_en_supabase_y_cobrar(telegram_id: int, file_path: Path, dni: str, tipo_documento: str = "Contrato de Arrendamiento") -> str:
    """Sube el documento, lo enlaza al usuario y descuenta 1 crédito atómicamente."""
    def _op():
        # 1. Obtención de UUID de Supabase y saldo
        res_user = supabase.table("profiles").select("id").eq("telegram_id", telegram_id).execute()
        if not res_user.data: raise StorageError("Usuario no encontrado en la base de datos.")
        user_uuid = res_user.data[0]["id"]
        
        # 2. Transaccionalidad Atómica (Descontar 1 crédito vía RPC SQL)
        res_cobro = supabase.rpc("cobrar_creditos", {
            "p_user_uuid": user_uuid, "p_costo": 1, "p_descripcion": f"Generación: {tipo_documento} (DNI {dni})"
        }).execute()
        if res_cobro.data is not True: raise StorageError("Créditos insuficientes para procesar la transacción.")

        # 3. Subida al Storage
        fecha = datetime.now().strftime("%Y-%m-%d")
        file_name = f"contratos/{user_uuid}/{fecha}/{dni}.docx"
        with open(file_path, "rb") as f:
            supabase.storage.from_("documentos").upload(file_name, f, {"upsert": "true"})
        
        # 3. Registrar documento en tabla
        supabase.table("documentos").insert({
            "user_id": user_uuid,
            "tipo_documento": tipo_documento,
            "storage_path": file_name,
            "metadata": {"dni": dni, "fecha": fecha}
        }).execute()
        return file_name
    return await asyncio.to_thread(_op)

async def descontar_creditos_basico(telegram_id: int, costo: int, descripcion: str):
    """Helper para descontar créditos en servicios rápidos como consultas de DNI/RUC."""
    def _op():
        res_user = supabase.table("profiles").select("id").eq("telegram_id", telegram_id).execute()
        if res_user.data:
            supabase.rpc("cobrar_creditos", {
                "p_user_uuid": res_user.data[0]["id"], "p_costo": costo, "p_descripcion": descripcion
            }).execute()
    await asyncio.to_thread(_op)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manejador para el comando /start con onboarding seguro y estético en MarkdownV2."""
    user = update.effective_user
    telegram_id = user.id
    username = user.first_name or user.username or "Usuario"
    
    # Detección de enlace de referidos (Deep Linking)
    referral_id = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            referral_id = int(context.args[0].split("_")[1])
        except ValueError:
            pass

    # Identificación silenciosa y asignación de cortesías (Asyncio-safe)
    db_user = await db_get_or_create_user(telegram_id, username, referral_id)
    
    # Escapamos los textos variables en HTML
    safe_name = html.escape(db_user.get("nombre", username))
    credits = db_user.get("credits", 0)
    plan = "Premium 💎" if credits > 10 else ("Standard 🌟" if credits > 0 else "Free ⚪")
    
    welcome_text = (
        "🏛️ <b>LYP PRO - INTELIGENCIA ARTIFICIAL LEGAL</b> 🏛️\n\n"
        f"Bienvenido(a), <b>{safe_name}</b>. Ha ingresado a la plataforma de automatización documental más avanzada del Perú.\n\n"
        "Nuestra tecnología le permite redactar <b>contratos blindados, demandas y certificados en segundos</b>, "
        "con validación oficial del Estado (RENIEC, SUNAT, SUNARP).\n\n"
        "📊 <b>SU PANEL CORPORATIVO:</b>\n"
        f"👤 <b>Titular:</b> {safe_name}\n"
        f"💳 <b>Saldo Disponible:</b> {credits} Crédito(s)\n"
        f"⭐ <b>Nivel de Cuenta:</b> {plan}\n\n"
        "👇 <i>Seleccione una opción rápida o escriba /menu para abrir el panel completo:</i>"
    )
    
    # Botonera Inline de Acceso Rápido
    keyboard = [
        [InlineKeyboardButton("📄 Crear Contrato", callback_data="cat_inmobiliario")],
        [InlineKeyboardButton("🔍 Consultar DNI/RUC", callback_data="cat_reniec")],
        [InlineKeyboardButton("💰 Recargar Créditos", callback_data="buy_credits")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manejador para el comando /menu con Panel de Control Interactivo."""
    keyboard = [
        [InlineKeyboardButton("⚖️ LEGAL", callback_data="cat_legal"), InlineKeyboardButton("🏢 INMOBILIARIO", callback_data="cat_inmobiliario")],
        [InlineKeyboardButton("👥 RRHH", callback_data="cat_rrhh"), InlineKeyboardButton("🏛️ SUNARP", callback_data="cat_sunarp")],
        [InlineKeyboardButton("🏢 SUNAT", callback_data="cat_sunat"), InlineKeyboardButton("👤 RENIEC", callback_data="cat_reniec")],
        [InlineKeyboardButton("⚙️ AJUSTES", callback_data="ajustes"), InlineKeyboardButton("🤖 Soporte IA", callback_data="menu_soporte")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    menu_text = (
        "📋 <b>PANEL DE CONTROL - LYP PRO</b> 📋\n\n"
        "Bienvenido al panel de gestión interactiva.\n"
        "Por favor, seleccione un módulo de nuestra botonera para comenzar su trámite:"
    )

    base_dir = Path(__file__).resolve().parent
    logo_path = base_dir / "assets" / "logo.png"

    if update.message:
        # Verifica si el logo existe para enviarlo como foto, si no, envía solo el texto
        if logo_path.exists():
            with open(logo_path, "rb") as photo:
                await update.message.reply_photo(
                    photo=photo, caption=menu_text, reply_markup=reply_markup, parse_mode="HTML"
                )
        else:
            await update.message.reply_text(menu_text, reply_markup=reply_markup, parse_mode="HTML")

async def _actualizar_mensaje(query, texto, reply_markup=None, img_name="logo.png"):
    """Helper asíncrono para actualizar el texto y la imagen en el menú dinámico."""
    base_dir = Path(__file__).resolve().parent
    img_path = base_dir / "assets" / img_name
    
    # Si el mensaje original tenía foto y la nueva foto existe, actualizamos la multimedia entera
    if query.message.photo:
        if img_path.exists():
            with open(img_path, "rb") as f:
                await query.edit_message_media(
                    media=InputMediaPhoto(f, caption=texto, parse_mode="HTML"),
                    reply_markup=reply_markup
                )
        else:
            await query.edit_message_caption(caption=texto, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await query.edit_message_text(text=texto, reply_markup=reply_markup, parse_mode="HTML")

async def mostrar_opciones_compra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el panel de recarga premium de forma unificada para comandos y botones."""
    keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_principal")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    texto = (
        "💎 <b>LYP PRO - RECARGA DE CRÉDITOS</b> 💎\n\n"
        "Potencia tus trámites y contratos con Inteligencia Artificial.\n\n"
        "💳 <b>Tarifario Oficial:</b>\n"
        "• 10 Créditos ➡️ S/ 15.00\n"
        "• 50 Créditos ➡️ S/ 50.00 <i>(Más popular)</i>\n\n"
        "Para recargar, comunícate directamente con nuestro administrador:\n"
        "👉 <b>@Fenhixde</b>\n\n"
        "<i>Envía el comprobante de Yape/Plin y tus créditos serán asignados al instante.</i>"
    )
    if update.callback_query:
        await _actualizar_mensaje(update.callback_query, texto, reply_markup, "logo.png")
    elif update.message:
        await update.message.reply_text(texto, reply_markup=reply_markup, parse_mode="HTML")

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /buy para acceso rápido a recargas."""
    await mostrar_opciones_compra(update, context)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /help para mostrar el manual de usuario corporativo."""
    texto = (
        "🛠️ <b>CENTRO DE AYUDA LYP PRO</b> 🛠️\n\n"
        "Aquí tienes la lista de comandos para aprovechar al máximo el bot:\n\n"
        "<b>📌 Comandos Principales:</b>\n"
        "🔹 /start - Iniciar el bot y ver tu perfil.\n"
        "🔹 /menu - Abrir el panel de control interactivo.\n"
        "🔹 /help - Mostrar este mensaje de ayuda.\n"
        "🔹 /info - Información corporativa del sistema.\n"
        "🔹 /staff - Conocer al equipo detrás del bot.\n"
        "🔹 /buy - Recargar créditos (coins).\n"
        "🔹 /tarifario - Ver costos de los servicios.\n"
        "🔹 /cancelar - Detener cualquier trámite en curso.\n\n"
        "<b>⚖️ Redacción Legal IA:</b>\n"
        "🔹 /contrato_alquiler - Arrendamiento Ley 30933.\n"
        "🔹 /compraventa - Compraventa Inmobiliaria.\n"
        "🔹 /poder_simple - Carta poder para trámites.\n"
        "🔹 /demanda_alimentos - Demanda de pensión.\n"
        "🔹 /liquidacion - Beneficios sociales.\n"
        "🔹 /certificado - Certificado de trabajo.\n"
        "🔹 /soporte - Asesoría legal directa con la IA.\n\n"
        "<b>🔍 Validaciones del Estado:</b>\n"
        "🔹 /dni [número] - Consulta RENIEC.\n"
        "🔹 /ruc [número] - Consulta SUNAT.\n"
        "🔹 /placa [número] - Consulta SUNARP.\n"
        "🔹 /soat [placa] - Consulta SOAT.\n"
        "🔹 /licencia [dni] - Consulta MTC."
    )
    await update.message.reply_text(texto, parse_mode="HTML")

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /info para detalles del bot."""
    texto = (
        "🏛️ <b>SOBRE LYP DOC BOT</b> 🏛️\n\n"
        "<b>Versión:</b> 3.0.0 Premium (2026)\n"
        "<b>Motor IA:</b> IA Legal de LYP PRO\n"
        "<b>Empresa:</b> LYP PRO S.A.C.\n\n"
        "Este sistema ha sido diseñado para revolucionar el sector legal en el Perú, "
        "brindando herramientas de validación oficial y automatización documental con "
        "respaldo de Inteligencia Artificial.\n\n"
        "🌐 <b>Contacto y Soporte:</b> @Fenhixde"
    )
    await update.message.reply_text(texto, parse_mode="HTML")

async def cmd_staff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /staff para mostrar el equipo detrás de LYP PRO."""
    texto = (
        "👨‍💼 <b>STAFF CORPORATIVO - LYP PRO PREMIUM</b> 👨‍💼\n\n"
        "Conoce al equipo que impulsa la excelencia legal y tecnológica:\n\n"
        "👑 <b>Co-Founder, CEO & Lead Developer:</b>\n"
        "•Ing Alexander (@Fenhixde)\n\n"
        "🛡️ <b>Administradores & Sellers Oficiales:</b>\n"
        "• Directorio de Ventas Premium LYP PRO\n\n"
        "⚖️ <b>Dirección Legal (Chief Legal Officer):</b>\n"
        "• Departamento Jurídico Especializado LYP PRO\n\n"
        "🤖 <b>Infraestructura Tecnológica:</b>\n"
        "• Motor Principal: IA de LYP PRO (Redacción Legal Avanzada)\n"
        "• IA de LYP PRO (Motor de Respaldo y Análisis)\n\n"
        "<i>Innovando la gestión documental con tecnología de punta.</i>"
    )
    await update.message.reply_text(texto, parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Procesa los clics en los botones del menú interactivo de forma asíncrona."""
    query = update.callback_query
    # Obligatorio: responder a la consulta para que Telegram deje de mostrar el icono de "cargando"
    await query.answer()

    # Flujo: Volver al menú principal
    if query.data == "menu_principal":
        keyboard = [
            [InlineKeyboardButton("⚖️ LEGAL", callback_data="cat_legal"), InlineKeyboardButton("🏢 INMOBILIARIO", callback_data="cat_inmobiliario")],
            [InlineKeyboardButton("👥 RRHH", callback_data="cat_rrhh"), InlineKeyboardButton("🏛️ SUNARP", callback_data="cat_sunarp")],
            [InlineKeyboardButton("🏢 SUNAT", callback_data="cat_sunat"), InlineKeyboardButton("👤 RENIEC", callback_data="cat_reniec")],
        [InlineKeyboardButton("⚙️ AJUSTES", callback_data="ajustes"), InlineKeyboardButton("🤖 Soporte IA", callback_data="menu_soporte")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        texto = "📋 <b>PANEL DE CONTROL - LYP PRO</b> 📋\n\nSeleccione un módulo para comenzar su trámite:"
        await _actualizar_mensaje(query, texto, reply_markup, "logo.png")
        
    # Flujo: Recarga de Créditos (Coins)
    elif query.data == "buy_credits":
        await mostrar_opciones_compra(update, context)
            
    # Flujo: Categoría Legal
    elif query.data == "cat_legal":
        keyboard = [
            [InlineKeyboardButton("🔙 Volver al Panel", callback_data="menu_principal")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        texto = (
            "⚖️ <b>MÓDULO LEGAL</b> ⚖️\n\n"
            "Puede solicitar los siguientes servicios escribiendo su comando en el chat:\n\n"
            "🔹 /poder_simple - Carta poder para delegación de trámites.\n"
            "🔹 /demanda_alimentos - Formato de demanda de pensión alimenticia."
        )
        await _actualizar_mensaje(query, texto, reply_markup, "logo_legal.png")

    # Flujo: Categoría Inmobiliaria
    elif query.data == "cat_inmobiliario":
        keyboard = [[InlineKeyboardButton("🔙 Volver al Panel", callback_data="menu_principal")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        texto = (
            "🏢 <b>MÓDULO INMOBILIARIO</b> 🏢\n\n"
            "Comandos disponibles para esta área:\n\n"
            "🔹 /contrato_alquiler - Genera un contrato de arrendamiento.\n"
            "🔹 /compraventa - Redacta un contrato de compraventa de bien inmueble."
        )
        await _actualizar_mensaje(query, texto, reply_markup, "logo_inmobiliario.png")

    # Flujo: Categoría RRHH
    elif query.data == "cat_rrhh":
        keyboard = [[InlineKeyboardButton("🔙 Volver al Panel", callback_data="menu_principal")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        texto = (
            "👥 <b>MÓDULO RECURSOS HUMANOS</b> 👥\n\n"
            "Comandos disponibles para esta área:\n\n"
            "🔹 /liquidacion - Calcula y redacta una liquidación de beneficios sociales.\n"
            "🔹 /certificado - Emite un certificado de trabajo estándar."
        )
        await _actualizar_mensaje(query, texto, reply_markup, "logo_rrhh.png")
            
    # Flujo: Categoría SUNARP
    elif query.data == "cat_sunarp":
        keyboard = [[InlineKeyboardButton("🔙 Volver al Panel", callback_data="menu_principal")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        texto = (
            "🏛️ <b>MÓDULO SUNARP</b> 🏛️\n\n"
            "Comandos disponibles para esta área:\n\n"
            "🔹 /placa [numero] - Consulta de datos vehiculares."
        )
        await _actualizar_mensaje(query, texto, reply_markup, "logo_sunarp.png")

    # Flujo: Categoría SUNAT
    elif query.data == "cat_sunat":
        keyboard = [[InlineKeyboardButton("🔙 Volver al Panel", callback_data="menu_principal")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        texto = (
            "🏢 <b>MÓDULO SUNAT</b> 🏢\n\n"
            "Comandos disponibles para esta área:\n\n"
            "🔹 /ruc [numero] - Validación de estado y condición de empresas."
        )
        await _actualizar_mensaje(query, texto, reply_markup, "logo_sunat.png")

    # Flujo: Categoría RENIEC
    elif query.data == "cat_reniec":
        keyboard = [[InlineKeyboardButton("🔙 Volver al Panel", callback_data="menu_principal")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        texto = (
            "👤 <b>MÓDULO RENIEC</b> 👤\n\n"
            "Comandos disponibles para esta área:\n\n"
            "🔹 /dni [numero] - Verificación de identidad y ficha Reniec."
        )
        await _actualizar_mensaje(query, texto, reply_markup, "logo_reniec.png")

    # Flujo: Ajustes (Perfil de Usuario)
    elif query.data == "ajustes":
        keyboard = [[InlineKeyboardButton("🔙 Volver al Panel", callback_data="menu_principal")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        user_id = update.effective_user.id
        def _get_user_info():
            try:
                return supabase.table("profiles").select("*").eq("telegram_id", user_id).execute()
            except Exception as e:
                return None
            
        res = await asyncio.to_thread(_get_user_info)
        user_data = res.data[0] if res and hasattr(res, 'data') and res.data else {}
        credits = user_data.get("credits", 0)
        
        texto = (
            "⚙️ <b>AJUSTES Y PERFIL</b> ⚙️\n\n"
            f"👤 <b>Usuario:</b> {update.effective_user.first_name}\n"
            f"💳 <b>Créditos Disponibles:</b> {credits}\n"
            f"🆔 <b>ID Telegram:</b> <code>{user_id}</code>\n\n"
            "Para ver tus documentos usa el comando /mis_contratos."
        )
        await _actualizar_mensaje(query, texto, reply_markup, "logo.png")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manejador para el comando /status que verifica la existencia de carpetas clave."""
    base_dir = Path(__file__).resolve().parent
    temp_dir = base_dir / "data" / "temp"
    templates_dir = base_dir / "app" / "generators" / "templates"

    temp_status = "✅ Disponible" if temp_dir.exists() else "❌ No encontrada"
    templates_status = "✅ Disponible" if templates_dir.exists() else "❌ No encontrada"

    status_text = (
        "🛠️ <b>Estado de la Infraestructura</b>\n\n"
        f"📂 <code>data/temp</code>: {temp_status}\n"
        f"📂 <code>app/generators/templates</code>: {templates_status}"
    )
    if update.message:
        await update.message.reply_text(status_text, parse_mode="HTML")

async def contrato_prueba(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Genera un documento de prueba y lo envía al usuario."""
    if not update.message:
        return

    await update.message.reply_text("⏳ Procesando su documento, por favor aguarde un momento...")

    base_dir = Path(__file__).resolve().parent
    template_path = base_dir / "app" / "generators" / "templates" / "plantilla_prueba.docx"
    output_path = base_dir / "data" / "temp" / "contrato_generado.docx"

    # Datos ficticios
    monto = 2500.00
    contexto = {
        "nombre_cliente": "Juan Pérez",
        "dni_cliente": "12345678",
        "monto_numero": f"{monto:,.2f}",
        "monto_texto": monto_a_letras(monto),
        "direccion_inmueble": "Av. Principal 123, Lima",
        "plazo_meses": 12,
        "garantia_meses": 2,
        "garantia_total_numero": f"{monto*2:,.2f}",
        "garantia_total_texto": monto_a_letras(monto*2),
        "fecha_actual": fecha_legal()
    }

    motor = GeneradorWord(template_path)
    exito, msg_error = motor.generar(contexto, output_path)

    if exito:
        with open(output_path, "rb") as doc_file:
            await update.message.reply_document(
                document=doc_file,
                caption="✅ Su documento ha sido generado con éxito. Por favor, revise los detalles.",
                filename="Contrato_Prueba_LYP_PRO.docx"
            )
    else:
        await update.message.reply_text(
            f"❌ <b>Error del sistema:</b> No se pudo procesar el documento.\n\n"
            f"🛠️ <b>Detalle técnico:</b> <code>{msg_error}</code>",
            parse_mode="HTML"
        )

# --- COMANDOS DE SERVICIOS ESPECÍFICOS ---

# Estados para el ConversationHandler
(
    ASK_ALQ_N_ARRE, ASK_ALQ_DNI_ARRE, ASK_ALQ_N_ARRE_T, ASK_ALQ_DNI_ARRE_T, ASK_ALQ_DIR, ASK_ALQ_USO, ASK_ALQ_PLAZO, ASK_ALQ_RENTA, ASK_ALQ_GARANTIA, ASK_ALQ_FECHA,
    ASK_POD_N_OTOR, ASK_POD_DNI_OTOR, ASK_POD_DOM_OTOR, ASK_POD_N_APOD, ASK_POD_DNI_APOD, ASK_POD_DOM_APOD, ASK_POD_FACULTADES, ASK_POD_VIGENCIA,
    ASK_DEM_N_DDANTE, ASK_DEM_DNI_DDANTE, ASK_DEM_DOM_DDANTE, ASK_DEM_N_DDADO, ASK_DEM_DOM_DDADO, ASK_DEM_N_MENOR, ASK_DEM_EDAD_MENOR, ASK_DEM_MONTO, ASK_DEM_CONCEPTOS, ASK_DEM_JUSTIFICACION,
    ASK_CV_N_VEND, ASK_CV_DNI_VEND, ASK_CV_EST_VEND, ASK_CV_N_COMP, ASK_CV_DNI_COMP, ASK_CV_EST_COMP, ASK_CV_UBICACION, ASK_CV_PARTIDA, ASK_CV_OFICINA, ASK_CV_PRECIO, ASK_CV_PAGO,
    ASK_LIQ_RUC, ASK_LIQ_EMP, ASK_LIQ_N_TRAB, ASK_LIQ_DNI_TRAB, ASK_LIQ_CARGO, ASK_LIQ_INICIO, ASK_LIQ_FIN, ASK_LIQ_MOTIVO, ASK_LIQ_SUELDO, ASK_LIQ_VACACIONES,
    ASK_CER_RUC, ASK_CER_EMP, ASK_CER_N_TRAB, ASK_CER_DNI_TRAB, ASK_CER_CARGO, ASK_CER_FUNCIONES, ASK_CER_INICIO, ASK_CER_FIN, ASK_CER_LOGO_OPT, ASK_CER_LOGO_IMG
) = range(59)
ASK_SOPORTE_QUERY = 100

@require_credits
async def cmd_contrato_alquiler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = (
        "📄 <b>Generación de Contrato de Alquiler</b>\n\n"
        "💰 <i>Costo: 1 Crédito</i>\n\n"
        "Asistente paso a paso activado (Use /cancelar para salir).\n\n"
        "1/10. Ingrese el <b>Nombre Completo del Arrendador</b> (Dueño):"
    )
    await update.message.reply_text(texto, parse_mode="HTML")
    return ASK_ALQ_N_ARRE

async def r_alq_n_arre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["nombre_arrendador"] = update.message.text
    await update.message.reply_text("2/10. Ingrese el <b>DNI/RUC</b> del Arrendador:", parse_mode="HTML")
    return ASK_ALQ_DNI_ARRE
async def r_alq_dni_arre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dni_arrendador"] = update.message.text
    await update.message.reply_text("3/10. Ingrese el <b>Nombre Completo del Arrendatario</b> (Inquilino):", parse_mode="HTML")
    return ASK_ALQ_N_ARRE_T
async def r_alq_n_arre_t(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["nombre_arrendatario"] = update.message.text
    await update.message.reply_text("4/10. Ingrese el <b>DNI/RUC</b> del Arrendatario:", parse_mode="HTML")
    return ASK_ALQ_DNI_ARRE_T
async def r_alq_dni_arre_t(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dni_arrendatario"] = update.message.text
    await update.message.reply_text("5/10. Escriba la <b>Dirección Exacta del Inmueble</b> a alquilar:", parse_mode="HTML")
    return ASK_ALQ_DIR
async def r_alq_dir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["direccion_inmueble"] = update.message.text
    await update.message.reply_text("6/10. ¿Uso <b>Vivienda</b> o <b>Comercial</b>?", parse_mode="HTML")
    return ASK_ALQ_USO
async def r_alq_uso(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["tipo_uso"] = update.message.text.strip().capitalize()
    await update.message.reply_text("7/10. Ingrese el <b>Plazo del Contrato</b> en meses (Ej: 12):", parse_mode="HTML")
    return ASK_ALQ_PLAZO
async def r_alq_plazo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["plazo_meses"] = update.message.text
    await update.message.reply_text("8/10. Ingrese el <b>Monto de Renta Mensual</b> (Ej: 1500.50):", parse_mode="HTML")
    return ASK_ALQ_RENTA
async def r_alq_renta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["monto_numero"] = float(update.message.text.replace(",", ""))
    except ValueError:
        await update.message.reply_text("❌ Monto inválido. Intente de nuevo:")
        return ASK_ALQ_RENTA
    await update.message.reply_text("9/10. ¿Cuántos <b>Meses de Garantía</b> exigirá? (Ej: 1, 2):", parse_mode="HTML")
    return ASK_ALQ_GARANTIA
async def r_alq_garantia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["garantia_meses"] = int(update.message.text)
    except ValueError:
        await update.message.reply_text("❌ Número inválido. Intente de nuevo:")
        return ASK_ALQ_GARANTIA
    await update.message.reply_text("10/10. Indique la <b>Fecha de Inicio</b> del Contrato (Ej: 01 de Enero de 2026):", parse_mode="HTML")
    return ASK_ALQ_FECHA

async def r_alq_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["fecha_inicio"] = update.message.text
    await update.message.reply_text("⏳ Procesando validaciones y redactando su documento legal...")
    try:
        dir_limpia = await normalizar_direccion(context.user_data["direccion_inmueble"])
        base_dir = Path(__file__).resolve().parent
        template_path = base_dir / "app" / "generators" / "templates" / "plantilla_arrendamiento.docx"
        output_path = base_dir / "data" / "temp" / f"ALQ_{context.user_data['dni_arrendatario']}.docx"

        monto = context.user_data["monto_numero"]
        garantia_total = monto * context.user_data["garantia_meses"]
        
        contexto = context.user_data.copy()
        contexto.update({
            "monto_texto": monto_a_letras(monto),
            "direccion_inmueble": dir_limpia,
            "garantia_total_numero": f"{garantia_total:,.2f}",
            "garantia_total_texto": monto_a_letras(garantia_total),
            "fecha_actual": fecha_legal()
        })

        motor = GeneradorWord(template_path)
        exito, msg_error = await motor.generar_async(contexto, output_path)
        if not exito:
            raise DocumentError(f"Error de ensamblado: {msg_error}")

        await guardar_en_supabase_y_cobrar(update.effective_user.id, output_path, contexto["dni_arrendatario"], "Alquiler")

        with open(output_path, "rb") as doc_file:
            await update.message.reply_document(
                document=doc_file,
                caption="✅ Contrato de Alquiler Blindado Generado. (1 Crédito Descontado)",
                filename=f"Alquiler_{contexto['dni_arrendatario']}.docx"
            )
    except Exception as e:
        await update.message.reply_text(f"🛑 <b>Excepción del Sistema:</b>\n{str(e)}", parse_mode="HTML")
        
    return ConversationHandler.END

async def cancelar_tramite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela la conversación actual."""
    await update.message.reply_text("❌ Trámite cancelado. Puede volver al /menu cuando guste.")
    return ConversationHandler.END

@require_credits
async def cmd_poder_simple(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia el proceso para generar una Carta Poder Simple interactiva."""
    texto = (
        "📝 <b>Servicio: /poder_simple</b>\n\n"
        "💰 <i>Costo: 1 Crédito</i>\n\n"
        "1/8. Ingrese su <b>Nombre Completo</b> (El Otorgante):"
    )
    await update.message.reply_text(texto, parse_mode="HTML")
    return ASK_POD_N_OTOR

async def r_pod_n_otor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["poder_n_otor"] = update.message.text
    await update.message.reply_text("2/8. Ingrese su <b>DNI</b>:", parse_mode="HTML")
    return ASK_POD_DNI_OTOR
async def r_pod_dni_otor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["poder_dni_otor"] = update.message.text
    await update.message.reply_text("3/8. Ingrese su <b>Domicilio Real</b>:", parse_mode="HTML")
    return ASK_POD_DOM_OTOR
async def r_pod_dom_otor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["poder_dom_otor"] = update.message.text
    await update.message.reply_text("4/8. Ingrese el <b>Nombre del Apoderado</b> (Quien lo representará):", parse_mode="HTML")
    return ASK_POD_N_APOD
async def r_pod_n_apod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["poder_n_apod"] = update.message.text
    await update.message.reply_text("5/8. Ingrese el <b>DNI del Apoderado</b>:", parse_mode="HTML")
    return ASK_POD_DNI_APOD
async def r_pod_dni_apod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["poder_dni_apod"] = update.message.text
    await update.message.reply_text("6/8. Ingrese el <b>Domicilio del Apoderado</b>:", parse_mode="HTML")
    return ASK_POD_DOM_APOD
async def r_pod_dom_apod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["poder_dom_apod"] = update.message.text
    await update.message.reply_text("7/8. Especifique las <b>Facultades</b> (Ej: 'Asistir al banco BCP y recoger mi tarjeta'):", parse_mode="HTML")
    return ASK_POD_FACULTADES
async def r_pod_facultades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["poder_facultades"] = update.message.text
    await update.message.reply_text("8/8. Ingrese la <b>Vigencia</b> del poder (Ej: '30 días' o '1 año'):", parse_mode="HTML")
    return ASK_POD_VIGENCIA

async def r_pod_vigencia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("⏳ <i>Redactando Carta Poder con Inteligencia Artificial...</i>", parse_mode="HTML")
    prompt = (
        "Redacta el cuerpo de una carta poder simple, formal y legal en Perú. "
        f"Otorgante: {context.user_data['poder_n_otor']} (DNI {context.user_data['poder_dni_otor']}), Domicilio: {context.user_data['poder_dom_otor']}. "
        f"Apoderado: {context.user_data['poder_n_apod']} (DNI {context.user_data['poder_dni_apod']}), Domicilio: {context.user_data['poder_dom_apod']}. "
        f"Facultades: {context.user_data['poder_facultades']}. "
        f"Vigencia del poder: {update.message.text}. "
        "No incluyas fecha ni firmas, solo el texto central empezando con 'Conste por el presente documento...'"
    )
    cuerpo_poder = await consultar_gemini(prompt)
    texto_final = (
        "📄 <b>CARTA PODER SIMPLE</b>\n\n"
        f"{cuerpo_poder}\n\n"
        "<i>✨ Documento redactado por LYP PRO. Puede copiar y pegar este texto.</i>"
    )
    await update.message.reply_text(texto_final, parse_mode="HTML")
    
    await descontar_creditos_basico(update.effective_user.id, 1, "Generación Carta Poder IA")
    return ConversationHandler.END

async def manejar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Captura cualquier texto fuera de contexto y guía al usuario amablemente."""
    texto = (
        "✨ <b>LYP DOC BOT PREMIUM</b> ✨\n\n"
        "He recibido su mensaje, pero actualmente no nos encontramos dentro de ningún trámite.\n\n"
        "📌 <b>Opciones rápidas:</b>\n"
        "• Use /menu para ver el panel de control.\n"
        "• Use /soporte para realizar una consulta jurídica a la Inteligencia Artificial.\n"
        "• Use /help para conocer todos mis comandos."
    )
    await update.message.reply_text(texto, parse_mode="HTML")

@require_credits
async def manejar_archivo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Procesa imágenes o PDFs de contratos enviados por el usuario."""
    file = None
    mime_type = "image/jpeg"
    
    if update.message.photo:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type != "application/pdf":
            await update.message.reply_text("❌ Solo procesamos imágenes o documentos PDF para el resumen.")
            return
        file = await context.bot.get_file(doc.file_id)
        mime_type = "application/pdf"
    else:
        return
        
    msg = await update.message.reply_text("👁️ <i>Escaneando documento con Inteligencia Artificial...</i>", parse_mode="HTML")
    
    try:
        file_bytearray = await file.download_as_bytearray()
        
        # Extraemos información mediante Gemini
        resultado = await analizar_documento_contrato(bytes(file_bytearray), mime_type)
        if resultado.startswith("❌"):
            await msg.edit_text(resultado)
            return

        # Generación del archivo PDF
        base_dir = Path(__file__).resolve().parent
        temp_dir = base_dir / "data" / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = temp_dir / f"Resumen_{update.effective_user.id}.pdf"
        
        def _crear_pdf():
            if FPDF is None:
                raise Exception("La librería FPDF no está instalada. Ejecute en su consola: pip install fpdf2")
            pdf = FPDF()
            pdf.add_page()
            pdf.set_auto_page_break(auto=True, margin=15)
            pdf.set_font("Helvetica", "B", 15)
            pdf.cell(0, 10, "RESUMEN EJECUTIVO - LYP PRO", ln=True, align="C")
            pdf.ln(5)
            pdf.set_font("Helvetica", "", 11)
            
            # Limpiamos formato markdown, comillas raras y emojis que rompen FPDF2 con Helvetica
            texto_limpio = re.sub(r'[*`#]', '', resultado)
            texto_limpio = texto_limpio.replace('“', '"').replace('”', '"').replace('—', '-').replace('–', '-')
            texto_limpio = texto_limpio.encode('latin-1', 'replace').decode('latin-1')
            
            pdf.multi_cell(0, 6, texto_limpio)
            pdf.output(str(pdf_path))
            
        await asyncio.to_thread(_crear_pdf)
        
        # Descontar crédito atómicamente
        res_user = supabase.table("profiles").select("id").eq("telegram_id", update.effective_user.id).execute()
        if res_user.data:
            res_cobro = supabase.rpc("cobrar_creditos", {"p_user_uuid": res_user.data[0]["id"], "p_costo": 1, "p_descripcion": "Extracción OCR y Resumen PDF"}).execute()
            if res_cobro.data is not True:
                await msg.edit_text("❌ <b>Saldo Insuficiente.</b> Recargue usando /buy.", parse_mode="HTML")
                return
            
        await msg.delete() # Borra el mensaje de "escaneando"
        
        with open(pdf_path, "rb") as doc_file:
            await update.message.reply_document(
                document=doc_file,
                caption="✅ <b>ANÁLISIS COMPLETADO</b>\n\nSe ha generado su Resumen Ejecutivo.\n\n<i>(1 Crédito descontado)</i>",
                filename="Resumen_Ejecutivo.pdf",
                parse_mode="HTML"
            )
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error al procesar el archivo:</b> {str(e)}", parse_mode="HTML")

async def cmd_invitar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Genera un link de referidos y explica la recompensa."""
    user = update.effective_user
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    
    texto = (
        "🎁 *Programa de Crecimiento LYP PRO*\n\n"
        "Invita a tus colegas y gana créditos gratis para generar contratos blindados\\.\n\n"
        f"🔗 *Tu enlace personal:*\n`{link}`\n\n"
        "💸 *Recompensa:* Recibirás *2 créditos* automáticamente por cada usuario nuevo que se registre usando tu enlace\\."
    )
    if update.message:
        await update.message.reply_text(texto, parse_mode="MarkdownV2")

async def cmd_launch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envía el mensaje maestro de lanzamiento a todos los usuarios registrados (Solo Admin)."""
    admin_id = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
    if update.effective_user.id != admin_id:
        return
        
    texto = (
        "🏛️ *LYP PRO anuncia el lanzamiento oficial de LYP DOC BOT* 🏛️\n\n"
        "La primera IA legal de Perú capaz de blindar tus contratos en segundos con respaldo de RENIEC y SUNAT\\.\n\n"
        "🔥 *Oferta Exclusiva por Lanzamiento*\n"
        "Solo por hoy: Contratos de Alquiler Ley 30933 a solo *S/ 15\\.00*\\.\n\n"
        "Presiona /menu para comenzar a redactar\\."
    )
    if update.message: await update.message.reply_text("⏳ Iniciando broadcast masivo...")
    def _get_users():
        res = supabase.table("profiles").select("telegram_id").execute()
        return res.data if res.data else []
    users = await asyncio.to_thread(_get_users)
    enviados = 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u["telegram_id"], text=texto, parse_mode="MarkdownV2")
            enviados += 1
            await asyncio.sleep(0.05) # Rate limiting de Telegram
        except Exception:
            pass
    if update.message: await update.message.reply_text(f"✅ Broadcast finalizado. Entregado a {enviados} usuarios.")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando oculto para enviar mensajes masivos a todos los usuarios (Solo Admin)."""
    admin_ids_env = os.getenv("ADMIN_TELEGRAM_ID", "")
    allowed_ids = [int(uid.strip()) for uid in admin_ids_env.split(",") if uid.strip().isdigit()]
    
    if update.effective_user.id not in allowed_ids:
        return
        
    if not context.args:
        await update.message.reply_text("❌ Uso incorrecto. Formato: `/broadcast <Tu mensaje aquí>`", parse_mode="Markdown")
        return
        
    mensaje = " ".join(context.args)
    texto = f"📢 <b>ANUNCIO OFICIAL LYP PRO</b> 📢\n\n{mensaje}"
    
    if update.message: await update.message.reply_text("⏳ Iniciando envío masivo a la base de datos...")
    
    def _get_users():
        res = supabase.table("profiles").select("telegram_id").execute()
        return res.data if res.data else []
        
    users = await asyncio.to_thread(_get_users)
    enviados = 0
    
    for u in users:
        try:
            await context.bot.send_message(chat_id=u["telegram_id"], text=texto, parse_mode="HTML")
            enviados += 1
            await asyncio.sleep(0.05) # Límite de seguridad de Telegram (20 msg/segundo max)
        except Exception:
            pass
            
    if update.message: await update.message.reply_text(f"✅ Broadcast finalizado. Entregado a {enviados} usuarios activos.")

@require_credits
async def cmd_soporte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = (
        "🤖 <b>Soporte Legal IA - Premium</b>\n\n"
        "Ha ingresado a la sala de consulta con nuestra Inteligencia Artificial.\n"
        "Por favor, escriba detalladamente su caso, duda jurídica o solicite la redacción de una cláusula.\n\n"
        "💰 <i>Costo: 1 Crédito por consulta.</i>\n"
        "🛑 <i>Para salir en cualquier momento, escriba /cancelar</i>"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await _actualizar_mensaje(update.callback_query, texto, None, "logo_soporte.png")
    elif update.message:
        await update.message.reply_text(texto, parse_mode="HTML")
    return ASK_SOPORTE_QUERY

async def recibir_consulta_soporte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    consulta = update.message.text
    msg = await update.message.reply_text("⏳ <i>Analizando jurisprudencia y redactando dictamen...</i>", parse_mode="HTML")
    
    try:
        user_id = update.effective_user.id
        res_user = supabase.table("profiles").select("id").eq("telegram_id", user_id).execute()
        user_uuid = res_user.data[0]["id"] if res_user.data else None
        
        res_cobro = supabase.rpc("cobrar_creditos", {"p_user_uuid": user_uuid, "p_costo": 1, "p_descripcion": "Consulta IA: Soporte Legal"}).execute()
        if res_cobro.data is not True:
            await msg.edit_text("❌ <b>Saldo Insuficiente.</b> Recargue usando /buy.", parse_mode="HTML")
            return ConversationHandler.END

        respuesta = await consultar_gemini(consulta)
        await guardar_texto_contrato_async(user_uuid, "Consulta Soporte IA", f"Q: {consulta}\n\nA: {respuesta}")
        await msg.edit_text(f"⚖️ <b>DICTAMEN IA:</b>\n\n{respuesta}\n\n<i>(1 Crédito descontado) • Use /menu para volver.</i>", parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ <b>Error en la IA:</b> {str(e)}", parse_mode="HTML")
        
    return ConversationHandler.END

@require_credits
async def cmd_demanda_alimentos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = (
        "⚖️ <b>Asistente de Demanda de Alimentos</b>\n\n"
        "💰 <i>Costo: 1 Crédito</i>\n\n"
        "1/10. Ingrese el <b>Nombre Completo del Demandante</b> (Quien pide la pensión):"
    )
    await update.message.reply_text(texto, parse_mode="HTML")
    return ASK_DEM_N_DDANTE

async def r_dem_n_ddante(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dem_n_ddante"] = update.message.text
    await update.message.reply_text("2/10. Ingrese el <b>DNI</b> del Demandante:", parse_mode="HTML")
    return ASK_DEM_DNI_DDANTE
async def r_dem_dni_ddante(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dem_dni_ddante"] = update.message.text
    await update.message.reply_text("3/10. Ingrese el <b>Domicilio Real</b> del Demandante:", parse_mode="HTML")
    return ASK_DEM_DOM_DDANTE
async def r_dem_dom_ddante(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dem_dom_ddante"] = update.message.text
    await update.message.reply_text("4/10. Ingrese el <b>Nombre del Demandado</b> (Quien pagará):", parse_mode="HTML")
    return ASK_DEM_N_DDADO
async def r_dem_n_ddado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dem_n_ddado"] = update.message.text
    await update.message.reply_text("5/10. Ingrese el <b>Domicilio del Demandado</b> (Para notificarle):", parse_mode="HTML")
    return ASK_DEM_DOM_DDADO
async def r_dem_dom_ddado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dem_dom_ddado"] = update.message.text
    await update.message.reply_text("6/10. Ingrese el <b>Nombre Completo del Menor</b> (Hijo/a):", parse_mode="HTML")
    return ASK_DEM_N_MENOR
async def r_dem_n_menor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dem_n_menor"] = update.message.text
    await update.message.reply_text("7/10. Indique la <b>Edad del Menor</b> (Ej: '8 años'):", parse_mode="HTML")
    return ASK_DEM_EDAD_MENOR
async def r_dem_edad_menor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dem_edad_menor"] = update.message.text
    await update.message.reply_text("8/10. Ingrese el <b>Monto Mensual Solicitado</b> (Ej: 1200.50):", parse_mode="HTML")
    return ASK_DEM_MONTO
async def r_dem_monto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["dem_monto_numero"] = float(update.message.text.replace(",", ""))
    except ValueError:
        await update.message.reply_text("❌ Monto inválido. Intente de nuevo:")
        return ASK_DEM_MONTO
    await update.message.reply_text("9/10. Indique qué <b>Conceptos</b> cubre este monto (Ej: 'Alimentación, educación y salud'):", parse_mode="HTML")
    return ASK_DEM_CONCEPTOS
async def r_dem_conceptos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dem_conceptos"] = update.message.text
    await update.message.reply_text("10/10. Finalmente, escriba la <b>Justificación de Hechos</b> (Ej: 'El demandado abandonó el hogar y no cumple con sus obligaciones...'):", parse_mode="HTML")
    return ASK_DEM_JUSTIFICACION

async def r_dem_justificacion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dem_justificacion"] = update.message.text
    await update.message.reply_text("⏳ <i>Redactando la Demanda de Alimentos...</i>", parse_mode="HTML")

    base_dir = Path(__file__).resolve().parent
    template_path = base_dir / "app" / "generators" / "templates" / "plantilla_demanda.docx"
    output_path = base_dir / "data" / "temp" / f"demanda_{context.user_data['dem_dni_ddante']}.docx"

    contexto = context.user_data.copy()
    contexto["dem_monto_texto"] = monto_a_letras(context.user_data["dem_monto_numero"])
    contexto["fecha_actual"] = fecha_legal()

    motor = GeneradorWord(template_path)
    exito, msg_error = await motor.generar_async(contexto, output_path)

    if not exito:
        await update.message.reply_text(f"❌ <b>Error:</b> {msg_error}", parse_mode="HTML")
        return ConversationHandler.END

    try:
        await guardar_en_supabase_y_cobrar(
            update.effective_user.id, 
            output_path, 
            context.user_data["dem_dni_ddante"], 
            "Demanda de Alimentos"
        )

        with open(output_path, "rb") as doc_file:
            await update.message.reply_document(
                document=doc_file,
                caption="✅ Demanda generada y blindada. (1 Crédito Descontado)\n\n<i>Recomendación: Imprimir y firmar para su presentación en el Juzgado de Paz Letrado.</i>",
                filename=f"Demanda_Alimentos_{context.user_data['dem_dni_ddante']}.docx",
                parse_mode="HTML"
            )
    except Exception as e:
        await update.message.reply_text(f"🛑 <b>Excepción del Sistema:</b>\n{str(e)}", parse_mode="HTML")

    return ConversationHandler.END

# ==========================================
# 4. MÓDULO: COMPRAVENTA INMOBILIARIA
# ==========================================
@require_credits
async def cmd_compraventa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🏢 <b>Módulo Compraventa</b>\n💰 <i>Costo: 1 Crédito</i>\n\n1/11. Ingrese el Nombre del <b>Vendedor</b>:", parse_mode="HTML")
    return ASK_CV_N_VEND
async def r_cv_n_vend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cv_n_vend"] = update.message.text
    await update.message.reply_text("2/11. Ingrese el <b>DNI del Vendedor</b>:", parse_mode="HTML")
    return ASK_CV_DNI_VEND
async def r_cv_dni_vend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cv_dni_vend"] = update.message.text
    await update.message.reply_text("3/11. Estado Civil del Vendedor (Ej: Soltero, Casado con...):", parse_mode="HTML")
    return ASK_CV_EST_VEND
async def r_cv_est_vend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cv_est_vend"] = update.message.text
    await update.message.reply_text("4/11. Ingrese el Nombre del <b>Comprador</b>:", parse_mode="HTML")
    return ASK_CV_N_COMP
async def r_cv_n_comp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cv_n_comp"] = update.message.text
    await update.message.reply_text("5/11. Ingrese el <b>DNI del Comprador</b>:", parse_mode="HTML")
    return ASK_CV_DNI_COMP
async def r_cv_dni_comp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cv_dni_comp"] = update.message.text
    await update.message.reply_text("6/11. Estado Civil del Comprador:", parse_mode="HTML")
    return ASK_CV_EST_COMP
async def r_cv_est_comp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cv_est_comp"] = update.message.text
    await update.message.reply_text("7/11. Ingrese la <b>Ubicación Exacta</b> del inmueble que se vende:", parse_mode="HTML")
    return ASK_CV_UBICACION
async def r_cv_ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cv_ubicacion"] = update.message.text
    await update.message.reply_text("8/11. Ingrese el Número de <b>Partida Registral</b> (SUNARP):", parse_mode="HTML")
    return ASK_CV_PARTIDA
async def r_cv_partida(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cv_partida"] = update.message.text
    await update.message.reply_text("9/11. Indique la <b>Oficina Registral</b> (Ej: Zona Registral N° IX - Sede Lima):", parse_mode="HTML")
    return ASK_CV_OFICINA
async def r_cv_oficina(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cv_oficina"] = update.message.text
    await update.message.reply_text("10/11. Ingrese el <b>Precio de Venta</b> (Ej: 150000.00):", parse_mode="HTML")
    return ASK_CV_PRECIO
async def r_cv_precio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["cv_precio_numero"] = float(update.message.text.replace(",", ""))
    except ValueError:
        await update.message.reply_text("❌ Monto inválido. Intente de nuevo:")
        return ASK_CV_PRECIO
    await update.message.reply_text("11/11. Medio de pago (Ej: Transferencia Bancaria BCP, Cheque de Gerencia):", parse_mode="HTML")
    return ASK_CV_PAGO

async def r_cv_pago(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cv_pago"] = update.message.text
    await update.message.reply_text("⏳ <i>Redactando Compraventa...</i>", parse_mode="HTML")
    
    base_dir = Path(__file__).resolve().parent
    out_path = base_dir / "data" / "temp" / f"CV_{context.user_data['cv_dni_comp']}.docx"
    
    contexto = context.user_data.copy()
    contexto["cv_precio_texto"] = monto_a_letras(context.user_data["cv_precio_numero"])
    contexto["fecha_actual"] = fecha_legal()
    
    motor = GeneradorWord(base_dir / "app" / "generators" / "templates" / "plantilla_compraventa.docx")
    exito, err = await motor.generar_async(contexto, out_path)
    if exito:
        await guardar_en_supabase_y_cobrar(update.effective_user.id, out_path, contexto["cv_dni_comp"], "Compraventa")
        with open(out_path, "rb") as f:
            await update.message.reply_document(document=f, caption="✅ Compraventa Generada. (1 Crédito descontado)")
    return ConversationHandler.END

# ==========================================
# 5. MÓDULO: LIQUIDACIÓN DE BENEFICIOS
# ==========================================
@require_credits
async def cmd_liquidacion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("👥 <b>Liquidación</b>\n💰 <i>Costo: 1 Crédito</i>\n\n1/10. Ingrese el <b>RUC</b> de la Empresa:", parse_mode="HTML")
    return ASK_LIQ_RUC
async def r_liq_ruc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["liq_ruc"] = update.message.text
    await update.message.reply_text("2/10. Ingrese la <b>Razón Social</b> de la Empresa:", parse_mode="HTML")
    return ASK_LIQ_EMP
async def r_liq_emp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["liq_emp"] = update.message.text
    await update.message.reply_text("3/10. Nombre Completo del <b>Trabajador</b>:", parse_mode="HTML")
    return ASK_LIQ_N_TRAB
async def r_liq_n_trab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["liq_n_trab"] = update.message.text
    await update.message.reply_text("4/10. Ingrese el <b>DNI</b> del Trabajador:", parse_mode="HTML")
    return ASK_LIQ_DNI_TRAB
async def r_liq_dni(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["liq_dni_trab"] = update.message.text
    await update.message.reply_text("5/10. Cargo que ocupaba:", parse_mode="HTML")
    return ASK_LIQ_CARGO
async def r_liq_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["liq_cargo"] = update.message.text
    await update.message.reply_text("6/10. <b>Fecha de Ingreso</b> (Ej: 01/01/2020):", parse_mode="HTML")
    return ASK_LIQ_INICIO
async def r_liq_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["liq_inicio"] = update.message.text
    await update.message.reply_text("7/10. <b>Fecha de Cese</b> (Ej: 31/12/2025):", parse_mode="HTML")
    return ASK_LIQ_FIN
async def r_liq_fin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["liq_fin"] = update.message.text
    await update.message.reply_text("8/10. Motivo de Cese (Ej: Renuncia Voluntaria, Despido):", parse_mode="HTML")
    return ASK_LIQ_MOTIVO
async def r_liq_motivo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["liq_motivo"] = update.message.text
    await update.message.reply_text("9/10. <b>Sueldo Mensual</b> Computable (Ej: 2000.00):", parse_mode="HTML")
    return ASK_LIQ_SUELDO
async def r_liq_sueldo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["liq_sueldo"] = update.message.text
    await update.message.reply_text("10/10. Días de <b>Vacaciones Pendientes</b> (Ej: 15):", parse_mode="HTML")
    return ASK_LIQ_VACACIONES

async def r_liq_vacaciones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["liq_vacaciones"] = update.message.text
    await update.message.reply_text("⏳ <i>Generando Liquidación...</i>", parse_mode="HTML")
    
    base_dir = Path(__file__).resolve().parent
    out_path = base_dir / "data" / "temp" / f"LIQ_{context.user_data['liq_dni_trab']}.docx"
    contexto = context.user_data.copy()
    contexto["fecha_actual"] = fecha_legal()
    
    motor = GeneradorWord(base_dir / "app" / "generators" / "templates" / "plantilla_liquidacion.docx")
    exito, err = await motor.generar_async(contexto, out_path)
    if exito:
        await guardar_en_supabase_y_cobrar(update.effective_user.id, out_path, contexto["liq_dni_trab"], "Liquidación")
        with open(out_path, "rb") as f:
            await update.message.reply_document(document=f, caption="✅ Liquidación Generada. (1 Crédito descontado)")
    return ConversationHandler.END

# ==========================================
# 6. MÓDULO: CERTIFICADO DE TRABAJO
# ==========================================
@require_credits
async def cmd_certificado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("📄 <b>Certificado Premium (HTML/PDF)</b>\n\n1/9. Ingrese el <b>RUC</b> de la Empresa:", parse_mode="HTML")
    return ASK_CER_RUC
async def r_cer_ruc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cer_ruc"] = update.message.text
    await update.message.reply_text("2/8. Ingrese la <b>Razón Social</b> de la Empresa:", parse_mode="HTML")
    return ASK_CER_EMP
async def r_cer_emp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cer_emp"] = update.message.text
    await update.message.reply_text("3/9. Nombre Completo del <b>Trabajador</b>:", parse_mode="HTML")
    return ASK_CER_N_TRAB
async def r_cer_n_trab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cer_n_trab"] = update.message.text
    await update.message.reply_text("4/9. Ingrese el <b>DNI</b> del Trabajador:", parse_mode="HTML")
    return ASK_CER_DNI_TRAB
async def r_cert_dni(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cer_dni_trab"] = update.message.text
    await update.message.reply_text("5/9. Ingrese el <b>Cargo Desempeñado</b>:", parse_mode="HTML")
    return ASK_CER_CARGO
async def r_cert_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cer_cargo"] = update.message.text
    await update.message.reply_text("6/9. Describa brevemente sus <b>Funciones</b> (Ej: Gestión contable, atención al cliente):", parse_mode="HTML")
    return ASK_CER_FUNCIONES
async def r_cer_funciones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cer_funciones"] = update.message.text
    await update.message.reply_text("7/9. <b>Fecha de Inicio</b> (Ej: 01 de Enero de 2022):", parse_mode="HTML")
    return ASK_CER_INICIO
async def r_cert_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cer_inicio"] = update.message.text
    await update.message.reply_text("8/9. <b>Fecha de Fin</b> (Ej: 31 de Diciembre de 2025):", parse_mode="HTML")
    return ASK_CER_FIN

async def r_cert_fin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cer_fin"] = update.message.text
    texto = (
        "9/9. 🎨 <b>Personalización Premium</b>\n\n"
        "¿Desea incluir el <b>LOGO DE SU EMPRESA</b> en el encabezado del certificado?\n\n"
        "• <b>NO</b> (Diseño estándar - Costo: 1 Crédito)\n"
        "• <b>SI</b> (Diseño con Logo - Costo: 2 Créditos)\n\n"
        "Responda <b>SI</b> o <b>NO</b>:"
    )
    await update.message.reply_text(texto, parse_mode="HTML")
    return ASK_CER_LOGO_OPT

async def r_cer_logo_opt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    respuesta = update.message.text.strip().upper()
    if respuesta == "SI":
        context.user_data["cer_usa_logo"] = True
        await update.message.reply_text("🖼️ Por favor, adjunte y envíe la <b>IMAGEN</b> del logo de su empresa:", parse_mode="HTML")
        return ASK_CER_LOGO_IMG
    else:
        context.user_data["cer_usa_logo"] = False
        context.user_data["cer_logo_path"] = None
        return await generar_certificado_pdf(update, context)

async def r_cer_logo_img(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("❌ Por favor envíe una IMAGEN válida como foto.")
        return ASK_CER_LOGO_IMG
        
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    
    base_dir = Path(__file__).resolve().parent
    temp_dir = base_dir / "data" / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    logo_path = temp_dir / f"logo_{update.effective_user.id}.jpg"
    
    await file.download_to_drive(logo_path)
    context.user_data["cer_logo_path"] = str(logo_path)
    
    return await generar_certificado_pdf(update, context)

async def generar_certificado_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("⏳ <i>Renderizando motor HTML/CSS a PDF Alta Calidad...</i>", parse_mode="HTML")
    
    costo = 2 if context.user_data.get("cer_usa_logo") else 1
    user_id = update.effective_user.id
    
    # Verificación estricta de saldo para cobrar 1 o 2 créditos según corresponda
    res_user = supabase.table("profiles").select("id, credits").eq("telegram_id", user_id).execute()
    if not res_user.data or res_user.data[0]["credits"] < costo:
        await update.message.reply_text(f"❌ <b>Saldo Insuficiente.</b> Esta versión del certificado requiere {costo} créditos.", parse_mode="HTML")
        return ConversationHandler.END
        
    user_uuid = res_user.data[0]["id"]
    base_dir = Path(__file__).resolve().parent
    out_path = base_dir / "data" / "temp" / f"CERT_PRO_{context.user_data['cer_dni_trab']}.pdf"
    
    logo_html = ""
    if context.user_data.get("cer_logo_path"):
        with open(context.user_data["cer_logo_path"], "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
            logo_html = f'<img src="data:image/jpeg;base64,{b64}" class="logo">'
            
    # DISEÑO PREMIUM HTML/CSS PARA EL CERTIFICADO
    html_content = f"""
    <html>
    <head>
    <style>
        @page {{ size: A4 landscape; margin: 1.5cm; }}
        body {{ font-family: 'Helvetica', sans-serif; border: 12px double #1a252f; padding: 45px; text-align: center; color: #2c3e50; }}
        .header {{ color: #1a252f; font-size: 42px; font-weight: bold; text-transform: uppercase; letter-spacing: 4px; margin-bottom: 30px; }}
        .content {{ font-size: 20px; line-height: 1.8; text-align: justify; margin: 0 50px; color: #34495e; }}
        .logo {{ max-width: 200px; max-height: 120px; margin-bottom: 20px; }}
        .signature {{ margin-top: 80px; border-top: 2px solid #1a252f; width: 350px; display: inline-block; padding-top: 10px; font-weight: bold; font-size: 18px; }}
    </style>
    </head>
    <body>
        {logo_html}
        <div class="header">Certificado de Trabajo</div>
        <p style="font-size: 22px; text-align: left; font-weight: bold; margin-left: 50px;">A QUIEN CORRESPONDA:</p>
        <div class="content">
            Por el presente documento, la empresa <b>{context.user_data['cer_emp']}</b> (RUC: {context.user_data['cer_ruc']}), certifica que el Sr./Sra. <b>{context.user_data['cer_n_trab']}</b>, identificado(a) con DNI N° <b>{context.user_data['cer_dni_trab']}</b>, ha laborado en nuestra institución desempeñando el cargo de <b>{context.user_data['cer_cargo']}</b>.
            <br><br>
            Durante su permanencia, cumplió con dedicación y alto sentido de responsabilidad las siguientes funciones: <i>{context.user_data['cer_funciones']}</i>.
            <br><br>
            <b>Periodo laborado:</b><br>
            Desde: {context.user_data['cer_inicio']}<br>
            Hasta: {context.user_data['cer_fin']}
        </div>
        <p style="text-align: right; margin-top: 50px; margin-right: 50px; font-style: italic; font-size: 18px;">Expedido el {fecha_legal()}</p>
        <div class="signature">
            GERENCIA DE RECURSOS HUMANOS<br>
            {context.user_data['cer_emp']}
        </div>
    </body>
    </html>
    """
    
    def _crear_pdf():
        try:
            from weasyprint import HTML
        except (ImportError, OSError):
            raise Exception("Faltan las librerías gráficas GTK3 en su sistema (libgobject). Si está probando en Windows, instale GTK3. Si es producción, asegúrese de tener el archivo Aptfile configurado en Railway.")
            
        HTML(string=html_content).write_pdf(str(out_path))
        supabase.rpc("cobrar_creditos", {
            "p_user_uuid": user_uuid, "p_costo": costo, "p_descripcion": "Generación Certificado PRO (HTML/PDF)"
        }).execute()
        
    try:
        await asyncio.to_thread(_crear_pdf)
        # Guardar URL en base de datos para la Bóveda Legal
        file_name = f"contratos/{user_uuid}/certificado_{context.user_data['cer_dni_trab']}.pdf"
        with open(out_path, "rb") as f:
            supabase.storage.from_("documentos").upload(file_name, f, {"upsert": "true"})
        supabase.table("documentos").insert({"user_id": user_uuid, "tipo_documento": "Certificado PRO", "storage_path": file_name}).execute()
        
        with open(out_path, "rb") as f:
            await update.message.reply_document(
                document=f, 
                caption=f"✅ <b>Certificado Premium Generado.</b>\n<i>({costo} Créditos descontados)</i>",
                filename=f"Certificado_{context.user_data['cer_n_trab']}.pdf",
                parse_mode="HTML"
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error interno al renderizar CSS/PDF: {str(e)}")
        
    return ConversationHandler.END


# --- MOTORES DE VALIDACIÓN (RENIEC, SUNAT, SUNARP) EN PYTHON ---

async def consultar_api_jsonpe(endpoint: str, payload: dict) -> dict:
    """Consulta la API con caché inteligente en Supabase para ahorrar peticiones."""
    tipo = endpoint.upper()
    doc_val = list(payload.values())[0] if payload else "DESCONOCIDO"
    
    # 1. Buscar en Caché de Supabase
    def _get_cache():
        return supabase.table("consultas").select("resultado").eq("tipo", tipo).eq("numero_documento", doc_val).execute()
    
    try:
        res_cache = await asyncio.to_thread(_get_cache)
        if res_cache.data:
            logger.info(f"Caché HIT para {tipo} {doc_val} - Ahorrando petición API")
            return res_cache.data[0]["resultado"]
    except Exception as e:
        pass
        
    # 2. Si no hay caché, hacer la petición HTTP
    url = f"https://api.json.pe/api/{endpoint}"
    token = os.getenv("JSONPE_TOKEN")
    
    if not token:
        logger.error("JSONPE_TOKEN no está configurado en las variables de entorno.")
        return {"success": False, "message": "Error de configuración de API externa."}
        
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            # 1. Intentamos GET por Parámetros (Estándar común de APIs Peruanas)
            response = await client.get(url, params=payload, headers=headers)
            
            # 2. Si falla (404/405), probamos GET por Ruta Variable
            if response.status_code in [404, 405]:
                response = await client.get(f"{url}/{doc_val}", headers=headers)
                
            # 3. Si sigue fallando, usamos POST como método de respaldo total
            if response.status_code in [404, 405]:
                response = await client.post(url, json=payload, headers=headers)
                
            # Eliminamos raise_for_status() para capturar los JSON reales de error (404, 400)
            raw_res = response.json()
            
            # NORMALIZAR RESPUESTA: Forzamos que siempre exista 'success' y 'data'
            resultado = {}
            if isinstance(raw_res, dict):
                if response.status_code >= 400:
                    resultado["success"] = False
                else:
                    resultado["success"] = raw_res.get("success", True)
                
                if "data" in raw_res:
                    resultado["data"] = raw_res["data"]
                else:
                    resultado["data"] = {k: v for k, v in raw_res.items() if k not in ["success", "message", "error"]}
            else:
                resultado = {"success": True, "data": raw_res}
                
            # Validación extra: si devuelve Success=True pero no hay data
            if not resultado.get("data") and resultado.get("success"):
                resultado["success"] = False
            
            # 3. Guardar en Caché
            if resultado.get("success"):
                def _save_cache():
                    supabase.table("consultas").insert({"tipo": tipo, "numero_documento": doc_val, "resultado": resultado}).execute()
                asyncio.create_task(asyncio.to_thread(_save_cache)) # Guarda en segundo plano sin bloquear
                
            return resultado
        except Exception as e:
            logger.error(f"Error consultando {endpoint}: {e}")
            return {"success": False, "message": str(e)}

@require_credits
async def cmd_dni(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 1 or not context.args[0].isdigit() or len(context.args[0]) != 8:
        if update.message:
            await update.message.reply_text("❌ <b>Formato incorrecto.</b> Uso: <code>/dni 12345678</code>\n\n💰 <i>Costo: 1 Crédito</i>", parse_mode="HTML")
        return

    dni = context.args[0]
    if update.message:
        msg = await update.message.reply_text("⏳ <i>Consultando RENIEC...</i>", parse_mode="HTML")
        resultado = await consultar_api_jsonpe("dni", {"dni": dni})
        
        if not resultado.get("success"):
            await msg.edit_text("⚠️ No se encontró información oficial para ese número.", parse_mode="HTML")
            return
            
        data = resultado.get("data", {})
        
        # Soportamos tanto 'apellido_paterno' como 'apellidoPaterno'
        nombres = data.get('nombres', '')
        ap_paterno = data.get('apellido_paterno', data.get('apellidoPaterno', ''))
        ap_materno = data.get('apellido_materno', data.get('apellidoMaterno', ''))
        direccion = data.get('direccion_completa', data.get('direccion', 'No registrada'))
        foto_b64 = data.get('foto', data.get('foto_base64', data.get('imagen', '')))
        
        texto = (
            f"🏛️ <b>FICHA RENIEC</b>\n\n"
            f"👤 <b>Nombre:</b> {nombres} {ap_paterno} {ap_materno}\n"
            f"📍 <b>Dirección:</b> {direccion}\n\n"
            f"💰 <i>Costo: 1 Crédito descontado</i>\n"
            f"<i>✨ Verificado por LYP PRO</i>"
        )
        
        if foto_b64:
            try:
                # Limpiamos prefijos comunes que algunas APIs añaden (ej: "data:image/jpeg;base64,")
                if "," in foto_b64:
                    foto_b64 = foto_b64.split(",")[1]
                foto_bytes = base64.b64decode(foto_b64)
                await msg.delete() # Borramos el mensaje de texto de carga
                await update.message.reply_photo(photo=foto_bytes, caption=texto, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error al decodificar la foto del DNI: {e}")
                await msg.edit_text(texto, parse_mode="HTML")
        else:
            await msg.edit_text(texto, parse_mode="HTML")
            
        await descontar_creditos_basico(update.effective_user.id, 1, f"Consulta RENIEC: {dni}")

@require_credits
async def cmd_ruc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 1 or not context.args[0].isdigit() or len(context.args[0]) != 11:
        if update.message:
            await update.message.reply_text("❌ <b>Formato incorrecto.</b> Uso: <code>/ruc 20123456789</code>\n\n💰 <i>Costo: 1 Crédito</i>", parse_mode="HTML")
        return

    ruc = context.args[0]
    if update.message:
        msg = await update.message.reply_text("⏳ <i>Consultando SUNAT...</i>", parse_mode="HTML")
        resultado = await consultar_api_jsonpe("ruc", {"ruc": ruc})
        
        if not resultado.get("success"):
            await msg.edit_text("⚠️ No se encontró información oficial para ese número.", parse_mode="HTML")
            return
            
        data = resultado.get("data", {})
        
        estado = data.get("estado", data.get("estado_del_contribuyente", ""))
        condicion = data.get("condicion", data.get("condicion_de_domicilio", ""))
        razon_social = data.get("nombre_o_razon_social", data.get("razonSocial", ""))
        direccion = data.get("direccion_fiscal", data.get("direccion", "No registrada"))
        
        apto = "✅ <b>APTO PARA CONTRATAR</b>" if estado == "ACTIVO" and condicion == "HABIDO" else "⛔ <b>RIESGO DETECTADO</b>"
        
        texto = (
            f"🏢 <b>FICHA SUNAT</b>\n\n"
            f"📌 <b>Razón Social:</b> {razon_social}\n"
            f"📊 <b>Estado:</b> {estado} | <b>Condición:</b> {condicion}\n"
            f"📍 <b>Dirección:</b> {direccion}\n\n"
            f"{apto}\n\n"
            f"💰 <i>Costo: 1 Crédito descontado</i>"
        )
        await msg.edit_text(texto, parse_mode="HTML")
        await descontar_creditos_basico(update.effective_user.id, 1, f"Consulta SUNAT: {ruc}")

@require_credits
async def cmd_placa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 1:
        if update.message:
            await update.message.reply_text("❌ <b>Formato incorrecto.</b> Uso: <code>/placa ABC123</code>\n\n💰 <i>Costo: 1 Crédito</i>", parse_mode="HTML")
        return

    placa = context.args[0].upper()
    if update.message:
        msg = await update.message.reply_text("⏳ <i>Consultando SUNARP (Placa)...</i>", parse_mode="HTML")
        resultado = await consultar_api_jsonpe("placa", {"placa": placa})
        
        if not resultado.get("success"):
            await msg.edit_text("⚠️ No se encontró información vehicular para esa placa.", parse_mode="HTML")
            return
            
        data = resultado.get("data", {})
        propietarios_raw = data.get('propietarios', [])
        if isinstance(propietarios_raw, list):
            propietarios = ", ".join([p.get('nombre', str(p)) if isinstance(p, dict) else str(p) for p in propietarios_raw])
        else:
            propietarios = str(propietarios_raw)
            
        texto = (
            f"🚗 <b>FICHA VEHICULAR SUNARP</b>\n\n"
            f"📌 <b>Propietario(s):</b> {propietarios}\n"
            f"🚘 <b>Vehículo:</b> {data.get('marca', '')} {data.get('modelo', '')} ({data.get('color', '')})\n"
            f"🔢 <b>Placa:</b> {data.get('placa', '')}\n"
            f"🏢 <b>Sede:</b> {data.get('sede', '')}\n\n"
            f"💰 <i>Costo: 1 Crédito descontado</i>"
        )
        await msg.edit_text(texto, parse_mode="HTML")
        await descontar_creditos_basico(update.effective_user.id, 1, f"Consulta SUNARP: {placa}")

@require_credits
async def cmd_soat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 1:
        if update.message:
            await update.message.reply_text("❌ <b>Formato incorrecto.</b> Uso: <code>/soat ABC123</code>\n\n💰 <i>Costo: 1 Crédito</i>", parse_mode="HTML")
        return

    placa = context.args[0].upper()
    if update.message:
        msg = await update.message.reply_text("⏳ <i>Consultando SOAT...</i>", parse_mode="HTML")
        resultado = await consultar_api_jsonpe("soat", {"placa": placa})
        
        if not resultado.get("success"):
            await msg.edit_text("⚠️ No se encontró SOAT para esa placa.", parse_mode="HTML")
            return
            
        data = resultado.get("data", {})
        estado = data.get('estado', '')
        icono = "✅" if estado == "VIGENTE" else "❌"
        
        texto = (
            f"🏥 <b>FICHA SOAT</b>\n\n"
            f"🚘 <b>Placa:</b> {data.get('placa', '')}\n"
            f"🏢 <b>Compañía:</b> {data.get('nombre_compania', '')}\n"
            f"📅 <b>Vigencia:</b> {data.get('fecha_inicio', '')} al {data.get('fecha_fin', '')}\n"
            f"{icono} <b>Estado:</b> {estado}\n"
            f"📄 <b>Póliza:</b> <code>{data.get('numero_poliza', '')}</code>\n\n"
            f"💰 <i>Costo: 1 Crédito descontado</i>"
        )
        await msg.edit_text(texto, parse_mode="HTML")
        await descontar_creditos_basico(update.effective_user.id, 1, f"Consulta SOAT: {placa}")

@require_credits
async def cmd_licencia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 1 or len(context.args[0]) != 8:
        if update.message:
            await update.message.reply_text("❌ <b>Formato incorrecto.</b> Uso: <code>/licencia 12345678</code>\n\n💰 <i>Costo: 1 Crédito</i>", parse_mode="HTML")
        return

    dni = context.args[0]
    if update.message:
        msg = await update.message.reply_text("⏳ <i>Consultando MTC...</i>", parse_mode="HTML")
        resultado = await consultar_api_jsonpe("licencia", {"dni": dni})
        
        if not resultado.get("data") or "licencia" not in resultado.get("data", {}):
            await msg.edit_text("⚠️ No se encontró licencia de conducir para ese DNI.", parse_mode="HTML")
            return
            
        data = resultado.get("data", {})
        lic = data.get("licencia", {})
        
        texto = (
            f"🪪 <b>LICENCIA DE CONDUCIR (MTC)</b>\n\n"
            f"👤 <b>Conductor:</b> {data.get('nombre_completo', '')}\n"
            f"🔢 <b>Nro Licencia:</b> <code>{lic.get('numero', '')}</code>\n"
            f"🏷️ <b>Categoría:</b> {lic.get('categoria', '')}\n"
            f"📅 <b>Vencimiento:</b> {lic.get('fecha_vencimiento', '')}\n"
            f"📊 <b>Estado:</b> {lic.get('estado', '')}\n\n"
            f"💰 <i>Costo: 1 Crédito descontado</i>"
        )
        await msg.edit_text(texto, parse_mode="HTML")
        await descontar_creditos_basico(update.effective_user.id, 1, f"Consulta MTC: {dni}")

@require_credits
async def cmd_cee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 1:
        if update.message:
            await update.message.reply_text("❌ <b>Formato incorrecto.</b> Uso: <code>/cee 000000001</code>\n\n💰 <i>Costo: 1 Crédito</i>", parse_mode="HTML")
        return

    cee = context.args[0]
    if update.message:
        msg = await update.message.reply_text("⏳ <i>Consultando Migraciones...</i>", parse_mode="HTML")
        resultado = await consultar_api_jsonpe("cee", {"cee": cee})
        
        if not resultado.get("success"):
            await msg.edit_text("⚠️ No se encontró Carnet de Extranjería.", parse_mode="HTML")
            return
            
        data = resultado.get("data", {})
        texto = (
            f"🛂 <b>CARNET DE EXTRANJERÍA</b>\n\n"
            f"👤 <b>Nombre:</b> {data.get('nombres', '')} {data.get('apellido_paterno', '')} {data.get('apellido_materno', '')}\n"
            f"🔢 <b>Número:</b> <code>{data.get('numero', '')}</code>\n\n"
            f"💰 <i>Costo: 1 Crédito descontado</i>"
        )
        await msg.edit_text(texto, parse_mode="HTML")
        await descontar_creditos_basico(update.effective_user.id, 1, f"Consulta CEE: {cee}")

@require_credits
async def cmd_tc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 1:
        if update.message:
            await update.message.reply_text("❌ <b>Formato incorrecto.</b> Uso: <code>/tc 2024-01-01</code> (Año-Mes-Día)\n\n💰 <i>Costo: 1 Crédito</i>", parse_mode="HTML")
        return

    fecha = context.args[0]
    if update.message:
        msg = await update.message.reply_text("⏳ <i>Consultando SUNAT (Tipo de Cambio)...</i>", parse_mode="HTML")
        resultado = await consultar_api_jsonpe("tipo_de_cambio", {"fecha": fecha})
        
        if not resultado.get("success"):
            await msg.edit_text("⚠️ No se encontró tipo de cambio para esa fecha.", parse_mode="HTML")
            return
            
        data = resultado.get("data", {})
        texto = (
            f"💵 <b>TIPO DE CAMBIO SUNAT</b>\n\n"
            f"📅 <b>Fecha:</b> {data.get('fecha_sunat', '')}\n"
            f"🟢 <b>Compra:</b> S/ {data.get('compra', '')}\n"
            f"🔴 <b>Venta:</b> S/ {data.get('venta', '')}\n\n"
            f"💰 <i>Costo: 1 Crédito descontado</i>"
        )
        await msg.edit_text(texto, parse_mode="HTML")
        await descontar_creditos_basico(update.effective_user.id, 1, f"Consulta TC: {fecha}")

async def cmd_mis_contratos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Busca en Supabase los últimos 3 contratos generados y devuelve enlaces firmados seguros."""
    user_id = update.effective_user.id
    
    if update.message:
        msg = await update.message.reply_text("⏳ <i>Buscando en su archivo legal seguro...</i>", parse_mode="HTML")
        
    def _get_contratos():
        # 1. Buscar el UUID del usuario
        res_user = supabase.table("profiles").select("id").eq("telegram_id", user_id).execute()
        if not res_user.data: return None
            
        user_uuid = res_user.data[0]["id"]
        
        # 2. Consultar los últimos 3 documentos reales (Excluyendo las consultas de texto IA)
        res_docs = (supabase.table("documentos")
                    .select("tipo_documento, storage_path, creado_at")
                    .eq("user_id", user_uuid)
                    .neq("storage_path", "texto_ia")
                    .order("creado_at", desc=True)
                    .limit(3)
                    .execute())
                    
        # 3. Generar URLs firmadas válidas por 1 hora (3600 segundos)
        enlaces = []
        for doc in res_docs.data:
            signed_res = supabase.storage.from_("documentos").create_signed_url(doc["storage_path"], 3600)
            # Extraemos la URL firmada dependiendo del formato que retorne supabase-py
            url = signed_res if isinstance(signed_res, str) else signed_res.get("signedURL", signed_res.get("signedUrl", "#"))
            fecha_corta = doc["creado_at"].split("T")[0]
            enlaces.append({"tipo": doc["tipo_documento"], "fecha": fecha_corta, "url": url})
            
        return enlaces

    try:
        docs = await asyncio.to_thread(_get_contratos)
        
        if docs is None:
            await msg.edit_text("❌ Perfil no encontrado. Presione /start para registrarse.", parse_mode="HTML")
        elif not docs:
            await msg.edit_text("📂 <b>Su archivo legal está vacío.</b>\n\nAún no ha generado documentos. Presione /menu para comenzar.", parse_mode="HTML")
        else:
            texto = "📂 <b>MIS ÚLTIMOS DOCUMENTOS</b> 📂\n\nEstos enlaces de descarga seguros caducarán en 1 hora:\n\n"
            for i, d in enumerate(docs, 1):
                texto += f"<b>{i}. {d['tipo']}</b> (<i>{d['fecha']}</i>)\n🔗 <a href='{d['url']}'>Descargar Archivo</a>\n\n"
            await msg.edit_text(texto, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Error al obtener historial: {e}")
        await msg.edit_text("❌ Hubo un error al acceder a la bóveda legal. Intente más tarde.", parse_mode="HTML")

async def cmd_tarifario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el tarifario premium desde la base de datos."""
    def _get_tarifario():
        try:
            return supabase.table("servicios").select("*").eq("activo", True).order("categoria").execute()
        except Exception:
            return None
            
    res = await asyncio.to_thread(_get_tarifario)
    if not res or not res.data:
        await update.message.reply_text("⚠️ Tarifario no disponible por el momento.")
        return
        
    texto = "💎 <b>TARIFARIO OFICIAL LYP PRO</b> 💎\n\n"
    categoria_actual = ""
    for s in res.data:
        if s["categoria"] != categoria_actual:
            categoria_actual = s["categoria"]
            texto += f"\n📌 <b>{categoria_actual}</b>\n"
        costo = "Gratis 🎁" if s["costo_creditos"] == 0 else f"{int(s['costo_creditos'])} Crédito(s)"
        texto += f"• {s['nombre']}: <i>{costo}</i>\n"
    texto += "\n💳 <i>Para recargar saldo, usa el comando /buy.</i>"
    if update.message:
        await update.message.reply_text(texto, parse_mode="HTML")

async def cmd_recargar_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando oculto para recargar saldo. Solo accesible para admins y revendedores autorizados."""
    
    # 1. Validación de permisos (Admins y Revendedores Autorizados)
    admin_ids_env = os.getenv("ADMIN_TELEGRAM_ID", "")
    resellers_env = os.getenv("AUTHORIZED_RESELLERS", "")
    
    allowed_ids = []
    # Combinamos ambas listas de la variable de entorno separadas por coma
    for uid in (admin_ids_env + "," + resellers_env).split(","):
        if uid.strip().isdigit():
            allowed_ids.append(int(uid.strip()))
            
    if update.effective_user.id not in allowed_ids:
        # Comando oculto: si no estás en la lista, el bot te ignora silenciosamente
        return
        
    # 2. Validación de argumentos de entrada
    if len(context.args) != 2:
        await update.message.reply_text("❌ Uso incorrecto. Formato: `/recargar_user <telegram_id> <cantidad>`", parse_mode="Markdown")
        return
        
    target_telegram_id_str, cant_str = context.args[0], context.args[1]
    
    if not target_telegram_id_str.isdigit() or not cant_str.lstrip('-').isdigit():
        await update.message.reply_text("❌ Ambos parámetros deben ser numéricos.")
        return
        
    target_telegram_id = int(target_telegram_id_str)
    cant = int(cant_str)
    admin_id = update.effective_user.id
    
    # 3. Operación segura en la Base de Datos
    def _op():
        res_user = supabase.table("profiles").select("id, credits").eq("telegram_id", target_telegram_id).execute()
        if not res_user.data:
            # AUTO-REGISTRO PREMIUM: Si el usuario no existe, lo creamos para no bloquear la recarga.
            res_insert = supabase.table("profiles").insert({
                "telegram_id": target_telegram_id,
                "nombre": f"Cliente {target_telegram_id}",
                "credits": 0
            }).execute()
            user_uuid = res_insert.data[0]["id"]
            current_credits = 0
        else:
            user_uuid = res_user.data[0]["id"]
            current_credits = res_user.data[0]["credits"]
            
        new_credits = current_credits + cant
        
        supabase.table("profiles").update({"credits": new_credits}).eq("id", user_uuid).execute()
        supabase.table("credit_transactions").insert({
            "user_id": user_uuid, 
            "monto": cant, 
            "descripcion": f"Recarga manual por admin/reseller (ID: {admin_id})"
        }).execute()
        
        return True, new_credits

    if update.message:
        await update.message.reply_text("⏳ Procesando recarga...")
        
    success, result = await asyncio.to_thread(_op)
    
    if success:
        await update.message.reply_text(f"✅ *Recarga exitosa*.\nEl usuario `{target_telegram_id}` ahora tiene *{result}* créditos.", parse_mode="Markdown")
        
        # Intentar notificar al cliente de forma automática
        try:
            msg_cliente = (
                "🎉 <b>¡RECARGA EXITOSA!</b> 🎉\n\n"
                f"Se han añadido <b>{cant} créditos</b> a tu cuenta.\n"
                f"Tu saldo actual es de <b>{result} créditos</b>.\n\n"
                "Escribe /menu para empezar a generar tus documentos."
            )
            await context.bot.send_message(chat_id=target_telegram_id, text=msg_cliente, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Nota: La recarga se guardó, pero no se pudo notificar al usuario (puede que haya bloqueado al bot o eliminado el chat).")
    else:
        await update.message.reply_text(f"❌ Error: {result}")

def main() -> None:
    """Inicializa y arranca el bot de Telegram."""
    logger.info("Iniciando los servicios de LYP DOC BOT...")
    
    # Construcción de la aplicación asíncrona inyectando el token validado por Pydantic
    application = Application.builder().token(config.telegram_token).build()

    # Registro de comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("info", cmd_info))
    application.add_handler(CommandHandler("staff", cmd_staff))
    application.add_handler(CommandHandler("buy", cmd_buy))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("tarifario", cmd_tarifario))
    application.add_handler(CommandHandler("mis_contratos", cmd_mis_contratos))
    application.add_handler(CommandHandler("invitar", cmd_invitar))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("launch", cmd_launch))
    application.add_handler(CommandHandler("recargar_user", cmd_recargar_user))
    application.add_handler(CommandHandler("contrato_prueba", contrato_prueba))
    
    # Unificamos todos los flujos en un solo ConversationHandler Maestro
    # Esto soluciona los problemas de "estados cruzados" donde un comando contestaba preguntas de otro.
    master_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("contrato_alquiler", cmd_contrato_alquiler),
            CommandHandler("poder_simple", cmd_poder_simple),
            CommandHandler("soporte", cmd_soporte),
            CallbackQueryHandler(cmd_soporte, pattern="^menu_soporte$"),
            CommandHandler("demanda_alimentos", cmd_demanda_alimentos),
            CommandHandler("compraventa", cmd_compraventa),
            CommandHandler("liquidacion", cmd_liquidacion),
            CommandHandler("certificado", cmd_certificado)
        ],
        states={
            # Alquiler
            ASK_ALQ_N_ARRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_alq_n_arre)],
            ASK_ALQ_DNI_ARRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_alq_dni_arre)],
            ASK_ALQ_N_ARRE_T: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_alq_n_arre_t)],
            ASK_ALQ_DNI_ARRE_T: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_alq_dni_arre_t)],
            ASK_ALQ_DIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_alq_dir)],
            ASK_ALQ_USO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_alq_uso)],
            ASK_ALQ_PLAZO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_alq_plazo)],
            ASK_ALQ_RENTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_alq_renta)],
            ASK_ALQ_GARANTIA: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_alq_garantia)],
            ASK_ALQ_FECHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_alq_fecha)],
            
            # Poder Simple
            ASK_POD_N_OTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_pod_n_otor)],
            ASK_POD_DNI_OTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_pod_dni_otor)],
            ASK_POD_DOM_OTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_pod_dom_otor)],
            ASK_POD_N_APOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_pod_n_apod)],
            ASK_POD_DNI_APOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_pod_dni_apod)],
            ASK_POD_DOM_APOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_pod_dom_apod)],
            ASK_POD_FACULTADES: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_pod_facultades)],
            ASK_POD_VIGENCIA: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_pod_vigencia)],
            
            # Soporte IA
            ASK_SOPORTE_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_consulta_soporte)],
            
            # Demanda de Alimentos
            ASK_DEM_N_DDANTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_dem_n_ddante)],
            ASK_DEM_DNI_DDANTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_dem_dni_ddante)],
            ASK_DEM_DOM_DDANTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_dem_dom_ddante)],
            ASK_DEM_N_DDADO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_dem_n_ddado)],
            ASK_DEM_DOM_DDADO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_dem_dom_ddado)],
            ASK_DEM_N_MENOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_dem_n_menor)],
            ASK_DEM_EDAD_MENOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_dem_edad_menor)],
            ASK_DEM_MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_dem_monto)],
            ASK_DEM_CONCEPTOS: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_dem_conceptos)],
            ASK_DEM_JUSTIFICACION: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_dem_justificacion)],
            
            # Compraventa
            ASK_CV_N_VEND: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cv_n_vend)],
            ASK_CV_DNI_VEND: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cv_dni_vend)],
            ASK_CV_EST_VEND: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cv_est_vend)],
            ASK_CV_N_COMP: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cv_n_comp)],
            ASK_CV_DNI_COMP: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cv_dni_comp)],
            ASK_CV_EST_COMP: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cv_est_comp)],
            ASK_CV_UBICACION: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cv_ubicacion)],
            ASK_CV_PARTIDA: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cv_partida)],
            ASK_CV_OFICINA: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cv_oficina)],
            ASK_CV_PRECIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cv_precio)],
            ASK_CV_PAGO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cv_pago)],
            
            # Liquidación
            ASK_LIQ_RUC: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_liq_ruc)],
            ASK_LIQ_EMP: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_liq_emp)],
            ASK_LIQ_N_TRAB: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_liq_n_trab)],
            ASK_LIQ_DNI_TRAB: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_liq_dni)],
            ASK_LIQ_CARGO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_liq_cargo)],
            ASK_LIQ_INICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_liq_inicio)],
            ASK_LIQ_FIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_liq_fin)],
            ASK_LIQ_MOTIVO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_liq_motivo)],
            ASK_LIQ_SUELDO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_liq_sueldo)],
            ASK_LIQ_VACACIONES: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_liq_vacaciones)],
            
            # Certificado
            ASK_CER_RUC: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cer_ruc)],
            ASK_CER_EMP: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cer_emp)],
            ASK_CER_N_TRAB: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cer_n_trab)],
            ASK_CER_DNI_TRAB: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cert_dni)],
            ASK_CER_CARGO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cert_cargo)],
            ASK_CER_FUNCIONES: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cer_funciones)],
            ASK_CER_INICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cert_inicio)],
            ASK_CER_FIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cert_fin)],
            ASK_CER_LOGO_OPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_cer_logo_opt)],
            ASK_CER_LOGO_IMG: [MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, r_cer_logo_img)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_tramite)]
    )
    application.add_handler(master_conv_handler)

    application.add_handler(CommandHandler("dni", cmd_dni))
    application.add_handler(CommandHandler("ruc", cmd_ruc))
    application.add_handler(CommandHandler("placa", cmd_placa))
    application.add_handler(CommandHandler("soat", cmd_soat))
    application.add_handler(CommandHandler("licencia", cmd_licencia))
    application.add_handler(CommandHandler("cee", cmd_cee))
    application.add_handler(CommandHandler("tc", cmd_tc))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.PDF, manejar_archivo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto))

    # Producción: Webhooks vs Polling
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", "8080"))
    
    if WEBHOOK_URL:
        logger.info(f"Iniciando en modo Webhook en puerto {PORT}...")
        application.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)
    else:
        logger.info("Iniciando en modo Polling (Desarrollo). Presiona Ctrl+C para detener.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()