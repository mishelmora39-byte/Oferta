import json
import os
import logging
import httpx
from bs4 import BeautifulSoup
from telegram import Bot, Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "TU_TOKEN_AQUI")
AFFILIATE_ID = os.getenv("AFFILIATE_ID", "293AH0-18PY")

# Cupones bancarios opcionales — déjalos vacíos ("") si no hay vigentes
CUPONES = {
    "BBVA":            "",
    "BANAMEX":         "",
    "AMEX":            "",
    "AFIRME":          "",
    "MIFEL":           "",
    "MERCADO PAGO":    "",
    "MESES SIN TARJETA": "",
}

CHAT_IDS_FILE  = "chat_ids.json"
SEEN_DEALS_FILE = "seen_deals.json"
MIN_DISCOUNT   = 40

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "es-MX,es;q=0.9",
}

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)

def make_affiliate_link(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}matt_tool={AFFILIATE_ID}&matt_word=&matt_source=telegram&matt_campaign=jackrocko"

def cupones_text() -> str:
    activos = {banco: codigo for banco, codigo in CUPONES.items() if codigo.strip()}
    if not activos:
        return ""
    lineas = "\n".join(f'🟡 {banco}: *"{codigo}"*' for banco, codigo in activos.items())
    return f"\n\n💳 *Cupones bancarios:*\n{lineas}"

def format_deal(deal: dict) -> str:
    cupones = cupones_text()
    return (
        f"🔥 *{deal['title']}*\n\n"
        f"💰 ~${deal['original']:,.0f}~ → *${deal['price']:,.0f} MXN*\n"
        f"🏷️ *{deal['discount']}% OFF*"
        f"{cupones}\n\n"
        f"🛒 [Ver oferta en Mercado Libre]({deal['url']})"
    )

# ── Scraping de mercadolibre.com.mx/ofertas ────────────────────────────────────
async def scrape_ofertas() -> list:
    deals = []
    urls = [
        "https://www.mercadolibre.com.mx/ofertas",
        "https://www.mercadolibre.com.mx/ofertas#nav-header",
    ]
    async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
        for url in urls:
            try:
                r = await client.get(url)
                soup = BeautifulSoup(r.text, "html.parser")
                items = soup.select("li.promotion-item, div.andes-card, article[class*='item']")
                logger.info(f"Scraping {url}: {len(items)} items encontrados")
                for item in items:
                    try:
                        # Título
                        title_el = item.select_one("p.promotion-item__title, h2, [class*='title']")
                        title = title_el.get_text(strip=True) if title_el else None
                        if not title:
                            continue

                        # Precios
                        price_el    = item.select_one("[class*='price__fraction'], [class*='current']")
                        original_el = item.select_one("[class*='original'], s, del, [class*='crossed']")
                        discount_el = item.select_one("[class*='discount'], [class*='off'], [class*='pill']")

                        price_text    = price_el.get_text(strip=True).replace(",","").replace("$","") if price_el else ""
                        original_text = original_el.get_text(strip=True).replace(",","").replace("$","") if original_el else ""
                        discount_text = discount_el.get_text(strip=True).replace("%","").replace("-","").replace("OFF","").strip() if discount_el else ""

                        price    = float(price_text)    if price_text.replace(".","").isdigit()    else 0
                        original = float(original_text) if original_text.replace(".","").isdigit() else 0
                        discount = int(discount_text)   if discount_text.isdigit()                 else 0

                        # Calcular descuento si no viene explícito
                        if discount == 0 and price > 0 and original > price:
                            discount = round((1 - price / original) * 100)

                        if discount < MIN_DISCOUNT:
                            continue

                        # Link
                        link_el = item.select_one("a[href]")
                        link    = link_el["href"] if link_el else ""
                        if not link or "mercadolibre" not in link:
                            continue

                        # Imagen
                        img_el = item.select_one("img")
                        img    = img_el.get("data-src") or img_el.get("src","") if img_el else ""
                        # Convertir thumbnail a imagen grande
                        img = img.replace("-I.jpg","").replace("-O.jpg","")
                        if img and not img.startswith("http"):
                            img = ""

                        # ID único del producto
                        prod_id = link.split("/p/")[-1].split("?")[0] if "/p/" in link else link.split("-")[-1].split("?")[0]

                        deals.append({
                            "id":       prod_id,
                            "title":    title[:70],
                            "price":    price,
                            "original": original if original > 0 else price,
                            "discount": discount,
                            "url":      make_affiliate_link(link),
                            "img":      img,
                        })
                    except Exception as e:
                        logger.debug(f"Item skip: {e}")
                        continue
            except Exception as e:
                logger.error(f"Error scraping {url}: {e}")

    # Deduplicar por ID
    unique = list({d["id"]: d for d in deals}.values())
    logger.info(f"Total ofertas scrapeadas: {len(unique)}")
    return unique

