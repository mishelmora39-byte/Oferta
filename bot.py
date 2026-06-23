import json
import re
import os
import logging
import httpx
from bs4 import BeautifulSoup
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "TU_TOKEN_AQUI")
AFFILIATE_ID = os.getenv("AFFILIATE_ID", "293AH0-18PY")

# Cupones bancarios opcionales — déjalos vacíos ("") si no hay vigentes
CUPONES = {
    "BBVA":              "",
    "BANAMEX":           "",
    "AMEX":              "",
    "AFIRME":            "",
    "MIFEL":             "",
    "MERCADO PAGO":      "",
    "MESES SIN TARJETA": "",
}

CHANNEL_ID      = int(os.getenv("CHANNEL_ID", "-1004405739696"))  # @ofertasmx3
ADMIN_ID        = 333569583  # Solo Edwing puede mandar links

CHAT_IDS_FILE   = "chat_ids.json"
SEEN_DEALS_FILE = "seen_deals.json"
MIN_DISCOUNT    = 10

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "es-MX,es;q=0.9",
}

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.debug(f"load_json({path}): {e}")
        return default

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"save_json({path}): {e}")

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

def parse_price(text: str) -> float:
    """Extrae el primer número de precio de un texto, ignorando texto extra."""
    # Eliminar espacios entre dígitos y coma (ej: "553 , 87" → "553.87")
    text = re.sub(r'(\d)\s*,\s*(\d{2})(?!\d)', r'\1.\2', text)
    # Quitar comas de miles (ej: "1,119" → "1119")
    text = text.replace(',', '')
    # Buscar primer número flotante
    m = re.search(r'[\d]+(?:\.\d+)?', text)
    return float(m.group()) if m else 0.0

# ── Scraping de mercadolibre.com.mx/ofertas ────────────────────────────────────
async def scrape_ofertas() -> list:
    deals = []
    url = "https://www.mercadolibre.com.mx/ofertas"
    async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
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

                    # Extraer precios y descuento con regex sobre el texto completo del card
                    card_text = item.get_text(" ", strip=True)

                    # Descuento: buscar "XX% OFF"
                    discount_match = re.search(r'(\d+)%\s*OFF', card_text)
                    discount = int(discount_match.group(1)) if discount_match else 0

                    if discount < MIN_DISCOUNT:
                        continue

                    # Precios: buscar todos los patrones "$NNN" en el texto
                    raw_prices = re.findall(r'\$\s*([\d,]+(?:\s*,\s*\d{2})?)', card_text)
                    prices_clean = []
                    for p in raw_prices:
                        val = parse_price(p)
                        if val > 0:
                            prices_clean.append(val)

                    if len(prices_clean) < 2:
                        # Intentar con un solo precio y calcular original desde descuento
                        if len(prices_clean) == 1 and discount > 0:
                            price = prices_clean[0]
                            original = round(price / (1 - discount / 100))
                        else:
                            continue
                    else:
                        # El precio más alto es el original, el más bajo es el actual
                        original = max(prices_clean[:3])
                        price    = min(prices_clean[:3])

                    if price <= 0 or original <= 0:
                        continue

                    # Recalcular descuento si no vino explícito
                    if discount == 0 and original > price:
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
                    img = ""
                    if img_el:
                        img = img_el.get("src") or img_el.get("data-src") or ""
                    if img and not img.startswith("http"):
                        img = ""

                    # ID único del producto
                    prod_id = link.split("/p/")[-1].split("?")[0] if "/p/" in link else link.split("-")[-1].split("?")[0]

                    deals.append({
                        "id":       prod_id,
                        "title":    title[:70],
                        "price":    price,
                        "original": original,
                        "discount": discount,
                        "url":      make_affiliate_link(link),
                        "img":      img,
                    })
                except Exception as e:
                    logger.debug(f"Item skip: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")

    unique = list({d["id"]: d for d in deals}.values())
    logger.info(f"Total ofertas scrapeadas: {len(unique)}")
    return unique

