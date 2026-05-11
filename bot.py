#!/usr/bin/env python3
"""
Asistencia_bot (@JackRocko_bot) — Tecnología con descuento ≥40%, precio $100–$6000
"""

import json
import logging
import asyncio
import hashlib
import datetime

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update

BOT_TOKEN       = "8699234184:AAFphRqFAJtt3C99stShlYfwJFoPpz0cVZA"
CHAT_ID_FILE    = "chat_ids.json"
SEEN_FILE       = "seen_deals.json"
PRECIO_MIN      = 100
PRECIO_MAX      = 6000
DESCUENTO_MIN   = 40
INTERVALO_HORAS = 3

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ─── HELPERS ────────────────────────────────────────────────────────────────

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def deal_id(deal):
    return hashlib.md5(f"{deal['title']}{deal['url']}".encode()).hexdigest()[:12]

def escape_md(text):
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = str(text).replace(ch, f"\\{ch}")
    return text

# ─── BÚSQUEDA ────────────────────────────────────────────────────────────────

async def buscar_ofertas_ml(query: str, limit: int = 30) -> list[dict]:
    """Una sola búsqueda en ML, sin parámetros restringidos."""
    deals = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                "https://api.mercadolibre.com/sites/MLM/search",
                params={
                    "q":     query,
                    "price": f"{PRECIO_MIN}-{PRECIO_MAX}",
                    "limit": limit,
                    "sort":  "relevance",
                },
            )
            r.raise_for_status()
            for item in r.json().get("results", []):
                current  = float(item.get("price") or 0)
                original = float(item.get("original_price") or 0)
                if current <= 0 or original <= current:
                    continue
                discount = round((1 - current / original) * 100)
                if discount < DESCUENTO_MIN:
                    continue
                deals.append({
                    "title":    item.get("title", "Sin título"),
                    "price":    current,
                    "original": original,
                    "discount": discount,
                    "url":      item.get("permalink", ""),
                    "seller":   item.get("seller", {}).get("nickname", ""),
                })
        log.info(f"ML '{query}': {len(deals)} ofertas con ≥{DESCUENTO_MIN}%")
    except Exception as e:
        log.error(f"Error ML ({query}): {e}")
    return deals


async def collect_deals() -> list[dict]:
    """Búsqueda en 3 keywords secuenciales — rápido y confiable."""
    all_deals = []
    keywords = ["tecnologia oferta", "laptop tablet smartphone", "electronica descuento"]

    for kw in keywords:
        result = await buscar_ofertas_ml(kw, limit=50)
        all_deals.extend(result)
        await asyncio.sleep(0.5)

    # Deduplicar
    seen_titles: set = set()
    unique = []
    for d in all_deals:
        key = d["title"][:40].lower()
        if key not in seen_titles:
            seen_titles.add(key)
            unique.append(d)

    unique.sort(key=lambda x: x["discount"], reverse=True)
    log.info(f"Total únicos: {len(unique)}")
    return unique[:20]

# ─── FORMATO ─────────────────────────────────────────────────────────────────

