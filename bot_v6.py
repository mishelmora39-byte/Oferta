"""
JackRocko Bot v6.5 — Ofertas de Mercado Libre → Telegram
========================================================
Cambios respecto a v6.4:
 [14] Fix crítico: el bot entraba en crash loop cuando el canal (del cual
      es admin) publicaba algo — esas actualizaciones (channel_post) NO
      traen effective_user, y los handlers de /reset, /status, /manual y
      el link automático asumían que siempre había un usuario. Ahora
      todos verifican que exista antes de comparar con ADMIN_ID.

Cambios respecto a v6.2:
 [13] Comando /manual (admin): publica una oferta dando tú el precio
      original y el % de descuento — el bot calcula el precio final y
      publica en el canal. No depende de que la API o el scraping de ML
      cooperen, así que es la vía más confiable cuando encuentras una
      oferta navegando tú mismo.

      Formato:
        /manual <link> | <título> | <precio original> | <% descuento>
      Con foto (opcional, 5ta parte):
        /manual <link> | <título> | <precio original> | <% descuento> | <url de imagen>

      Ejemplo:
        /manual https://articulo.mercadolibre.com.mx/MLM-123456789 | Samsung Galaxy A07 64GB Negro | 2999 | 15

Cambios de v6.2 (se mantienen):
 [12] Handler de link manual automático (admin): pegas un link y el bot
      intenta sacar los datos solo (API item → scraping CSS → Open Graph).
      Con la restricción actual de ML (403 en /items/ también), este
      camino puede fallar seguido — para esos casos usa /manual.

Cambios de v6.1 (se mantienen):
 [11] Refresh automático de token ML (módulo ml_auth.py).

Correcciones de v6 (se mantienen todas):
  [1] Scheduler: JobQueue integrado de python-telegram-bot.
  [2] API de ML: soporta token OAuth con refresh automático.
  [3] Formato: parse_mode HTML con html.escape().
  [4] Persistencia: JSON en DATA_DIR, escritura atómica.
  [5] Afiliado: el tag va en matt_word.
  [6] Scraper endurecido: JSON embebido primero, CSS específico después.
  [7] Broadcast: mismo lote para canal y suscriptores.
  [8] Rate limit en /ofertas.
  [9] Historial ampliado a 2000 IDs.
 [10] /status para el admin.

Variables de entorno requeridas en Railway:
  BOT_TOKEN         → token de @BotFather
  CHANNEL_ID        → -1004405739696
  ADMIN_ID          → 333569583
  AFFILIATE_ID      → 293AH0-18PY
  ML_CLIENT_ID      → App ID del DevCenter de ML
  ML_CLIENT_SECRET  → Secret Key del DevCenter de ML
  ML_REFRESH_TOKEN  → refresh_token ya obtenido
  MATT_TOOL_ID      → (opcional) ID numérico de herramienta de afiliados
  DATA_DIR          → /data  (montar volumen de Railway ahí)
  MIN_DISCOUNT      → descuento mínimo % para publicar en broadcast automático
"""

import json
import re
import os
import html
import time
import asyncio
import logging

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from ml_auth import ml_token_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("jackrocko")

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
AFFILIATE_ID    = os.getenv("AFFILIATE_ID", "293AH0-18PY")
MATT_TOOL_ID    = os.getenv("MATT_TOOL_ID", "")
CHANNEL_ID      = int(os.getenv("CHANNEL_ID", "0"))
ADMIN_ID        = int(os.getenv("ADMIN_ID", "0"))
MIN_DISCOUNT    = int(os.getenv("MIN_DISCOUNT", "15"))
BROADCAST_MIN   = int(os.getenv("BROADCAST_MINUTES", "15"))

DATA_DIR = os.getenv("DATA_DIR", "/data")
if not os.path.isdir(DATA_DIR) or not os.access(DATA_DIR, os.W_OK):
    logger.warning(
        f"⚠️ DATA_DIR '{DATA_DIR}' no existe o no es escribible. "
        "Usando directorio actual (los datos se PERDERÁN en cada deploy). "
        "Monta un volumen de Railway en /data."
    )
    DATA_DIR = "."