# ── Búsqueda via API pública de ML (con Access Token si tienes) ────────────────
async def search_api(keyword: str, min_discount: int = MIN_DISCOUNT) -> list:
    """
    Busca productos en la API de Mercado Libre.
    Si tienes un Access Token de ML, ponlo en la variable de entorno ML_ACCESS_TOKEN
    para evitar el error 403.
    """
    deals = []
    ml_token = os.getenv("ML_ACCESS_TOKEN", "")
    req_headers = dict(HEADERS)
    if ml_token:
        req_headers["Authorization"] = f"Bearer {ml_token}"

    try:
        async with httpx.AsyncClient(timeout=15, headers=req_headers) as client:
            r = await client.get(
                "https://api.mercadolibre.com/sites/MLM/search",
                params={"q": keyword, "limit": 30}
            )
            if r.status_code == 403:
                logger.warning(f"API ML devolvió 403 para '{keyword}'. Necesitas un ML_ACCESS_TOKEN.")
                return []
            if r.status_code != 200:
                logger.warning(f"API ML status {r.status_code} para '{keyword}'")
                return []

            for item in r.json().get("results", []):
                price    = item.get("price", 0)
                original = item.get("original_price", 0)
                if not original or original <= price:
                    continue
                discount = round((1 - price / original) * 100)
                if discount < min_discount:
                    continue
                thumb = item.get("thumbnail", "")
                img   = thumb.replace("-I.jpg", "").replace("-O.jpg", "").replace("http://", "https://")
                attrs = {a["id"]: a.get("value_name", "") for a in item.get("attributes", [])}
                deals.append({
                    "id":            item["id"],
                    "title":         item["title"][:70],
                    "price":         price,
                    "original":      original,
                    "discount":      discount,
                    "url":           make_affiliate_link(item["permalink"]),
                    "img":           img,
                    "condition":     item.get("condition", ""),
                    "sold_quantity": item.get("sold_quantity", 0),
                    "brand":         attrs.get("BRAND", ""),
                    "model":         attrs.get("MODEL", ""),
                    "ram":           attrs.get("RAM", ""),
                    "storage":       attrs.get("STORAGE_CAPACITY", ""),
                })
    except Exception as e:
        logger.error(f"API error [{keyword}]: {e}")
    return deals

async def get_all_deals() -> list:
    deals = []
    # Intentar API primero (requiere ML_ACCESS_TOKEN para funcionar)
    keywords = [
        "laptop reacondicionado", "tablet oferta", "smartphone descuento",
        "audifonos bluetooth", "smartwatch barato", "consola videojuegos",
        "ssd disco duro", "monitor pc", "bocina portatil", "camara seguridad"
    ]
    for kw in keywords:
        deals += await search_api(kw)

    # Scraping como fuente principal (siempre funciona)
    deals += await scrape_ofertas()

    # Filtrar solo los que tienen precio válido y deduplicar
    valid = list({d["id"]: d for d in deals if d["price"] > 0}.values())
    logger.info(f"Total deals válidos: {len(valid)}")
    return valid

# ── Envío de mensajes ──────────────────────────────────────────────────────────
async def send_deals(bot: Bot, deals: list, chat_id: int, limit: int = 5, seen_override: list = None) -> int:
    """
    Envía deals a un chat_id.
    seen_override: si se pasa, usa esa lista para filtrar (no modifica seen_deals.json).
    """
    if seen_override is not None:
        seen = seen_override
        update_global_seen = False
    else:
        seen = load_json(SEEN_DEALS_FILE, [])
        update_global_seen = True

    new  = [d for d in deals if d["id"] not in seen]
    sent = 0

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
            logger.error(f"Send error (chat {chat_id}): {e}")

    if update_global_seen:
        save_json(SEEN_DEALS_FILE, seen[-1000:])

    return sent

async def broadcast(bot: Bot):
    deals = await get_all_deals()
    if not deals:
        logger.info("broadcast: sin deals válidos esta vez.")
        return

    # Publicar en el canal público
    await send_deals(bot, deals, CHANNEL_ID, limit=10)

    # Notificar a suscriptores individuales usando el seen global actualizado
    seen_global = load_json(SEEN_DEALS_FILE, [])
    chat_ids = load_json(CHAT_IDS_FILE, [])
    for cid in chat_ids:
        await send_deals(bot, deals, cid, limit=5, seen_override=list(seen_global))

# ── Comandos ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    ids = load_json(CHAT_IDS_FILE, [])
    if cid not in ids:
        ids.append(cid)
        save_json(CHAT_IDS_FILE, ids)
    await update.message.reply_text(
        "🔥 *@JackRocko\\_bot activado*\n\n"
        f"Te mando ofertas de Mercado Libre con ≥{MIN_DISCOUNT}% descuento cada 3 horas.\n\n"
        "/ofertas — buscar ahora\n"
        "/buscar \\[producto\\] — buscar algo específico\n"
        "/estado — ver estadísticas\n"
        "/salir — darse de baja",
        parse_mode="MarkdownV2"
    )

async def cmd_ofertas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Buscando ofertas, un momento...")
    deals = await get_all_deals()
    if not deals:
        await update.message.reply_text("😔 Sin ofertas disponibles por ahora. Intenta más tarde.")
        return
    n = await send_deals(ctx.bot, deals, update.effective_chat.id)
    if n == 0:
        await update.message.reply_text("😔 Sin ofertas nuevas por ahora. Usa /limpiar para reiniciar el historial.")

