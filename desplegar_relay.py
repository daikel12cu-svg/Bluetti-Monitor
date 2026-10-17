#!/usr/bin/env python3
"""
desplegar_relay.py - Crea (o actualiza) TU relay en Cloudflare para ver la Bluetti desde internet
================================================================================================
Solo hace falta Python (el mismo de la app). No instala nada. Pasos:

  1. CIERRA la app de la Bluetti (este script escribe su configuracion).
  2. Ten a mano:
       * tu "Account ID" de Cloudflare  (Cloudflare -> Workers y Pages: aparece en la barra derecha)
       * un "API Token":  Cloudflare -> Mi perfil -> Tokens de API -> Crear token -> plantilla
         "Edit Cloudflare Workers" -> Continuar -> Crear.  Copia el token (solo se muestra una vez).
  3. Ejecuta:   python desplegar_relay.py
  4. Abre la app: en ⚙ del servidor veras la direccion y la clave del relay ya rellenadas.

El token solo se usa en esta ejecucion: no se guarda en ningun sitio. Puedes borrarlo despues en Cloudflare.
Volver a ejecutarlo actualiza el relay conservando la misma direccion y clave.
"""
import getpass
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telemetry_server as ts     # noqa: E402  (aqui estan la pagina del telefono y la configuracion)

API = os.environ.get("CF_API_BASE", "https://api.cloudflare.com/client/v4")
WORKER_NAME = "bluetti-relay"
COMPAT_DATE = "2025-09-01"

