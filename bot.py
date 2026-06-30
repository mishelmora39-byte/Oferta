import json
import re
import os
import time
import logging
import httpx
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "TU_TOKEN_AQUI")
AFFILIATE_ID = os.getenv("AFFILIATE_ID", "293AH0-18PY")
CHANNEL_ID   = int(os.getenv("CHANNEL_ID", "-1004405739696"))
ADMIN_ID     = 333569583

CHAT_IDS_FILE   = "chat_ids.json"
SEEN_DEALS_FILE = "seen_deals.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "es-MX,es;q=0.9",
}

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except: return default

def save_json(path, data):
    with open(path, "w") as f: json.dump(data, f)

def make_affiliate_link(url: str) -> str:
    if not url: return ""
    clean = url.split("?")[0].split("#")[0]
    return f"{clean}?matt_tool={AFFILIATE_ID}&matt_source=telegram&matt_campaign=jackrocko"

def format_deal(deal: dict) -> str:
    price_str = f"${deal['price']:,.0f} MXN"
    orig_str  = f"~~${deal['original']:,.0f}~~ → " if deal['original'] > deal['price'] else ""
    disc_str  = f"🏷️ *{deal['discount']}% OFF*" if deal['discount'] > 0 else "🔥 ¡Gran Precio!"
    return (
        f"🔥 *{deal['title']}*\n\n"
        f"💰 {orig_str}*{price_str}*\n"
        f"{disc_str}\n\n"
        f"🛒 [Ver en Mercado Libre]({deal['url']})"
    )

# ── FUENTE 1: API pública de ML (sin token) ────────────────────────────────────
# Usamos el endpoint de búsqueda con filtro de promociones activas.
# Este endpoint es público y no requiere autenticación.
async def fetch_api_deals() -> list:
    deals = []
    # Endpoint público: busca productos con descuento activo en México
    endpoints = [
        "https://api.mercadolibre.com/sites/MLM/search?promotion_type=deal_of_the_day&limit=50",
        "https://api.mercadolibre.com/sites/MLM/search?promotion_type=lightning_deal&limit=50",
        "https://api.mercadolibre.com/sites/MLM/search?q=oferta&sort=relevance&limit=50",
    ]
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
        for url in endpoints:
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    logger.warning(f"API status {r.status_code} para {url}")
                    continue
                results = r.json().get("results", [])
                logger.info(f"API devolvió {len(results)} items de {url}")
                for item in results:
                    price = item.get("price", 0)
                    orig  = item.get("original_price") or 0
                    if price <= 0: continue
                    disc = round((1 - price / orig) * 100) if orig > price else 0
                    deals.append({
                        "id":       item["id"],
                        "title":    item["title"][:80],
                        "price":    price,
                        "original": orig if orig > price else price,
                        "discount": disc,
                        "url":      make_affiliate_link(item.get("permalink", "")),
                        "img":      item.get("thumbnail", "").replace("-I.jpg", "-O.jpg"),
                    })
            except Exception as e:
                logger.error(f"Error en API endpoint: {e}")
    return deals

