import json
import re
import os
import time
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

# ── Credenciales OAuth de Mercado Libre ────────────────────────────────────────
ML_CLIENT_ID     = os.getenv("ML_CLIENT_ID", "")
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET", "")
ML_ACCESS_TOKEN  = os.getenv("ML_ACCESS_TOKEN", "")
ML_REFRESH_TOKEN = os.getenv("ML_REFRESH_TOKEN", "")

# Archivo para persistir tokens renovados
ML_TOKENS_FILE = "ml_tokens.json"

# Token en memoria (se carga al iniciar, se renueva automáticamente)
_ml_token_data = {
    "access_token": "",
    "refresh_token": "",
    "expires_at": 0,  # timestamp unix cuando expira
}

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

CHANNEL_ID     = int(os.getenv("CHANNEL_ID", "-1004405739696"))  # @ofertasmx3
ADMIN_ID       = 333569583  # Solo Edwing puede mandar links

CHAT_IDS_FILE  = "chat_ids.json"
SEEN_DEALS_FILE = "seen_deals.json"
MIN_DISCOUNT   = 10

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "es-MX,es;q=0.9",
}

# ── Gestión de Tokens OAuth de Mercado Libre ───────────────────────────────────

def _load_tokens():
    """Carga tokens desde archivo persistente o variables de entorno."""
    global _ml_token_data
    # Intentar cargar tokens guardados en disco (renovados previamente)
    try:
        with open(ML_TOKENS_FILE) as f:
            saved = json.load(f)
            if saved.get("access_token"):
                _ml_token_data = saved
                logger.info("✅ Tokens OAuth cargados desde archivo persistente")
                return
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Si no hay archivo, usar variables de entorno (primer arranque)
    if ML_ACCESS_TOKEN:
        _ml_token_data["access_token"] = ML_ACCESS_TOKEN
        _ml_token_data["refresh_token"] = ML_REFRESH_TOKEN
        # Asumir que el token de env podría estar próximo a expirar;
        # poner expires_at = ahora + 5 horas (ML da 6h, dejamos margen)
        _ml_token_data["expires_at"] = time.time() + 5 * 3600
        _save_tokens()
        logger.info("✅ Tokens OAuth inicializados desde variables de entorno")
    else:
        logger.warning("⚠️ No hay ML_ACCESS_TOKEN configurado — las llamadas API irán sin autenticación")


def _save_tokens():
    """Persiste los tokens en disco para sobrevivir reinicios."""
    try:
        with open(ML_TOKENS_FILE, "w") as f:
            json.dump(_ml_token_data, f)
    except Exception as e:
        logger.error(f"Error guardando tokens: {e}")


def _token_is_expired() -> bool:
    """Verifica si el access token ha expirado o está por expirar (margen 5 min)."""
    if not _ml_token_data.get("access_token"):
        return True
    return time.time() >= (_ml_token_data.get("expires_at", 0) - 300)


