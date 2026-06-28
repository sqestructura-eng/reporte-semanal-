# =============================================================================
# BOT DE TELEGRAM - ANÁLISIS COT (Commitment of Traders) PARA SWING TRADING
# =============================================================================
# Incluye: Scheduler automático los viernes a las 20:00 UTC (= 16:00 EST/EDT)
# =============================================================================

import telebot
import pandas as pd
import requests
import zipfile
import io
import os
import logging
import threading
import schedule
import time
from datetime import datetime, timezone

# -----------------------------------------------------------------------------
# CONFIGURACIÓN PRINCIPAL
# -----------------------------------------------------------------------------

# El token se lee desde la variable de entorno definida en Render dashboard.
# En Render: Environment → Add Environment Variable → TELEGRAM_TOKEN = tu_token
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

# ID del chat/grupo donde se enviarán los reportes automáticos del viernes.
# Obtén tu chat_id hablando con @userinfobot en Telegram.
# En Render: Environment → Add Environment Variable → CHAT_ID_ALERTAS = tu_chat_id
CHAT_ID_ALERTAS = os.environ.get("CHAT_ID_ALERTAS")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# URL DEL REPORTE COT - CFTC
# -----------------------------------------------------------------------------

def get_cot_url():
    """Genera la URL del reporte COT del año actual."""
    year = datetime.now(timezone.utc).year
    return f"https://www.cftc.gov/files/dea/history/deacot_{year}.zip"

# -----------------------------------------------------------------------------
# MAPEO DE ACTIVOS
# -----------------------------------------------------------------------------

ASSET_MAP = {
    "oro":      "GOLD",
    "plata":    "SILVER",
    "euro":     "EURO FX",
    "btc":      "BITCOIN",
    "libra":    "BRITISH POUND",
    "dolar":    "U.S. DOLLAR INDEX",
    "petroleo": "CRUDE OIL",
    "jpy":      "JAPANESE YEN",         # Japanese Yen - Chicago Mercantile Exchange
}

ASSET_EMOJI = {
    "oro":      "🥇",
    "plata":    "🥈",
    "euro":     "💶",
    "btc":      "₿",
    "libra":    "💷",
    "dolar":    "💵",
    "petroleo": "🛢️",
    "jpy":      "🇯🇵",
}

# -----------------------------------------------------------------------------
# FUNCIÓN: DESCARGAR Y PARSEAR EL REPORTE COT
# -----------------------------------------------------------------------------

def download_cot_data():
    """
    Descarga el archivo ZIP anual de la CFTC y retorna un DataFrame de pandas.
    """
    url = get_cot_url()
    logger.info(f"Descargando datos COT desde: {url}")

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error al descargar el ZIP: {e}")
        raise ConnectionError(f"No se pudo descargar el reporte COT: {e}")

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        csv_filename = [f for f in z.namelist() if f.endswith(".txt")][0]
        logger.info(f"Leyendo archivo interno: {csv_filename}")
        with z.open(csv_filename) as f:
            df = pd.read_csv(f, low_memory=False)

    logger.info(f"Datos cargados: {len(df)} filas, {len(df.columns)} columnas")
    return df

# -----------------------------------------------------------------------------
# FUNCIÓN: ANALIZAR UN ACTIVO ESPECÍFICO
# -----------------------------------------------------------------------------

def analyze_asset(df, asset_key):
    """
    Filtra el DataFrame y calcula métricas de Swing Trading para el activo.
    """
    search_term = ASSET_MAP[asset_key]
    mask = df["Market_and_Exchange_Names"].str.contains(search_term, case=False, na=False)
    asset_df = df[mask].copy()

    if asset_df.empty:
        raise ValueError(f"No se encontraron datos para '{search_term}' en el reporte COT.")

    asset_df = asset_df.sort_values("As_of_Date_In_Form_YYMMDD", ascending=False)

    latest = asset_df.iloc[0]
    prev   = asset_df.iloc[1] if len(asset_df) > 1 else None

    long_col  = "NonComm_Positions_Long_All"
    short_col = "NonComm_Positions_Short_All"
    oi_col    = "Open_Interest_All"

    long_nc  = float(latest[long_col])
    short_nc = float(latest[short_col])
    oi       = float(latest[oi_col])

    total_nc      = long_nc + short_nc
    sentiment_pct = (long_nc / total_nc * 100) if total_nc > 0 else 50.0
    net_position  = long_nc - short_nc

    net_change = 0
    if prev is not None:
        prev_net   = float(prev[long_col]) - float(prev[short_col])
        net_change = net_position - prev_net

    if sentiment_pct >= 65 and net_change > 0:
        bias        = "🟢 ALCISTA FUERTE"
        bias_detail = "Institucionales acumulando agresivamente posiciones largas."
    elif sentiment_pct >= 55 and net_change >= 0:
        bias        = "🔵 ALCISTA MODERADO"
        bias_detail = "Ligera inclinación compradora. Confirmar con precio."
    elif sentiment_pct <= 35 and net_change < 0:
        bias        = "🔴 BAJISTA FUERTE"
        bias_detail = "Institucionales presionando con posiciones cortas."
    elif sentiment_pct <= 45 and net_change <= 0:
        bias        = "🟠 BAJISTA MODERADO"
        bias_detail = "Presión vendedora creciente. Proceder con cautela."
    else:
        bias        = "⚪ NEUTRO"
        bias_detail = "Sin dirección clara. Esperar confirmación del mercado."

    report_date = str(latest["As_of_Date_In_Form_YYMMDD"])

    return {
        "asset":         asset_key.upper(),
        "emoji":         ASSET_EMOJI[asset_key],
        "report_date":   report_date,
        "long_nc":       int(long_nc),
        "short_nc":      int(short_nc),
        "net_position":  int(net_position),
        "net_change":    int(net_change),
        "sentiment_pct": round(sentiment_pct, 2),
        "bias":          bias,
        "bias_detail":   bias_detail,
        "open_interest": int(oi),
    }

