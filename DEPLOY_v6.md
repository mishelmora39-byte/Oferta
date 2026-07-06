# JackRocko Bot v6 — Guía de deploy en Railway

## 0. URGENTE antes de todo
Revoca el token filtrado: @BotFather → /mybots → tu bot → API Token → **Revoke current token**. Guarda el nuevo.

## 1. Variables de entorno en Railway
| Variable | Valor | Notas |
|---|---|---|
| `BOT_TOKEN` | (token NUEVO) | El viejo quedó expuesto en logs |
| `CHANNEL_ID` | `-1004405739696` | Canal @ofertasmx3 |
| `ADMIN_ID` | `333569583` | Tu ID de Telegram |
| `AFFILIATE_ID` | `293AH0-18PY` | Va en `matt_word` |
| `ML_ACCESS_TOKEN` | (ver paso 2) | Sin él, la fuente API queda apagada |
| `MATT_TOOL_ID` | (opcional) | ID numérico de tu panel de afiliados |
| `DATA_DIR` | `/data` | Requiere el volumen del paso 3 |
| `MIN_DISCOUNT` | `15` | % mínimo para publicar |
| `BROADCAST_MINUTES` | `15` | Frecuencia del broadcast |

## 2. Token de la API de Mercado Libre
1. Entra a https://developers.mercadolibre.com.mx con tu cuenta de ML.
2. Crea una aplicación (nombre libre, redirect URI puede ser `https://localhost`).
3. Obtén tu **access token** (flujo OAuth; el token de prueba del DevCenter
   sirve para validar). Ojo: los tokens expiran en ~6 horas — para producción
   necesitarás el refresh token. Si quieres, ese flujo de auto-renovación
   se agrega en una v6.1.
4. Ponlo en `ML_ACCESS_TOKEN`.

Sin este token el bot sigue funcionando solo con el scraper, pero la API
es la fuente confiable.

## 3. Volumen persistente en Railway
Servicio → Settings → **Volumes** → Add Volume → Mount path: `/data`.
Sin esto, suscriptores e historial se borran en cada deploy (el bot lo
avisa en logs y en /status).

## 4. Verificación después del deploy
1. En los logs debe aparecer `🚀 JackRocko Bot v6 iniciado` y, **15 segundos
   después, el primer broadcast** (esto confirma que el fix del scheduler
   funciona — en v5 nunca corría).
2. Mándale `/status` al bot (solo responde a tu ADMIN_ID): te dice cuántas
   ofertas devuelve cada fuente, suscriptores, historial y si los datos
   son persistentes.
3. Verifica un link publicado: debe traer `matt_word=293AH0-18PY`. Compáralo
   con un link generado desde tu panel de afiliados para confirmar tracking.

## 5. Qué cambió respecto a v5 (resumen)
- **Scheduler**: JobQueue de PTB en lugar de APScheduler → el broadcast
  automático ahora sí corre.
- **API ML**: con token OAuth; sin token se omite con aviso (el endpoint
  ya no es público).
- **Formato**: HTML + `html.escape` → títulos con símbolos ya no rompen
  mensajes; tachado real con `<s>`.
- **Persistencia**: `/data` + escritura atómica.
- **Afiliado**: tag en `matt_word` (antes iba mal en `matt_tool`).
- **Scraper**: primero JSON embebido de la página, luego CSS específico;
  se eliminaron los fallbacks genéricos que publicaban basura; detecta
  captcha/bloqueo de IP y lo reporta en logs.
- **Broadcast justo**: canal y suscriptores reciben del mismo lote de
  ofertas nuevas (antes el canal las "quemaba" todas).
- **Rate limit** de 60 s en /ofertas por chat (el admin está exento).
- Historial ampliado a 2000 IDs; logs de httpx silenciados (adiós al
  spam de getUpdates).
- Nuevo comando **/status** (solo admin) para diagnóstico en un mensaje.
