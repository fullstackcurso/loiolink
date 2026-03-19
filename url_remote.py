# -*- coding: utf-8 -*-
"""
Servidor HTTP local para recibir URLs o texto desde movil o PC.

Inicia un mini servidor en la red local que sirve una pagina web
donde el usuario puede pegar una URL o escribir texto. Al enviar,
Kodi lo recibe y actua (reproduce la URL o usa el texto).
El servidor se cierra tras recibir los datos o al cancelar.
"""
import json
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import xbmc
import xbmcgui

_PORT_START = 8089
_PORT_END = 8099
_TIMEOUT_SECONDS = 300  # 5 minutos
_POLL_INTERVAL_MS = 500


def _get_local_ip():
    """Obtiene la IP local del dispositivo en la red."""
    # Kodi puede proporcionar la IP directamente
    try:
        ip = xbmc.getIPAddress()
        if ip and ip != "127.0.0.1" and not ip.startswith("0."):
            return ip
    except Exception:
        pass

    # Fallback: conexion UDP sin envio real para obtener la IP de la interfaz
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


class _RemoteHandler(BaseHTTPRequestHandler):
    """Maneja peticiones GET (pagina) y POST (recibir URL)."""

    def log_message(self, fmt, *args):
        """Silencia los logs del servidor HTTP en la consola."""
        xbmc.log("[EspaTV/remote] {0}".format(fmt % args), xbmc.LOGDEBUG)

    def do_GET(self):
        if self.path == "/" or self.path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(self.server.html_page.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/send":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0 or content_length > 4096:
            self._json_response(400, {"error": "Datos no validos"})
            return

        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json_response(400, {"error": "JSON no valido"})
            return

        url = (data.get("url") or data.get("text") or "").strip()
        if not url:
            self._json_response(400, {"error": "Campo vacio"})
            return

        if self.server.mode == "url":
            valid_schemes = ("http://", "https://", "rtmp://", "rtsp://")
            if not url.startswith(valid_schemes):
                self._json_response(400, {"error": "URL no valida. Debe empezar por http:// o https://"})
                return

        self.server.received_url = url
        self.server.url_event.set()
        msg = "URL recibida. Reproduciendo en Kodi..." if self.server.mode == "url" else "Texto recibido."
        self._json_response(200, {"ok": True, "message": msg})

    def _json_response(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


class _RemoteServer(HTTPServer):
    """HTTPServer con atributos extra para comunicacion entre threads."""

    def __init__(self, address, handler_class, mode="url"):
        self.received_url = None
        self.url_event = threading.Event()
        self.mode = mode
        self.html_page = _HTML_URL if mode == "url" else _HTML_TEXT
        HTTPServer.__init__(self, address, handler_class)


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
textarea{
  width:100%;min-height:80px;padding:12px;
  background:#0d1117;color:#c9d1d9;
  border:1px solid #30363d;border-radius:8px;
  font-size:16px;font-family:monospace;
  resize:vertical;outline:none;
  transition:border-color .2s;
}
textarea:focus{border-color:#58a6ff}
textarea::placeholder{color:#484f58}
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
.msg.ok{display:block;background:#0d2818;color:#3fb950;border:1px solid #238636}
.msg.err{display:block;background:#2d1117;color:#f85149;border:1px solid #da3633}
.footer{
  text-align:center;color:#484f58;font-size:.75em;margin-top:20px;
}
</style>
</head>
<body>
<div class="card">
  <h1>EspaTV Remote</h1>
  <p class="sub">Pega una URL para reproducirla en Kodi</p>
  <label for="url">URL del video</label>
  <textarea id="url" placeholder="https://www.dailymotion.com/video/..." autofocus></textarea>
  <button class="btn" id="send" onclick="enviar()">Enviar a Kodi</button>
  <div class="msg" id="msg"></div>
  <p class="footer">El servidor se cerrara automaticamente tras recibir la URL.</p>
</div>
<script>
function enviar(){
  var url=document.getElementById('url').value.trim();
  var msg=document.getElementById('msg');
  var btn=document.getElementById('send');
  msg.className='msg';msg.style.display='none';
  if(!url){
    msg.className='msg err';msg.textContent='Escribe una URL';msg.style.display='block';
    return;
  }
  if(!/^https?:\\/\\//i.test(url)&&!/^rtmp:\\/\\//i.test(url)&&!/^rtsp:\\/\\//i.test(url)){
    msg.className='msg err';msg.textContent='La URL debe empezar por http:// o https://';msg.style.display='block';
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
      msg.textContent='URL enviada. Reproduciendo en Kodi...';
      btn.textContent='Enviado';
    }else{
      msg.className='msg err';
      msg.textContent=d.error||'Error desconocido';
      btn.disabled=false;btn.textContent='Enviar a Kodi';
    }
    msg.style.display='block';
  })
  .catch(function(){
    msg.className='msg err';
    msg.textContent='Error de conexion con Kodi';
    msg.style.display='block';
    btn.disabled=false;btn.textContent='Enviar a Kodi';
  });
}
document.getElementById('url').addEventListener('keydown',function(e){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();enviar()}
});
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
.msg.ok{display:block;background:#0d2818;color:#3fb950;border:1px solid #238636}
.msg.err{display:block;background:#2d1117;color:#f85149;border:1px solid #da3633}
.footer{
  text-align:center;color:#484f58;font-size:.75em;margin-top:20px;
}
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
</div>
<script>
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
      msg.textContent='Texto enviado a Kodi';
      btn.textContent='Enviado';
    }else{
      msg.className='msg err';
      msg.textContent=d.error||'Error desconocido';
      btn.disabled=false;btn.textContent='Enviar a Kodi';
    }
    msg.style.display='block';
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

