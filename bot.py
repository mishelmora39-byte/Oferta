import json
import re
import os
import asyncio
import logging
import httpx
from bs4 import BeautifulSoup
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

# Headers que simulan un navegador real para evitar bloqueos
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
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
    orig_str  = f"~~${deal['original']:,.0f}~~ → " if deal.get('original', 0) > deal['price'] else ""
    disc_str  = f"🏷️ *{deal['discount']}% OFF*" if deal.get('discount', 0) > 0 else "🔥 ¡Gran Precio!"
    return (
        f"🔥 *{deal['title']}*\n\n"
        f"💰 {orig_str}*{price_str}*\n"
        f"{disc_str}\n\n"
        f"🛒 [Ver en Mercado Libre]({deal['url']})"
    )

# ── FUENTE 1: API pública de ML (endpoint que SÍ funciona sin token) ───────────
# Este endpoint devuelve productos en oferta sin requerir autenticación
async def fetch_api_deals() -> list:
    deals = []
    # Endpoint de búsqueda pública que funciona sin token
    urls = [
        "https://api.mercadolibre.com/sites/MLM/search?q=oferta+del+dia&sort=relevance&limit=50&offset=0",
        "https://api.mercadolibre.com/sites/MLM/search?q=remate+liquidacion&sort=price_asc&limit=50",
    ]
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": HEADERS["User-Agent"]}) as client:
        for url in urls:
            try:
                r = await client.get(url)
                logger.info(f"API status {r.status_code} → {url[:60]}")
                if r.status_code != 200:
                    continue
                results = r.json().get("results", [])
                logger.info(f"API devolvió {len(results)} items")
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
                logger.error(f"Error API: {e}")
    logger.info(f"API total: {len(deals)} productos")
    return deals

# ── FUENTE 2: Scraper con BeautifulSoup ───────────────────────────────────────
# Parsea el HTML real de la página de ofertas de ML
async def fetch_scraper_deals() -> list:
    deals = []
    url = "https://www.mercadolibre.com.mx/ofertas"

    async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
        try:
            r = await client.get(url)
            if r.status_code != 200:
                logger.error(f"Scraper HTTP {r.status_code}")
                return []

            soup = BeautifulSoup(r.text, "html.parser")

            # Buscar todos los contenedores de producto
            # ML usa varias clases según el layout, probamos todas
            containers = (
                soup.select("li.promotion-item") or
                soup.select("div.promotion-item") or
                soup.select("li[class*='promotion']") or
                soup.select("div[class*='andes-card']") or
                soup.select("article") or
                soup.select("li[class*='result']")
            )

            logger.info(f"Scraper encontró {len(containers)} contenedores de producto")

            for i, item in enumerate(containers):
                try:
                    # Título
                    title_el = (
                        item.select_one("p.promotion-item__title") or
                        item.select_one("[class*='title']") or
                        item.select_one("h2") or
                        item.select_one("h3") or
                        item.select_one("p")
                    )
                    title = title_el.get_text(strip=True) if title_el else f"Producto {i+1}"
                    if len(title) < 5: continue

                    # Link
                    link_el = item.select_one("a[href*='mercadolibre']") or item.select_one("a")
                    link = link_el["href"] if link_el and link_el.get("href") else ""
                    if not link: continue

                    # Precio actual
                    price_el = (
                        item.select_one("span.andes-money-amount__fraction") or
                        item.select_one("[class*='price__fraction']") or
                        item.select_one("[class*='amount__fraction']") or
                        item.select_one("span[class*='price']")
                    )
                    price_text = price_el.get_text(strip=True).replace(",", "").replace("$", "") if price_el else "0"
                    price = float(re.sub(r"[^\d.]", "", price_text)) if price_text else 0
                    if price <= 0: continue

                    # Precio original (tachado)
                    orig_el = (
                        item.select_one("s span.andes-money-amount__fraction") or
                        item.select_one("del span") or
                        item.select_one("[class*='original'] span") or
                        item.select_one("s")
                    )
                    orig_text = orig_el.get_text(strip=True).replace(",", "").replace("$", "") if orig_el else "0"
                    orig = float(re.sub(r"[^\d.]", "", orig_text)) if orig_text else 0

                    # Descuento
                    disc_el = item.select_one("[class*='discount']") or item.select_one("[class*='off']")
                    disc_text = disc_el.get_text(strip=True) if disc_el else ""
                    disc_match = re.search(r"(\d+)", disc_text)
                    disc = int(disc_match.group(1)) if disc_match else (
                        round((1 - price / orig) * 100) if orig > price else 0
                    )

                    # Imagen
                    img_el = item.select_one("img")
                    img = img_el.get("data-src") or img_el.get("src") or "" if img_el else ""

                    # ID desde el link
                    id_match = re.search(r"MLM-?(\d+)", link)
                    prod_id = f"MLM{id_match.group(1)}" if id_match else f"scraper_{i}"

                    deals.append({
                        "id":       prod_id,
                        "title":    title[:80],
                        "price":    price,
                        "original": orig if orig > price else price,
                        "discount": disc,
                        "url":      make_affiliate_link(link),
                        "img":      img,
                    })
                except Exception as e:
                    logger.debug(f"Error en item {i}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error general scraper: {e}")

    logger.info(f"Scraper procesó {len(deals)} productos válidos")
    return deals

# ── Combinar fuentes ───────────────────────────────────────────────────────────
async def get_all_deals() -> list:
    scraper_deals = await fetch_scraper_deals()
    api_deals     = await fetch_api_deals()
    combined = scraper_deals + api_deals
    unique   = list({d["id"]: d for d in combined}.values())
    logger.info(f"📢 Total único para enviar: {len(unique)}")
    return unique

# ── Envío ──────────────────────────────────────────────────────────────────────
async def send_deals(bot: Bot, deals: list, chat_id: int, limit: int = 5) -> int:
    seen  = load_json(SEEN_DEALS_FILE, [])
    new   = [d for d in deals if d["id"] not in seen]
    count = 0
    for d in new[:limit]:
        try:
            text = format_deal(d)
            if d.get("img") and d["img"].startswith("http"):
                await bot.send_photo(chat_id, d["img"], caption=text, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id, text, parse_mode="Markdown")
            seen.append(d["id"])
            count += 1
            await asyncio.sleep(1.5)
        except Exception as e:
            logger.warning(f"Error enviando {d['id']}: {e}")
    save_json(SEEN_DEALS_FILE, seen[-500:])
    return count

# ── Broadcast programado ───────────────────────────────────────────────────────
async def broadcast(bot: Bot):
    deals = await get_all_deals()
    if not deals:
        logger.warning("⚠️ No se encontraron ofertas en esta ronda.")
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
    if not deals:
        await update.message.reply_text("⚠️ No encontré ofertas. Revisa los logs del servidor.")
        return
    sent = await send_deals(ctx.bot, deals, update.effective_chat.id, limit=5)
    if sent == 0:
        await update.message.reply_text(
            f"✅ Encontré {len(deals)} ofertas pero ya las viste antes.\n"
            "Usa /reset para reiniciar el historial."
        )

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    save_json(SEEN_DEALS_FILE, [])
    await update.message.reply_text("✅ Historial reiniciado. Usa /ofertas para ver todo de nuevo.")

# ── Main ───────────────────────────────────────────────────────────────────────
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
