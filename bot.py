import json
import re
import os
import time
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

# ── Credenciales OAuth de Mercado Libre ────────────────────────────────────────
ML_CLIENT_ID     = os.getenv("ML_CLIENT_ID", "")
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET", "")
ML_ACCESS_TOKEN  = os.getenv("ML_ACCESS_TOKEN", "")
ML_REFRESH_TOKEN = os.getenv("ML_REFRESH_TOKEN", "")

ML_TOKENS_FILE = "ml_tokens.json"
_ml_token_data = {"access_token": "", "refresh_token": "", "expires_at": 0}

CUPONES = {
    "BBVA": "", "BANAMEX": "", "AMEX": "", "AFIRME": "", 
    "MIFEL": "", "MERCADO PAGO": "", "MESES SIN TARJETA": ""
}

CHANNEL_ID     = int(os.getenv("CHANNEL_ID", "-1004405739696"))
ADMIN_ID       = 333569583
CHAT_IDS_FILE  = "chat_ids.json"
SEEN_DEALS_FILE = "seen_deals.json"

# BAJAMOS LA BARRERA TOTALMENTE: Si está en la página de ofertas, ES UNA OFERTA.
MIN_DISCOUNT   = 5 

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "es-MX,es;q=0.9",
}

# ── Gestión de Tokens ─────────────────────────────────────────────────────────
def _load_tokens():
    global _ml_token_data
    try:
        with open(ML_TOKENS_FILE) as f:
            _ml_token_data = json.load(f)
            logger.info("✅ Tokens cargados de archivo")
    except:
        if ML_ACCESS_TOKEN:
            _ml_token_data = {
                "access_token": ML_ACCESS_TOKEN, 
                "refresh_token": ML_REFRESH_TOKEN,
                "expires_at": time.time() + 3600
            }

async def get_ml_access_token():
    # Por ahora, si falla el refresh, devolvemos lo que tengamos o nada
    return _ml_token_data.get("access_token", "")

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except: return default

def save_json(path, data):
    with open(path, "w") as f: json.dump(data, f)

def make_affiliate_link(url: str) -> str:
    if not url: return ""
    clean_url = url.split("#")[0].split("?")[0]
    return f"{clean_url}?matt_tool={AFFILIATE_ID}&matt_source=telegram&matt_campaign=jackrocko"

def format_deal(deal: dict) -> str:
    price_str = f"${deal['price']:,.0f} MXN"
    orig_str = f"~${deal['original']:,.0f}~ " if deal['original'] > deal['price'] else ""
    discount_str = f"🏷️ *{deal['discount']}% OFF*" if deal['discount'] > 0 else "🔥 ¡Gran Precio!"

    return (
        f"🔥 *{deal['title']}*\n\n"
        f"💰 {orig_str}* {price_str}*\n"
        f"{discount_str}\n\n"
        f"🛒 [Ver en Mercado Libre]({deal['url']})"
    )

# ── SCRAPER REFORMADO (SIN FILTROS AGRESIVOS) ──────────────────────────────────
async def scrape_ofertas() -> list:
    deals = []
    url = "https://www.mercadolibre.com.mx/ofertas"

    async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
        try:
            r = await client.get(url)
            if r.status_code != 200:
                logger.error(f"Error HTTP {r.status_code} en ofertas")
                return []

            soup = BeautifulSoup(r.text, "html.parser")
            # Selector más robusto para los cuadros de productos
            items = soup.find_all("div", class_=re.compile(r"promotion-item|andes-card"))
            logger.info(f"Scraper: {len(items)} contenedores encontrados")

            for item in items:
                try:
                    # 1. Título
                    title_el = item.find(["p", "h2", "span"], class_=re.compile(r"title"))
                    if not title_el: continue
                    title = title_el.get_text(strip=True)

                    # 2. Link (Crucial)
                    link_el = item.find("a", href=True)
                    if not link_el: continue
                    link = link_el["href"]

                    # 3. Precio Actual (Detección simplificada)
                    price_text = ""
                    current_price_container = item.find("span", class_=re.compile(r"price-part"))
                    if current_price_container:
                        fraction = current_price_container.find("span", class_=re.compile(r"fraction"))
                        if fraction: price_text = fraction.get_text(strip=True)

                    if not price_text:
                        # Intento alternativo
                        prices = item.find_all("span", class_=re.compile(r"price"))
                        for p in prices:
                            if "original" not in str(p).lower():
                                txt = p.get_text(strip=True).replace("$","").replace(",","")
                                if txt.isdigit(): price_text = txt; break

                    price = float(price_text.replace(",","")) if price_text else 0
                    if price <= 0: continue

                    # 4. Precio Original y Descuento (Opcionales para el scraper)
                    original = 0
                    orig_el = item.find(["s", "del", "span"], class_=re.compile(r"original"))
                    if orig_el:
                        orig_txt = orig_el.get_text(strip=True).replace("$","").replace(",","")
                        original = float(orig_txt) if orig_txt.replace(".","").isdigit() else 0

                    discount = 0
                    disc_el = item.find("span", class_=re.compile(r"discount"))
                    if disc_el:
                        disc_txt = re.sub(r"\D", "", disc_el.get_text())
                        discount = int(disc_txt) if disc_txt else 0

                    if discount == 0 and original > price:
                        discount = round((1 - price/original) * 100)

                    # 5. Imagen
                    img_el = item.find("img")
                    img = img_el.get("data-src") or img_el.get("src", "") if img_el else ""

                    # Identificador único
                    prod_id = re.search(r"MLM-?(\d+)", link)
                    prod_id = prod_id.group(0) if prod_id else title[:20]

                    deals.append({
                        "id": prod_id,
                        "title": title[:75],
                        "price": price,
                        "original": original if original > 0 else price,
                        "discount": discount,
                        "url": make_affiliate_link(link),
                        "img": img
                    })
                except Exception as e:
                    logger.debug(f"Error procesando item: {e}")
                    continue
        except Exception as e:
            logger.error(f"Fallo general scraper: {e}")

    return list({d["id"]: d for d in deals}.values())

