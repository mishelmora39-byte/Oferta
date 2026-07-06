"""
JackRocko Bot v6 — Ofertas de Mercado Libre → Telegram
========================================================
Correcciones respecto a v5:
  [1] Scheduler: APScheduler eliminado. Ahora usa el JobQueue integrado de
      python-telegram-bot, que corre DENTRO del event loop correcto.
  [2] API de ML: soporta token OAuth (ML_ACCESS_TOKEN). Sin token, la fuente
      se omite con un aviso claro en logs (el endpoint ya no es público).
  [3] Formato: parse_mode HTML con html.escape() — títulos con *, _, [, %
      ya no rompen el envío. Tachado real con <s>.
  [4] Persistencia: los JSON viven en DATA_DIR (montar volumen de Railway
      en /data). Escritura atómica (tmp + os.replace) para evitar corrupción.
  [5] Afiliado: el tag va en matt_word (no en matt_tool). matt_tool es
      opcional y numérico (MATT_TOOL_ID).
  [6] Scraper endurecido: primero intenta el JSON embebido en la página
      (__PRELOADED_STATE__ / __NEXT_DATA__), luego selectores CSS específicos.
      Sin fallbacks genéricos tipo <article> que publicaban basura.
  [7] Broadcast: las ofertas nuevas se calculan UNA vez y se reparten a
      canal y suscriptores del mismo lote (antes el canal "quemaba" todo).
  [8] Rate limit en /ofertas (cooldown por chat) para no invitar bloqueos.
  [9] Historial ampliado a 2000 IDs para evitar re-envíos cíclicos.
 [10] /status para el admin: salud de fuentes en un mensaje.

Variables de entorno requeridas en Railway:
  BOT_TOKEN        → token NUEVO de @BotFather (revocar el filtrado)
  CHANNEL_ID       → -1004405739696
  ADMIN_ID         → 333569583
  AFFILIATE_ID     → 293AH0-18PY
  ML_ACCESS_TOKEN  → (opcional pero MUY recomendado) token OAuth del
                     DevCenter de ML: https://developers.mercadolibre.com.mx
  MATT_TOOL_ID     → (opcional) ID numérico de herramienta de afiliados
  DATA_DIR         → /data  (montar volumen de Railway ahí)
  MIN_DISCOUNT     → descuento mínimo % para publicar (default 15)
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
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Silenciar el spam de getUpdates en los logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("jackrocko")

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
AFFILIATE_ID    = os.getenv("AFFILIATE_ID", "293AH0-18PY")
MATT_TOOL_ID    = os.getenv("MATT_TOOL_ID", "")            # numérico, opcional
CHANNEL_ID      = int(os.getenv("CHANNEL_ID", "0"))
ADMIN_ID        = int(os.getenv("ADMIN_ID", "0"))
ML_ACCESS_TOKEN = os.getenv("ML_ACCESS_TOKEN", "")
MIN_DISCOUNT    = int(os.getenv("MIN_DISCOUNT", "15"))
BROADCAST_MIN   = int(os.getenv("BROADCAST_MINUTES", "15"))

# Persistencia: montar un volumen de Railway en /data
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

# Cooldown de /ofertas por chat (segundos)
OFERTAS_COOLDOWN = 60
_last_ofertas: dict[int, float] = {}

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
    """Escritura atómica: nunca deja un archivo a medias."""
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

# ── Formato de mensaje (HTML: a prueba de títulos con símbolos) ────────────────
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
    return (
        f"🔥 <b>{title}</b>\n\n"
        f"💰 {orig_str}<b>{price_str}</b>\n"
        f"{disc_str}\n\n"
        f"🛒 <a href=\"{html.escape(deal['url'])}\">Ver en Mercado Libre</a>"
    )

# ── FUENTE 1: API oficial de ML (requiere token OAuth) ─────────────────────────
async def fetch_api_deals() -> list:
    if not ML_ACCESS_TOKEN:
        logger.warning(
            "⚠️ ML_ACCESS_TOKEN no configurado — fuente API omitida. "
            "El endpoint de búsqueda ya NO es público. Registra una app en "
            "https://developers.mercadolibre.com.mx y agrega el token."
        )
        return []

    deals = []
    queries = [
        # Búsquedas de categorías populares; el descuento real se calcula
        # con original_price y se filtra con MIN_DISCOUNT.
        "https://api.mercadolibre.com/sites/MLM/search?q=audifonos&limit=50",
        "https://api.mercadolibre.com/sites/MLM/search?q=smartwatch&limit=50",
        "https://api.mercadolibre.com/sites/MLM/search?q=laptop&limit=50",
        "https://api.mercadolibre.com/sites/MLM/search?q=herramientas&limit=50",
    ]
    api_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Authorization": f"Bearer {ML_ACCESS_TOKEN}",
    }
    async with httpx.AsyncClient(timeout=20, headers=api_headers) as client:
        for url in queries:
            try:
                r = await client.get(url)
                logger.info(f"API status {r.status_code} → {url[:70]}")
                if r.status_code == 401:
                    logger.error("❌ Token de ML inválido o expirado (401). Renuévalo en el DevCenter.")
                    return deals
                if r.status_code != 200:
                    continue
                for item in r.json().get("results", []):
                    price = item.get("price") or 0
                    orig  = item.get("original_price") or 0
                    if price <= 0 or orig <= price:
                        continue  # sin descuento real → no interesa
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
                    })
            except httpx.HTTPError as e:
                logger.error(f"Error API: {e}")
    logger.info(f"API total: {len(deals)} ofertas con ≥{MIN_DISCOUNT}% de descuento")
    return deals

# ── FUENTE 2: Scraper de /ofertas ──────────────────────────────────────────────
def _extract_embedded_json(html_text: str) -> list:
    """
    ML incrusta el estado inicial de la página en un <script> como JSON.
    Es MUCHO más estable que los selectores CSS. Buscamos objetos con
    pinta de producto (id MLM + price) en ese estado.
    """
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
                    deals.append({
                        "id":       pid,
                        "title":    str(title)[:80],
                        "price":    price,
                        "original": orig if orig > price else price,
                        "discount": disc,
                        "url":      make_affiliate_link(str(link)),
                        "img":      str(node.get("picture") or node.get("thumbnail") or ""),
                    })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(state)
    return deals

def _extract_css(soup: BeautifulSoup) -> list:
    """Plan B: selectores CSS ESPECÍFICOS. Sin fallbacks genéricos que
    publiquen basura ('Producto 7 — $0') en el canal."""
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
            price_el = item.select_one("span.andes-money-amount__fraction")
            if not (title_el and link_el and price_el):
                continue

            title = title_el.get_text(strip=True)
            link  = link_el.get("href", "")
            price = float(re.sub(r"[^\d]", "", price_el.get_text(strip=True)) or 0)
            if len(title) < 5 or not link or price <= 0:
                continue

            orig_el = item.select_one("s span.andes-money-amount__fraction") or item.select_one("s")
            orig = float(re.sub(r"[^\d]", "", orig_el.get_text(strip=True)) or 0) if orig_el else 0

            disc_el = item.select_one("[class*='discount']")
            m = re.search(r"(\d+)", disc_el.get_text(strip=True)) if disc_el else None
            disc = int(m.group(1)) if m else (round((1 - price / orig) * 100) if orig > price else 0)

            img_el = item.select_one("img")
            img = (img_el.get("data-src") or img_el.get("src") or "") if img_el else ""

            id_m = re.search(r"MLM-?(\d+)", link)
            if not id_m:
                continue  # sin ID real no publicamos

            deals.append({
                "id":       f"MLM{id_m.group(1)}",
                "title":    title[:80],
                "price":    price,
                "original": orig if orig > price else price,
                "discount": disc,
                "url":      make_affiliate_link(link),
                "img":      img,
            })
        except (ValueError, AttributeError) as e:
            logger.debug(f"Item {i} descartado: {e}")
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

    # Detección de bloqueo/captcha
    lowered = r.text[:3000].lower()
    if "captcha" in lowered or "robot" in lowered:
        logger.error("🚫 ML está sirviendo un captcha — IP posiblemente bloqueada.")
        return []

    # 1) JSON embebido (estable)
    deals = _extract_embedded_json(r.text)
    if deals:
        logger.info(f"Scraper (JSON embebido): {len(deals)} productos")
        return deals

    # 2) CSS específico (plan B)
    deals = _extract_css(BeautifulSoup(r.text, "html.parser"))
    if deals:
        logger.info(f"Scraper (CSS): {len(deals)} productos")
    else:
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
    # El scraper va al final para que su versión gane en duplicados
    # (suele traer el descuento mostrado en la página, más confiable).
    combined = api_deals + scraper_deals
    unique = list({d["id"]: d for d in combined}.values())
    # Mejores descuentos primero
    unique.sort(key=lambda d: d.get("discount", 0), reverse=True)
    logger.info(f"📢 Total único: {len(unique)} ofertas")
    return unique

# ── Envío ──────────────────────────────────────────────────────────────────────
async def _send_one(bot, chat_id: int, deal: dict) -> bool:
    text = format_deal(deal)
    try:
        if deal.get("img", "").startswith("http"):
            try:
                await bot.send_photo(chat_id, deal["img"], caption=text, parse_mode=ParseMode.HTML)
                return True
            except TelegramError:
                pass  # imagen falló (hotlink, formato) → texto plano
        await bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=False
        )
        return True
    except TelegramError as e:
        logger.warning(f"No se pudo enviar {deal['id']} a {chat_id}: {e}")
        return False

async def broadcast(context: ContextTypes.DEFAULT_TYPE):
    """Job periódico: calcula ofertas nuevas UNA vez y reparte el mismo
    lote al canal y a los suscriptores (nadie recibe 'sobras')."""
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
    if update.effective_user.id != ADMIN_ID:
        return
    save_json(SEEN_DEALS_FILE, [])
    await update.message.reply_text("✅ Historial reiniciado.")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Solo admin: diagnóstico rápido de fuentes y estado."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🩺 Probando fuentes...")
    api_deals, scraper_deals = await asyncio.gather(
        fetch_api_deals(), fetch_scraper_deals()
    )
    subs = load_json(CHAT_IDS_FILE, [])
    seen = load_json(SEEN_DEALS_FILE, [])
    token_state = "✅ configurado" if ML_ACCESS_TOKEN else "❌ FALTA (fuente API muerta)"
    data_state  = "✅ /data (persistente)" if DATA_DIR != "." else "⚠️ efímero (montar volumen)"
    await update.message.reply_text(
        f"<b>Estado JackRocko v6</b>\n\n"
        f"🔑 Token ML: {token_state}\n"
        f"📡 API: {len(api_deals)} ofertas\n"
        f"🕷 Scraper: {len(scraper_deals)} ofertas\n"
        f"👥 Suscriptores: {len(subs)}\n"
        f"🗂 Historial: {len(seen)}/{SEEN_MAX}\n"
        f"💾 Datos: {data_state}\n"
        f"⏱ Broadcast: cada {BROADCAST_MIN} min",
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

    # [FIX 1] JobQueue de PTB: corre dentro del event loop correcto.
    # first=15 → primer broadcast 15s después de arrancar (útil para verificar).
    app.job_queue.run_repeating(broadcast, interval=BROADCAST_MIN * 60, first=15)

    logger.info("🚀 JackRocko Bot v6 iniciado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