CHAT_IDS_FILE   = os.path.join(DATA_DIR, "chat_ids.json")
SEEN_DEALS_FILE = os.path.join(DATA_DIR, "seen_deals.json")
SEEN_MAX        = 2000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9",
    "Connection": "keep-alive",
}

OFERTAS_COOLDOWN = 60
_last_ofertas: dict[int, float] = {}

LINK_RE = re.compile(
    r"https?://\S*(?:mercadolibre\.com|meli\.la)\S*",
    re.IGNORECASE,
)

# ── Persistencia segura ────────────────────────────────────────────────────────
def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        logger.error(f"Archivo corrupto: {path}. Usando default.")
        return default

def save_json(path: str, data) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)

# ── Links de afiliado ──────────────────────────────────────────────────────────
def make_affiliate_link(url: str) -> str:
    if not url:
        return ""
    clean = url.split("?")[0].split("#")[0]
    params = [f"matt_word={AFFILIATE_ID}", "matt_source=telegram", "matt_campaign=jackrocko"]
    if MATT_TOOL_ID:
        params.insert(0, f"matt_tool={MATT_TOOL_ID}")
    return f"{clean}?{'&'.join(params)}"

# ── Formato de mensaje ──────────────────────────────────────────────────────────
def format_deal(deal: dict) -> str:
    title     = html.escape(deal["title"])
    price_str = f"${deal['price']:,.0f} MXN"
    orig_str  = (
        f"<s>${deal['original']:,.0f}</s> → "
        if deal.get("original", 0) > deal["price"] else ""
    )
    disc_str = (
        f"🏷️ <b>{deal['discount']}% OFF</b>"
        if deal.get("discount", 0) > 0 else "🔥 ¡Gran Precio!"
    )
    ship_str = "\n🚚 <b>Envío GRATIS</b>" if deal.get("free_shipping") else ""
    return (
        f"🔥 <b>{title}</b>\n\n"
        f"💰 {orig_str}<b>{price_str}</b>\n"
        f"{disc_str}{ship_str}\n\n"
        f"🛒 <a href=\"{html.escape(deal['url'])}\">Ver en Mercado Libre</a>"
    )

# ── FUENTE 1: API oficial de ML ─────────────────────────────────────────────────
async def fetch_api_deals() -> list:
    token = ml_token_manager.get_token()
    if token is None:
        logger.warning(
            "⚠️ Sin token ML válido — fuente API omitida. Detalle: %s",
            ml_token_manager.last_error,
        )
        return []

    deals = []
    queries = [
        "https://api.mercadolibre.com/sites/MLM/search?q=audifonos&limit=50",
        "https://api.mercadolibre.com/sites/MLM/search?q=smartwatch&limit=50",
        "https://api.mercadolibre.com/sites/MLM/search?q=laptop&limit=50",
        "https://api.mercadolibre.com/sites/MLM/search?q=herramientas&limit=50",
    ]
    api_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Authorization": f"Bearer {token}",
    }
    async with httpx.AsyncClient(timeout=20, headers=api_headers) as client:
        for url in queries:
            try:
                r = await client.get(url)
                logger.info(f"API status {r.status_code} → {url[:70]}")
                if r.status_code == 401:
                    logger.error(
                        "❌ Token de ML rechazado (401) incluso tras renovar. "
                        "Revisa ML_CLIENT_ID/ML_CLIENT_SECRET/ML_REFRESH_TOKEN."
                    )
                    return deals
                if r.status_code != 200:
                    continue
                for item in r.json().get("results", []):
                    price = item.get("price") or 0
                    orig  = item.get("original_price") or 0
                    if price <= 0 or orig <= price:
                        continue
                    disc = round((1 - price / orig) * 100)
                    if disc < MIN_DISCOUNT:
                        continue
                    deals.append({
                        "id":       item["id"],
                        "title":    item["title"][:80],
                        "price":    price,
                        "original": orig,
                        "discount": disc,
                        "url":      make_affiliate_link(item.get("permalink", "")),
                        "img":      (item.get("thumbnail") or "").replace("http://", "https://"),
                        "free_shipping": bool((item.get("shipping") or {}).get("free_shipping")),
                    })
            except httpx.HTTPError as e:
                logger.error(f"Error API: {e}")
    logger.info(f"API total: {len(deals)} ofertas con ≥{MIN_DISCOUNT}% de descuento")
    return deals

