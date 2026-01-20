/**
 * CocoroGhost Web UI
 * - CocoroUI.png 互換レイアウト（ゼロベース）
 *
 * 目的:
 * - /api/auth/login で Cookie セッションを張る
 * - /api/chat の SSE を逐次表示する
 * - /api/events/stream (WebSocket) を受信して吹き出しとして表示する
 *
 * 方針:
 * - フレームワーク無し（単一HTML + JS）
 * - UIに端末ID（ws_client_id）は表示しない
 * - 余計な永続はしない（ws_client_id だけ localStorage）
 */

(() => {
  // --- Constants ---
  const webSocketClientIdKey = "cocoro_ws_client_id";
  const apiLoginPath = "/api/auth/login";
  const apiAutoLoginPath = "/api/auth/auto_login";
  const apiLogoutPath = "/api/auth/logout";
  const apiChatPath = "/api/chat";
  // NOTE: CocoroGhost は HTTPS 必須（自己署名TLS）なので WebSocket も wss を使う。
  const webSocketEventsUrl = `wss://${location.host}/api/events/stream`;

  // --- DOM refs ---
  const panelLogin = document.getElementById("panel-login");
  const panelChat = document.getElementById("panel-chat");
  const inputPanel = document.getElementById("input-panel");

  const loginTokenInput = document.getElementById("login-token");
  const loginStatus = document.getElementById("login-status");
  const loginButton = document.getElementById("btn-login");

  const micButton = document.getElementById("btn-mic");
  const soundButton = document.getElementById("btn-sound");
  const screenButton = document.getElementById("btn-screen");
  const gearButton = document.getElementById("btn-gear");

  const chatScroll = document.getElementById("chat-scroll");
  const textInput = document.getElementById("text-input");
  const fileInput = document.getElementById("file-input");
  const attachments = document.getElementById("attachments");
  const sendButton = document.getElementById("btn-send");

  const statusLeft = document.getElementById("status-left");
  const statusRight = document.getElementById("status-right");

  // --- State ---
  /** @type {WebSocket|null} */
  let eventsSocket = null;
  /** @type {AbortController|null} */
  let chatAbortController = null;
  /** @type {HTMLElement|null} */
  let inflightAssistantBubble = null;
  /** @type {boolean} */
  let wsReauthInProgress = false;
  /** @type {boolean} */
  let wsReconnectEnabled = false;
  /** @type {number|null} */
  let wsReconnectTimerId = null;

  // --- UI helpers ---
  function setLoginStatus(text, isError) {
    loginStatus.textContent = text || "";
    loginStatus.classList.toggle("error", !!isError);
  }

  function setStatusBar(leftText, rightText) {
    statusLeft.textContent = leftText || "";
    statusRight.textContent = rightText || "";
  }

  function showLogin() {
    panelLogin.classList.remove("hidden");
    panelChat.classList.add("hidden");
    inputPanel.classList.add("hidden");
    setStatusBar("状態: ログイン待ち", "");
  }

  function showChat() {
    panelLogin.classList.add("hidden");
    panelChat.classList.remove("hidden");
    inputPanel.classList.remove("hidden");
    setStatusBar("状態: 正常動作中", "");
  }

  function scrollToBottom() {
    chatScroll.scrollTop = chatScroll.scrollHeight;
  }

  function createBubbleRow(kind) {
    // --- bubble row aligns left/right like the reference UI ---
    const row = document.createElement("div");
    row.className = `bubble-row ${kind}`;
    return row;
  }

  function createBubble(kind, text) {
    const bubble = document.createElement("div");
    bubble.className = `bubble ${kind}`;
    bubble.textContent = text || "";
    return bubble;
  }

  function appendBubble(kind, text) {
    const row = createBubbleRow(kind);
    const bubble = createBubble(kind, text);
    row.appendChild(bubble);
    chatScroll.appendChild(row);
    scrollToBottom();
    return bubble;
  }

  function ensureWebSocketClientId() {
    // --- localStorage に保持（UI非表示の端末ID） ---
    let cid = localStorage.getItem(webSocketClientIdKey) || "";
    cid = String(cid).trim();
    if (cid) return cid;

    // --- 生成（crypto.randomUUID があれば使う） ---
    const newId = typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    localStorage.setItem(webSocketClientIdKey, newId);
    return newId;
  }

  // --- API helpers ---
  async function apiLogin(token) {
    // --- ログイン（Cookie は Set-Cookie で返る） ---
    const response = await fetch(apiLoginPath, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: String(token || "") }),
    });
    return response.status === 204;
  }

  async function apiAutoLogin() {
    // --- 自動ログイン（サーバー設定で有効なときだけ 204 を返す） ---
    const response = await fetch(apiAutoLoginPath, { method: "POST" });
    return response.status === 204;
  }

  async function apiLogout() {
    // --- ログアウト（Cookie 削除） ---
    await fetch(apiLogoutPath, { method: "POST" });
  }

  // --- WebSocket (events) ---
  /**
   * WebSocket を閉じる（再接続の有効/無効は変更しない）。
   */
  function closeEventsSocketInternal() {
    if (!eventsSocket) return;
    try {
      // --- イベントハンドラを外す（明示クローズで reconnect が走るのを防ぐ） ---
      eventsSocket.onopen = null;
      eventsSocket.onclose = null;
      eventsSocket.onerror = null;
      eventsSocket.onmessage = null;
      eventsSocket.close();
    } catch (_) {}
    eventsSocket = null;
  }

  /**
   * WebSocket の再接続タイマーを解除する。
   */
  function clearEventsReconnectTimer() {
    if (wsReconnectTimerId == null) return;
    try {
      clearTimeout(wsReconnectTimerId);
    } catch (_) {}
    wsReconnectTimerId = null;
  }

  /**
   * WebSocket を停止する（ログアウト等の明示操作）。
   * 再接続を無効化し、接続を閉じ、再接続タイマーも消す。
   */
  function stopEventsSocket() {
    // --- 明示停止は再接続しない ---
    wsReconnectEnabled = false;
    clearEventsReconnectTimer();
    closeEventsSocketInternal();
  }

  function connectEventsSocket() {
    // --- ログイン中/チャット中は再接続を有効にする ---
    wsReconnectEnabled = true;
    clearEventsReconnectTimer();
    closeEventsSocketInternal();

    const socket = new WebSocket(webSocketEventsUrl);
    eventsSocket = socket;

    socket.onopen = () => {
      // --- hello を送る（端末識別） ---
      const clientId = ensureWebSocketClientId();
      socket.send(JSON.stringify({ type: "hello", client_id: clientId, caps: [] }));
      setStatusBar("状態: 正常動作中", "WS: connected");
    };

    socket.onclose = (event) => {
      // --- ログアウト等の明示停止中は再接続しない ---
      if (!wsReconnectEnabled) return;

      // --- 認証エラー（policy violation）は再接続しても無駄 ---
      if (event && Number(event.code) === 1008) {
        // --- 自動ログインが有効なら、Cookie セッションを再発行して再接続する ---
        if (!wsReauthInProgress) {
          wsReauthInProgress = true;
          setStatusBar("状態: 認証を更新中...", "WS: auth failed");
          (async () => {
            try {
              const ok = await apiAutoLogin();
              if (ok) {
                setStatusBar("状態: 再接続中...", "WS: reauth ok");
                connectEventsSocket();
                return;
              }

              // --- 自動ログインが無効 or 失敗: 手動ログインへ ---
              stopEventsSocket();
              showLogin();
              setLoginStatus("ログインが必要です（再起動した場合は再ログインしてください）", true);
              setStatusBar("状態: ログインが必要です", "WS: auth failed");
            } finally {
              wsReauthInProgress = false;
            }
          })();
          return;
        }

        setStatusBar("状態: 認証を更新中...", "WS: auth failed");
        return;
      }

      setStatusBar("状態: 再接続中...", "WS: reconnecting");
      clearEventsReconnectTimer();
      wsReconnectTimerId = setTimeout(() => connectEventsSocket(), 1500);
    };

    socket.onerror = () => {
      setStatusBar("状態: 接続エラー", "WS: error");
    };

    socket.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        const type = String(payload.type || "");
        const data = payload.data || {};

        // --- notification/reminder はAI側の吹き出しとして扱う ---
        if (type === "notification" || type === "reminder" || type === "meta-request" || type === "desktop_watch") {
          const message = String(data.message || "");
          if (message) appendBubble("ai", message);
          return;
        }
      } catch (_) {
        return;
      }
    };
  }

  // --- Attachments ---
  function renderAttachments(selectedFiles) {
    // --- 画像が無いときは空にする（CSSの :empty で高さゼロになる） ---
    attachments.innerHTML = "";

    const files = Array.from(selectedFiles || []);
    if (!files.length) return;

    // --- 画像名だけ簡易表示（UI上の識別用） ---
    for (const file of files) {
      const chip = document.createElement("div");
      chip.className = "chip";
      chip.textContent = String(file && file.name ? file.name : "image");
      attachments.appendChild(chip);
    }
  }

  function autoResizeTextarea() {
    // --- LINE系の入力欄っぽく、内容に応じて高さを伸縮する ---
    // NOTE: max-height は CSS 側（120px）と合わせる
    textInput.style.height = "auto";
    const maxHeight = 120;
    const next = Math.min(Number(textInput.scrollHeight || 0), maxHeight);
    textInput.style.height = `${Math.max(next, 20)}px`;
  }

  async function filesToDataUris(files) {
    // --- 画像上限（設計と合わせる） ---
    const maxFiles = 5;
    const maxBytesPerFile = 5 * 1024 * 1024;
    const maxTotalBytes = 20 * 1024 * 1024;

    const list = Array.from(files || []).slice(0, maxFiles);
    let total = 0;
    for (const file of list) total += Number(file.size || 0);
    if (total > maxTotalBytes) {
      throw new Error("画像の合計サイズが大きすぎます（20MB以内）");
    }
    for (const file of list) {
      if (Number(file.size || 0) > maxBytesPerFile) {
        throw new Error(`画像が大きすぎます（5MB以内）: ${file.name}`);
      }
    }

    // --- FileReader で Data URI 化 ---
    const out = [];
    for (const file of list) {
      const dataUri = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("画像の読み込みに失敗しました"));
        reader.onload = () => resolve(String(reader.result || ""));
        reader.readAsDataURL(file);
      });
      out.push(dataUri);
    }
    return out;
  }

  function refreshSendButtonEnabled() {
    const text = String(textInput.value || "").trim();
    const hasFiles = fileInput.files && fileInput.files.length > 0;
    sendButton.disabled = !(text || hasFiles) || !!chatAbortController;
  }

  // --- SSE ---
  async function consumeSse(stream, onEvent) {
    // --- TextDecoder + buffer ---
    const decoder = new TextDecoder("utf-8");
    const reader = stream.getReader();
    let buffer = "";

    // --- Parse one SSE event block ---
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

    // --- Read loop ---
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // --- SSE events are separated by blank line ---
      while (true) {
        const idx = buffer.indexOf("\n\n");
        if (idx === -1) break;
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        if (block.trim()) parseEventBlock(block);
      }
    }

    // --- Tail ---
    if (buffer.trim()) parseEventBlock(buffer);
  }

  // --- Chat send ---
  async function sendChat() {
    if (chatAbortController) return;

    const messageText = String(textInput.value || "").trim();
    const selectedFiles = fileInput.files;

    // --- User bubble (text or [画像]) ---
    const userLabel = messageText || (selectedFiles && selectedFiles.length ? "[画像]" : "");
    if (userLabel) appendBubble("user", userLabel);

    // --- Images to Data URIs ---
    let images = [];
    try {
      images = await filesToDataUris(selectedFiles);
    } catch (error) {
      setStatusBar("状態: 送信失敗", String(error && error.message ? error.message : error));
      return;
    }

    // --- Prepare ---
    chatAbortController = new AbortController();
    inflightAssistantBubble = appendBubble("ai", "");
    setStatusBar("状態: 送信中...", "");
    refreshSendButtonEnabled();

    try {
      const response = await fetch(apiChatPath, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          input_text: messageText,
          images: images,
          client_context: { locale: navigator.language || "ja-JP" },
        }),
        signal: chatAbortController.signal,
      });

      if (!response.ok || !response.body) {
        setStatusBar("状態: 送信失敗", `HTTP ${response.status}`);
        return;
      }

      await consumeSse(response.body, (eventName, data) => {
        if (!inflightAssistantBubble) return;

        if (eventName === "token") {
          inflightAssistantBubble.textContent += String(data.text || "");
          scrollToBottom();
          return;
        }

        if (eventName === "error") {
          const msg = String(data.message || "error");
          inflightAssistantBubble.textContent = msg;
          inflightAssistantBubble = null;
          setStatusBar("状態: エラー", String(data.code || ""));
          return;
        }

        if (eventName === "done") {
          inflightAssistantBubble = null;
          setStatusBar("状態: 完了", "");
          return;
        }
      });
    } catch (error) {
      if (error && error.name === "AbortError") {
        setStatusBar("状態: 中断", "");
      } else {
        setStatusBar("状態: エラー", String(error && error.message ? error.message : error));
      }
    } finally {
      // --- Clear inputs ---
      textInput.value = "";
      fileInput.value = "";
      renderAttachments([]);
      autoResizeTextarea();

      // --- Reset state ---
      chatAbortController = null;
      inflightAssistantBubble = null;
      refreshSendButtonEnabled();
    }
  }

  // --- Events wiring ---
  /**
   * ログイン処理（手動ログイン）。
   * ボタン押下/Enter どちらでも同じ挙動にする。
   */
  async function submitLogin() {
    // --- Token を送ってログイン ---
    setLoginStatus("ログイン中...", false);
    const ok = await apiLogin(loginTokenInput.value);
    if (!ok) {
      setLoginStatus("ログインに失敗しました（Tokenが違います）", true);
      setStatusBar("状態: ログイン失敗", "");
      return;
    }

    // --- Switch UI ---
    loginTokenInput.value = "";
    setLoginStatus("", false);
    showChat();
    connectEventsSocket();
    refreshSendButtonEnabled();
  }

  loginButton.addEventListener("click", async () => {
    // --- ボタン押下でログイン ---
    await submitLogin();
  });

  loginTokenInput.addEventListener("keydown", async (event) => {
    // --- Enter でログイン（Shift+Enter は何もしない） ---
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    await submitLogin();
  });

  // --- Icons: no-op except gear (logout) ---
  micButton.addEventListener("click", () => {});
  soundButton.addEventListener("click", () => {});
  screenButton.addEventListener("click", () => {});

  gearButton.addEventListener("click", async () => {
    const ok = confirm("ログアウトしますか？");
    if (!ok) return;

    stopEventsSocket();
    await apiLogout();
    showLogin();
    setLoginStatus("", false);
  });

  // --- Input handling ---
  sendButton.addEventListener("click", () => sendChat());

  textInput.addEventListener("input", () => {
    autoResizeTextarea();
    refreshSendButtonEnabled();
  });
  fileInput.addEventListener("change", () => {
    renderAttachments(fileInput.files);
    refreshSendButtonEnabled();
  });

  textInput.addEventListener("keydown", (event) => {
    // --- Enter 送信（Shift+Enter は改行） ---
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendChat();
      return;
    }
  });

  // --- Init ---
  (async () => {
    // --- まず自動ログインを試す（有効ならログイン画面を出さない） ---
    setLoginStatus("自動ログイン中...", false);
    const ok = await apiAutoLogin();
    if (ok) {
      // --- Switch UI ---
      setLoginStatus("", false);
      showChat();
      connectEventsSocket();
      autoResizeTextarea();
      refreshSendButtonEnabled();
      return;
    }

    // --- 従来ログインへフォールバック ---
    stopEventsSocket();
    showLogin();
    setLoginStatus("※自己署名HTTPSの警告は許容してください", false);
    autoResizeTextarea();
    refreshSendButtonEnabled();
  })();
})();
