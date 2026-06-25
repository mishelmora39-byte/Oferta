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
ML_CLIENT_ID     = os.getenv("ML_CLIENT_ID", "127137755266066")
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET", "RDWxlk7I8jlWyQ39yu2Sd8L0AJPZXEty")

CUPONES = {
    "BBVA":              "",
    "BANAMEX":           "",
    "AMEX":              "",
    "AFIRME":            "",
    "MIFEL":             "",
    "MERCADO PAGO":      "",
    "MESES SIN TARJETA": "",
}

CHANNEL_ID      = int(os.getenv("CHANNEL_ID", "-1004405739696"))
ADMIN_ID        = 333569583
CHAT_IDS_FILE   = "chat_ids.json"
SEEN_DEALS_FILE = "seen_deals.json"
MIN_DISCOUNT    = 10

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "es-MX,es;q=0.9",
}

# Token de ML en memoria (se renueva automáticamente)
_ml_token = {"value": "", "expires_at": 0}

# ── Token ML Auto-Renovable ────────────────────────────────────────────────────
async def get_ml_token() -> str:
    """Obtiene o renueva el Access Token de Mercado Libre automáticamente."""
    import time
    if _ml_token["value"] and time.time() < _ml_token["expires_at"] - 300:
        return _ml_token["value"]

    if not ML_CLIENT_ID or not ML_CLIENT_SECRET:
        logger.warning("ML_CLIENT_ID o ML_CLIENT_SECRET no configurados.")
        return ""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.mercadolibre.com/oauth/token",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     ML_CLIENT_ID,
                    "client_secret": ML_CLIENT_SECRET,
                }
            )
            if r.status_code == 200:
                data = r.json()
                import time
                _ml_token["value"]      = data["access_token"]
                _ml_token["expires_at"] = time.time() + data.get("expires_in", 21600)
                logger.info("✅ Token ML renovado correctamente.")
                return _ml_token["value"]
            else:
                logger.error(f"Error obteniendo token ML: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.error(f"Excepción obteniendo token ML: {e}")
    return ""

async def ml_headers() -> dict:
    """Devuelve headers con Authorization si hay token disponible."""
    token = await get_ml_token()
    h = dict(HEADERS)
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.debug(f"load_json {path}: {e}")
        return default

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"save_json {path}: {e}")

def make_affiliate_link(url: str) -> str:
    clean = url.split("#")[0]
    sep = "&" if "?" in clean else "?"
    return f"{clean}{sep}matt_tool={AFFILIATE_ID}&matt_source=telegram&matt_campaign=jackrocko"

def cupones_text() -> str:
    activos = {b: c for b, c in CUPONES.items() if c.strip()}
    if not activos:
        return ""
    lineas = "\n".join(f'🟡 {b}: *"{c}"*' for b, c in activos.items())
    return f"\n\n💳 *Cupones bancarios:*\n{lineas}"

def format_deal(deal: dict) -> str:
    cupones   = cupones_text()
    original  = deal.get("original", 0)
    price     = deal.get("price", 0)
    discount  = deal.get("discount", 0)

    # Precio final = precio de oferta (ya con descuento aplicado)
    final_price = price

    # Línea de precio: tachado → precio final
    if original > price:
        price_line = f"~~${original:,.0f}~~ → 💵 *${final_price:,.0f} MXN*"
    else:
        price_line = f"💵 *${final_price:,.0f} MXN*"

    disc_str = f"\n🏷️ *{discount}% OFF*" if discount >= MIN_DISCOUNT else ""

    return (
        f"🔥 *{deal['title']}*\n\n"
        f"💰 Precio final: {price_line}"
        f"{disc_str}"
        f"{cupones}\n\n"
        f"🛒 [Ver oferta en Mercado Libre]({deal['url']})"
    )

# ── Extracción de precio segura (anti-cuotas) ──────────────────────────────────
def extract_price_safe(text: str) -> float:
    """
    Extrae el precio real de un texto de tarjeta de ML.
    Ignora cuotas (meses sin intereses) y precios absurdamente bajos.
    """
    # Buscar todos los números con formato de precio ($1,234 o $1234)
    raw_prices = re.findall(r'\$\s?([\d]{1,3}(?:,[\d]{3})*(?:\.[\d]{1,2})?)', text)
    if not raw_prices:
        # Intentar sin símbolo de peso
        raw_prices = re.findall(r'\b([\d]{3,7})\b', text)

    prices = []
    for p in raw_prices:
        try:
            val = float(p.replace(",", ""))
            # Ignorar valores que parecen cuotas (< 200 pesos) o irreales (> 500,000)
            if 200 <= val <= 500000:
                prices.append(val)
        except:
            pass

    if not prices:
        return 0.0

    # Si hay texto de "meses" o "cuotas", el precio real es el MAYOR encontrado
    if any(w in text.lower() for w in ["meses", "cuotas", "sin interés", "mensual"]):
        return max(prices)

    # En general, el precio de oferta es el primero grande que encontramos
    return prices[0]

