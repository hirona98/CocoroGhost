/**
 * CocoroGhost Web UI (minimal)
 *
 * 目的:
 * - /api/auth/login で Cookie セッションを張る
 * - /api/chat の SSE を逐次表示する
 * - /api/events/stream (WebSocket) を受信して吹き出しとして表示する
 *
 * 方針:
 * - フレームワーク無し（単一HTML + JS）
 * - 履歴はリロードで消えてよい
 */

(() => {
  /** @type {string} */
  const WS_CLIENT_ID_KEY = "cocoro_ws_client_id";

  /** @type {string} */
  const API_LOGIN = "/api/auth/login";
  /** @type {string} */
  const API_LOGOUT = "/api/auth/logout";
  /** @type {string} */
  const API_CHAT = "/api/chat";
  /** @type {string} */
  const WS_EVENTS = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events/stream`;

  // --- DOM ---
  const panelLogin = document.getElementById("panel-login");
  const panelChat = document.getElementById("panel-chat");
  const composer = document.getElementById("composer");

  const loginToken = document.getElementById("login-token");
  const loginStatus = document.getElementById("login-status");
  const btnLogin = document.getElementById("btn-login");
  const btnLogout = document.getElementById("btn-logout");

  const chatScroll = document.getElementById("chat-scroll");
  const chatStatus = document.getElementById("chat-status");
  const textInput = document.getElementById("text-input");
  const fileInput = document.getElementById("file-input");
  const attachments = document.getElementById("attachments");
  const btnSend = document.getElementById("btn-send");

  // --- State ---
  /** @type {WebSocket|null} */
  let ws = null;
  /** @type {AbortController|null} */
  let chatAbort = null;
  /** @type {HTMLElement|null} */
  let inflightBubble = null;

  // --- Utilities ---
  function setStatus(el, text, isError) {
    el.textContent = text || "";
    el.classList.toggle("error", !!isError);
  }

  function showLogin() {
    panelLogin.classList.remove("hidden");
    panelChat.classList.add("hidden");
    composer.classList.add("hidden");
    btnLogout.disabled = true;
  }

  function showChat() {
    panelLogin.classList.add("hidden");
    panelChat.classList.remove("hidden");
    composer.classList.remove("hidden");
    btnLogout.disabled = false;
  }

  function scrollToBottom() {
    chatScroll.scrollTop = chatScroll.scrollHeight;
  }

  function createBubble(kind, text) {
    // --- Bubble container ---
    const bubble = document.createElement("div");
    bubble.className = `bubble ${kind}`;
    bubble.textContent = text || "";
    return bubble;
  }

  function appendBubble(kind, text) {
    const bubble = createBubble(kind, text);
    chatScroll.appendChild(bubble);
    scrollToBottom();
    return bubble;
  }

  function ensureWsClientId() {
    // --- localStorage に保持（UI非表示の端末ID） ---
    let cid = localStorage.getItem(WS_CLIENT_ID_KEY) || "";
    cid = String(cid).trim();
    if (cid) return cid;

    // --- 生成（crypto.randomUUID があれば使う） ---
    const newId = typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    localStorage.setItem(WS_CLIENT_ID_KEY, newId);
    return newId;
  }

  async function apiLogin(token) {
    // --- ログイン（Cookie は Set-Cookie で返る） ---
    const res = await fetch(API_LOGIN, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: String(token || "") }),
    });
    return res.status === 204;
  }

  async function apiLogout() {
    // --- ログアウト（Cookie 削除） ---
    await fetch(API_LOGOUT, { method: "POST" });
  }

  function connectEventsWs() {
    // --- 既存接続があれば閉じる ---
    if (ws) {
      try {
        ws.close();
      } catch (_) {}
      ws = null;
    }

    // --- 接続 ---
    const socket = new WebSocket(WS_EVENTS);
    ws = socket;

    socket.onopen = () => {
      // --- hello を送る（端末識別） ---
      const cid = ensureWsClientId();
      socket.send(JSON.stringify({ type: "hello", client_id: cid, caps: [] }));
      setStatus(chatStatus, "connected", false);
    };

    socket.onclose = (ev) => {
      // --- 認証エラー（policy violation）は再接続しても無駄 ---
      if (ev && Number(ev.code) === 1008) {
        setStatus(chatStatus, "websocket auth failed (login required)", true);
        return;
      }

      setStatus(chatStatus, "disconnected (reconnecting...)", true);
      // --- バックオフは最小（単一ユーザー前提） ---
      setTimeout(() => {
        connectEventsWs();
      }, 1500);
    };

    socket.onerror = () => {
      // --- onclose が来るのでここではメッセージのみ ---
      setStatus(chatStatus, "websocket error", true);
    };

    socket.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        const type = String(payload.type || "");
        const data = payload.data || {};

        // --- notification/reminder はAI側の吹き出しとして扱う ---
        if (type === "notification" || type === "reminder" || type === "meta-request" || type === "desktop_watch") {
          const msg = String(data.message || "");
          if (msg) {
            appendBubble("ai", msg);
          }
          return;
        }
      } catch (_) {
        return;
      }
    };
  }

  async function filesToDataUris(files) {
    // --- 画像上限（設計と合わせる） ---
    const maxFiles = 5;
    const maxBytesPerFile = 5 * 1024 * 1024;
    const maxTotalBytes = 20 * 1024 * 1024;

    const list = Array.from(files || []).slice(0, maxFiles);
    let total = 0;
    for (const f of list) total += Number(f.size || 0);
    if (total > maxTotalBytes) {
      throw new Error("画像の合計サイズが大きすぎます（20MB以内）");
    }
    for (const f of list) {
      if (Number(f.size || 0) > maxBytesPerFile) {
        throw new Error(`画像が大きすぎます（5MB以内）: ${f.name}`);
      }
    }

    // --- FileReader で Data URI 化 ---
    const out = [];
    for (const f of list) {
      const dataUri = await new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onerror = () => reject(new Error("画像の読み込みに失敗しました"));
        r.onload = () => resolve(String(r.result || ""));
        r.readAsDataURL(f);
      });
      out.push(dataUri);
    }
    return out;
  }

  function renderAttachments(files) {
    attachments.innerHTML = "";
    const list = Array.from(files || []);
    if (!list.length) return;
    for (const f of list) {
      const chip = document.createElement("div");
      chip.className = "chip";
      chip.textContent = `${f.name} (${Math.round((f.size || 0) / 1024)} KB)`;
      attachments.appendChild(chip);
    }
  }

  async function sendChat() {
    // --- 送信中は二重送信を防止 ---
    if (chatAbort) return;

    // --- 入力取り出し ---
    const text = String(textInput.value || "").trim();
    const files = fileInput.files;

    // --- 先にユーザー吹き出し ---
    const userLabel = text || (files && files.length ? "[画像]" : "");
    if (userLabel) {
      appendBubble("user", userLabel);
    }

    // --- 画像を Data URI 化 ---
    let images = [];
    try {
      images = await filesToDataUris(files);
    } catch (e) {
      setStatus(chatStatus, String(e && e.message ? e.message : e), true);
      return;
    }

    // --- 送信 ---
    const controller = new AbortController();
    chatAbort = controller;
    btnSend.disabled = true;
    setStatus(chatStatus, "sending...", false);

    // --- 生成中バブル ---
    inflightBubble = appendBubble("ai", "");

    try {
      const res = await fetch(API_CHAT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          input_text: text,
          images: images,
          client_context: { locale: navigator.language || "ja-JP" },
        }),
        signal: controller.signal,
      });

      if (!res.ok || !res.body) {
        setStatus(chatStatus, `chat failed (${res.status})`, true);
        return;
      }

      // --- SSE を逐次パースして描画 ---
      await consumeSse(res.body, (evName, data) => {
        if (!inflightBubble) return;

        if (evName === "token") {
          inflightBubble.textContent += String(data.text || "");
          scrollToBottom();
          return;
        }

        if (evName === "error") {
          const msg = String(data.message || "error");
          inflightBubble.textContent = msg;
          inflightBubble = null;
          return;
        }

        if (evName === "done") {
          inflightBubble = null;
          return;
        }
      });

      setStatus(chatStatus, "done", false);
    } catch (e) {
      if (e && e.name === "AbortError") {
        setStatus(chatStatus, "aborted", true);
      } else {
        setStatus(chatStatus, String(e && e.message ? e.message : e), true);
      }
    } finally {
      // --- 入力をクリア ---
      textInput.value = "";
      fileInput.value = "";
      renderAttachments([]);

      // --- 状態を戻す ---
      btnSend.disabled = false;
      chatAbort = null;
      inflightBubble = null;
    }
  }

  /**
   * SSE ストリームを読み、イベントをコールバックする。
   *
   * @param {ReadableStream<Uint8Array>} stream
   * @param {(eventName: string, data: any) => void} onEvent
   */
  async function consumeSse(stream, onEvent) {
    // --- TextDecoder + バッファ ---
    const decoder = new TextDecoder("utf-8");
    const reader = stream.getReader();
    let buf = "";

    // --- SSE の1イベントを解析する ---
    const parseEventBlock = (block) => {
      const lines = block.split("\n");
      let eventName = "message";
      const dataLines = [];

      for (const line of lines) {
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
          continue;
        }
        if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
          continue;
        }
      }

      const dataText = dataLines.join("\n");
      if (!dataText) return;

      try {
        const obj = JSON.parse(dataText);
        onEvent(eventName, obj);
      } catch (_) {
        onEvent(eventName, { text: dataText });
      }
    };

    // --- 読み取りループ ---
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      // --- SSE イベントは空行（\n\n）で区切られる ---
      while (true) {
        const idx = buf.indexOf("\n\n");
        if (idx === -1) break;
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (block.trim()) parseEventBlock(block);
      }
    }

    // --- 末尾に残った分 ---
    if (buf.trim()) parseEventBlock(buf);
  }

  // --- UI wiring ---
  btnLogin.addEventListener("click", async () => {
    setStatus(loginStatus, "logging in...", false);
    const ok = await apiLogin(loginToken.value);
    if (!ok) {
      setStatus(loginStatus, "ログインに失敗しました（Tokenが違います）", true);
      return;
    }

    // --- 画面切替 + WS 接続 ---
    loginToken.value = "";
    setStatus(loginStatus, "", false);
    showChat();
    connectEventsWs();
  });

  btnLogout.addEventListener("click", async () => {
    // --- WS も閉じる ---
    if (ws) {
      try {
        ws.close();
      } catch (_) {}
      ws = null;
    }
    await apiLogout();
    showLogin();
    setStatus(chatStatus, "", false);
  });

  btnSend.addEventListener("click", () => sendChat());

  textInput.addEventListener("keydown", (ev) => {
    // --- Enter 送信（Shift+Enter は改行） ---
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      sendChat();
    }
  });

  fileInput.addEventListener("change", () => {
    renderAttachments(fileInput.files);
  });

  // --- 初期表示 ---
  showLogin();
  setStatus(loginStatus, "※自己署名HTTPSの警告は許容してください", false);
})();