async def _refresh_access_token() -> bool:
    """
    Renueva el access token usando el refresh token.
    Endpoint: https://api.mercadolibre.com/oauth/token
    Grant type: refresh_token
    Retorna True si se renovó correctamente, False si falló.
    """
    global _ml_token_data

    refresh_token = _ml_token_data.get("refresh_token", "")
    if not refresh_token:
        logger.error("❌ No hay refresh_token disponible para renovar")
        return False

    if not ML_CLIENT_ID or not ML_CLIENT_SECRET:
        logger.error("❌ Faltan ML_CLIENT_ID o ML_CLIENT_SECRET para renovar token")
        return False

    payload = {
        "grant_type": "refresh_token",
        "client_id": ML_CLIENT_ID,
        "client_secret": ML_CLIENT_SECRET,
        "refresh_token": refresh_token,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post("https://api.mercadolibre.com/oauth/token", data=payload)
            if r.status_code == 200:
                data = r.json()
                _ml_token_data["access_token"] = data["access_token"]
                _ml_token_data["refresh_token"] = data.get("refresh_token", refresh_token)
                # ML devuelve expires_in en segundos (normalmente 21600 = 6 horas)
                expires_in = data.get("expires_in", 21600)
                _ml_token_data["expires_at"] = time.time() + expires_in
                _save_tokens()
                logger.info(f"🔄 Access token renovado exitosamente (expira en {expires_in//3600}h {(expires_in%3600)//60}m)")
                return True
            else:
                logger.error(f"❌ Error renovando token: {r.status_code} — {r.text}")
                return False
    except Exception as e:
        logger.error(f"❌ Excepción renovando token: {e}")
        return False


async def get_ml_access_token() -> str:
    """
    Devuelve un access token válido.
    Si está expirado, intenta renovarlo automáticamente.
    Si no hay token, devuelve cadena vacía (las llamadas irán sin auth).
    """
    if not _ml_token_data.get("access_token"):
        return ""

    if _token_is_expired():
        logger.info("⏰ Token expirado, intentando renovar...")
        success = await _refresh_access_token()
        if not success:
            logger.warning("⚠️ No se pudo renovar el token, usando el actual (podría fallar)")

    return _ml_token_data.get("access_token", "")


def _ml_auth_headers(token: str) -> dict:
    """Genera headers con autenticación Bearer para la API de Mercado Libre."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


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
    """
    Formato del mensaje de oferta:
    ~precio_original~ → precio_final (con el descuento%)

    CÁLCULO DE PRECIO FINAL:
    precio_final = precio_original × (1 - descuento/100)

    La API de Mercado Libre ya devuelve 'price' como el precio con descuento
    y 'original_price' como el precio original, así que:
      - deal['original'] = precio_original (sin descuento)
      - deal['price']    = precio_final (ya con descuento aplicado)
      - deal['discount'] = porcentaje de descuento

    Verificación: precio_final ≈ precio_original × (1 - descuento/100)
    """
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

    unique = list({d["id"]: d for d in deals}.values())
    logger.info(f"Total ofertas scrapeadas: {len(unique)}")
    return unique

# ── Búsqueda vía API de ML con autenticación OAuth ────────────────────────────
async def search_api(keyword: str, min_discount: int = MIN_DISCOUNT) -> list:
    """
    Busca productos en la API de Mercado Libre usando autenticación OAuth.
    Con el access token, se evitan errores 403 Forbidden.
    """
    deals = []
    try:
        token = await get_ml_access_token()
        auth_headers = _ml_auth_headers(token)

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://api.mercadolibre.com/sites/MLM/search",
                params={"q": keyword, "limit": 30},
                headers=auth_headers,
            )

            if r.status_code == 401 and token:
                # Token inválido — intentar renovar y reintentar
                logger.warning("⚠️ Token rechazado (401), renovando...")
                if await _refresh_access_token():
                    token = _ml_token_data["access_token"]
                    auth_headers = _ml_auth_headers(token)
                    r = await client.get(
                        "https://api.mercadolibre.com/sites/MLM/search",
                        params={"q": keyword, "limit": 30},
                        headers=auth_headers,
                    )

            if r.status_code == 403:
                logger.error(f"❌ 403 Forbidden en búsqueda '{keyword}' — verificar permisos OAuth")
                return deals

            if r.status_code != 200:
                logger.error(f"❌ API respondió {r.status_code} para '{keyword}'")
                return deals

            for item in r.json().get("results", []):
                price    = item.get("price", 0)
                original = item.get("original_price", 0)
                if not original or original <= price:
                    continue

                # Cálculo de descuento: discount = (1 - precio_final/precio_original) × 100
                # Verificación inversa: precio_final = precio_original × (1 - discount/100)
                discount = round((1 - price / original) * 100)
                if discount < min_discount:
                    continue

                # Imagen
                thumb = item.get("thumbnail","")
                img   = thumb.replace("-I.jpg","").replace("-O.jpg","").replace("http://","https://")
                # Atributos técnicos
                attrs = {a["id"]: a.get("value_name","") for a in item.get("attributes",[])}
                deals.append({
                    "id":           item["id"],
                    "title":        item["title"][:70],
                    "price":        price,         # precio_final (con descuento)
                    "original":     original,      # precio_original (sin descuento)
                    "discount":     discount,      # porcentaje OFF
                    "url":          make_affiliate_link(item["permalink"]),
                    "img":          img,
                    "condition":    item.get("condition",""),
                    "sold_quantity": item.get("sold_quantity", 0),
                    "brand":        attrs.get("BRAND",""),
                    "model":        attrs.get("MODEL",""),
                    "ram":          attrs.get("RAM",""),
                    "storage":      attrs.get("STORAGE_CAPACITY",""),
                })
    except Exception as e:
        logger.error(f"API error [{keyword}]: {e}")
    return deals

# ── Consulta de un item específico con autenticación ──────────────────────────
async def get_item_by_id(item_id: str) -> dict | None:
    """
    Obtiene información de un producto por su ID (ej: MLM123456)
    usando autenticación OAuth para evitar 403.
    """
    try:
        token = await get_ml_access_token()
        auth_headers = _ml_auth_headers(token)

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.mercadolibre.com/items/{item_id}",
                headers=auth_headers,
            )

            if r.status_code == 401 and token:
                logger.warning("⚠️ Token rechazado (401) en get_item, renovando...")
                if await _refresh_access_token():
                    auth_headers = _ml_auth_headers(_ml_token_data["access_token"])
                    r = await client.get(
                        f"https://api.mercadolibre.com/items/{item_id}",
                        headers=auth_headers,
                    )

            if r.status_code != 200:
                logger.error(f"❌ Error obteniendo item {item_id}: {r.status_code}")
                return None

            return r.json()
    except Exception as e:
        logger.error(f"Error get_item {item_id}: {e}")
        return None

async def get_all_deals() -> list:
    deals = []
    # Usar API de ML con autenticación como fuente principal
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
        "🔥 *@JackRocko\\_bot activado*\n\n"
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
    deals = await search_api(q, min_discount=20)
    n = await send_deals(ctx.bot, deals, update.effective_chat.id, limit=5)
    if n == 0:
        await update.message.reply_text("😔 Sin resultados con ese criterio.")

async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ids  = load_json(CHAT_IDS_FILE, [])
    seen = load_json(SEEN_DEALS_FILE, [])
    cupones_activos = sum(1 for v in CUPONES.values() if v.strip())

    # Estado del token OAuth
    if _ml_token_data.get("access_token"):
        remaining = _ml_token_data.get("expires_at", 0) - time.time()
        if remaining > 0:
            hours = int(remaining // 3600)
            mins  = int((remaining % 3600) // 60)
            token_status = f"✅ Activo (expira en {hours}h {mins}m)"
        else:
            token_status = "⚠️ Expirado (se renovará automáticamente)"
    else:
        token_status = "❌ No configurado"

    await update.message.reply_text(
        f"📊 *Estado de JackRocko Bot*\n\n"
        f"👥 Suscriptores: *{len(ids)}*\n"
        f"📦 Ofertas enviadas: *{len(seen)}*\n"
        f"🔗 Links afiliado: ✅\n"
        f"💳 Cupones activos: *{cupones_activos}*\n"
        f"🔑 OAuth ML: {token_status}",
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
            async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
                r = await client.get(text)
                final_url = str(r.url)
                logger.info(f"URL final: {final_url}")

                match = re.search(r'MLM-?(\d+)', final_url, re.IGNORECASE)
                if not match:
                    match = re.search(r'MLM-?(\d+)', r.text, re.IGNORECASE)

                if match:
                    item_id = f"MLM{match.group(1)}"
                    # Usar función autenticada para obtener el item
                    item = await get_item_by_id(item_id)
                    if item:
                        price = item.get("price", 0)
                        original = item.get("original_price") or price
                        # Cálculo: discount = (1 - price/original) × 100
                        # Inverso: price = original × (1 - discount/100) ✓
                        discount = round((1 - price / original) * 100) if original > price else 0
                        thumb = item.get("thumbnail", "").replace("-I.jpg", "").replace("http://", "https://")
                        attrs = {a["id"]: a.get("value_name","") for a in item.get("attributes",[])}
                        deal = {
                            "id":           item_id,
                            "title":        item.get("title", "")[:70],
                            "price":        price,
                            "original":     original,
                            "discount":     discount,
                            "url":          make_affiliate_link(item.get("permalink", text)),
                            "img":          thumb,
                            "condition":    item.get("condition",""),
                            "sold_quantity": item.get("sold_quantity", 0),
                            "brand":        attrs.get("BRAND",""),
                            "model":        attrs.get("MODEL",""),
                            "ram":          attrs.get("RAM",""),
                            "storage":      attrs.get("STORAGE_CAPACITY",""),
                        }
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

async def cmd_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Solo admin — muestra estado del token y fuerza renovación si se pide."""
    if update.effective_user.id != ADMIN_ID:
        return

    args = ctx.args
    if args and args[0] == "renovar":
        await update.message.reply_text("🔄 Forzando renovación de token...")
        success = await _refresh_access_token()
        if success:
            remaining = _ml_token_data.get("expires_at", 0) - time.time()
            hours = int(remaining // 3600)
            mins  = int((remaining % 3600) // 60)
            await update.message.reply_text(f"✅ Token renovado. Expira en {hours}h {mins}m.")
        else:
            await update.message.reply_text("❌ No se pudo renovar. Revisa las credenciales.")
    else:
        if _ml_token_data.get("access_token"):
            remaining = _ml_token_data.get("expires_at", 0) - time.time()
            if remaining > 0:
                hours = int(remaining // 3600)
                mins  = int((remaining % 3600) // 60)
                status = f"✅ Activo — expira en {hours}h {mins}m"
            else:
                status = "⚠️ Expirado — se renovará automáticamente en la próxima petición"
            # Mostrar primeros/últimos chars del token para verificación
            t = _ml_token_data["access_token"]
            masked = f"{t[:8]}...{t[-4:]}" if len(t) > 12 else "***"
            await update.message.reply_text(
                f"🔑 *Estado OAuth de Mercado Libre*\n\n"
                f"Token: `{masked}`\n"
                f"Estado: {status}\n\n"
                f"Usa `/token renovar` para forzar renovación.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "❌ No hay token configurado.\n\n"
                "Configura las variables de entorno:\n"
                "• `ML_CLIENT_ID`\n"
                "• `ML_CLIENT_SECRET`\n"
                "• `ML_ACCESS_TOKEN`\n"
                "• `ML_REFRESH_TOKEN`",
                parse_mode="Markdown"
            )

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    # Cargar tokens OAuth al iniciar
    _load_tokens()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("ofertas", cmd_ofertas))
    app.add_handler(CommandHandler("buscar",  cmd_buscar))
    app.add_handler(CommandHandler("estado",  cmd_estado))
    app.add_handler(CommandHandler("salir",    cmd_salir))
    app.add_handler(CommandHandler("limpiar",  cmd_limpiar))
    app.add_handler(CommandHandler("token",    cmd_token))
    # Handler para links enviados por el admin
    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"https?://"), handle_link))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(broadcast, "interval", minutes=10, args=[app.bot])
    scheduler.start()

    logger.info("🤖 JackRocko Bot iniciado con OAuth de Mercado Libre")
    app.run_polling()

if __name__ == "__main__":
    main()
