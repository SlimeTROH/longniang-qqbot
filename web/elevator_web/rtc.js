"use strict";

// Only the opted-in game instance loads this script. Authentication stays with the web app.
window.ElevatorRTC = (() => {
  let owner = "", current = null, retry = 0;
  const trial = location.protocol === "https:";
  const fallback = trial ? "当前线路：网页中转" : "网页连接";

  function showStatus(label) {
    const el = document.getElementById("game-transport");
    if (el) { el.hidden = !owner; el.textContent = label; }
  }

  function drop(link) {
    if (!link || link !== current) return;
    current = null;
    clearTimeout(link.deadline);
    link.signal.abort();
    link.pc.close();
    for (const pending of link.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error("游戏直连已断开"));
    }
    link.pending.clear();
    showStatus(fallback);
    if (owner) retry = setTimeout(connect, 60000);
  }

  function receive(link, frame) {
    if (current !== link) return;
    try {
      if (typeof frame === "string") {
        const { id, status, bytes } = JSON.parse(frame);
        if (link.reply || !link.pending.has(id) || !Number.isInteger(bytes) || bytes < 1 ||
            bytes > 4 * 1024 * 1024 || !Number.isInteger(status) || status < 200 || status > 599) throw Error();
        link.reply = { id, status, data: new Uint8Array(bytes), offset: 0 };
        return;
      }
      const reply = link.reply;
      if (!reply || !(frame instanceof ArrayBuffer) || !frame.byteLength ||
          reply.offset + frame.byteLength > reply.data.length) throw Error();
      reply.data.set(new Uint8Array(frame), reply.offset);
      reply.offset += frame.byteLength;
      if (reply.offset < reply.data.length) return;
      const pending = link.pending.get(reply.id);
      link.reply = null;
      link.pending.delete(reply.id);
      clearTimeout(pending.timer);
      pending.resolve(new Response(reply.data, { status: reply.status, headers: {"Content-Type":"application/json"} }));
      if (reply.status === 502 || reply.status === 401) drop(link);
    } catch {
      drop(link);
    }
  }

  function send(link, op, body, base) {
    return new Promise((resolve, reject) => {
      if (link !== current || link.dc.readyState !== "open") { reject(new Error("游戏直连未连接")); return; }
      const id = ++link.seq;
      const timer = setTimeout(() => drop(link), 18000);
      link.pending.set(id, { resolve, reject, timer });
      try { link.dc.send(JSON.stringify({ id, op, base, body })); }
      catch { drop(link); }
    });
  }

  async function connect() {
    clearTimeout(retry);
    if (!owner || current || typeof RTCPeerConnection === "undefined") return;
    if (document.hidden) { retry = setTimeout(connect, 60000); return; }
    let link;
    try {
      const pc = new RTCPeerConnection({ iceServers: [] });
      const dc = pc.createDataChannel("elevator.v1", { ordered: true });
      link = current = { pc, dc, signal: new AbortController(), pending: new Map(), seq: 0, ready: false };
      link.deadline = setTimeout(() => drop(link), 13000);
      dc.binaryType = "arraybuffer";
      dc.onmessage = e => receive(link, e.data);
      dc.onclose = dc.onerror = () => drop(link);
      pc.onconnectionstatechange = () => {
        if (["failed", "closed", "disconnected"].includes(pc.connectionState)) drop(link);
      };
      dc.onopen = async () => {
        try {
          const started = performance.now();
          const ack = await send(link, "ping");
          if (!(await ack.json()).ok || current !== link) throw Error();
          clearTimeout(link.deadline);
          link.ready = true;
          const delay = Math.round(performance.now() - started);
          showStatus(trial ? `当前线路：上海直连（${delay} ms）` : "WebRTC 直连");
        } catch { drop(link); }
      };
      await pc.setLocalDescription(await pc.createOffer());
      // The answering endpoint supplies its complete candidates. The browser sends ICE checks.
      const reply = await fetch("/elevator/api/rtc/offer", {
        method: "POST", headers: {"Content-Type":"application/json"}, credentials: "same-origin",
        cache: "no-store", body: JSON.stringify(pc.localDescription), signal: link.signal.signal,
      });
      if (!reply.ok || current !== link) throw Error();
      await pc.setRemoteDescription(await reply.json());
    } catch { drop(link); }
  }

  function setAccount(name) {
    if (name === owner) return;
    owner = "";
    clearTimeout(retry);
    drop(current);
    owner = name;
    showStatus(fallback);
    if (owner) connect();
  }

  function request(path, body, base) {
    const op = {"/game/state":"state", "/game/action":"action", "/game/command":"command"}[path];
    return op && current?.ready ? send(current, op, body, base) : null;
  }
  return { setAccount, request };
})();