# -----------------------------------------------------------------------------
# FUNCIÓN: FORMATEAR MENSAJE DE TELEGRAM
# -----------------------------------------------------------------------------

def format_message(data):
    """Construye el mensaje de respuesta visual con Markdown v2."""
    change_sign  = "+" if data["net_change"] >= 0 else ""
    change_emoji = "📈" if data["net_change"] >= 0 else "📉"
    filled       = int(data["sentiment_pct"] / 10)
    bar          = "█" * filled + "░" * (10 - filled)

    message = (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{data['emoji']} *REPORTE COT — {data['asset']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 *Fecha del reporte:* `{data['report_date']}`\n\n"
        f"──────────────────────\n"
        f"📊 *POSICIONAMIENTO NON\\-COMMERCIALS*\n"
        f"──────────────────────\n"
        f"🟢 Longs:   `{data['long_nc']:,}` contratos\n"
        f"🔴 Shorts:  `{data['short_nc']:,}` contratos\n"
        f"⚖️  Neto:    `{data['net_position']:,}` contratos\n\n"
        f"──────────────────────\n"
        f"🎯 *SENTIMIENTO INSTITUCIONAL*\n"
        f"──────────────────────\n"
        f"`{bar}` {data['sentiment_pct']}%\n\n"
        f"{change_emoji} *Cambio neto semanal:* `{change_sign}{data['net_change']:,}` contratos\n\n"
        f"──────────────────────\n"
        f"🏹 *SESGO SWING TRADING*\n"
        f"──────────────────────\n"
        f"{data['bias']}\n"
        f"_{data['bias_detail']}_\n\n"
        f"📌 *Open Interest total:* `{data['open_interest']:,}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Solo análisis de posicionamiento\\. No es asesoría financiera\\._"
    )
    return message

# -----------------------------------------------------------------------------
# FUNCIÓN: REPORTE AUTOMÁTICO SEMANAL (todos los activos)
# -----------------------------------------------------------------------------

def send_weekly_cot_report():
    """
    Descarga el reporte COT y envía el análisis de TODOS los activos al chat
    configurado. Se ejecuta automáticamente cada viernes a las 20:00 UTC.
    """
    now_utc = datetime.now(timezone.utc)
    logger.info(f"[SCHEDULER] Ejecutando reporte semanal — {now_utc.strftime('%A %Y-%m-%d %H:%M UTC')}")

    header = (
        "🗓️ *REPORTE COT SEMANAL — VIERNES*\n"
        f"📡 _Publicado: {now_utc.strftime('%d/%m/%Y %H:%M')} UTC \\(16:00 EST\\)_\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Analizando todos los activos\\.\\.\\."
    )

    try:
        bot.send_message(CHAT_ID_ALERTAS, header, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"[SCHEDULER] Error enviando encabezado: {e}")
        return

    try:
        df = download_cot_data()
    except ConnectionError as e:
        bot.send_message(
            CHAT_ID_ALERTAS,
            f"🌐 *Error al descargar el reporte COT:*\n`{e}`",
            parse_mode="MarkdownV2"
        )
        return

    # Itera sobre todos los activos — JPY incluido automáticamente
    for asset_key in ASSET_MAP.keys():
        try:
            data = analyze_asset(df, asset_key)
            msg  = format_message(data)
            bot.send_message(CHAT_ID_ALERTAS, msg, parse_mode="MarkdownV2")
            logger.info(f"[SCHEDULER] ✅ Enviado: {asset_key}")
            time.sleep(1)  # Pausa anti-flood de Telegram
        except Exception as e:
            logger.error(f"[SCHEDULER] ❌ Error en {asset_key}: {e}")
            bot.send_message(
                CHAT_ID_ALERTAS,
                f"❌ Error analizando `{asset_key.upper()}`: `{e}`",
                parse_mode="MarkdownV2"
            )

    footer = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ *Reporte COT semanal completado\\.*\n"
        "_Próximo reporte: viernes 20:00 UTC_"
    )
    bot.send_message(CHAT_ID_ALERTAS, footer, parse_mode="MarkdownV2")
    logger.info("[SCHEDULER] Reporte semanal completo enviado.")