WORKER_TEMPLATE = r"""// Bluetti relay (Cloudflare Worker + Durable Object).
// Es una "centralita": la app (PC con la Bluetti) llama a /host y los telefonos a /ws; el relay solo
// reenvia mensajes. NO guarda datos ni conoce las contrasenas: la autenticacion la hace la app.
import { DurableObject } from "cloudflare:workers";

const HTML = __INDEX_HTML__;           // pagina de respaldo; la app envia la ultima al conectarse
const SW_JS = __SW_JS__;
const MANIFEST = __MANIFEST__;
const ICON192 = "__ICON192__";
const ICON512 = "__ICON512__";
const MAX_PAGE = 1500000;
const CHUNK = 30000;
const MAX_PHONES = 8;            // telefonos/copias conectados a la vez
const MAX_MSG = 8192;            // bytes por mensaje desde un telefono
const PHONE_BURST = 40;          // mensajes permitidos cada 10 s por telefono
const CONN_PER_MIN = 30;         // conexiones nuevas por minuto y por IP

function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/ws" || url.pathname === "/host") {
      if ((request.headers.get("Upgrade") || "").toLowerCase() !== "websocket")
        return new Response("Se esperaba un WebSocket", { status: 426 });
      if (url.pathname === "/host") {
        const key = typeof env.RELAY_KEY === "string" ? env.RELAY_KEY : "";
        if (!key || !safeEqual(request.headers.get("Authorization") || "", "Bearer " + key))
          return new Response("No autorizado", { status: 401 });
      }
      return env.ROOM.get(env.ROOM.idFromName("main")).fetch(request);
    }
    if (url.pathname === "/") {
      let html = HTML, src = "embedded";
      try {                                    // ultima pagina que mando la app
        const r = await env.ROOM.get(env.ROOM.idFromName("main")).fetch(new Request(url.origin + "/page"));
        if (r.status === 200) { html = await r.text(); src = "host"; }
      } catch (_) {}
      return new Response(html, {
        headers: {
          "content-type": "text/html; charset=utf-8",
          "cache-control": "no-store",
          "x-page-src": src,
          "x-content-type-options": "nosniff",
          "x-frame-options": "DENY",
          "referrer-policy": "no-referrer",
          "content-security-policy":
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data: 'self'; media-src data:; worker-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'",
        },
      });
    }
    if (url.pathname === "/sw.js")
      return new Response(SW_JS, { headers: { "content-type": "application/javascript; charset=utf-8", "cache-control": "no-cache", "service-worker-allowed": "/" } });
    if (url.pathname === "/manifest.webmanifest")
      return new Response(MANIFEST, { headers: { "content-type": "application/manifest+json; charset=utf-8", "cache-control": "public, max-age=3600" } });
    if (url.pathname === "/icon-192.png" || url.pathname === "/icon-512.png") {
      const b = Uint8Array.from(atob(url.pathname === "/icon-192.png" ? ICON192 : ICON512), (c) => c.charCodeAt(0));
      return new Response(b, { headers: { "content-type": "image/png", "cache-control": "public, max-age=86400" } });
    }
    if (url.pathname === "/favicon.ico") return new Response(null, { status: 204 });
    return new Response("No encontrado", { status: 404 });
  },
};

export class Room extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.buckets = new Map();   // id de telefono -> {t0, n}
    this.conns = new Map();     // ip -> [instantes]
    this.page = null;           // ultima pagina enviada por la app (se guarda en el almacen del DO)
  }

  hostSocket() {
    return this.ctx.getWebSockets("host")[0];
  }

  sendHost(obj) {
    const h = this.hostSocket();
    if (!h) return false;
    try { h.send(JSON.stringify(obj)); return true; } catch (_) { return false; }
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/page") {            // solo lo llama el propio Worker
      if (this.page == null) {
        try {
          const n = await this.ctx.storage.get("page_n");
          if (typeof n === "number" && n > 0) {
            let t = "";
            for (let i = 0; i < n; i++) t += (await this.ctx.storage.get("page_" + i)) || "";
            this.page = t;
          }
        } catch (_) {}
      }
      return this.page ? new Response(this.page, { status: 200 }) : new Response("", { status: 404 });
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    if (url.pathname === "/host") {
      for (const w of this.ctx.getWebSockets("host")) { try { w.close(1012, "reemplazado"); } catch (_) {} }
      for (const w of this.ctx.getWebSockets("phone")) {      // la sesion nueva empieza limpia
        try { w.send('{"v":1,"type":"bye"}'); w.close(1013, "host-reiniciado"); } catch (_) {}
      }
      this.ctx.acceptWebSocket(server, ["host"]);
      return new Response(null, { status: 101, webSocket: client });
    }

    const ip = request.headers.get("CF-Connecting-IP") || "?";
    const now = Date.now();
    const recent = (this.conns.get(ip) || []).filter((t) => now - t < 60000);
    recent.push(now);
    this.conns.set(ip, recent);
    if (this.conns.size > 500) this.conns.clear();
    if (recent.length > CONN_PER_MIN) return new Response("Demasiados intentos", { status: 429 });

    const reject = (reason) => {
      server.accept();
      server.close(1013, reason);
      return new Response(null, { status: 101, webSocket: client });
    };
    if (!this.hostSocket()) return reject("host-offline");
    if (this.ctx.getWebSockets("phone").length >= MAX_PHONES) return reject("lleno");
    const id = crypto.randomUUID().replaceAll("-", "").slice(0, 10);
    this.ctx.acceptWebSocket(server, ["phone"]);
    server.serializeAttachment({ id });
    this.sendHost({ t: "open", c: id, ip });
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, message) {
    const isHost = this.ctx.getTags(ws).includes("host");
    if (isHost) {
      if (typeof message !== "string") return;
      let m;
      try { m = JSON.parse(message); } catch (_) { return; }
      if (!m || typeof m !== "object") return;
      if (m.t === "page") {
        if (typeof m.d === "string" && m.d.length > 1000 && m.d.length <= MAX_PAGE && m.d.trimStart().toLowerCase().startsWith("<!doctype html") && m.d !== this.page) {
          const n = Math.ceil(m.d.length / CHUNK), o = { page_n: n };
          for (let i = 0; i < n; i++) o["page_" + i] = m.d.slice(i * CHUNK, (i + 1) * CHUNK);
          try { await this.ctx.storage.put(o); this.page = m.d; } catch (_) {}
        }
      } else if (m.t === "m" && typeof m.c === "string" && typeof m.d === "string") {
        const w = this.phoneById(m.c);
        if (w) { try { w.send(m.d); } catch (_) {} }
      } else if (m.t === "b" && Array.isArray(m.cs) && typeof m.d === "string") {
        for (const c of m.cs.slice(0, MAX_PHONES)) {
          const w = typeof c === "string" ? this.phoneById(c) : null;
          if (w) { try { w.send(m.d); } catch (_) {} }
        }
      } else if (m.t === "close" && typeof m.c === "string") {
        const w = this.phoneById(m.c);
        if (w) { try { w.close(1000, "cerrado"); } catch (_) {} }
      }
      return;
    }
    // telefono
    const att = ws.deserializeAttachment() || {};
    if (!att.id) return;
    if (typeof message !== "string" || message.length > MAX_MSG) { try { ws.close(1009, "mensaje no valido"); } catch (_) {} return; }
    const now = Date.now();
    let b = this.buckets.get(att.id);
    if (!b || now - b.t0 > 10000) { b = { t0: now, n: 0 }; this.buckets.set(att.id, b); }
    if (++b.n > PHONE_BURST) { try { ws.close(1008, "demasiado rapido"); } catch (_) {} return; }
    if (!this.sendHost({ t: "m", c: att.id, d: message })) {
      try { ws.send('{"v":1,"type":"bye"}'); ws.close(1013, "host-offline"); } catch (_) {}
    }
  }

  phoneById(id) {
    for (const w of this.ctx.getWebSockets("phone")) {
      const a = w.deserializeAttachment();
      if (a && a.id === id) return w;
    }
    return null;
  }

  async webSocketClose(ws, code, reason) { this.gone(ws); try { ws.close(code === 1005 ? 1000 : code, reason); } catch (_) {} }
  async webSocketError(ws) { this.gone(ws); }

  gone(ws) {
    const isHost = this.ctx.getTags(ws).includes("host");
    if (isHost) {
      for (const w of this.ctx.getWebSockets("phone")) {
        try { w.send('{"v":1,"type":"bye"}'); w.close(1013, "host-offline"); } catch (_) {}
      }
      return;
    }
    const att = ws.deserializeAttachment() || {};
    if (att.id) { this.buckets.delete(att.id); this.sendHost({ t: "close", c: att.id }); }
  }
}
"""