# ── FUENTE 2: Scraper de /ofertas ──────────────────────────────────────────────
def _extract_embedded_json(html_text: str) -> list:
    deals = []
    patterns = [
        r"window\.__PRELOADED_STATE__\s*=\s*({.*?});",
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>({.*?})</script>',
    ]
    state = None
    for pat in patterns:
        m = re.search(pat, html_text, re.DOTALL)
        if m:
            try:
                state = json.loads(m.group(1))
                break
            except json.JSONDecodeError:
                continue
    if state is None:
        return []

    def walk(node):
        if isinstance(node, dict):
            pid = node.get("id") or node.get("item_id") or ""
            price_node = node.get("price")
            if isinstance(pid, str) and pid.startswith("MLM"):
                price, orig = 0.0, 0.0
                if isinstance(price_node, dict):
                    price = float(price_node.get("amount") or price_node.get("value") or 0)
                    orig  = float(price_node.get("original_amount") or price_node.get("original_value") or 0)
                elif isinstance(price_node, (int, float)):
                    price = float(price_node)
                    orig  = float(node.get("original_price") or 0)
                title = node.get("title") or node.get("name") or ""
                link  = node.get("permalink") or node.get("url") or ""
                if price > 0 and title and link:
                    disc = round((1 - price / orig) * 100) if orig > price else 0
                    ship = node.get("shipping") or {}
                    free = bool(ship.get("free_shipping")) if isinstance(ship, dict) else False
                    deals.append({
                        "id":       pid,
                        "title":    str(title)[:80],
                        "price":    price,
                        "original": orig if orig > price else price,
                        "discount": disc,
                        "url":      make_affiliate_link(str(link)),
                        "img":      str(node.get("picture") or node.get("thumbnail") or ""),
                        "free_shipping": free,
                    })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(state)
    return deals

def _extract_css(soup: BeautifulSoup) -> list:
    deals = []
    containers = (
        soup.select("li.promotion-item")
        or soup.select("div.promotion-item")
        or soup.select("div.poly-card")
    )
    for i, item in enumerate(containers):
        try:
            title_el = (
                item.select_one("p.promotion-item__title")
                or item.select_one("a.poly-component__title")
                or item.select_one("h3")
            )
            link_el = item.select_one("a[href*='mercadolibre']")
            if not (title_el and link_el):
                continue

            title = title_el.get_text(strip=True)
            link  = link_el.get("href", "")
            if len(title) < 5 or not link:
                continue

            # [MÁS RESISTENTE v6.5] en vez de depender de una clase exacta
            # para "precio original" (que se rompe cada vez que ML cambia
            # su HTML), juntamos TODOS los precios visibles en la tarjeta
            # y usamos el más alto como original y el más bajo como final.
            # Si solo hay uno, no hay descuento visible y queda igual.
            price_texts = [
                el.get_text(strip=True)
                for el in item.select("span.andes-money-amount__fraction")
            ]
            prices = []
            for t in price_texts:
                digits = re.sub(r"[^\d]", "", t)
                if digits:
                    prices.append(float(digits))
            if not prices:
                continue

            price = min(prices)
            orig  = max(prices)
            if price <= 0:
                continue

            # [MÁS RESISTENTE v6.5] % de descuento: primero buscamos el
            # patrón "NN%" en TODO el texto de la tarjeta (sin depender de
            # una clase específica), y si no aparece, lo calculamos con
            # los precios recolectados arriba.
            card_text = item.get_text(" ", strip=True)
            pct_m = re.search(r"(\d{1,2})\s*%", card_text)
            if pct_m:
                disc = int(pct_m.group(1))
            elif orig > price:
                disc = round((1 - price / orig) * 100)
            else:
                disc = 0

            img_el = item.select_one("img")
            img = (img_el.get("data-src") or img_el.get("src") or "") if img_el else ""

            id_m = re.search(r"MLM-?(\d+)", link)
            if not id_m:
                continue

            ship_txt = card_text.lower()
            free = "envío gratis" in ship_txt or "envio gratis" in ship_txt

            deals.append({
                "id":       f"MLM{id_m.group(1)}",
                "title":    title[:80],
                "price":    price,
                "original": orig if orig > price else price,
                "discount": disc,
                "url":      make_affiliate_link(link),
                "img":      img,
                "free_shipping": free,
            })
        except (ValueError, AttributeError) as e:
            logger.debug(f"Item {i} descartado: {e}")

    with_discount = sum(1 for d in deals if d.get("discount", 0) > 0)
    logger.info(
        f"Scraper (CSS): {len(deals)} productos, {with_discount} con descuento detectado"
    )
    return deals