# ── API (Ajustada para seguir intentando sin Auth si es necesario) ────────────
async def search_api(keyword: str) -> list:
    deals = []
    token = await get_ml_access_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                "https://api.mercadolibre.com/sites/MLM/search",
                params={"q": keyword, "limit": 30},
                headers=headers
            )
            if r.status_code == 200:
                for item in r.json().get("results", []):
                    price = item.get("price", 0)
                    orig = item.get("original_price", 0)
                    if price <= 0: continue

                    disc = round((1 - price/orig)*100) if orig and orig > price else 0

                    # En la API sí mantenemos un filtro mínimo para no saturar
                    if disc < MIN_DISCOUNT and "oferta" not in keyword: continue

                    deals.append({
                        "id": item["id"],
                        "title": item["title"][:75],
                        "price": price,
                        "original": orig if orig > 0 else price,
                        "discount": disc,
                        "url": make_affiliate_link(item["permalink"]),
                        "img": item.get("thumbnail", "").replace("-I.jpg", "-O.jpg")
                    })
        except: pass
    return deals

async def get_all_deals() -> list:
    # 1. Scraping (Prioridad 1 porque no usa Tokens y ya está en /ofertas)
    all_deals = await scrape_ofertas()

    # 2. API (Complemento)
    keywords = ["ofertas", "remate", "liquidación"]
    for kw in keywords:
        all_deals += await search_api(kw)

    unique = list({d["id"]: d for d in all_deals}.values())
    logger.info(f"📢 Total final para enviar: {len(unique)}")
    return unique

# ── Envío y Bot ───────────────────────────────────────────────────────────────
async def send_deals(bot: Bot, deals: list, chat_id: int, limit: int = 5):
    seen = load_json(SEEN_DEALS_FILE, [])
    new_deals = [d for d in deals if d["id"] not in seen]
    count = 0
    for d in new_deals[:limit]:
        try:
            text = format_deal(d)
            if d["img"]:
                await bot.send_photo(chat_id, d["img"], caption=text, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id, text, parse_mode="Markdown")
            seen.append(d["id"])
            count += 1
            time.sleep(1) # Evitar spam / limit
        except: continue
    save_json(SEEN_DEALS_FILE, seen[-500:])
    return count

async def broadcast(bot: Bot):
    deals = await get_all_deals()
    if not deals: return
    # Al canal
    await send_deals(bot, deals, CHANNEL_ID, limit=8)
    # A usuarios directos
    for cid in load_json(CHAT_IDS_FILE, []):
        await send_deals(bot, deals, cid, limit=3)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    ids = load_json(CHAT_IDS_FILE, [])
    if cid not in ids: ids.append(cid); save_json(CHAT_IDS_FILE, ids)
    await update.message.reply_text("👋 ¡Bot JackRocko listo! Buscando ofertas automáticamente.")

async def cmd_ofertas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Buscando en vivo...")
    deals = await get_all_deals()
    sent = await send_deals(ctx.bot, deals, update.effective_chat.id, limit=5)
    if sent == 0: await update.message.reply_text("No encontré nada nuevo. Intenta en 10 min.")

def main():
    _load_tokens()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ofertas", cmd_ofertas))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(broadcast, "interval", minutes=15, args=[app.bot])
    scheduler.start()

    logger.info("🚀 Bot iniciado")
    app.run_polling()

if __name__ == "__main__":
    main()