# También buscar via API de ML como respaldo
async def search_api(keyword: str) -> list:
    deals = []
    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
            r = await client.get(
                "https://api.mercadolibre.com/sites/MLM/search",
                params={"q": keyword, "limit": 30}
            )
            for item in r.json().get("results", []):
                price    = item.get("price", 0)
                original = item.get("original_price", 0)
                if not original or original <= price:
                    continue
                discount = round((1 - price / original) * 100)
                if discount < MIN_DISCOUNT:
                    continue
                # Imagen
                thumb = item.get("thumbnail","")
                img   = thumb.replace("-I.jpg","").replace("-O.jpg","").replace("http://","https://")
                deals.append({
                    "id":       item["id"],
                    "title":    item["title"][:70],
                    "price":    price,
                    "original": original,
                    "discount": discount,
                    "url":      make_affiliate_link(item["permalink"]),
                    "img":      img,
                })
    except Exception as e:
        logger.error(f"API error [{keyword}]: {e}")
    return deals

async def get_all_deals() -> list:
    deals = await scrape_ofertas()
    # Respaldo con API para palabras clave tech
    for kw in ["laptop reacondicionado", "tablet outlet", "smartphone liquidacion"]:
        deals += await search_api(kw)
    return list({d["id"]: d for d in deals}.values())

# ── Envío de mensajes ──────────────────────────────────────────────────────────
async def send_deals(bot: Bot, deals: list, chat_id: int, limit: int = 5) -> int:
    seen    = load_json(SEEN_DEALS_FILE, [])
    new     = [d for d in deals if d["id"] not in seen]
    sent    = 0
    for deal in new[:limit]:
        text = format_deal(deal)
        try:
            if deal.get("img"):
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=deal["img"],
                    caption=text,
                    parse_mode="Markdown"
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=False
                )
            seen.append(deal["id"])
            sent += 1
        except Exception as e:
            logger.error(f"Send error: {e}")
    save_json(SEEN_DEALS_FILE, seen[-1000:])
    return sent

async def broadcast(bot: Bot):
    chat_ids = load_json(CHAT_IDS_FILE, [])
    if not chat_ids:
        return
    deals = await get_all_deals()
    for cid in chat_ids:
        await send_deals(bot, deals, cid)

# ── Comandos ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    ids = load_json(CHAT_IDS_FILE, [])
    if cid not in ids:
        ids.append(cid)
        save_json(CHAT_IDS_FILE, ids)
    await update.message.reply_text(
        "🔥 *@JackRocko_bot activado*\n\n"
        "Te mando ofertas de Mercado Libre con ≥40% descuento cada 3 horas.\n\n"
        "/ofertas — buscar ahora\n"
        "/buscar [producto] — buscar algo específico\n"
        "/estado — ver estadísticas\n"
        "/salir — darse de baja",
        parse_mode="Markdown"
    )

async def cmd_ofertas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Buscando ofertas, un momento...")
    deals = await get_all_deals()
    n = await send_deals(ctx.bot, deals, update.effective_chat.id)
    if n == 0:
        await update.message.reply_text("😔 Sin ofertas nuevas por ahora. Vuelve más tarde.")

async def cmd_buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args)
    if not q:
        await update.message.reply_text("Uso: /buscar laptop — busca un producto específico")
        return
    await update.message.reply_text(f"🔍 Buscando: *{q}*...", parse_mode="Markdown")
    deals = await search_api(q)
    n = await send_deals(ctx.bot, deals, update.effective_chat.id, limit=5)
    if n == 0:
        await update.message.reply_text("😔 Sin resultados con ese criterio.")

async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ids  = load_json(CHAT_IDS_FILE, [])
    seen = load_json(SEEN_DEALS_FILE, [])
    cupones_activos = sum(1 for v in CUPONES.values() if v.strip())
    await update.message.reply_text(
        f"📊 *Estado de JackRocko Bot*\n\n"
        f"👥 Suscriptores: *{len(ids)}*\n"
        f"📦 Ofertas enviadas: *{len(seen)}*\n"
        f"🔗 Links afiliado: ✅\n"
        f"💳 Cupones activos: *{cupones_activos}*",
        parse_mode="Markdown"
    )

async def cmd_salir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    ids = load_json(CHAT_IDS_FILE, [])
    if cid in ids:
        ids.remove(cid)
        save_json(CHAT_IDS_FILE, ids)
    await update.message.reply_text("👋 Te diste de baja. Escribe /start para volver.")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("ofertas", cmd_ofertas))
    app.add_handler(CommandHandler("buscar",  cmd_buscar))
    app.add_handler(CommandHandler("estado",  cmd_estado))
    app.add_handler(CommandHandler("salir",   cmd_salir))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(broadcast, "interval", hours=3, args=[app.bot])
    scheduler.start()

    logger.info("🤖 JackRocko Bot iniciado con scraping de ofertas")
    app.run_polling()

if __name__ == "__main__":
    main()