def extract_discount_safe(text: str, price: float, original: float) -> int:
    """Extrae el descuento real. Si parece absurdo (>90%), lo recalcula o descarta."""
    # Buscar % en el texto
    match = re.search(r'(\d{1,2})\s*%\s*(?:OFF|off|descuento)', text)
    if match:
        disc = int(match.group(1))
        if disc <= 90:
            return disc

    # Calcular desde precios
    if original > 0 and price > 0 and original > price:
        disc = round((1 - price / original) * 100)
        if disc <= 90:
            return disc

    return 0

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
                        title_el = item.select_one(
                            "p.promotion-item__title, h2, [class*='title'], a[title]"
                        )
                        title = title_el.get_text(strip=True) if title_el else None
                        if not title or len(title) < 5:
                            continue

                        full_text = item.get_text(" ", strip=True)

                        # Precio actual (seguro, anti-cuotas)
                        price_el = item.select_one(
                            ".andes-money-amount__fraction, [class*='price__fraction'], [class*='current-price']"
                        )
                        if price_el:
                            price_text = price_el.get_text(strip=True).replace(",", "").replace("$", "")
                            try:
                                price = float(price_text)
                            except:
                                price = extract_price_safe(full_text)
                        else:
                            price = extract_price_safe(full_text)

                        # Precio original (tachado)
                        original_el = item.select_one(
                            "s .andes-money-amount__fraction, del .andes-money-amount__fraction, "
                            "[class*='original'] .andes-money-amount__fraction, [class*='crossed']"
                        )
                        if original_el:
                            orig_text = original_el.get_text(strip=True).replace(",", "").replace("$", "")
                            try:
                                original = float(orig_text)
                            except:
                                original = 0
                        else:
                            original = 0

                        # Descuento seguro
                        discount = extract_discount_safe(full_text, price, original)

                        # Validaciones de seguridad
                        if price <= 0:
                            continue
                        if discount < MIN_DISCOUNT:
                            continue
                        # Si el descuento parece absurdo (precio < 5% del original), descartar
                        if original > 0 and price < (original * 0.05):
                            logger.debug(f"Descuento absurdo descartado: {title[:40]} ${price} vs ${original}")
                            continue

                        # Link
                        link_el = item.select_one("a[href]")
                        link = link_el["href"] if link_el else ""
                        if not link or "mercadolibre" not in link:
                            continue

                        # Imagen
                        img_el = item.select_one("img")
                        img = ""
                        if img_el:
                            img = img_el.get("data-src") or img_el.get("src", "")
                            img = img.replace("-I.jpg", "").replace("-O.jpg", "")
                            if not img.startswith("http"):
                                img = ""

                        # ID único
                        prod_id = re.search(r'MLM-?(\d+)', link)
                        prod_id = f"MLM{prod_id.group(1)}" if prod_id else link.split("?")[0][-20:]

                        deals.append({
                            "id":       prod_id,
                            "title":    title[:70],
                            "price":    price,
                            "original": original if original > price else price,
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

# ── Búsqueda via API de ML ─────────────────────────────────────────────────────
async def search_api(keyword: str, min_discount: int = MIN_DISCOUNT) -> list:
    deals = []
    try:
        headers = await ml_headers()
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            r = await client.get(
                "https://api.mercadolibre.com/sites/MLM/search",
                params={"q": keyword, "limit": 30}
            )
            if r.status_code == 403:
                logger.warning(f"API ML 403 para '{keyword}' — sin token válido.")
                return []
            for item in r.json().get("results", []):
                price    = item.get("price", 0)
                original = item.get("original_price", 0)
                if not original or original <= price:
                    continue
                discount = round((1 - price / original) * 100)
                if discount < min_discount:
                    continue
                thumb = item.get("thumbnail", "").replace("-I.jpg", "").replace("http://", "https://")
                attrs = {a["id"]: a.get("value_name", "") for a in item.get("attributes", [])}
                deals.append({
                    "id":           item["id"],
                    "title":        item["title"][:70],
                    "price":        price,
                    "original":     original,
                    "discount":     discount,
                    "url":          make_affiliate_link(item["permalink"]),
                    "img":          thumb,
                    "condition":    item.get("condition", ""),
                    "sold_quantity": item.get("sold_quantity", 0),
                    "brand":        attrs.get("BRAND", ""),
                    "model":        attrs.get("MODEL", ""),
                })
    except Exception as e:
        logger.error(f"API error [{keyword}]: {e}")
    return deals

async def get_all_deals() -> list:
    deals = []
    keywords = [
        "laptop reacondicionado", "tablet oferta", "smartphone descuento",
        "audifonos bluetooth", "smartwatch barato", "consola videojuegos",
        "ssd disco duro", "monitor pc", "bocina portatil", "camara seguridad"
    ]
    for kw in keywords:
        deals += await search_api(kw)
    # Scraping como complemento
    deals += await scrape_ofertas()
    return list({d["id"]: d for d in deals if d["price"] > 0}.values())

# ── Obtener info de producto por ID MLM ───────────────────────────────────────
async def get_item_by_id(item_id: str) -> dict | None:
    """Consulta la API de ML para obtener info de un producto por su ID."""
    try:
        headers = await ml_headers()
        async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
            r = await client.get(f"https://api.mercadolibre.com/items/{item_id}")
            if r.status_code != 200:
                logger.error(f"API items/{item_id}: {r.status_code} {r.text[:150]}")
                return None
            item = r.json()
            price    = item.get("price", 0)
            original = item.get("original_price") or price
            discount = round((1 - price / original) * 100) if original > price else 0
            thumb    = item.get("thumbnail", "").replace("-I.jpg", "").replace("http://", "https://")
            attrs    = {a["id"]: a.get("value_name", "") for a in item.get("attributes", [])}
            return {
                "id":           item_id,
                "title":        item.get("title", "")[:70],
                "price":        price,
                "original":     original,
                "discount":     discount,
                "url":          make_affiliate_link(item.get("permalink", "")),
                "img":          thumb,
                "condition":    item.get("condition", ""),
                "sold_quantity": item.get("sold_quantity", 0),
                "brand":        attrs.get("BRAND", ""),
                "model":        attrs.get("MODEL", ""),
            }
    except Exception as e:
        logger.error(f"get_item_by_id {item_id}: {e}")
        return None

# ── Scraping de producto individual (fallback sin API) ─────────────────────────
async def scrape_product_page(url: str) -> dict | None:
    """
    Intenta extraer título, precio e imagen directamente del HTML del producto.
    Fallback cuando la API no está disponible.
    """
    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url)
            final_url = str(r.url)

            # Si ML redirige a verificación, no podemos hacer nada sin sesión
            if "account-verification" in final_url or "login" in final_url:
                logger.warning("ML redirigió a verificación de cuenta — scraping bloqueado.")
                return None

            soup = BeautifulSoup(r.text, "html.parser")

            # Intentar JSON-LD primero (más confiable)
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string or "")
                    if data.get("@type") == "Product":
                        offers = data.get("offers", {})
                        price  = float(offers.get("price", 0))
                        name   = data.get("name", "")
                        image  = data.get("image", "")
                        if isinstance(image, list):
                            image = image[0] if image else ""
                        if price > 0 and name:
                            item_id = re.search(r'MLM-?(\d+)', final_url)
                            return {
                                "id":       f"MLM{item_id.group(1)}" if item_id else final_url[-15:],
                                "title":    name[:70],
                                "price":    price,
                                "original": price,
                                "discount": 0,
                                "url":      make_affiliate_link(final_url),
                                "img":      image,
                            }
                except:
                    pass

            # Selectores directos del HTML
            title_el    = soup.select_one("h1.ui-pdp-title, h1[class*='title']")
            price_el    = soup.select_one(".andes-money-amount__fraction")
            original_el = soup.select_one(
                ".ui-pdp-price__original-value .andes-money-amount__fraction"
            )
            img_el = soup.select_one(
                ".ui-pdp-gallery__figure img, figure img, [class*='gallery'] img"
            )

            title = title_el.get_text(strip=True) if title_el else None
            if not title:
                return None

            full_text = soup.get_text(" ", strip=True)
            price     = extract_price_safe(
                price_el.get_text(strip=True) if price_el else full_text
            )
            original  = 0
            if original_el:
                try:
                    original = float(original_el.get_text(strip=True).replace(",", "").replace("$", ""))
                except:
                    pass

            discount = extract_discount_safe(full_text, price, original)
            img = ""
            if img_el:
                img = img_el.get("data-zoom") or img_el.get("src", "")
                if not img.startswith("http"):
                    img = ""

            item_id = re.search(r'MLM-?(\d+)', final_url)
            return {
                "id":       f"MLM{item_id.group(1)}" if item_id else final_url[-15:],
                "title":    title[:70],
                "price":    price,
                "original": original if original > price else price,
                "discount": discount,
                "url":      make_affiliate_link(final_url),
                "img":      img,
            }
    except Exception as e:
        logger.error(f"scrape_product_page: {e}")
        return None