def build_source():
    """Worker listo para subir: pagina, service worker, manifest e iconos incrustados."""
    return (WORKER_TEMPLATE
            .replace("__INDEX_HTML__", json.dumps(ts.INDEX_HTML))
            .replace("__SW_JS__", json.dumps(ts.SW_JS))
            .replace("__MANIFEST__", json.dumps(ts.MANIFEST_JSON))
            .replace("__ICON192__", ts.ICON_192_B64)
            .replace("__ICON512__", ts.ICON_512_B64))


def api(method, path, token, body=None, multipart=None):
    """Llama a la API de Cloudflare. Devuelve (estado_http, json | None)."""
    headers = {"Authorization": "Bearer " + token, "User-Agent": "bluetti-relay-deploy/1.0"}
    data = None
    if multipart is not None:
        data, ctype = multipart
        headers["Content-Type"] = ctype
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as exc:
        raw, code = exc.read(), exc.code
    except Exception as exc:
        return 0, {"errors": [{"code": 0, "message": f"{type(exc).__name__}: {exc}"}]}
    try:
        return code, json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return code, {"errors": [{"code": code, "message": raw.decode("utf-8", "replace")[:300]}]}


def errors_text(js):
    errs = (js or {}).get("errors") or []
    return "; ".join(f"[{e.get('code')}] {e.get('message')}" for e in errs) or "(sin detalle)"


def build_multipart(metadata, source):
    boundary = "----bluetti" + uuid.uuid4().hex
    parts = []
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n'
                 f'Content-Type: application/json\r\n\r\n{json.dumps(metadata)}\r\n')
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
                 f'Content-Type: application/javascript+module\r\n\r\n{source}\r\n')
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode("utf-8"), "multipart/form-data; boundary=" + boundary


def upload_worker(acct, token, key, source):
    path = f"/accounts/{acct}/workers/scripts/{WORKER_NAME}"
    code, _ = api("GET", path + "/settings", token)
    exists = code == 200
    for with_migration in ([False, True] if exists else [True, False]):
        meta = {
            "main_module": "worker.js",
            "compatibility_date": COMPAT_DATE,
            "bindings": [
                {"type": "durable_object_namespace", "name": "ROOM", "class_name": "Room"},
                {"type": "secret_text", "name": "RELAY_KEY", "text": key},
            ],
        }
        if with_migration:
            meta["migrations"] = {"new_tag": "v1", "new_sqlite_classes": ["Room"]}
        code, js = api("PUT", path, token, multipart=build_multipart(meta, source))
        if js and js.get("success"):
            return True, js
        text = errors_text(js).lower()
        if "migration" in text or "durable" in text or "tag" in text or "class" in text:
            continue             # probamos la otra forma (con/sin migracion)
        return False, js
    return False, js


