# -*- coding: utf-8 -*-
"""
Servidor HTTP local para recibir URLs o texto desde movil o PC.

Inicia un mini servidor en la red local que sirve una pagina web
donde el usuario puede pegar una URL o escribir texto. Al enviar,
Kodi lo recibe y actua (reproduce la URL o usa el texto).

Incluye controles de reproduccion y estado en tiempo real
mediante JSON-RPC de Kodi.

Idea original de RubenSDFA1labernt

Visita el github https://github.com/loioloio para ver el código del addon completo, colaborar o usar la API.
"""
import json
import os
import re
import socket
import threading
import time as _time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.parse import quote
try:
    from http.server import ThreadingHTTPServer
except ImportError:
    # Python < 3.7 fallback
    from socketserver import ThreadingMixIn
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

_PORT_START = 8089
_PORT_END = 8099
_TIMEOUT_SECONDS = 300  # 5 minutos
_POLL_INTERVAL_MS = 500


def _get_local_ip():
    """Obtiene la IP local del dispositivo en la red."""
    try:
        ip = xbmc.getIPAddress()
        if ip and ip != "127.0.0.1" and not ip.startswith("0."):
            return ip
    except Exception:
        pass

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and ip != "127.0.0.1":
                return ip
        finally:
            s.close()
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
#  Historial sincronizado
# ---------------------------------------------------------------------------

_MAX_HISTORY = 50
_history_lock = threading.Lock()
_sse_connections = 0
_sse_lock = threading.Lock()
_MAX_SSE = 3

# --- Preview / oEmbed ---
_OEMBED_PROVIDERS = {
    "youtube": "https://www.youtube.com/oembed?url={url}&format=json",
    "dailymotion": "https://www.dailymotion.com/services/oembed?url={url}&format=json",
    "vimeo": "https://vimeo.com/api/oembed.json?url={url}",
}
_preview_cache = {}
_PREVIEW_CACHE_MAX = 20


def _history_path():
    """Devuelve la ruta absoluta del archivo de historial."""
    try:
        profile = xbmcaddon.Addon().getAddonInfo("profile")
        folder = xbmcvfs.translatePath(profile)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        return os.path.join(folder, "url_history.json")
    except Exception:
        return None


def _load_history():
    """Carga el historial desde disco. Devuelve lista de dicts."""
    path = _history_path()
    if not path or not os.path.isfile(path):
        return []
    try:
        with _history_lock:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_history(entries):
    """Guarda el historial en disco."""
    path = _history_path()
    if not path:
        return
    try:
        with _history_lock:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(entries[:_MAX_HISTORY], fh, ensure_ascii=False)
    except Exception:
        pass


def _add_to_history(url, url_type=""):
    """Anade una URL al inicio del historial."""
    entries = _load_history()
    entry = {
        "url": url,
        "type": url_type,
        "ts": int(_time.time()),
    }
    # Evitar duplicados consecutivos
    if entries and entries[0].get("url") == url:
        entries[0] = entry
    else:
        entries.insert(0, entry)
    _save_history(entries[:_MAX_HISTORY])


def _detect_platform(url):
    """Devuelve la plataforma oEmbed de la URL, o None."""
    if re.search(r'youtu\.?be|youtube', url, re.I):
        return "youtube"
    if re.search(r'dailymotion\.com|dai\.ly', url, re.I):
        return "dailymotion"
    if re.search(r'vimeo\.com', url, re.I):
        return "vimeo"
    return None