async def fetch_scraper_deals() -> list:
    url = "https://www.mercadolibre.com.mx/ofertas"
    async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
        try:
            r = await client.get(url)
        except httpx.HTTPError as e:
            logger.error(f"Scraper: error de red: {e}")
            return []

    if r.status_code != 200:
        logger.error(f"Scraper HTTP {r.status_code}")
        return []

    lowered = r.text[:3000].lower()
    if "captcha" in lowered or "robot" in lowered:
        logger.error("🚫 ML está sirviendo un captcha — IP posiblemente bloqueada.")
        return []

    deals = _extract_embedded_json(r.text)
    if deals:
        logger.info(f"Scraper (JSON embebido): {len(deals)} productos")
        return deals

    deals = _extract_css(BeautifulSoup(r.text, "html.parser"))
    if not deals:
        logger.warning(
            "Scraper: 0 productos. Primeros 300 chars del HTML para diagnóstico:\n"
            + r.text[:300]
        )
    return deals

# ── Combinar fuentes ───────────────────────────────────────────────────────────
async def get_all_deals() -> list:
    api_deals, scraper_deals = await asyncio.gather(
        fetch_api_deals(), fetch_scraper_deals()
    )
    combined = api_deals + scraper_deals
    unique = list({d["id"]: d for d in combined}.values())
    unique.sort(key=lambda d: d.get("discount", 0), reverse=True)
    logger.info(f"📢 Total único: {len(unique)} ofertas")
    return unique