# -----------------------------------------------------------------------------
# SCHEDULER: VIERNES 20:00 UTC (= 16:00 EST / 17:00 EDT)
# -----------------------------------------------------------------------------

def run_scheduler():
    """
    Corre el scheduler en un hilo separado para no bloquear el polling del bot.
    20:00 UTC garantiza que el reporte de la CFTC ya está publicado (~15:30 EST).
    """
    schedule.every().friday.at("20:00").do(send_weekly_cot_report)
    logger.info("⏰ Scheduler activo: reporte automático cada viernes a las 20:00 UTC (16:00 EST)")

    while True:
        schedule.run_pending()
        time.sleep(30)  # Revisa cada 30 segundos (bajo consumo de CPU)

# -----------------------------------------------------------------------------
# COMANDOS DEL BOT
# -----------------------------------------------------------------------------

@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    """Mensaje de bienvenida y lista de activos disponibles."""
    text = (
        "👋 *Bienvenido al Bot COT de Swing Trading*\n\n"
        "Analizo el posicionamiento institucional de los reportes *COT de la CFTC* "
        "para darte un sesgo de mercado claro\\.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 *ACTIVOS DISPONIBLES*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🥇 `/cot oro` — Gold Futures\n"
        "🥈 `/cot plata` — Silver Futures\n"
        "💶 `/cot euro` — Euro FX Futures\n"
        "₿ `/cot btc` — Bitcoin Futures\n"
        "💷 `/cot libra` — British Pound Futures\n"
        "💵 `/cot dolar` — US Dollar Index\n"
        "🛢️ `/cot petroleo` — Crude Oil Futures\n"
        "🇯🇵 `/cot jpy` — Japanese Yen Futures\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🗓️ *Reporte automático:* cada viernes 20:00 UTC \\(16:00 EST\\)\n\n"
        "💡 *Consulta manual:* `/cot oro`\n"
        "🔁 *Forzar reporte ahora:* `/reporteahora`\n\n"
        "⏳ _La primera consulta tarda \\~10 seg mientras descargo el reporte\\._"
    )
    bot.reply_to(message, text, parse_mode="MarkdownV2")


@bot.message_handler(commands=["cot"])
def handle_cot(message):
    """Comando principal: /cot [activo]"""
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(
            message,
            "⚠️ Debes especificar un activo\\.\nEjemplo: `/cot oro`\n\nUsa `/help` para ver todos\\.",
            parse_mode="MarkdownV2"
        )
        return

    asset_key = parts[1].lower().strip()

    if asset_key not in ASSET_MAP:
        available = ", ".join(f"`{k}`" for k in ASSET_MAP.keys())
        bot.reply_to(
            message,
            f"❌ Activo no reconocido: `{asset_key}`\n\n*Activos válidos:* {available}",
            parse_mode="MarkdownV2"
        )
        return

    processing_msg = bot.reply_to(
        message,
        "⏳ _Descargando y analizando el reporte COT\\.\\.\\. Un momento\\._",
        parse_mode="MarkdownV2"
    )

    try:
        df   = download_cot_data()
        data = analyze_asset(df, asset_key)
        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=processing_msg.message_id,
            text=format_message(data),
            parse_mode="MarkdownV2"
        )
    except ConnectionError as e:
        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=processing_msg.message_id,
            text=f"🌐 *Error de conexión:*\n_{str(e)}_\n\nIntenta de nuevo en unos minutos\\.",
            parse_mode="MarkdownV2"
        )
    except ValueError as e:
        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=processing_msg.message_id,
            text=f"🔍 *Datos no encontrados:*\n_{str(e)}_",
            parse_mode="MarkdownV2"
        )
    except Exception as e:
        logger.error(f"Error en /cot {asset_key}: {e}", exc_info=True)
        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=processing_msg.message_id,
            text=f"❗ *Error inesperado:*\n`{str(e)}`",
            parse_mode="MarkdownV2"
        )


@bot.message_handler(commands=["reporteahora"])
def handle_force_report(message):
    """Fuerza el envío inmediato del reporte completo. Útil para pruebas."""
    bot.reply_to(message, "🔄 _Generando reporte completo\\.\\.\\._", parse_mode="MarkdownV2")
    threading.Thread(target=send_weekly_cot_report, daemon=True).start()


@bot.message_handler(func=lambda message: True)
def handle_unknown(message):
    """Respuesta para mensajes no reconocidos."""
    bot.reply_to(
        message,
        "🤖 No entiendo ese comando\\.\nUsa `/help` para ver los disponibles\\.",
        parse_mode="MarkdownV2"
    )

# -----------------------------------------------------------------------------
# PUNTO DE ENTRADA PRINCIPAL
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("🤖 Bot COT iniciado.")
    logger.info("⏰ Scheduler: viernes 20:00 UTC = 16:00 EST / 17:00 EDT")

    # Scheduler en hilo daemon (no bloquea el polling)
    threading.Thread(target=run_scheduler, daemon=True).start()

    logger.info("📡 Polling activo. Esperando mensajes...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