def ensure_subdomain(acct, token):
    code, js = api("GET", f"/accounts/{acct}/workers/subdomain", token)
    sub = ((js or {}).get("result") or {}).get("subdomain") if js and js.get("success") else None
    if sub:
        return sub
    sub = "bluetti-" + secrets.token_hex(3)
    code, js = api("PUT", f"/accounts/{acct}/workers/subdomain", token, body={"subdomain": sub})
    if js and js.get("success"):
        return ((js.get("result") or {}).get("subdomain")) or sub
    return None


def main():
    print(__doc__)
    if input("¿Cerraste la app de la Bluetti y quieres continuar? (s/n): ").strip().lower() not in ("s", "si", "sí", "y"):
        return
    acct = input("Account ID de Cloudflare: ").strip()
    token = getpass.getpass("API Token (no se ve al escribir): ").strip()
    if not acct or not token:
        sys.exit("Faltan datos.")

    cfg = ts.load_server_config()
    key = cfg.get("relay_key") or secrets.token_urlsafe(32)
    source = build_source()

    print("\n1/4 Subiendo el relay a Cloudflare...")
    ok, js = upload_worker(acct, token, key, source)
    if not ok:
        print("   ✗ No se pudo subir:", errors_text(js))
        print("   Revisa que el Account ID sea correcto y que el token tenga permiso «Workers Scripts: Edit».")
        sys.exit(1)
    print("   ✔ Relay subido.")

    print("2/4 Activando la direccion publica (workers.dev)...")
    sub = ensure_subdomain(acct, token)
    if not sub:
        print("   ✗ No pude crear/leer tu subdominio workers.dev. En Cloudflare: Workers y Pages -> tu cuenta ->")
        print("     'Subdominio de Workers' (crealo) y vuelve a ejecutar este script.")
        sys.exit(1)
    code, js = api("POST", f"/accounts/{acct}/workers/scripts/{WORKER_NAME}/subdomain", token,
                   body={"enabled": True, "previews_enabled": False})
    if not (js and js.get("success")):
        print("   (aviso)", errors_text(js))
        print(f"   Si la direccion no responde: Cloudflare -> Workers y Pages -> {WORKER_NAME} -> Configuracion ->")
        print("   Dominios y rutas -> activa 'workers.dev'.")
    url = f"https://{WORKER_NAME}.{sub}.workers.dev"
    print("   ✔", url)

    print("3/4 Comprobando que responde (puede tardar hasta un minuto)...")
    alive = False
    for _ in range(12):
        try:
            req = urllib.request.Request(url + "/", headers={"User-Agent": "bluetti-relay-deploy/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                alive = r.status == 200
        except Exception:
            alive = False
        if alive:
            break
        time.sleep(5)
    print("   ✔ Responde." if alive else "   (aun no responde; la propagacion a veces tarda. Pruébala en unos minutos.)")

    print("4/4 Guardando la direccion y la clave en la app...")
    cfg.update(relay_url=url, relay_key=key)
    short = len(cfg.get("view_password", "")) < ts.MIN_REMOTE_PW or (
        cfg.get("control_password") and len(cfg["control_password"]) < ts.MIN_REMOTE_PW)
    if not short:
        cfg["relay_enabled"] = True
    if ts.save_server_config(cfg):
        print("   ✔ Guardado.")
    else:
        print("   ✗ No pude guardar la configuracion. Escribela a mano en ⚙ del servidor:")
    print("\n================ LISTO ================")
    print("Direccion del relay :", url)
    print("Clave del relay     :", key)
    if short:
        print(f"\nIMPORTANTE: para activar internet, las contraseñas de la app deben tener al menos {ts.MIN_REMOTE_PW} caracteres.")
        print("Cambialas en ⚙ del servidor y marca 'Permitir ver los datos desde internet'.")
    else:
        print("\nAbre la app y enciende el servidor: en ⚙ veras el QR para internet.")
    print("Desde internet solo se puede VER, salvo que marques el permiso de control en ⚙.")


if __name__ == "__main__":
    main()