def _fetch_og_meta(url):
    """Descarga los primeros 50KB de HTML y extrae Open Graph meta tags."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Kodi)"})
        with urlopen(req, timeout=4) as resp:
            html = resp.read(51200).decode("utf-8", errors="ignore")
        og_title = re.search(
            r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)',
            html, re.I
        )
        og_image = re.search(
            r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)',
            html, re.I
        )
        og_site = re.search(
            r'<meta\s+(?:property|name)=["\']og:site_name["\']\s+content=["\']([^"\']+)',
            html, re.I
        )
        # Fallback al <title> si no hay og:title
        if not og_title:
            og_title = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
        title = og_title.group(1).strip() if og_title else ""
        thumb = og_image.group(1).strip() if og_image else ""
        site = og_site.group(1).strip() if og_site else ""
        if not title and not thumb:
            return None
        return {
            "title": title,
            "thumbnail": thumb,
            "author": "",
            "provider": site,
            "duration": None,
        }
    except Exception:
        return None


def _fetch_preview(url):
    """Obtiene metadatos de preview: oEmbed (YouTube) o Open Graph (resto)."""
    if url in _preview_cache:
        return _preview_cache[url]

    result = None

    # Intentar oEmbed para YouTube (mas rapido y fiable)
    platform = _detect_platform(url)
    if platform == "youtube":
        endpoint = _OEMBED_PROVIDERS[platform].format(url=quote(url, safe=""))
        try:
            req = Request(endpoint, headers={"User-Agent": "Kodi/EspaTV"})
            with urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = {
                "title": data.get("title", ""),
                "thumbnail": data.get("thumbnail_url", ""),
                "author": data.get("author_name", ""),
                "provider": data.get("provider_name", platform),
                "duration": data.get("duration"),
            }
        except Exception:
            pass

    # Fallback universal: Open Graph meta tags
    if not result and url.startswith(("http://", "https://")):
        result = _fetch_og_meta(url)

    if result:
        # Cache con eviccion simple
        if len(_preview_cache) >= _PREVIEW_CACHE_MAX:
            _preview_cache.pop(next(iter(_preview_cache)))
        _preview_cache[url] = result

    return result


def _kodi_rpc(method, params=None):
    """Ejecuta un comando JSON-RPC de Kodi y devuelve el resultado."""
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        request["params"] = params
    try:
        raw = xbmc.executeJSONRPC(json.dumps(request))
        return json.loads(raw).get("result")
    except Exception:
        return None


def _get_player_status():
    """Obtiene el estado actual del reproductor de Kodi."""
    players = _kodi_rpc("Player.GetActivePlayers")
    if not players:
        return {"playing": False}

    pid = players[0].get("playerid", 0)

    item = _kodi_rpc("Player.GetItem", {"playerid": pid, "properties": ["title"]})
    title = ""
    if item and item.get("item"):
        title = item["item"].get("title") or item["item"].get("label", "")

    props = _kodi_rpc("Player.GetProperties", {
        "playerid": pid,
        "properties": ["speed", "time", "totaltime"]
    })

    speed = 0
    time_str = ""
    total_str = ""
    seconds = 0
    totalseconds = 0
    if props:
        speed = props.get("speed", 0)
        t = props.get("time", {})
        tt = props.get("totaltime", {})
        time_str = "{0:02d}:{1:02d}:{2:02d}".format(
            t.get("hours", 0), t.get("minutes", 0), t.get("seconds", 0)
        )
        total_str = "{0:02d}:{1:02d}:{2:02d}".format(
            tt.get("hours", 0), tt.get("minutes", 0), tt.get("seconds", 0)
        )
        seconds = t.get("hours", 0) * 3600 + t.get("minutes", 0) * 60 + t.get("seconds", 0)
        totalseconds = tt.get("hours", 0) * 3600 + tt.get("minutes", 0) * 60 + tt.get("seconds", 0)

    vol_result = _kodi_rpc("Application.GetProperties", {"properties": ["volume", "muted"]})
    volume = 100
    muted = False
    if vol_result:
        volume = vol_result.get("volume", 100)
        muted = vol_result.get("muted", False)

    percentage = 0
    if totalseconds > 0:
        percentage = round((seconds / totalseconds) * 100, 1)

    return {
        "playing": True,
        "paused": speed == 0,
        "title": title,
        "time": time_str,
        "totaltime": total_str,
        "seconds": seconds,
        "totalseconds": totalseconds,
        "percentage": percentage,
        "volume": volume,
        "muted": muted,
    }


class _RemoteHandler(BaseHTTPRequestHandler):
    """Maneja peticiones GET (pagina, estado) y POST (URL, controles)."""

    def log_message(self, fmt, *args):
        """Redirige logs del servidor HTTP al log de Kodi."""
        xbmc.log("[EspaTV/remote] {0}".format(fmt % args), xbmc.LOGDEBUG)

    def do_GET(self):
        if self.path == "/" or self.path == "":
            self._serve_html()
        elif self.path == "/status":
            self._serve_status()
        elif self.path == "/events":
            self._serve_sse()
        elif self.path == "/history":
            self._serve_history()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/send":
            self._handle_send()
        elif self.path == "/control":
            self._handle_control()
        elif self.path == "/toggle-keep-alive":
            self._handle_toggle_keep_alive()
        elif self.path == "/history/clear":
            self._handle_history_clear()
        elif self.path == "/preview":
            self._handle_preview()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(self.server.html_page.encode("utf-8"))

    def _serve_status(self):
        status = _get_player_status()
        self._json_response(200, status)

    def _handle_send(self):
        data = self._read_json_body()
        if data is None:
            return

        url = (data.get("url") or data.get("text") or "").strip()
        if not url:
            self._json_response(400, {"error": "Campo vacio"})
            return

        if self.server.mode == "url":
            valid_schemes = ("http://", "https://", "rtmp://", "rtsp://",
                            "magnet:?", "acestream://")
            if not url.startswith(valid_schemes):
                self._json_response(400, {"error": "URL no valida. Esquemas soportados: http, magnet, acestream"})
                return

        self.server.received_url = url
        # Guardar en historial sincronizado
        if self.server.mode == "url":
            _add_to_history(url)
        if self.server.keep_alive and self.server.mode == "url":
            msg = "URL recibida. Reproduciendo en Kodi..."
            self._json_response(200, {"ok": True, "message": msg, "keepAlive": True})
            import url_player
            threading.Thread(
                target=url_player.play_url_action,
                args=(url,),
                daemon=True,
            ).start()
        else:
            self.server.url_event.set()
            msg = "URL recibida. Reproduciendo en Kodi..." if self.server.mode == "url" else "Texto recibido."
            self._json_response(200, {"ok": True, "message": msg})

    def _handle_control(self):
        data = self._read_json_body()
        if data is None:
            return

        action = data.get("action", "")
        result = False

        if action == "playpause":
            players = _kodi_rpc("Player.GetActivePlayers")
            if players:
                pid = players[0].get("playerid", 0)
                _kodi_rpc("Player.PlayPause", {"playerid": pid})
                result = True

        elif action == "stop":
            players = _kodi_rpc("Player.GetActivePlayers")
            if players:
                pid = players[0].get("playerid", 0)
                _kodi_rpc("Player.Stop", {"playerid": pid})
                result = True

        elif action == "volup":
            vol = _kodi_rpc("Application.GetProperties", {"properties": ["volume"]})
            if vol:
                new_vol = min(100, vol.get("volume", 50) + 5)
                _kodi_rpc("Application.SetVolume", {"volume": new_vol})
                result = True

        elif action == "voldown":
            vol = _kodi_rpc("Application.GetProperties", {"properties": ["volume"]})
            if vol:
                new_vol = max(0, vol.get("volume", 50) - 5)
                _kodi_rpc("Application.SetVolume", {"volume": new_vol})
                result = True

        elif action == "mute":
            _kodi_rpc("Application.SetMute", {"mute": "toggle"})
            result = True

        elif action == "seek":
            value = data.get("value")
            if value is not None:
                players = _kodi_rpc("Player.GetActivePlayers")
                if players:
                    pid = players[0].get("playerid", 0)
                    pct = max(0, min(100, float(value)))
                    _kodi_rpc("Player.Seek", {
                        "playerid": pid,
                        "value": {"percentage": pct},
                    })
                    result = True

        if result:
            self._json_response(200, {"ok": True})
        else:
            self._json_response(400, {"error": "Accion no reconocida o sin reproductor activo"})

    def _read_json_body(self):
        """Lee y parsea el body JSON. Devuelve dict o None si error."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0 or content_length > 4096:
            self._json_response(400, {"error": "Datos no validos"})
            return None

        body = self.rfile.read(content_length)
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json_response(400, {"error": "JSON no valido"})
            return None

    def _json_response(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _handle_toggle_keep_alive(self):
        self.server.keep_alive = not self.server.keep_alive
        self._json_response(200, {
            "ok": True,
            "keepAlive": self.server.keep_alive,
        })

    def _serve_sse(self):
        """Sirve Server-Sent Events con estado del reproductor."""
        global _sse_connections
        with _sse_lock:
            if _sse_connections >= _MAX_SSE:
                self._json_response(429, {"error": "Demasiadas conexiones SSE"})
                return
            _sse_connections += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            while True:
                status = _get_player_status()
                payload = "data: " + json.dumps(status) + "\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
                _time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sse_lock:
                _sse_connections -= 1

    def _serve_history(self):
        entries = _load_history()
        self._json_response(200, {"history": entries})

    def _handle_history_clear(self):
        _save_history([])
        self._json_response(200, {"ok": True})

    def _handle_preview(self):
        """Devuelve metadatos oEmbed de una URL."""
        data = self._read_json_body()
        if data is None:
            return
        url = (data.get("url") or "").strip()
        if not url:
            self._json_response(400, {"error": "URL vacia"})
            return
        meta = _fetch_preview(url)
        if meta:
            self._json_response(200, {"ok": True, "preview": meta})
        else:
            # Sin oEmbed: devolver solo el tipo detectado
            platform = _detect_platform(url)
            self._json_response(200, {"ok": True, "preview": None, "platform": platform})


class _RemoteServer(ThreadingHTTPServer):
    """HTTPServer multihilo con atributos extra para comunicacion entre threads."""

    daemon_threads = True

    def __init__(self, address, handler_class, mode="url"):
        self.received_url = None
        self.url_event = threading.Event()
        self.mode = mode
        self.keep_alive = False
        self.html_page = _HTML_URL if mode == "url" else _HTML_TEXT
        ThreadingHTTPServer.__init__(self, address, handler_class)


def _try_start_server(mode="url"):
    """Intenta iniciar el servidor en los puertos disponibles."""
    for port in range(_PORT_START, _PORT_END + 1):
        try:
            server = _RemoteServer(("", port), _RemoteHandler, mode)
            return server, port
        except OSError:
            continue
    return None, 0


def _run_server(mode, title, wait_msg):
    """Ciclo comun: inicia servidor, muestra dialogo, espera datos.

    Devuelve el texto/URL recibido o None si se cancelo o expiro.
    """
    ip = _get_local_ip()
    if not ip:
        xbmcgui.Dialog().ok(
            "EspaTV",
            "No se pudo detectar la IP local.\n\n"
            "Asegurate de estar conectado a una red WiFi o Ethernet."
        )
        return None

    server, port = _try_start_server(mode)
    if not server:
        xbmcgui.Dialog().ok(
            "EspaTV",
            "No se pudo iniciar el servidor.\n\n"
            "Los puertos {0}-{1} estan ocupados.".format(_PORT_START, _PORT_END)
        )
        return None

    addr = "http://{0}:{1}".format(ip, port)
    xbmc.log("[EspaTV/remote] Servidor iniciado en {0} (modo: {1})".format(addr, mode), xbmc.LOGINFO)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    pdp = xbmcgui.DialogProgress()
    pdp.create(
        title,
        "Abre esta direccion en tu navegador:\n\n"
        "[B]{0}[/B]\n\n"
        "{1}".format(addr, wait_msg)
    )

    received = None
    elapsed = 0

    while elapsed < _TIMEOUT_SECONDS:
        if pdp.iscanceled():
            break

        if server.url_event.wait(timeout=_POLL_INTERVAL_MS / 1000.0):
            received = server.received_url
            break

        elapsed += _POLL_INTERVAL_MS / 1000.0
        remaining = int(_TIMEOUT_SECONDS - elapsed)
        progress = int((elapsed / _TIMEOUT_SECONDS) * 100)
        pdp.update(
            progress,
            "Abre esta direccion en tu navegador:\n\n"
            "[B]{0}[/B]\n\n"
            "{1} ({2}s)".format(addr, wait_msg, remaining)
        )

    pdp.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)

    if elapsed >= _TIMEOUT_SECONDS and not received:
        xbmcgui.Dialog().notification(
            "EspaTV", "Tiempo agotado",
            xbmcgui.NOTIFICATION_WARNING, 2000
        )

    return received