# ── Envío de mensajes ──────────────────────────────────────────────────────────
async def send_deals(bot: Bot, deals: list, chat_id: int, limit: int = 5) -> int:
    seen = load_json(SEEN_DEALS_FILE, [])
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
            logger.error(f"Send error: {e}")
    save_json(SEEN_DEALS_FILE, seen[-1000:])
    return sent

async def broadcast(bot: Bot):
    deals = await get_all_deals()
    await send_deals(bot, deals, CHANNEL_ID, limit=10)
    chat_ids = load_json(CHAT_IDS_FILE, [])
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
        "Te mando ofertas de Mercado Libre con ≥10% descuento cada 3 horas.\n\n"
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
    deals = await search_api(q, min_discount=10)
    n = await send_deals(ctx.bot, deals, update.effective_chat.id, limit=5)
    if n == 0:
        await update.message.reply_text("😔 Sin resultados con ese criterio.")

async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ids  = load_json(CHAT_IDS_FILE, [])
    seen = load_json(SEEN_DEALS_FILE, [])
    cupones_activos = sum(1 for v in CUPONES.values() if v.strip())
    token_ok = "✅ Configurado" if ML_CLIENT_ID and ML_CLIENT_SECRET else "❌ Sin configurar"
    await update.message.reply_text(
        f"📊 *Estado de JackRocko Bot*\n\n"
        f"👥 Suscriptores: *{len(ids)}*\n"
        f"📦 Ofertas enviadas: *{len(seen)}*\n"
        f"🔑 Token ML: {token_ok}\n"
        f"🔗 Links afiliado: ✅\n"
        f"💳 Cupones activos: *{cupones_activos}*",
        parse_mode="Markdown"
    )