async def cmd_buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args) if ctx.args else ""
    if not q:
        await update.message.reply_text("Uso: /buscar laptop — busca un producto específico")
        return
    await update.message.reply_text(f"🔍 Buscando: *{q}*...", parse_mode="Markdown")

    # Intentar API primero con umbral más bajo
    deals = await search_api(q, min_discount=20)

    # Si la API no devuelve nada (403 o sin resultados), buscar en el scraping general
    if not deals:
        logger.info(f"cmd_buscar: API sin resultados para '{q}', usando scraping general")
        all_deals = await scrape_ofertas()
        q_lower = q.lower()
        deals = [d for d in all_deals if q_lower in d["title"].lower()]

    if not deals:
        await update.message.reply_text(f"😔 Sin resultados para *{q}*. Intenta con otra palabra.", parse_mode="Markdown")
        return

    n = await send_deals(ctx.bot, deals, update.effective_chat.id, limit=5)
    if n == 0:
        await update.message.reply_text("😔 Ya te mandé esas ofertas antes. Usa /limpiar para ver de nuevo.")

async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ids  = load_json(CHAT_IDS_FILE, [])
    seen = load_json(SEEN_DEALS_FILE, [])
    cupones_activos = sum(1 for v in CUPONES.values() if v.strip())
    ml_token = "✅ Configurado" if os.getenv("ML_ACCESS_TOKEN") else "⚠️ No configurado (API limitada)"
    await update.message.reply_text(
        f"📊 *Estado de JackRocko Bot*\n\n"
        f"👥 Suscriptores: *{len(ids)}*\n"
        f"📦 Ofertas enviadas: *{len(seen)}*\n"
        f"🔗 Links afiliado: ✅\n"
        f"💳 Cupones activos: *{cupones_activos}*\n"
        f"🔑 ML Access Token: {ml_token}",
        parse_mode="Markdown"
    )

async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin manda un link de ML/Amazon → bot lo procesa y publica en el canal"""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Solo el admin puede usar esta función.")
        return

    text = update.message.text.strip()
    if not any(d in text for d in ["mercadolibre", "meli.la", "amazon.com"]):
        await update.message.reply_text("⚠️ Manda un link de Mercado Libre o Amazon.")
        return

    await update.message.reply_text("🔍 Procesando link...")

    deal = None
    try:
        if "mercadolibre" in text or "meli.la" in text:
            ml_token = os.getenv("ML_ACCESS_TOKEN", "")
            req_headers = dict(HEADERS)
            if ml_token:
                req_headers["Authorization"] = f"Bearer {ml_token}"

            async with httpx.AsyncClient(timeout=20, headers=req_headers, follow_redirects=True) as client:
                r = await client.get(text)
                final_url = str(r.url)
                logger.info(f"URL final: {final_url}")

                match = re.search(r'MLM-?(\d+)', final_url, re.IGNORECASE)
                if not match:
                    match = re.search(r'MLM-?(\d+)', r.text, re.IGNORECASE)

                if match:
                    item_id = f"MLM{match.group(1)}"
                    api_r = await client.get(f"https://api.mercadolibre.com/items/{item_id}")
                    if api_r.status_code == 200:
                        item = api_r.json()
                        price    = item.get("price", 0)
                        original = item.get("original_price") or price
                        discount = round((1 - price / original) * 100) if original > price else 0
                        thumb    = item.get("thumbnail", "").replace("-I.jpg", "").replace("http://", "https://")
                        attrs    = {a["id"]: a.get("value_name", "") for a in item.get("attributes", [])}
                        deal = {
                            "id":            item_id,
                            "title":         item.get("title", "")[:70],
                            "price":         price,
                            "original":      original,
                            "discount":      discount,
                            "url":           make_affiliate_link(item.get("permalink", text)),
                            "img":           thumb,
                            "condition":     item.get("condition", ""),
                            "sold_quantity": item.get("sold_quantity", 0),
                            "brand":         attrs.get("BRAND", ""),
                            "model":         attrs.get("MODEL", ""),
                            "ram":           attrs.get("RAM", ""),
                            "storage":       attrs.get("STORAGE_CAPACITY", ""),
                        }
                    else:
                        logger.warning(f"API items/{item_id} status {api_r.status_code}")
                else:
                    logger.error(f"No se encontró ID MLM en: {final_url}")
    except Exception as e:
        logger.error(f"Error procesando link: {e}")

    if not deal or deal["price"] == 0:
        await update.message.reply_text("❌ No pude obtener info del producto. Verifica el link.")
        return

    text_msg = format_deal(deal)
    try:
        if deal.get("img"):
            await ctx.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=deal["img"],
                caption=text_msg,
                parse_mode="Markdown"
            )
        else:
            await ctx.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text_msg,
                parse_mode="Markdown"
            )
        await update.message.reply_text("✅ Publicado en el canal.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error publicando: {e}")

async def cmd_limpiar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Solo admin puede limpiar el historial de ofertas vistas"""
    if update.effective_user.id != ADMIN_ID:
        return
    save_json(SEEN_DEALS_FILE, [])
    await update.message.reply_text("🗑️ Historial limpiado. La próxima búsqueda mostrará todas las ofertas.")

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
    app.add_handler(CommandHandler("limpiar", cmd_limpiar))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"https?://"), handle_link))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(broadcast, "interval", hours=3, args=[app.bot])
    scheduler.start()

    logger.info("🤖 JackRocko Bot iniciado")
    app.run_polling()

if __name__ == "__main__":
    main()