def start_remote():
    """Punto de entrada URL: inicia servidor, espera URL y la reproduce."""
    received = _run_server("url", "Enviar URL desde movil/PC", "Esperando URL...")
    if received:
        xbmcgui.Dialog().notification(
            "EspaTV", "URL recibida, reproduciendo...",
            xbmcgui.NOTIFICATION_INFO, 2000
        )
        import url_player
        url_player.play_url_action(received)


def receive_text(title="Escribir desde movil/PC"):
    """Punto de entrada texto: inicia servidor, espera texto y lo devuelve."""
    received = _run_server("text", title, "Esperando texto...")
    if received:
        xbmcgui.Dialog().notification(
            "EspaTV", "Texto recibido",
            xbmcgui.NOTIFICATION_INFO, 1500
        )
    return received


# ---------------------------------------------------------------------------
#  HTML embebido — pagina URL
# ---------------------------------------------------------------------------

_HTML_URL = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>EspaTV Remote</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#0d1117;color:#c9d1d9;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:16px;
}
.card{
  background:#161b22;border:1px solid #30363d;border-radius:12px;
  padding:32px 24px;max-width:520px;width:100%;
  box-shadow:0 8px 32px rgba(0,0,0,.4);
}
h1{
  font-size:1.4em;text-align:center;margin-bottom:4px;
  color:#58a6ff;font-weight:600;
}
.sub{
  text-align:center;color:#8b949e;font-size:.85em;margin-bottom:6px;
}
.status-dot{
  display:inline-block;width:8px;height:8px;border-radius:50%;
  background:#484f58;margin-right:6px;vertical-align:middle;
  transition:background .3s;
}
.status-dot.on{background:#3fb950}
.status-bar{
  text-align:center;font-size:.75em;color:#8b949e;margin-bottom:20px;
  min-height:16px;
}
label{
  display:block;font-size:.9em;color:#8b949e;margin-bottom:6px;
}
.badge{
  display:inline-block;font-size:.7em;padding:2px 8px;border-radius:10px;
  margin-left:8px;vertical-align:middle;font-weight:600;
  background:#21262d;color:#8b949e;transition:all .3s;
}
.badge.youtube{background:#2d1117;color:#f85149}
.badge.dailymotion{background:#1a1d2e;color:#58a6ff}
.badge.stream{background:#0d2818;color:#3fb950}
.badge.torrent{background:#2d1f0d;color:#d29922}
.badge.acestream{background:#0d1f2d;color:#39d2e5}
.badge.twitch{background:#24163a;color:#a970ff}
.badge.vimeo{background:#1a2d2e;color:#1ab7ea}
.drop-zone{
  position:relative;
  width:100%;min-height:80px;padding:12px;
  background:#0d1117;color:#c9d1d9;
  border:1px solid #30363d;border-radius:8px;
  font-size:16px;font-family:monospace;
  resize:vertical;outline:none;
  transition:border-color .2s;
}
.drop-zone:focus{border-color:#58a6ff}
.drop-zone::placeholder{color:#484f58}
.drop-zone.dragover{
  border-color:#58a6ff;border-style:dashed;
  background:rgba(88,166,255,.05);
}
.btn{
  display:block;width:100%;padding:14px;margin-top:16px;
  background:#238636;color:#fff;
  border:none;border-radius:8px;
  font-size:1em;font-weight:600;cursor:pointer;
  transition:background .2s;
}
.btn:hover{background:#2ea043}
.btn:active{background:#238636;transform:scale(.98)}
.btn:disabled{background:#21262d;color:#484f58;cursor:not-allowed}
.msg{
  margin-top:14px;padding:10px 14px;border-radius:8px;
  font-size:.9em;text-align:center;display:none;
}
.msg.ok{display:flex;align-items:center;justify-content:center;gap:8px;background:#0d2818;color:#3fb950;border:1px solid #238636}
.msg.err{display:block;background:#2d1117;color:#f85149;border:1px solid #da3633}
.check-svg{width:20px;height:20px;flex-shrink:0}
.check-svg circle{stroke:#3fb950;stroke-width:2;fill:none;stroke-dasharray:52;stroke-dashoffset:52;animation:circ .4s ease forwards}
.check-svg path{stroke:#3fb950;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:20;stroke-dashoffset:20;animation:tick .3s .35s ease forwards}
@keyframes circ{to{stroke-dashoffset:0}}
@keyframes tick{to{stroke-dashoffset:0}}

/* --- Controles --- */
.controls{
  display:flex;align-items:center;justify-content:center;gap:6px;
  margin-top:18px;padding-top:16px;border-top:1px solid #21262d;
}
.ctrl-btn{
  width:40px;height:40px;border-radius:8px;border:1px solid #30363d;
  background:#0d1117;color:#c9d1d9;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  font-size:1.1em;transition:background .15s,border-color .15s;
}
.ctrl-btn:hover{background:#161b22;border-color:#58a6ff}
.ctrl-btn:active{transform:scale(.93)}
.ctrl-btn svg{width:18px;height:18px;fill:#c9d1d9}
.vol-label{font-size:.7em;color:#8b949e;min-width:32px;text-align:center}
.now-playing{
  text-align:center;font-size:.78em;color:#8b949e;margin-top:8px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  max-width:100%;min-height:16px;
}

/* --- Barra de progreso --- */
.progress-container{
  margin-top:8px;padding:0 4px;display:none;
}
.progress-container.visible{display:block}
.progress-times{
  display:flex;justify-content:space-between;
  font-size:.7em;color:#484f58;margin-bottom:4px;font-variant-numeric:tabular-nums;
}
.progress-bar{
  -webkit-appearance:none;appearance:none;width:100%;height:6px;
  background:#21262d;border-radius:3px;outline:none;cursor:pointer;
}
.progress-bar::-webkit-slider-thumb{
  -webkit-appearance:none;appearance:none;
  width:14px;height:14px;border-radius:50%;
  background:#58a6ff;cursor:pointer;
  border:2px solid #0d1117;
  margin-top:-4px;
}
.progress-bar::-moz-range-thumb{
  width:14px;height:14px;border-radius:50%;
  background:#58a6ff;cursor:pointer;
  border:2px solid #0d1117;
}
.progress-bar::-webkit-slider-runnable-track{
  height:6px;border-radius:3px;
}
.progress-bar::-moz-range-track{
  height:6px;border-radius:3px;background:#21262d;
}

/* --- Preview card --- */
.preview-card{
  display:none;margin-top:10px;padding:10px;border-radius:8px;
  background:#0d1117;border:1px solid #30363d;
  gap:10px;align-items:center;
}
.preview-card.visible{display:flex}
.preview-card.loading{display:flex}
.preview-thumb{
  width:100px;min-width:100px;height:56px;border-radius:4px;
  object-fit:cover;background:#21262d;
}
.preview-info{flex:1;overflow:hidden}
.preview-title{
  font-size:.82em;color:#c9d1d9;font-weight:600;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.preview-author{
  font-size:.72em;color:#8b949e;margin-top:2px;
}
.preview-skeleton{
  background:linear-gradient(90deg,#21262d 25%,#30363d 50%,#21262d 75%);
  background-size:400% 100%;animation:shimmer 1.5s infinite;
  border-radius:4px;
}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
.preview-skeleton-thumb{width:100px;min-width:100px;height:56px}
.preview-skeleton-text{height:12px;width:70%;margin-bottom:6px}
.preview-skeleton-text2{height:10px;width:45%}

/* --- Historial --- */
.history{margin-top:18px;padding-top:14px;border-top:1px solid #21262d}
.history-header{
  display:flex;align-items:center;justify-content:space-between;
  cursor:pointer;user-select:none;
}
.history-header span{font-size:.85em;color:#8b949e}
.history-header .arrow{
  display:inline-block;transition:transform .2s;font-size:.7em;color:#484f58;
}
.history-header .arrow.open{transform:rotate(90deg)}
.history-list{
  list-style:none;margin-top:8px;max-height:200px;overflow-y:auto;
}
.history-list.hidden{display:none}
.history-item{
  padding:8px 10px;margin-bottom:4px;border-radius:6px;
  font-size:.82em;font-family:monospace;color:#c9d1d9;
  background:#0d1117;border:1px solid transparent;
  cursor:pointer;transition:border-color .15s;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.history-item:hover{border-color:#30363d}
.clear-btn{
  background:none;border:none;color:#484f58;font-size:.75em;
  cursor:pointer;padding:2px 6px;
}
.clear-btn:hover{color:#f85149}

.footer{
  text-align:center;color:#484f58;font-size:.75em;margin-top:20px;
}
.keep-alive-btn{
  display:block;margin:8px auto 0;padding:6px 16px;
  background:none;border:1px solid #30363d;border-radius:6px;
  color:#8b949e;font-size:.72em;cursor:pointer;
  transition:all .2s;
}
.keep-alive-btn:hover{border-color:#58a6ff;color:#58a6ff}
.keep-alive-btn.active{border-color:#238636;color:#3fb950;background:#0d2818}
.github-link{
  display:block;text-align:center;margin-top:8px;
  color:#484f58;font-size:.75em;text-decoration:none;
  transition:color .2s;
}
.github-link:hover{color:#58a6ff}

/* --- Tipos compatibles --- */
.compat{margin-top:2px;margin-bottom:2px}
.compat summary{
  font-size:.78em;color:#58a6ff;cursor:pointer;text-align:center;
  list-style:none;user-select:none;
}
.compat summary::-webkit-details-marker{display:none}
.compat-list{
  margin-top:8px;display:grid;grid-template-columns:1fr 1fr;
  gap:4px 12px;font-size:.72em;color:#8b949e;
}
.compat-item{display:flex;align-items:center;gap:6px;padding:3px 0}
.compat-item .badge{margin-left:0;font-size:.9em}
</style>
</head>
<body>
<div class="card">
  <h1>EspaTV Remote</h1>
  <p class="sub">Pega una URL para reproducirla en Kodi</p>

  <details class="compat" id="compat-details">
    <summary onclick="var a=document.getElementById('compat-arrow');a.innerHTML=this.parentElement.open?'&#9654;':'&#9660;'"><span id="compat-arrow">&#9654;</span> Enlaces compatibles</summary>
    <div class="compat-list">
      <div class="compat-item"><span class="badge youtube">YouTube</span> youtube.com, youtu.be</div>
      <div class="compat-item"><span class="badge dailymotion">Dailymotion</span> dailymotion.com</div>
      <div class="compat-item"><span class="badge twitch">Twitch</span> twitch.tv</div>
      <div class="compat-item"><span class="badge vimeo">Vimeo</span> vimeo.com</div>
      <div class="compat-item"><span class="badge stream">Stream</span> .m3u8, .mp4, .mpd</div>
      <div class="compat-item"><span class="badge torrent">Torrent</span> magnet:, .torrent</div>
      <div class="compat-item"><span class="badge acestream">AceStream</span> acestream://</div>
      <div class="compat-item"><span class="badge">Web</span> cualquier URL</div>
    </div>
  </details>

  <div class="status-bar">
    <span class="status-dot" id="dot"></span>
    <span id="status-text">Conectando...</span>
  </div>

  <label for="url">URL del video <span class="badge" id="badge"></span></label>
  <textarea class="drop-zone" id="url" placeholder="https://www.dailymotion.com/video/..." autofocus></textarea>
  <div class="preview-card" id="preview-card">
    <img class="preview-thumb" id="preview-thumb" src="" alt="">
    <div class="preview-info">
      <div class="preview-title" id="preview-title"></div>
      <div class="preview-author" id="preview-author"></div>
    </div>
  </div>
  <button class="btn" id="send" onclick="enviar()">Enviar a Kodi</button>
  <div class="msg" id="msg"></div>

  <div class="controls" id="controls">
    <button class="ctrl-btn" onclick="ctrl('voldown')" title="Vol -">
      <svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3z"/></svg>
    </button>
    <span class="vol-label" id="vol">--</span>
    <button class="ctrl-btn" onclick="ctrl('volup')" title="Vol +">
      <svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0012 7.5v9a4.5 4.5 0 004.5-4.5z"/></svg>
    </button>
    <div style="width:12px"></div>
    <button class="ctrl-btn" onclick="ctrl('playpause')" title="Play / Pausa" id="pp-btn">
      <svg viewBox="0 0 24 24" id="pp-icon"><polygon points="5,3 19,12 5,21"/></svg>
    </button>
    <button class="ctrl-btn" onclick="ctrl('stop')" title="Stop">
      <svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>
    </button>
  </div>
  <div class="now-playing" id="np"></div>
  <div class="progress-container" id="progress-container">
    <div class="progress-times">
      <span id="p-cur">00:00:00</span>
      <span id="p-tot">00:00:00</span>
    </div>
    <input type="range" class="progress-bar" id="progress-bar" min="0" max="100" step="0.1" value="0"
      oninput="this.dataset.seeking='1'" onchange="seekTo(this.value);this.dataset.seeking=''">
  </div>

  <div class="history" id="history-section">
    <div class="history-header" onclick="toggleHistory()">
      <span><span class="arrow" id="arrow">&#9654;</span> Historial</span>
      <button class="clear-btn" onclick="event.stopPropagation();clearHistory()">Limpiar</button>
    </div>
    <ul class="history-list hidden" id="history-list"></ul>
  </div>

  <p class="footer" id="footer-msg">El servidor se cerrara automaticamente tras recibir la URL.</p>
  <button class="keep-alive-btn" id="keep-alive-btn" onclick="toggleKeepAlive()">Mantener servidor abierto</button>
  <a class="github-link" href="https://github.com/loioloio" target="_blank">Sistema m\u00e1s completo</a>
  <p style="text-align:center;color:#484f58;font-size:.7em;margin-top:4px">Idea original: rubenSDFA1labernt</p>
</div>
<script>
/* --- Deteccion de tipo --- */
var TYPE_MAP=[
  [/youtu\\.?be|youtube/i,'YouTube','youtube'],
  [/dailymotion|dai\\.ly/i,'Dailymotion','dailymotion'],
  [/twitch\\.tv/i,'Twitch','twitch'],
  [/vimeo\\.com/i,'Vimeo','vimeo'],
  [/^magnet:\\?|\\.torrent(\\?|$)/i,'Torrent','torrent'],
  [/^acestream:\\/\\//i,'AceStream','acestream'],
  [/\\.m3u8|\\.mpd|\\.mp4|\\.mkv|\\.avi|\\.ts|\\.flv|\\.webm|\\.mov/i,'Stream','stream']
];
function detectType(u){
  for(var i=0;i<TYPE_MAP.length;i++){
    if(TYPE_MAP[i][0].test(u)) return {label:TYPE_MAP[i][1],cls:TYPE_MAP[i][2]};
  }
  return u?{label:'Web',cls:''}:null;
}
var urlEl=document.getElementById('url');
var badgeEl=document.getElementById('badge');
function updateBadge(){
  var t=detectType(urlEl.value.trim());
  if(t){badgeEl.textContent=t.label;badgeEl.className='badge '+t.cls;}
  else{badgeEl.textContent='';badgeEl.className='badge';}
}
urlEl.addEventListener('input',updateBadge);

/* --- Drag & drop --- */
urlEl.addEventListener('dragover',function(e){
  e.preventDefault();urlEl.classList.add('dragover');
});
urlEl.addEventListener('dragleave',function(){
  urlEl.classList.remove('dragover');
});
urlEl.addEventListener('drop',function(e){
  e.preventDefault();urlEl.classList.remove('dragover');
  var text=e.dataTransfer.getData('text/uri-list')||e.dataTransfer.getData('text/plain')||'';
  if(text){urlEl.value=text.split('\\n')[0].trim();updateBadge();}
});

/* --- Auto-paste --- */
(function(){
  var done=false;
  urlEl.addEventListener('focus',function(){
    if(done||urlEl.value.trim()) return;
    done=true;
    if(navigator.clipboard&&navigator.clipboard.readText){
      navigator.clipboard.readText().then(function(t){
        t=t.trim();
        if(t&&/^(https?:\\/\\/|magnet:\\?|acestream:\\/\\/)/i.test(t)&&!urlEl.value.trim()){
          urlEl.value=t;updateBadge();
        }
      }).catch(function(){});
    }
  });
})();

/* --- Historial (sincronizado con servidor) --- */
function fetchHistory(){
  fetch('/history').then(function(r){return r.json()}).then(function(d){
    renderHistory(d.history||[]);
  }).catch(function(){renderHistory([]);});
}
function renderHistory(h){
  var list=document.getElementById('history-list');
  list.innerHTML='';
  if(!h.length){
    document.getElementById('history-section').style.display='none';
    return;
  }
  document.getElementById('history-section').style.display='';
  h.forEach(function(item){
    var u=typeof item==='string'?item:item.url;
    var li=document.createElement('li');
    li.className='history-item';li.textContent=u;li.title=u;
    li.onclick=function(){urlEl.value=u;updateBadge();urlEl.focus();};
    list.appendChild(li);
  });
}
function toggleHistory(){
  var list=document.getElementById('history-list');
  var arrow=document.getElementById('arrow');
  var hidden=list.classList.toggle('hidden');
  arrow.classList.toggle('open',!hidden);
}
function clearHistory(){
  fetch('/history/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
  .then(function(){fetchHistory();});
}
fetchHistory();

/* --- Keep-alive --- */
var keepAlive=false;
function toggleKeepAlive(){
  fetch('/toggle-keep-alive',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
  .then(function(r){return r.json();})
  .then(function(d){
    keepAlive=d.keepAlive;
    var btn=document.getElementById('keep-alive-btn');
    var footer=document.getElementById('footer-msg');
    if(keepAlive){
      btn.textContent='Servidor fijado (click para cerrar tras envio)';
      btn.classList.add('active');
      footer.textContent='El servidor permanecera abierto. Puedes enviar varias URLs.';
    }else{
      btn.textContent='Mantener servidor abierto';
      btn.classList.remove('active');
      footer.textContent='El servidor se cerrara automaticamente tras recibir la URL.';
    }
  });
}

/* --- Enviar --- */
function checkSVG(){
  return '<svg class="check-svg" viewBox="0 0 24 24">'
    +'<circle cx="12" cy="12" r="10"/>'
    +'<path d="M7 12.5l3 3 7-7"/></svg>';
}
function enviar(){
  var url=urlEl.value.trim();
  var msg=document.getElementById('msg');
  var btn=document.getElementById('send');
  msg.className='msg';msg.style.display='none';
  if(!url){
    msg.className='msg err';msg.textContent='Escribe una URL';msg.style.display='block';
    return;
  }
  if(!/^(https?|rtmp|rtsp):\\/\\//i.test(url)&&!/^magnet:\\?/i.test(url)&&!/^acestream:\\/\\//i.test(url)){
    msg.className='msg err';msg.textContent='Esquemas soportados: http, magnet, acestream';msg.style.display='block';
    return;
  }
  btn.disabled=true;btn.textContent='Enviando...';
  fetch('/send',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:url})
  })
  .then(function(r){return r.json()})
  .then(function(d){
    if(d.ok){
      msg.className='msg ok';
      msg.innerHTML=checkSVG()+'<span>URL enviada a Kodi</span>';
      btn.textContent='Enviado';
      fetchHistory();
    }else{
      msg.className='msg err';
      msg.textContent=d.error||'Error desconocido';
      btn.disabled=false;btn.textContent='Enviar a Kodi';
    }
    msg.style.display='';
  })
  .catch(function(){
    msg.className='msg err';
    msg.textContent='Error de conexion con Kodi';
    msg.style.display='block';
    btn.disabled=false;btn.textContent='Enviar a Kodi';
  });
}
urlEl.addEventListener('keydown',function(e){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();enviar()}
});

/* --- Controles remotos --- */
function ctrl(action){
  fetch('/control',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:action})
  }).catch(function(){});
  if(action==='playpause'||action==='stop') setTimeout(pollStatus,300);
}

/* --- Preview oEmbed --- */
var _previewTimer=null;
var _previewAbort=null;
function requestPreview(url){
  var card=document.getElementById('preview-card');
  var thumb=document.getElementById('preview-thumb');
  var ptitle=document.getElementById('preview-title');
  var pauthor=document.getElementById('preview-author');
  if(!url||url.length<10){
    card.className='preview-card';return;
  }
  // Skeleton loader
  card.className='preview-card loading visible';
  thumb.src='';thumb.className='preview-thumb preview-skeleton preview-skeleton-thumb';
  ptitle.textContent='';ptitle.className='preview-title preview-skeleton preview-skeleton-text';
  ptitle.innerHTML='\u00a0';
  pauthor.textContent='';pauthor.className='preview-author preview-skeleton preview-skeleton-text2';
  pauthor.innerHTML='\u00a0';
  // Abort previous
  if(_previewAbort)try{_previewAbort.abort();}catch(e){}
  _previewAbort=new AbortController();
  fetch('/preview',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:url}),signal:_previewAbort.signal
  }).then(function(r){return r.json()}).then(function(d){
    if(d.ok&&d.preview){
      thumb.src=d.preview.thumbnail||'';
      thumb.className='preview-thumb';
      ptitle.textContent=d.preview.title||'';
      ptitle.className='preview-title';
      pauthor.textContent=d.preview.author?(d.preview.author+' \u2022 '+d.preview.provider):(d.preview.provider||'');
      pauthor.className='preview-author';
      card.className='preview-card visible';
    }else{
      card.className='preview-card';
    }
  }).catch(function(){
    card.className='preview-card';
  });
}
function debouncePreview(){
  clearTimeout(_previewTimer);
  _previewTimer=setTimeout(function(){
    var url=document.getElementById('url').value.trim();
    if(/^https?:\/\//i.test(url)) requestPreview(url);
    else document.getElementById('preview-card').className='preview-card';
  },400);
}
urlEl.addEventListener('input',debouncePreview);
urlEl.addEventListener('paste',function(){setTimeout(debouncePreview,50);});

/* --- Estado (SSE con fallback a polling) --- */
var ppIcon=document.getElementById('pp-icon');
var PLAY_PATH='<polygon points="5,3 19,12 5,21"/>';
var PAUSE_PATH='<rect x="5" y="4" width="4" height="16" rx="1"/><rect x="15" y="4" width="4" height="16" rx="1"/>';
var _pollTimer=null;
function updateStatus(s){
  var dot=document.getElementById('dot');
  var stxt=document.getElementById('status-text');
  var np=document.getElementById('np');
  var vol=document.getElementById('vol');
  var pC=document.getElementById('progress-container');
  var pBar=document.getElementById('progress-bar');
  var pCur=document.getElementById('p-cur');
  var pTot=document.getElementById('p-tot');
  dot.classList.add('on');
  if(s.playing){
    stxt.textContent=s.paused?'Pausado':'Reproduciendo';
    np.textContent=s.title||'';
    ppIcon.innerHTML=s.paused?PLAY_PATH:PAUSE_PATH;
    pC.classList.add('visible');
    if(!pBar.dataset.seeking){pBar.value=s.percentage||0;}
    pCur.textContent=s.time||'00:00:00';
    pTot.textContent=s.totaltime||'00:00:00';
  }else{
    stxt.textContent='Sin reproduccion';
    np.textContent='';
    ppIcon.innerHTML=PLAY_PATH;
    pC.classList.remove('visible');
  }
  var v=s.volume;vol.textContent=(v!=null)?(s.muted?'\U0001f507':v+'%'):'--';
}
function pollStatus(){
  fetch('/status').then(function(r){return r.json()}).then(updateStatus)
  .catch(function(){
    document.getElementById('dot').classList.remove('on');
    document.getElementById('status-text').textContent='Sin conexion';
  });
}
function startSSE(){
  if(typeof EventSource==='undefined'){startPolling();return;}
  var es=new EventSource('/events');
  es.onmessage=function(e){
    try{updateStatus(JSON.parse(e.data));}catch(err){}
  };
  es.onerror=function(){
    es.close();
    startPolling();
  };
}
function startPolling(){
  pollStatus();
  _pollTimer=setInterval(pollStatus,3000);
}
function seekTo(pct){
  fetch('/control',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'seek',value:parseFloat(pct)})
  });
}
startSSE();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
#  HTML embebido — pagina texto
# ---------------------------------------------------------------------------

_HTML_TEXT = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>EspaTV Remote</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#0d1117;color:#c9d1d9;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:16px;
}
.card{
  background:#161b22;border:1px solid #30363d;border-radius:12px;
  padding:32px 24px;max-width:480px;width:100%;
  box-shadow:0 8px 32px rgba(0,0,0,.4);
}
h1{
  font-size:1.4em;text-align:center;margin-bottom:6px;
  color:#58a6ff;font-weight:600;
}
.sub{
  text-align:center;color:#8b949e;font-size:.85em;margin-bottom:24px;
}
label{
  display:block;font-size:.9em;color:#8b949e;margin-bottom:6px;
}
input[type=text]{
  width:100%;padding:14px 12px;
  background:#0d1117;color:#c9d1d9;
  border:1px solid #30363d;border-radius:8px;
  font-size:16px;outline:none;
  transition:border-color .2s;
}
input[type=text]:focus{border-color:#58a6ff}
input[type=text]::placeholder{color:#484f58}
.btn{
  display:block;width:100%;padding:14px;margin-top:16px;
  background:#238636;color:#fff;
  border:none;border-radius:8px;
  font-size:1em;font-weight:600;cursor:pointer;
  transition:background .2s;
}
.btn:hover{background:#2ea043}
.btn:active{background:#238636;transform:scale(.98)}
.btn:disabled{background:#21262d;color:#484f58;cursor:not-allowed}
.msg{
  margin-top:14px;padding:10px 14px;border-radius:8px;
  font-size:.9em;text-align:center;display:none;
}
.msg.ok{display:flex;align-items:center;justify-content:center;gap:8px;background:#0d2818;color:#3fb950;border:1px solid #238636}
.msg.err{display:block;background:#2d1117;color:#f85149;border:1px solid #da3633}
.check-svg{width:20px;height:20px;flex-shrink:0}
.check-svg circle{stroke:#3fb950;stroke-width:2;fill:none;stroke-dasharray:52;stroke-dashoffset:52;animation:circ .4s ease forwards}
.check-svg path{stroke:#3fb950;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:20;stroke-dashoffset:20;animation:tick .3s .35s ease forwards}
@keyframes circ{to{stroke-dashoffset:0}}
@keyframes tick{to{stroke-dashoffset:0}}
.footer{
  text-align:center;color:#484f58;font-size:.75em;margin-top:20px;
}
.github-link{
  display:block;text-align:center;margin-top:8px;
  color:#484f58;font-size:.75em;text-decoration:none;
  transition:color .2s;
}
.github-link:hover{color:#58a6ff}
</style>
</head>
<body>
<div class="card">
  <h1>EspaTV Remote</h1>
  <p class="sub">Escribe texto para enviarlo a Kodi</p>
  <label for="txt">Texto</label>
  <input type="text" id="txt" placeholder="Escribe aqui..." autofocus>
  <button class="btn" id="send" onclick="enviar()">Enviar a Kodi</button>
  <div class="msg" id="msg"></div>
  <p class="footer">El servidor se cerrara automaticamente tras recibir el texto.</p>
  <a class="github-link" href="https://github.com/loioloio" target="_blank">Sistema m\u00e1s completo</a>
  <p style="text-align:center;color:#484f58;font-size:.7em;margin-top:4px">Idea original: rubenSDFA1labernt</p>
</div>
<script>
function checkSVG(){
  return '<svg class="check-svg" viewBox="0 0 24 24">'
    +'<circle cx="12" cy="12" r="10"/>'
    +'<path d="M7 12.5l3 3 7-7"/></svg>';
}
function enviar(){
  var txt=document.getElementById('txt').value.trim();
  var msg=document.getElementById('msg');
  var btn=document.getElementById('send');
  msg.className='msg';msg.style.display='none';
  if(!txt){
    msg.className='msg err';msg.textContent='Escribe algo';msg.style.display='block';
    return;
  }
  btn.disabled=true;btn.textContent='Enviando...';
  fetch('/send',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:txt})
  })
  .then(function(r){return r.json()})
  .then(function(d){
    if(d.ok){
      msg.className='msg ok';
      msg.innerHTML=checkSVG()+'<span>Texto enviado a Kodi</span>';
      btn.textContent='Enviado';
    }else{
      msg.className='msg err';
      msg.textContent=d.error||'Error desconocido';
      btn.disabled=false;btn.textContent='Enviar a Kodi';
    }
    msg.style.display='';
  })
  .catch(function(){
    msg.className='msg err';
    msg.textContent='Error de conexion con Kodi';
    msg.style.display='block';
    btn.disabled=false;btn.textContent='Enviar a Kodi';
  });
}
document.getElementById('txt').addEventListener('keydown',function(e){
  if(e.key==='Enter'){e.preventDefault();enviar()}
});
</script>
</body>
</html>"""