def format_deal(deal: dict, idx: int) -> str:
    ahorro = deal["original"] - deal["price"]
    pct    = deal["discount"]
    bar    = "█" * min(pct // 5, 20) + "░" * max(0, 20 - pct // 5)
    title  = escape_md(deal["title"][:80])
    seller = escape_md(deal.get("seller", ""))
    ts     = escape_md(datetime.datetime.now().strftime("%d/%m/%Y %H:%M"))
    msg = (
        f"🔥 *Oferta \\#{idx}*\n"
        f"📦 {title}\n\n"
        f"💰 *\\${deal['price']:,.0f} MXN*  ~~\\${deal['original']:,.0f}~~\n"
        f"🏷️ *{pct}% OFF* — ahorro \\${ahorro:,.0f}\n"
        f"`{bar}`\n"
    )
    if seller:
        msg += f"🏪 _{seller}_\n"
    msg += f"🕐 _{ts}_"
    return msg

# ─── ENVÍO ───────────────────────────────────────────────────────────────────

async def send_deals(bot, chat_ids: list, deals: list[dict]):
    if not deals:
        for cid in chat_ids:
            await bot.send_message(
                cid,
                "😔 No encontré productos con ≥40% descuento en este momento\\.\n"
                "Mercado Libre muestra poco precio original hoy\\.\n"
                "⏰ Reintentaré en 3 horas\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        return

    for cid in chat_ids:
        try:
            # Encabezado
            await bot.send_message(
                cid,
                f"🤖 *Ofertas Tech* — {escape_md(datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))}\n"
                f"📊 *{len(deals)} ofertas* \\| ≥40% desc\\. \\| \\$100–\\$6,000 MXN",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            # Ofertas
            for i, deal in enumerate(deals, 1):
                try:
                    kb = None
                    if deal.get("url"):
                        kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton("🛒 Ver en Mercado Libre", url=deal["url"])
                        ]])
                    await bot.send_message(
                        cid,
                        format_deal(deal, i),
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_markup=kb,
                        disable_web_page_preview=True,
                    )
                    await asyncio.sleep(0.3)
                except Exception as e:
                    log.warning(f"Error oferta {i}: {e}")

            await bot.send_message(
                cid,
                f"✅ Fin\\. Próxima búsqueda en *{INTERVALO_HORAS} horas*",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            log.error(f"Error enviando a {cid}: {e}")

# ─── JOB ─────────────────────────────────────────────────────────────────────

async def job_buscar(bot):
    log.info("⏰ JOB — buscando ofertas...")
    chat_ids = load_json(CHAT_ID_FILE, [])
    if not chat_ids:
        log.warning("Sin chats registrados.")
        return
    seen      = set(load_json(SEEN_FILE, []))
    deals     = await collect_deals()
    new_deals = [d for d in deals if deal_id(d) not in seen]
    for d in new_deals:
        seen.add(deal_id(d))
    save_json(SEEN_FILE, list(seen)[-500:])
    log.info(f"Nuevas: {len(new_deals)}")
    await send_deals(bot, chat_ids, new_deals)

# ─── COMANDOS ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    log.info(f"/start cid={cid}")
    ids = load_json(CHAT_ID_FILE, [])
    if cid not in ids:
        ids.append(cid)
        save_json(CHAT_ID_FILE, ids)
        txt = (
            f"✅ *¡Listo\\!* Quedaste registrado\\.\n\n"
            f"⏰ Recibirás ofertas cada *{INTERVALO_HORAS} horas*\n"
            f"💰 Precio: \\$100 – \\$6,000 MXN\n"
            f"🏷️ Descuento mínimo: 40%\n\n"
            f"/ofertas — buscar ahora\n"
            f"/buscar laptop — producto específico\n"
            f"/estado — info del bot\n"
            f"/salir — desactivar"
        )
    else:
        txt = "✅ Ya estás registrado\\. Usa /ofertas para buscar ahora\\."
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_ofertas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    log.info(f"/ofertas cid={cid}")
    await update.message.reply_text("🔍 Buscando... 30 segundos aprox\\.", parse_mode=ParseMode.MARKDOWN_V2)
    deals = await collect_deals()
    await send_deals(ctx.bot, [cid], deals)


async def cmd_buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args) if ctx.args else ""
    if not query:
        await update.message.reply_text("❓ Ejemplo: /buscar laptop gaming")
        return
    cid = update.effective_chat.id
    log.info(f"/buscar '{query}' cid={cid}")
    await update.message.reply_text(f"🔍 Buscando: *{escape_md(query)}*\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    deals = await buscar_ofertas_ml(query, limit=50)
    if deals:
        deals.sort(key=lambda x: x["discount"], reverse=True)
        await send_deals(ctx.bot, [cid], deals[:10])
    else:
        await update.message.reply_text(
            f"😔 Sin resultados para *{escape_md(query)}* con ≥40% desc\\. en ese rango\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ids  = load_json(CHAT_ID_FILE, [])
    seen = load_json(SEEN_FILE, [])
    await update.message.reply_text(
        f"📊 *Estado*\n"
        f"✅ Activo\n"
        f"👥 Chats: *{len(ids)}*\n"
        f"📦 Historial: *{len(seen)}*\n"
        f"⏰ Cada *{INTERVALO_HORAS}h*\n"
        f"💰 \\$100–\\$6,000 MXN \\| ≥40%\n"
        f"🕐 {escape_md(datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_salir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    ids = load_json(CHAT_ID_FILE, [])
    if cid in ids:
        ids.remove(cid)
        save_json(CHAT_ID_FILE, ids)
        await update.message.reply_text("👋 Listo, ya no recibirás alertas. Usa /start para volver.")
    else:
        await update.message.reply_text("No estabas registrado.")

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("ofertas", cmd_ofertas))
    app.add_handler(CommandHandler("buscar",  cmd_buscar))
    app.add_handler(CommandHandler("estado",  cmd_estado))
    app.add_handler(CommandHandler("salir",   cmd_salir))

    scheduler = AsyncIOScheduler(timezone="America/Mexico_City")
    scheduler.add_job(
        job_buscar,
        trigger="interval",
        hours=INTERVALO_HORAS,
        args=[app.bot],
        id="job_ofertas",
        next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=20),
    )

    async def on_startup(a):
        scheduler.start()
        log.info(f"✅ Bot listo — buscará cada {INTERVALO_HORAS}h")

    async def on_shutdown(a):
        scheduler.shutdown()

    app.post_init     = on_startup
    app.post_shutdown = on_shutdown

    log.info("🤖 Arrancando @JackRocko_bot...")
    app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    main()
