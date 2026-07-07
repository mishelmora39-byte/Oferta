"""
ml_auth.py — Renovación automática del token OAuth de Mercado Libre
=====================================================================
JackRocko Bot v6.1 — módulo independiente que se importa en bot_v6.py

Qué hace:
  - Guarda el access_token en memoria junto con su hora de expiración.
  - Antes de cada llamada a la API de ML, revisa si el token está por
    vencer (con margen de 5 minutos) y lo renueva usando el refresh_token.
  - Si el refresh falla (refresh_token revocado/expirado), lo reporta
    claramente en logs y en /status en vez de fallar en silencio.
  - Persiste el refresh_token más reciente en DATA_DIR, porque ML a
    veces rota el refresh_token en cada renovación (si no lo guardas
    actualizado, la siguiente renovación falla).

Variables de entorno nuevas requeridas:
  ML_CLIENT_ID      — App ID del DevCenter
  ML_CLIENT_SECRET  — Secret Key del DevCenter
  ML_REFRESH_TOKEN  — el refresh_token que ya obtuviste manualmente

ML_ACCESS_TOKEN ya NO se necesita como variable fija: este módulo lo
obtiene solo al arrancar y lo va renovando. Puedes dejar la variable
vieja o quitarla, no se usa una vez integrado este módulo.
"""

import os
import json
import time
import logging
import urllib.request
import urllib.parse
import urllib.error

logger = logging.getLogger("ml_auth")

TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
TOKEN_CACHE_PATH = os.path.join(DATA_DIR, "ml_token_cache.json")

# Margen de seguridad: renovar 5 min antes de que expire de verdad
SAFETY_MARGIN_SECONDS = 300


class MLTokenManager:
    def __init__(self):
        self.client_id = os.environ.get("ML_CLIENT_ID", "")
        self.client_secret = os.environ.get("ML_CLIENT_SECRET", "")
        self.refresh_token = os.environ.get("ML_REFRESH_TOKEN", "")
        self.access_token = None
        self.expires_at = 0  # timestamp unix
        self.last_error = None
        self._load_cache()

    # ── Persistencia local ──────────────────────────────────────────
    def _load_cache(self):
        """Si hubo un refresh reciente antes de un restart, reutilízalo
        en vez de pedir uno nuevo de inmediato (ahorra llamadas)."""
        try:
            with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.access_token = data.get("access_token")
            self.expires_at = data.get("expires_at", 0)
            # el refresh_token puede haber rotado en la última renovación
            if data.get("refresh_token"):
                self.refresh_token = data["refresh_token"]
            logger.info("Cache de token ML cargado desde %s", TOKEN_CACHE_PATH)
        except FileNotFoundError:
            logger.info("Sin cache previo de token ML, se pedirá uno nuevo")
        except Exception as e:
            logger.warning("No se pudo leer cache de token ML: %s", e)

    def _save_cache(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp_path = TOKEN_CACHE_PATH + ".tmp"
        data = {
            "access_token": self.access_token,
            "expires_at": self.expires_at,
            "refresh_token": self.refresh_token,
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, TOKEN_CACHE_PATH)  # escritura atómica

    # ── Lógica principal ─────────────────────────────────────────────
    def get_token(self) -> str | None:
        """Devuelve un access_token válido, renovándolo si hace falta.
        Devuelve None si no se pudo obtener (revisa self.last_error)."""
        now = time.time()
        if self.access_token and now < (self.expires_at - SAFETY_MARGIN_SECONDS):
            return self.access_token  # sigue vigente, no hace falta tocar nada

        return self._refresh()

    def _refresh(self) -> str | None:
        if not (self.client_id and self.client_secret and self.refresh_token):
            self.last_error = (
                "Faltan ML_CLIENT_ID / ML_CLIENT_SECRET / ML_REFRESH_TOKEN "
                "en las variables de entorno"
            )
            logger.error(self.last_error)
            return None

        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }).encode()

        req = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode(errors="replace")
            self.last_error = f"HTTP {e.code} al renovar token ML: {error_body}"
            logger.error(self.last_error)
            if e.code == 400:
                logger.error(
                    "El refresh_token probablemente fue revocado o expiró "
                    "(ML los invalida tras ~6 meses sin usarse, o si generas "
                    "un token nuevo manualmente desde el DevCenter). "
                    "Hay que repetir el flujo manual una vez y volver a "
                    "poner ML_REFRESH_TOKEN en Railway."
                )
            return None
        except Exception as e:
            self.last_error = f"Error de red al renovar token ML: {e}"
            logger.error(self.last_error)
            return None

        self.access_token = payload["access_token"]
        self.expires_at = time.time() + payload.get("expires_in", 21600)
        # ML a veces rota el refresh_token; si no viene uno nuevo, conserva el actual
        self.refresh_token = payload.get("refresh_token", self.refresh_token)
        self.last_error = None
        self._save_cache()

        logger.info(
            "Token ML renovado, válido por %s min",
            round(payload.get("expires_in", 21600) / 60),
        )
        return self.access_token

    def status_summary(self) -> str:
        """Para incluir en /status del bot."""
        if self.last_error:
            return f"⚠️ Error: {self.last_error}"
        if not self.access_token:
            return "⚠️ Sin token todavía"
        remaining = int(self.expires_at - time.time())
        if remaining <= 0:
            return "⚠️ Vencido, se renovará en el próximo uso"
        return f"✅ Válido por {remaining // 60} min más"


# Instancia única que importa bot_v6.py
ml_token_manager = MLTokenManager()