# ── FUENTE 2: Scraper directo de la página /ofertas ───────────────────────────
# Mercado Libre renderiza los productos en JSON dentro de un <script> en la página.
# Esto es más confiable que parsear el HTML visual.
async def fetch_scraper_deals() -> list:
    deals = []
    url = "https://www.mercadolibre.com.mx/ofertas"
    async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
        try:
            r = await client.get(url)
            if r.status_code != 200:
                logger.error(f"Scraper HTTP {r.status_code}")
                return []

            # Buscar JSON embebido en el HTML (window.__PRELOADED_STATE__ o similar)
            text = r.text

            # Patrón 1: JSON en script con datos de productos
            matches = re.findall(r'"permalink"\s*:\s*"(https://www\.mercadolibre\.com\.mx/[^"]+)"', text)
            titles  = re.findall(r'"title"\s*:\s*"([^"]{10,})"', text)
            prices  = re.findall(r'"price"\s*:\s*(\d+(?:\.\d+)?)', text)
            origs   = re.findall(r'"original_price"\s*:\s*(\d+(?:\.\d+)?)', text)
            imgs    = re.findall(r'"secure_thumbnail"\s*:\s*"([^"]+)"', text)
            ids     = re.findall(r'"id"\s*:\s*"(MLM\d+)"', text)

            logger.info(f"Scraper encontró: {len(ids)} IDs, {len(titles)} títulos, {len(prices)} precios, {len(matches)} links")

            for i, prod_id in enumerate(ids):
                try:
                    title = titles[i] if i < len(titles) else f"Producto {prod_id}"
                    price = float(prices[i]) if i < len(prices) else 0
                    orig  = float(origs[i])  if i < len(origs)  else 0
                    link  = matches[i]        if i < len(matches) else f"https://www.mercadolibre.com.mx/p/{prod_id}"
                    img   = imgs[i]           if i < len(imgs)   else ""

                    if price <= 0: continue
                    disc = round((1 - price / orig) * 100) if orig > price else 0

                    deals.append({
                        "id":       prod_id,
                        "title":    title[:80],
                        "price":    price,
                        "original": orig if orig > price else price,
                        "discount": disc,
                        "url":      make_affiliate_link(link),
                        "img":      img,
                    })
                except: continue

        except Exception as e:
            logger.error(f"Error general scraper: {e}")

    logger.info(f"Scraper procesó {len(deals)} productos")
    return deals

# ── Combinar fuentes ───────────────────────────────────────────────────────────
async def get_all_deals() -> list:
    api_deals     = await fetch_api_deals()
    scraper_deals = await fetch_scraper_deals()
    combined = api_deals + scraper_deals
    unique   = list({d["id"]: d for d in combined}.values())
    logger.info(f"📢 Total único para enviar: {len(unique)}")
    return unique

# ── Envío ──────────────────────────────────────────────────────────────────────
async def send_deals(bot: Bot, deals: list, chat_id: int, limit: int = 5) -> int:
    seen = load_json(SEEN_DEALS_FILE, [])
    new  = [d for d in deals if d["id"] not in seen]
    count = 0
    for d in new[:limit]:
        try:
            text = format_deal(d)
            if d.get("img"):
                await bot.send_photo(chat_id, d["img"], caption=text, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id, text, parse_mode="Markdown")
            seen.append(d["id"])
            count += 1
            await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"Error enviando {d['id']}: {e}")
    save_json(SEEN_DEALS_FILE, seen[-500:])
    return count

# ── Broadcast programado ───────────────────────────────────────────────────────
async def broadcast(bot: Bot):
    deals = await get_all_deals()
    if not deals:
        logger.warning("No se encontraron ofertas en esta ronda.")
        return
    await send_deals(bot, deals, CHANNEL_ID, limit=8)
    for cid in load_json(CHAT_IDS_FILE, []):
        await send_deals(bot, deals, cid, limit=3)

# ── Comandos ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    ids = load_json(CHAT_IDS_FILE, [])
    if cid not in ids:
        ids.append(cid)
        save_json(CHAT_IDS_FILE, ids)
    await update.message.reply_text(
        "👋 ¡Hola! Soy JackRocko Bot.\n"
        "Te mando las mejores ofertas de Mercado Libre automáticamente.\n"
        "Usa /ofertas para buscar ahora mismo."
    )

async def cmd_ofertas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Buscando ofertas en vivo...")
    deals = await get_all_deals()
    sent  = await send_deals(ctx.bot, deals, update.effective_chat.id, limit=5)
    if sent == 0:
        if not deals:
            await update.message.reply_text("⚠️ No encontré ofertas. Revisa los logs.")
        else:
            await update.message.reply_text(
                f"✅ Encontré {len(deals)} ofertas pero ya las enviaste antes.\n"
                "Borra seen_deals.json para reiniciar el historial."
            )

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    save_json(SEEN_DEALS_FILE, [])
    await update.message.reply_text("✅ Historial de ofertas reiniciado.")

# ── Main ───────────────────────────────────────────────────────────────────────
import asyncio

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("ofertas", cmd_ofertas))
    app.add_handler(CommandHandler("reset",   cmd_reset))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(broadcast, "interval", minutes=15, args=[app.bot])
    scheduler.start()

    logger.info("🚀 JackRocko Bot iniciado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