# ── Link manual con extracción automática (best-effort) ────────────────────────
async def resolve_and_fetch_item(raw_url: str) -> dict | None:
    """Intenta sacar los datos de un link de ML solo. Con las restricciones
    actuales de ML (403 en /items/ y a veces en scraping), puede fallar —
    para esos casos existe el comando /manual."""
    final_url = raw_url
    async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
        try:
            r = await client.get(raw_url)
            final_url = str(r.url)
        except httpx.HTTPError as e:
            logger.warning(f"Link manual: no se pudo resolver redirección: {e}")

    combined = f"{final_url} {raw_url}"
    wid_m = re.search(r"[?&#]wid=(MLM\d+)", combined)
    deal_m = re.search(r"deal%3A(MLM\d+)|deal:(MLM\d+)", combined)
    path_m = re.search(r"(?<!/p/)MLM-(\d+)(?!\?)", combined)
    catalog_m = re.search(r"/p/(MLM\d+)", combined)

    if wid_m:
        item_id = wid_m.group(1)
    elif deal_m:
        item_id = deal_m.group(1) or deal_m.group(2)
    elif path_m:
        item_id = f"MLM{path_m.group(1)}"
    elif catalog_m:
        item_id = catalog_m.group(1)
        logger.info(f"Link manual: solo ID de catálogo ({item_id})")
    else:
        logger.warning(f"Link manual: no se encontró ID de producto en {final_url}")
        return None

    token = ml_token_manager.get_token()
    api_headers = {"User-Agent": HEADERS["User-Agent"]}
    if token:
        api_headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=15, headers=api_headers) as client:
            r = await client.get(f"https://api.mercadolibre.com/items/{item_id}")
        if r.status_code == 200:
            item = r.json()
            price = item.get("price") or 0
            orig  = item.get("original_price") or 0
            title = item.get("title", "")
            permalink = item.get("permalink") or final_url
            pictures = item.get("pictures") or []
            img = pictures[0].get("secure_url", "") if pictures else ""
            free_shipping = bool((item.get("shipping") or {}).get("free_shipping"))
            if title and price > 0:
                disc = round((1 - price / orig) * 100) if orig > price else 0
                logger.info(f"Link manual: datos vía API para {item_id}")
                return {
                    "id":       item_id,
                    "title":    title[:80],
                    "price":    price,
                    "original": orig if orig > price else price,
                    "discount": disc,
                    "url":      make_affiliate_link(permalink),
                    "img":      img,
                    "free_shipping": free_shipping,
                }
        else:
            logger.info(f"Link manual: API item respondió {r.status_code}, probando scraping")
    except httpx.HTTPError as e:
        logger.warning(f"Link manual: fallo llamando a API item: {e}")

    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(final_url)
    except httpx.HTTPError as e:
        logger.error(f"Link manual: fallo scraping página de producto: {e}")
        return None
    if r.status_code != 200:
        logger.error(f"Link manual: HTTP {r.status_code} en página de producto")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    title_el = soup.select_one("h1.ui-pdp-title")
    price_el = soup.select_one(".ui-pdp-price__second-line .andes-money-amount__fraction") \
        or soup.select_one(".andes-money-amount__fraction")

    title, price = "", 0.0
    if title_el and price_el:
        title = title_el.get_text(strip=True)
        price = float(re.sub(r"[^\d]", "", price_el.get_text(strip=True)) or 0)

    if not title or price <= 0:
        logger.info("Link manual: selectores normales fallaron, probando Open Graph")
        og_title = soup.select_one("meta[property='og:title']")
        og_price = (
            soup.select_one("meta[property='product:price:amount']")
            or soup.select_one("meta[property='og:price:amount']")
        )
        if og_title:
            title = title or og_title.get("content", "").strip()
        if og_price:
            try:
                price = price or float(og_price.get("content", "0"))
            except ValueError:
                pass

    if not title or price <= 0:
        logger.warning("Link manual: no se pudo extraer título/precio ni por selectores ni por Open Graph")
        return None

    orig_el = soup.select_one(".ui-pdp-price__original-value .andes-money-amount__fraction")
    orig = float(re.sub(r"[^\d]", "", orig_el.get_text(strip=True)) or 0) if orig_el else 0
    disc = round((1 - price / orig) * 100) if orig > price else 0

    img = ""
    og_img = soup.select_one("meta[property='og:image']")
    if og_img:
        img = og_img.get("content", "")

    page_text = soup.get_text(" ", strip=True).lower()
    free_shipping = "envío gratis" in page_text or "envio gratis" in page_text

    logger.info(f"Link manual: datos vía scraping/OG para {item_id}")
    return {
        "id":       item_id,
        "title":    title[:80],
        "price":    price,
        "original": orig if orig > price else price,
        "discount": disc,
        "url":      make_affiliate_link(final_url),
        "img":      img,
        "free_shipping": free_shipping,
    }