async def cmd_limpiar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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

# ── Handler de links manuales (Admin) ─────────────────────────────────────────
async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin manda un link de ML → bot lo procesa y publica en el canal."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Solo el admin puede usar esta función.")
        return

    text = update.message.text.strip()
    if not any(d in text for d in ["mercadolibre", "meli.la"]):
        await update.message.reply_text("⚠️ Manda un link de Mercado Libre.")
        return

    await update.message.reply_text("🔍 Procesando link...")

    deal = None

    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            # Expandir shortlink si es meli.la
            r = await client.get(text)
            final_url = str(r.url)
            logger.info(f"URL final: {final_url}")

            # Buscar ID MLM en la URL o en los parámetros
            # Buscar en item_id=MLM... (parámetro de URL)
            match = re.search(r'item_id[=%3A:]+MLM-?(\d+)', final_url, re.IGNORECASE)
            if not match:
                match = re.search(r'wid=MLM-?(\d+)', final_url, re.IGNORECASE)
            if not match:
                match = re.search(r'MLM-?(\d+)', final_url, re.IGNORECASE)
            if not match:
                # Buscar en el HTML de la página
                match = re.search(r'MLM-?(\d+)', r.text, re.IGNORECASE)

            if match:
                item_id = f"MLM{match.group(1)}"
                logger.info(f"ID encontrado: {item_id}")
                # Intentar via API primero
                deal = await get_item_by_id(item_id)

            # Si la API falló, intentar scraping del HTML
            if not deal:
                logger.info("API falló, intentando scraping del HTML...")
                deal = await scrape_product_page(final_url)

    except Exception as e:
        logger.error(f"Error procesando link: {e}")

    if not deal or deal.get("price", 0) == 0:
        await update.message.reply_text(
            "❌ No pude obtener info del producto.\n\n"
            "Posibles causas:\n"
            "• Mercado Libre bloqueó el acceso sin sesión\n"
            "• El link es de catálogo y no de un producto directo\n\n"
            "Intenta con un link directo al producto (que tenga MLM en la URL)."
        )
        return

    # Publicar en el canal
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
        await update.message.reply_text(
            f"✅ Publicado en el canal.\n"
            f"📦 *{deal['title'][:40]}*\n"
            f"💰 ${deal['price']:,.0f} MXN",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error publicando: {e}")

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
