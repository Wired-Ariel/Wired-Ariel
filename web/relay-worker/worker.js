/* worker.js - il relay su Cloudflare Workers + Durable Objects (2026-08-23).
 *
 * L'OPZIONE "NESSUN SERVER": la stessa cosa che fa relay.py + relay_ws.py,
 * ma ospitata sull'edge di Cloudflare (piano gratuito), con wss:// e il
 * certificato gia' fatti. Una stanza = un Durable Object: tutti i browser
 * della stessa stanza finiscono sullo stesso oggetto, che inoltra ogni
 * datagramma agli altri e basta. Non guarda dentro il corpo, come relay.py.
 *
 * Differenza da relay_ws.py, da sapere: QUI non ci sono i client UDP
 * (client.py / mGBA): e' un mondo browser-only. Chi vuole mischiare web e
 * emulatore usa relay_ws.py davanti a relay.py su un VPS.
 *
 * SCRITTO E NON PROVATO (2026-08-23): non c'e' un account Cloudflare in
 * questa sessione. La logica e' quella di relay.py; la prova sta nella
 * procedura del README (wrangler dev, poi la pagina con ?relay=wss://...).
 *
 * URL: wss://<worker>/ws?stanza=N     (la stanza nell'URL: il DO va scelto
 *                                      PRIMA del primo messaggio)
 *      https://<worker>/              -> JSON di stato, come relay_ws.py
 */

const MAGIC = [0x4F, 0x57, 0x4C, 0x31];   // "OWL1"
const VERSION = 1;
const HEADER = 11;
const T = { HELLO: 0, EVENT: 1, PING: 2, PONG: 3, BYE: 4, CLUB: 6 };

function unpack(u8) {
  if (u8.length < HEADER) return null;
  if (u8[0] !== MAGIC[0] || u8[1] !== MAGIC[1] || u8[2] !== MAGIC[2] || u8[3] !== MAGIC[3] || u8[4] !== VERSION) return null;
  return { kind: u8[5], peerId: u8[6] | (u8[7] << 8), roomId: u8[8] | (u8[9] << 8), seq: u8[10] };
}

function pack(kind, peerId, roomId, seq, body) {
  const n = body ? body.length : 0;
  const out = new Uint8Array(HEADER + n);
  out.set(MAGIC, 0); out[4] = VERSION; out[5] = kind;
  out[6] = peerId & 0xFF; out[7] = (peerId >> 8) & 0xFF;
  out[8] = roomId & 0xFF; out[9] = (roomId >> 8) & 0xFF;
  out[10] = seq & 0xFF;
  if (n) out.set(body, HEADER);
  return out;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/ws") {
      if (request.headers.get("Upgrade") !== "websocket") {
        return new Response("serve un Upgrade: websocket", { status: 426 });
      }
      const stanza = parseInt(url.searchParams.get("stanza") || "0", 10);
      if (!(stanza > 0 && stanza < 65536)) {
        return new Response("stanza mancante o fuori range (1..65535): /ws?stanza=N", { status: 400 });
      }
      const id = env.ROOMS.idFromName("stanza-" + stanza);
      return env.ROOMS.get(id).fetch(request);
    }
    return new Response(JSON.stringify({ relay_ws: true, worker: true, uso: "wss://<host>/ws?stanza=N" }), {
      headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
    });
  }
};

export class Room {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    // Hibernation API: la connessione sopravvive ai riavvii dell'isolato e
    // il piano gratuito non paga il tempo in cui nessuno parla.
    this.state.acceptWebSocket(server);
    server.serializeAttachment({ peerId: null, roomId: 0 });
    return new Response(null, { status: 101, webSocket: client });
  }

  _others(ws) {
    return this.state.getWebSockets().filter((w) => w !== ws);
  }

  webSocketMessage(ws, message) {
    if (typeof message === "string") return;           // solo binario: il resto si ignora
    const u8 = new Uint8Array(message);
    const p = unpack(u8);
    if (!p) return;                                     // non e' OWL1: ignorato, come relay_ws.py
    const att = ws.deserializeAttachment() || {};
    if (att.peerId !== p.peerId || (p.roomId && att.roomId !== p.roomId)) {
      ws.serializeAttachment({ peerId: p.peerId, roomId: p.roomId || att.roomId || 0 });
    }
    if (p.kind === T.PING) {
      // Si rimanda indietro il corpo intatto: dentro c'e' il timestamp del client.
      ws.send(pack(T.PONG, p.peerId, p.roomId, p.seq, u8.subarray(HEADER)));
      return;
    }
    if (p.kind === T.HELLO) return;
    if (p.kind === T.EVENT || p.kind === T.CLUB) {
      for (const w of this._others(ws)) {
        try { w.send(u8); } catch (e) { /* il close arrivera' da solo */ }
      }
      return;
    }
    if (p.kind === T.BYE) {
      this._bye(ws, p.peerId, p.roomId);
      try { ws.close(1000, "bye"); } catch (e) { /* niente */ }
    }
  }

  _bye(ws, peerId, roomId) {
    const d = pack(T.BYE, peerId, roomId || 0, 0);
    for (const w of this._others(ws)) {
      try { w.send(d); } catch (e) { /* niente */ }
    }
  }

  webSocketClose(ws) {
    const att = ws.deserializeAttachment() || {};
    if (att.peerId !== null && att.peerId !== undefined) this._bye(ws, att.peerId, att.roomId);
  }

  webSocketError(ws) {
    this.webSocketClose(ws);
  }
}