async def handle_admin_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # [FIX] Las publicaciones del propio canal (channel_post) llegan sin
    # effective_user — sin este chequeo, el bot truena en cada post del canal.
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    text = update.message.text or ""
    m = LINK_RE.search(text)
    if not m:
        return

    url = m.group(0)
    status_msg = await update.message.reply_text("🔗 Procesando link...")

    deal = await resolve_and_fetch_item(url)
    if deal is None:
        await status_msg.edit_text(
            "⚠️ No pude sacar los datos automáticamente (ML está bloqueando "
            "bastante seguido últimamente).\n\n"
            "Usa /manual en su lugar:\n"
            "<code>/manual link | título | precio original | % descuento</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    ok = await _send_one(ctx.bot, CHANNEL_ID, deal)
    if ok:
        seen = load_json(SEEN_DEALS_FILE, [])
        seen.append(deal["id"])
        save_json(SEEN_DEALS_FILE, seen[-SEEN_MAX:])
        await status_msg.edit_text(f"✅ Publicado en el canal: {deal['title']}")
    else:
        await status_msg.edit_text("⚠️ Se extrajeron los datos pero falló la publicación en el canal.")

# ── /manual: publicar dando precio original y precio final ─────────────────────
MANUAL_USAGE = (
    "<b>Formato de /manual</b>\n\n"
    "<code>/manual link | título | precio original | precio final</code>\n\n"
    "El bot calcula el % de descuento solo — no tienes que sacar cuentas.\n\n"
    "Con foto (opcional, 5ta parte con la URL de una imagen):\n"
    "<code>/manual link | título | precio original | precio final | url imagen</code>\n\n"
    "Ejemplo:\n"
    "<code>/manual https://meli.la/1rCYTAu | Giorgio Armani Stronger with You "
    "Intensely EDP 100ml | 3899 | 1789</code>\n\n"
    "(También acepta % si prefieres escribirlo así: pon el número seguido de "
    "<code>%</code> en la 4ta parte, ej. <code>54%</code>)"
)

async def cmd_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    text = update.message.text or ""
    payload = text.split(None, 1)
    if len(payload) < 2 or not payload[1].strip():
        await update.message.reply_text(MANUAL_USAGE, parse_mode=ParseMode.HTML)
        return

    parts = [p.strip() for p in payload[1].split("|")]
    if len(parts) not in (4, 5):
        await update.message.reply_text(
            "⚠️ Necesito 4 o 5 partes separadas por <code>|</code>.\n\n" + MANUAL_USAGE,
            parse_mode=ParseMode.HTML,
        )
        return

    link, title, orig_str, fourth = parts[:4]
    img_url = parts[4] if len(parts) == 5 else ""

    if not LINK_RE.match(link):
        await update.message.reply_text("⚠️ La primera parte debe ser un link válido de Mercado Libre.")
        return
    if not title:
        await update.message.reply_text("⚠️ Falta el título del producto.")
        return

    try:
        original = float(re.sub(r"[^\d.]", "", orig_str))
    except ValueError:
        await update.message.reply_text("⚠️ El precio original no es un número válido.")
        return
    if original <= 0:
        await update.message.reply_text("⚠️ El precio original debe ser mayor a 0.")
        return

    # [NUEVO v6.4] la 4ta parte puede ser el % (si trae "%") o directamente
    # el precio final (número plano) — en ese caso el % se calcula solo,
    # y el precio queda exacto sin redondeos raros.
    fourth_clean = fourth.strip()
    try:
        if "%" in fourth_clean:
            discount = float(re.sub(r"[^\d.]", "", fourth_clean))
            if not (0 < discount < 100):
                await update.message.reply_text("⚠️ El % de descuento debe estar entre 1 y 99.")
                return
            final_price = round(original * (1 - discount / 100), 2)
        else:
            final_price = float(re.sub(r"[^\d.]", "", fourth_clean))
            if final_price <= 0 or final_price >= original:
                await update.message.reply_text(
                    "⚠️ El precio final debe ser mayor a 0 y menor al precio original."
                )
                return
            discount = round((original - final_price) / original * 100)
    except ValueError:
        await update.message.reply_text("⚠️ La 4ta parte (precio final o %) no es un número válido.")
        return

    deal = {
        "id":       f"MANUAL{int(time.time())}",
        "title":    title[:80],
        "price":    final_price,
        "original": original,
        "discount": round(discount),
        "url":      make_affiliate_link(link),
        "img":      img_url,
        "free_shipping": False,
    }

    ok = await _send_one(ctx.bot, CHANNEL_ID, deal)
    if ok:
        seen = load_json(SEEN_DEALS_FILE, [])
        seen.append(deal["id"])
        save_json(SEEN_DEALS_FILE, seen[-SEEN_MAX:])
        await update.message.reply_text(
            f"✅ Publicado en el canal:\n"
            f"<b>{html.escape(title)}</b>\n"
            f"${original:,.0f} → <b>${final_price:,.0f}</b> ({round(discount)}% OFF)",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("⚠️ Falló la publicación en el canal.")

# ── Envío ──────────────────────────────────────────────────────────────────────
async def _send_one(bot, chat_id: int, deal: dict) -> bool:
    text = format_deal(deal)
    try:
        if deal.get("img", "").startswith("http"):
            try:
                await bot.send_photo(chat_id, deal["img"], caption=text, parse_mode=ParseMode.HTML)
                return True
            except TelegramError:
                pass
        await bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=False
        )
        return True
    except TelegramError as e:
        logger.warning(f"No se pudo enviar {deal['id']} a {chat_id}: {e}")
        return False

async def broadcast(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    deals = await get_all_deals()
    if not deals:
        logger.warning("⚠️ Sin ofertas en esta ronda.")
        return

    seen = load_json(SEEN_DEALS_FILE, [])
    seen_set = set(seen)
    new = [d for d in deals if d["id"] not in seen_set]
    if not new:
        logger.info("Sin ofertas nuevas (todas ya enviadas).")
        return

    channel_batch = new[:8]
    subs_batch    = new[:3]

    sent_ids = set()
    if CHANNEL_ID:
        for d in channel_batch:
            if await _send_one(bot, CHANNEL_ID, d):
                sent_ids.add(d["id"])
            await asyncio.sleep(1.5)

    for cid in load_json(CHAT_IDS_FILE, []):
        for d in subs_batch:
            if await _send_one(bot, cid, d):
                sent_ids.add(d["id"])
            await asyncio.sleep(1.5)

    if sent_ids:
        seen.extend(sent_ids)
        save_json(SEEN_DEALS_FILE, seen[-SEEN_MAX:])
        logger.info(f"✅ Broadcast: {len(sent_ids)} ofertas nuevas publicadas.")

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
    cid = update.effective_chat.id
    now = time.monotonic()
    if now - _last_ofertas.get(cid, 0) < OFERTAS_COOLDOWN and cid != ADMIN_ID:
        await update.message.reply_text("⏳ Espera un momento antes de buscar de nuevo.")
        return
    _last_ofertas[cid] = now

    await update.message.reply_text("🔍 Buscando ofertas en vivo...")
    deals = await get_all_deals()
    if not deals:
        await update.message.reply_text(
            "⚠️ No encontré ofertas. Las fuentes pueden estar caídas — "
            "el admin puede revisar con /status."
        )
        return

    seen = load_json(SEEN_DEALS_FILE, [])
    seen_set = set(seen)
    new = [d for d in deals if d["id"] not in seen_set]
    if not new:
        await update.message.reply_text(
            f"✅ Encontré {len(deals)} ofertas pero ya las viste todas.\n"
            "El admin puede usar /reset para reiniciar el historial."
        )
        return

    sent_ids = []
    for d in new[:5]:
        if await _send_one(ctx.bot, cid, d):
            sent_ids.append(d["id"])
        await asyncio.sleep(1.2)
    if sent_ids:
        seen.extend(sent_ids)
        save_json(SEEN_DEALS_FILE, seen[-SEEN_MAX:])

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    save_json(SEEN_DEALS_FILE, [])
    await update.message.reply_text("✅ Historial reiniciado.")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🩺 Probando fuentes...")
    api_deals, scraper_deals = await asyncio.gather(
        fetch_api_deals(), fetch_scraper_deals()
    )
    subs = load_json(CHAT_IDS_FILE, [])
    seen = load_json(SEEN_DEALS_FILE, [])
    token_state = ml_token_manager.status_summary()
    data_state  = "✅ /data (persistente)" if DATA_DIR != "." else "⚠️ efímero (montar volumen)"
    await update.message.reply_text(
        f"<b>Estado JackRocko v6.5</b>\n\n"
        f"🔑 Token ML: {token_state}\n"
        f"📡 API: {len(api_deals)} ofertas\n"
        f"🕷 Scraper: {len(scraper_deals)} ofertas\n"
        f"👥 Suscriptores: {len(subs)}\n"
        f"🗂 Historial: {len(seen)}/{SEEN_MAX}\n"
        f"💾 Datos: {data_state}\n"
        f"⏱ Broadcast: cada {BROADCAST_MIN} min\n"
        f"🔗 Link automático: envíame un link de ML (best-effort)\n"
        f"✍️ /manual: publica con precio y descuento a mano (más confiable)",
        parse_mode=ParseMode.HTML,
    )

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ Falta BOT_TOKEN en variables de entorno.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("ofertas", cmd_ofertas))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("manual",  cmd_manual))  # [NUEVO v6.4]
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_link))

    app.job_queue.run_repeating(broadcast, interval=BROADCAST_MIN * 60, first=15)

    logger.info("🚀 JackRocko Bot v6.5 iniciado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
