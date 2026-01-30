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
  const cameraButton = document.getElementById("btn-camera");
  const cameraInput = document.getElementById("camera-input");
  const attachments = document.getElementById("attachments");
  const sendButton = document.getElementById("btn-send");

  const statusLeft = document.getElementById("status-left");
  const statusRight = document.getElementById("status-right");

  // --- Camera modal DOM refs ---
  const cameraModal = document.getElementById("camera-modal");
  const cameraVideo = document.getElementById("camera-video");
  const cameraCancelButton = document.getElementById("btn-camera-cancel");
  const cameraShotButton = document.getElementById("btn-camera-shot");
  const cameraStatus = document.getElementById("camera-status");
  const cameraFlipButton = document.getElementById("btn-camera-flip");
  const cameraZoomWrap = document.getElementById("camera-zoom");
  const cameraZoomSlider = document.getElementById("camera-zoom-slider");
  const cameraZoomValue = document.getElementById("camera-zoom-value");

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
  /** @type {string[]} */
  let attachmentPreviewObjectUrls = [];
  /** @type {File[]} */
  let selectedImageFiles = [];

  // --- Camera state ---
  /** @type {MediaStream|null} */
  let cameraStream = null;
  /** @type {boolean} */
  let cameraOpening = false;
  /** @type {"environment"|"user"} */
  let cameraFacingMode = "environment";
  /** @type {MediaStreamTrack|null} */
  let cameraVideoTrack = null;
  /** @type {{min:number,max:number,step:number,default:number}|null} */
  let cameraZoomCaps = null;
  /** @type {number|null} */
  let cameraZoomRafId = null;

  // --- Mobile viewport (keyboard) ---
  /**
   * モバイルのソフトキーボードで入力欄が隠れる問題に対処する。
   *
   * 背景:
   * - iOS Safari では、キーボード表示中に layout viewport の高さが縮まず、
   *   画面下部（composer/statusbar）がキーボードに隠れることがある。
   *
   * 方針:
   * - VisualViewport API（あれば）で「見えている高さ」を取得し、CSS変数 --app-height に反映する。
   * - app コンテナの height を --app-height に合わせることで、下部UIが可視領域に収まる。
   */
  function installMobileViewportHeightSync() {
    const root = document.documentElement;
    let rafId = null;

    /**
     * チャットが最下端付近にいるか（ユーザーが最下端で読んでいる状態か）を判定する。
     *
     * NOTE:
     * - viewport の高さが変わると、最下端にいたのに少し上へ戻ることがある。
     * - その場合だけ scrollToBottom() を当てて「最下端スティッキー」を実現する。
     */
    const isChatNearBottom = () => {
      // --- チャット画面が表示されているときだけ対象 ---
      if (panelChat.classList.contains("hidden")) return false;

      // --- 末尾からの距離で判定（少しの誤差は許容） ---
      const el = chatScroll;
      const gap = Number(el.scrollHeight || 0) - Number(el.clientHeight || 0) - Number(el.scrollTop || 0);
      return gap <= 12;
    };

    const updateNow = () => {
      rafId = null;

      // --- 変更前に「最下端だったか」を記録（変更で少し戻るのを防ぐ） ---
      const shouldStickToBottom = isChatNearBottom();

      const vv = window.visualViewport;
      const height = vv && Number(vv.height) > 0 ? Number(vv.height) : Number(window.innerHeight || 0);
      if (height > 0) {
        root.style.setProperty("--app-height", `${Math.round(height)}px`);
      }

      // --- 最下端にいた場合は、レイアウト反映後に最下端へ戻す ---
      if (shouldStickToBottom) {
        requestAnimationFrame(() => {
          scrollToBottom();
        });
      }
    };

    const scheduleUpdate = () => {
      if (rafId != null) return;
      rafId = requestAnimationFrame(updateNow);
    };

    // --- 初回 ---
    scheduleUpdate();

    // --- 画面回転/リサイズ ---
    window.addEventListener("resize", scheduleUpdate, { passive: true });

    // --- VisualViewport があれば、キーボード表示/スクロールも拾う ---
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", scheduleUpdate, { passive: true });
      window.visualViewport.addEventListener("scroll", scheduleUpdate, { passive: true });
    }

    // --- フォーカス/ブラーでも追従（キーボード表示直後の遅延を吸収） ---
    textInput.addEventListener("focus", () => {
      scheduleUpdate();
      setTimeout(() => {
        scheduleUpdate();
        scrollToBottom();
      }, 50);
    });
    textInput.addEventListener("blur", () => {
      scheduleUpdate();
      setTimeout(scheduleUpdate, 50);
    });
  }

  // --- Text sanitizers ---
  /**
   * 会話装飾タグ（例: [face:Fun]）をUI表示向けに除去する。
   *
   * 目的:
   * - CocoroShell 等が利用する装飾タグを、Web UI ではそのまま見せない。
   * - SSE のストリーミング中にタグが「途中まで」表示されてチラつくのも抑える。
   *
   * NOTE:
   * - [face:*] は本文の先頭に付くことが多いが、念のため本文中の出現も除去する。
   * - ストリーミング中に tag が未完（"]" がまだ来ていない）な場合は、tag開始以降を一時的に隠す。
   */
  function sanitizeChatTextForDisplay(text) {
    // --- 入力を文字列へ正規化 ---
    let s = String(text || "");

    // --- 末尾に未完の [face:... が残っている場合は一旦隠す（ストリーム中のチラつき防止） ---
    const lastIdx = s.lastIndexOf("[face:");
    if (lastIdx !== -1) {
      const closeIdx = s.indexOf("]", lastIdx);
      if (closeIdx === -1) {
        s = s.slice(0, lastIdx);
      }
    }

    // --- 完了した [face:...] を除去 ---
    s = s.replace(/\[face:[^\]]+\]/g, "");

    // --- タグ除去で増えがちな空行を軽く整形（見た目だけ） ---
    s = s.replace(/^\s+/, "");
    s = s.replace(/\n{3,}/g, "\n\n");
    return s;
  }

  /**
   * 吹き出しへ「生テキスト」を保存して、表示はサニタイズした文字列にする。
   *
   * NOTE:
   * - ストリーミング中は raw を保持し続け、毎回サニタイズ結果で描画する。
   * - これにより、途中で [face:*] が入っても UI で表示されない。
   */
  function setBubbleTextFromRaw(bubble, rawText) {
    if (!bubble) return;
    // --- 生テキストを保持 ---
    bubble.dataset.rawText = String(rawText || "");

    // --- 子要素（テキスト領域）へ反映 ---
    let textEl = bubble.querySelector(".bubble-text");
    if (!textEl) {
      textEl = document.createElement("div");
      textEl.className = "bubble-text";
      bubble.appendChild(textEl);
    }
    textEl.textContent = sanitizeChatTextForDisplay(bubble.dataset.rawText);

    // --- テキストが空なら余白を減らす（画像だけのとき） ---
    const hasText = !!String(textEl.textContent || "").trim();
    textEl.classList.toggle("hidden", !hasText);
  }

  /**
   * 吹き出しに画像プレビュー（data URI 等）を表示する。
   *
   * NOTE:
   * - Web UI は履歴を永続化しない前提なので、data URI をそのままDOMに持たせてよい。
   */
  function setBubbleImages(bubble, imageSrcList) {
    if (!bubble) return;

    // --- 画像領域を確保 ---
    let imagesEl = bubble.querySelector(".bubble-images");
    if (!imagesEl) {
      imagesEl = document.createElement("div");
      imagesEl.className = "bubble-images";
      bubble.appendChild(imagesEl);
    }

    // --- 一旦クリア ---
    imagesEl.innerHTML = "";

    const list = Array.from(imageSrcList || []).filter((x) => String(x || "").trim());
    if (!list.length) {
      imagesEl.classList.add("hidden");
      return;
    }

    imagesEl.classList.remove("hidden");

    // --- サムネイルを追加 ---
    for (const src of list) {
      const img = document.createElement("img");
      img.className = "bubble-image";
      img.alt = "image";
      img.src = String(src);
      img.loading = "lazy";

      // NOTE:
      // - data URI を新規ウィンドウで開こうとすると、環境によってポップアップブロックされやすい。
      // - クリックで何も起きなくてよいので、リンクにはせず画像として表示する。
      imagesEl.appendChild(img);
    }
  }

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
    // --- 念のため、開いているカメラがあれば閉じる ---
    closeCameraModal();
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

  // --- Camera helpers ---
  /**
   * カメラ（getUserMedia）が利用可能かを判定する。
   */
  function canUseGetUserMedia() {
    return !!(navigator && navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === "function");
  }

  /**
   * カメラモーダルのステータス表示を更新する。
   */
  function setCameraStatus(text, isError) {
    if (!cameraStatus) return;
    cameraStatus.textContent = String(text || "");
    cameraStatus.classList.toggle("error", !!isError);
  }

  /**
   * ズームUIを初期化する（非対応なら隠す）。
   */
  function initCameraZoomUiFromTrack(track) {
    cameraVideoTrack = track || null;
    cameraZoomCaps = null;

    if (!cameraZoomWrap || !cameraZoomSlider || !cameraZoomValue) return;

    // --- 既定では隠す ---
    cameraZoomWrap.classList.add("hidden");

    // --- Capabilities 取得 ---
    if (!track || typeof track.getCapabilities !== "function") return;
    const caps = track.getCapabilities();
    if (!caps || typeof caps !== "object" || !("zoom" in caps)) return;

    const zoom = caps.zoom;
    const min = Number(zoom && zoom.min != null ? zoom.min : 1);
    const max = Number(zoom && zoom.max != null ? zoom.max : 1);
    const step = Number(zoom && zoom.step != null ? zoom.step : 0.1);
    if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return;

    // --- Settings の現在値（可能なら反映） ---
    let current = 1;
    if (typeof track.getSettings === "function") {
      const settings = track.getSettings();
      if (settings && settings.zoom != null && Number.isFinite(Number(settings.zoom))) {
        current = Number(settings.zoom);
      }
    }
    current = Math.min(max, Math.max(min, current));

    cameraZoomCaps = { min, max, step: step > 0 ? step : 0.1, default: current };

    cameraZoomSlider.min = String(min);
    cameraZoomSlider.max = String(max);
    cameraZoomSlider.step = String(cameraZoomCaps.step);
    cameraZoomSlider.value = String(current);
    cameraZoomValue.textContent = `${current.toFixed(1)}×`;

    // --- 対応しているときだけ表示 ---
    cameraZoomWrap.classList.remove("hidden");
  }

  /**
   * 現在の video track にズームを適用する。
   */
  async function applyCameraZoom(zoomValue) {
    const track = cameraVideoTrack;
    if (!track) return;
    if (!cameraZoomCaps) return;
    if (typeof track.applyConstraints !== "function") return;

    const z = Number(zoomValue);
    if (!Number.isFinite(z)) return;
    const next = Math.min(cameraZoomCaps.max, Math.max(cameraZoomCaps.min, z));

    // --- 可能なら標準の constraints を適用 ---
    try {
      await track.applyConstraints({ advanced: [{ zoom: next }] });
      return;
    } catch (_) {}

    // --- 実装差分の吸収（advanced が効かない環境向け） ---
    try {
      await track.applyConstraints({ zoom: next });
    } catch (_) {}
  }

  /**
   * カメラモーダルを開く（getUserMedia を開始し、video にストリームを貼る）。
   *
   * NOTE:
   * - 失敗した場合は例外を投げる（呼び元で capture input へフォールバックする）。
   */
  async function openCameraModalWithPreview() {
    if (!cameraModal || !cameraVideo) throw new Error("camera modal not found");
    if (!canUseGetUserMedia()) throw new Error("getUserMedia not supported");
    if (cameraOpening) throw new Error("camera opening");

    cameraOpening = true;
    setCameraStatus("カメラを起動中...", false);
    cameraModal.classList.remove("hidden");

    // --- カメラ起動（背面カメラ優先） ---
    const constraints = {
      audio: false,
      video: {
        facingMode: { ideal: cameraFacingMode },
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
    };

    try {
      cameraStream = await navigator.mediaDevices.getUserMedia(constraints);
      cameraVideo.srcObject = cameraStream;

      // --- iOS などで play() が必要なことがある ---
      try {
        await cameraVideo.play();
      } catch (_) {}

      // --- ズームUI（対応していれば表示） ---
      try {
        const track = cameraStream.getVideoTracks && cameraStream.getVideoTracks()[0];
        initCameraZoomUiFromTrack(track || null);
      } catch (_) {
        initCameraZoomUiFromTrack(null);
      }

      setCameraStatus("", false);
    } catch (error) {
      // --- 表示は閉じる（フォールバックを優先） ---
      closeCameraModal();
      throw error;
    } finally {
      cameraOpening = false;
    }
  }

  /**
   * カメラモーダルを閉じる（ストリームを停止してリソースを解放）。
   */
  function closeCameraModal() {
    // --- ズーム関連の state をリセット ---
    cameraVideoTrack = null;
    cameraZoomCaps = null;
    if (cameraZoomWrap) cameraZoomWrap.classList.add("hidden");

    if (cameraVideo) {
      try {
        cameraVideo.pause();
      } catch (_) {}
      try {
        cameraVideo.srcObject = null;
      } catch (_) {}
    }

    if (cameraStream) {
      try {
        for (const track of cameraStream.getTracks()) {
          try {
            track.stop();
          } catch (_) {}
        }
      } catch (_) {}
    }
    cameraStream = null;

    if (cameraModal) cameraModal.classList.add("hidden");
    setCameraStatus("", false);
  }

  /**
   * video フレームを JPEG にして File 化する（サイズ制限に収まるよう軽く圧縮）。
   */
  async function captureJpegFileFromVideo(videoEl) {
    const video = videoEl;
    if (!video) throw new Error("video not ready");

    const vw = Number(video.videoWidth || 0);
    const vh = Number(video.videoHeight || 0);
    if (!vw || !vh) throw new Error("video size not ready");

    // --- 5MB制限に近づきにくいように縮小（最大辺 1280px） ---
    const maxSide = 1280;
    const scale = Math.min(1, maxSide / Math.max(vw, vh));
    const tw = Math.max(1, Math.round(vw * scale));
    const th = Math.max(1, Math.round(vh * scale));

    const canvas = document.createElement("canvas");
    canvas.width = tw;
    canvas.height = th;

    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("canvas ctx not available");
    ctx.drawImage(video, 0, 0, tw, th);

    const blob = await new Promise((resolve, reject) => {
      canvas.toBlob(
        (b) => {
          if (!b) {
            reject(new Error("撮影に失敗しました"));
            return;
          }
          resolve(b);
        },
        "image/jpeg",
        0.86
      );
    });

    const name = `camera-${Date.now()}.jpg`;
    return new File([blob], name, { type: "image/jpeg" });
  }

  // --- Chat timestamp helpers ---
  /**
   * Date を "HH:MM"（ローカル時刻）に整形する。
   */
  function formatTimeHHmm(date) {
    const d = date instanceof Date ? date : new Date();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  }

  /**
   * 吹き出しの時刻ラベルを更新する。
   *
   * 目的:
   * - 送信直後のプレースホルダー（応答中…）には時刻を出さず、
   *   実際に応答を受信した瞬間の時刻を表示する（LINE風）。
   */
  function setBubbleTimeFromTs(bubble, tsMs) {
    if (!bubble) return;
    const row = bubble.closest(".bubble-row");
    if (!row) return;

    const label = row.querySelector(".bubble-time");
    if (!label) return;

    label.textContent = formatTimeHHmm(new Date(Number(tsMs || Date.now())));
    label.classList.remove("empty");
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
    setBubbleTextFromRaw(bubble, text || "");
    return bubble;
  }

  function createBubbleTimeLabel(tsMs) {
    // --- LINE風に、吹き出しの隣へ小さく時刻を付ける ---
    const label = document.createElement("span");
    label.className = "bubble-time";
    if (tsMs == null) {
      // --- プレースホルダー等: 時刻未確定（受信時刻で上書きする） ---
      label.textContent = "";
      label.classList.add("empty");
      return label;
    }

    label.textContent = formatTimeHHmm(new Date(Number(tsMs || Date.now())));
    return label;
  }

  function appendBubble(kind, text, options) {
    // --- options ---
    // - timestampMs: 時刻（ms）。null の場合は未表示（後で更新する）
    // - images: data URI 等の画像配列（バブル内にプレビュー表示）
    const timestampMs = options && Object.prototype.hasOwnProperty.call(options, "timestampMs") ? options.timestampMs : Date.now();
    const images = options && Object.prototype.hasOwnProperty.call(options, "images") ? options.images : null;

    const row = createBubbleRow(kind);
    const bubble = createBubble(kind, text);
    const timeLabel = createBubbleTimeLabel(timestampMs);

    // --- user/ai で時刻の位置を入れ替える（LINEっぽい配置） ---
    if (kind === "user") {
      row.appendChild(timeLabel);
      row.appendChild(bubble);
    } else {
      row.appendChild(bubble);
      row.appendChild(timeLabel);
    }

    chatScroll.appendChild(row);
    scrollToBottom();

    // --- 画像（任意） ---
    if (images && Array.isArray(images) && images.length) {
      setBubbleImages(bubble, images);
    }

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
  /**
   * 選択中の添付を削除する（添付は File 配列で管理する）。
   */
  function removeAttachmentAtIndex(index) {
    const i = Number(index);
    if (!Number.isFinite(i) || i < 0 || i >= selectedImageFiles.length) return;

    // --- 指定を削除してUI更新 ---
    selectedImageFiles.splice(i, 1);
    renderAttachments(selectedImageFiles);
    refreshSendButtonEnabled();
  }

  /**
   * 添付に File を追加する（上限: 5枚）。
   */
  function addAttachmentsFromFiles(files) {
    const list = Array.from(files || []).filter((f) => f && String(f.type || "").startsWith("image/"));
    if (!list.length) return;

    const maxFiles = 5;
    if (selectedImageFiles.length >= maxFiles) {
      alert("画像は最大5枚までです。");
      return;
    }

    const room = maxFiles - selectedImageFiles.length;
    const toAdd = list.slice(0, room);
    if (list.length > toAdd.length) {
      alert("画像は最大5枚までです。");
    }

    selectedImageFiles = selectedImageFiles.concat(toAdd);
    renderAttachments(selectedImageFiles);
    refreshSendButtonEnabled();
  }

  function revokeAttachmentPreviewObjectUrls() {
    // --- 以前の objectURL を解放する（選び直し時のリーク防止） ---
    const urls = Array.from(attachmentPreviewObjectUrls || []);
    attachmentPreviewObjectUrls = [];
    for (const u of urls) {
      try {
        URL.revokeObjectURL(u);
      } catch (_) {}
    }
  }

  function renderAttachments(selectedFiles) {
    // --- 画像が無いときは空にする（CSSの :empty で高さゼロになる） ---
    attachments.innerHTML = "";
    revokeAttachmentPreviewObjectUrls();

    const files = Array.from(selectedFiles || []);
    if (!files.length) return;

    // --- 画像プレビュー（LINE風） ---
    for (const file of files) {
      if (!file || !String(file.type || "").startsWith("image/")) {
        continue;
      }

      const url = URL.createObjectURL(file);
      attachmentPreviewObjectUrls.push(url);

      const item = document.createElement("div");
      item.className = "attachment";

      const thumbWrap = document.createElement("div");
      thumbWrap.className = "attachment-thumb-wrap";

      const img = document.createElement("img");
      img.className = "attachment-thumb";
      img.alt = String(file && file.name ? file.name : "image");
      img.src = url;
      img.loading = "lazy";

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "attachment-remove";
      removeBtn.setAttribute("aria-label", "添付を削除");
      removeBtn.textContent = "×";
      removeBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        removeAttachmentAtIndex(files.indexOf(file));
      });

      thumbWrap.appendChild(img);
      item.appendChild(thumbWrap);
      item.appendChild(removeBtn);
      attachments.appendChild(item);
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
    const hasFiles = Array.isArray(selectedImageFiles) && selectedImageFiles.length > 0;
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
    // NOTE: 送信中に選択状態が変わっても影響しないよう、ここでスナップショットを取る。
    const selectedFileArray = Array.from(selectedImageFiles || []);

    // --- User bubble (text or [画像]) ---
    const hasFiles = selectedFileArray.length > 0;
    const userBubbleText = messageText || "";
    const userBubble = appendBubble("user", userBubbleText || (hasFiles ? "" : ""), { timestampMs: Date.now() });

    // --- 入力欄は送信時点でクリア（応答待ちの間に残さない） ---
    // NOTE:
    // - messageText は上で確保済み。
    // - 画像（fileInput）は、送信失敗時に再送できるようここでは消さない（従来どおり finally でクリア）。
    textInput.value = "";
    autoResizeTextarea();

    // --- Images to Data URIs ---
    let images = [];
    try {
      images = await filesToDataUris(selectedFileArray);
    } catch (error) {
      setStatusBar("状態: 送信失敗", String(error && error.message ? error.message : error));
      return;
    }

    // --- 送信が確定したら、入力欄側の添付は消す（プレビューも含む） ---
    selectedImageFiles = [];
    renderAttachments([]);
    refreshSendButtonEnabled();

    // --- 送信後もバブル内に画像プレビューを残す（data URI で保持） ---
    if (userBubble && images && images.length) {
      setBubbleImages(userBubble, images);
      scrollToBottom();
    }

    // --- Prepare ---
    chatAbortController = new AbortController();
    // --- 送信直後にプレースホルダーを出す（時刻は受信時に確定） ---
    inflightAssistantBubble = appendBubble("ai", "応答中…", { timestampMs: null });
    setBubbleTextFromRaw(inflightAssistantBubble, "応答中…");
    // --- 応答本文の受信が始まったか（プレースホルダーを捨てるためのフラグ） ---
    inflightAssistantBubble.dataset.hasRealContent = "0";
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
        if (eventName === "token") {
          if (!inflightAssistantBubble) return;

          // --- 初回受信: プレースホルダーを本文に置き換え、時刻を確定する ---
          if (String(inflightAssistantBubble.dataset.hasRealContent || "0") !== "1") {
            inflightAssistantBubble.dataset.hasRealContent = "1";
            setBubbleTextFromRaw(inflightAssistantBubble, "");
            setBubbleTimeFromTs(inflightAssistantBubble, Date.now());
          }

          // --- 生テキストへ追記して、表示はサニタイズして更新する ---
          const prevRaw = String(inflightAssistantBubble.dataset.rawText || "");
          const nextRaw = prevRaw + String(data.text || "");
          setBubbleTextFromRaw(inflightAssistantBubble, nextRaw);
          scrollToBottom();
          return;
        }

        if (eventName === "error") {
          const msg = String(data.message || "error");
          if (!inflightAssistantBubble) return;

          // --- エラー受信時刻を確定する ---
          setBubbleTimeFromTs(inflightAssistantBubble, Date.now());
          setBubbleTextFromRaw(inflightAssistantBubble, msg);
          inflightAssistantBubble = null;
          setStatusBar("状態: エラー", String(data.code || ""));
          return;
        }

        if (eventName === "done") {
          if (!inflightAssistantBubble) return;

          // --- token が来ないケースでも reply_text があるなら置き換える ---
          const replyText = String(data.reply_text || "");
          if (replyText) {
            setBubbleTimeFromTs(inflightAssistantBubble, Date.now());
            setBubbleTextFromRaw(inflightAssistantBubble, replyText);
            inflightAssistantBubble = null;
            setStatusBar("状態: 完了", "");
            return;
          }

          // --- token で本文が入っているなら、時刻だけ確定して終了 ---
          if (String(inflightAssistantBubble.dataset.hasRealContent || "0") === "1") {
            setBubbleTimeFromTs(inflightAssistantBubble, Date.now());
          }
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

    closeCameraModal();
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
    // --- 追加選択（選択が置き換えになっても、state に append するため問題ない） ---
    addAttachmentsFromFiles(fileInput.files);

    // --- 同じ画像を連続で選べるようにクリア ---
    fileInput.value = "";
  });

  cameraInput.addEventListener("change", () => {
    // --- capture input で撮影/選択した画像を添付へ追加 ---
    addAttachmentsFromFiles(cameraInput.files);
    cameraInput.value = "";
  });

  cameraButton.addEventListener("click", async () => {
    // --- 対応環境: プレビュー撮影（getUserMedia） ---
    if (canUseGetUserMedia()) {
      try {
        await openCameraModalWithPreview();
        return;
      } catch (_) {
        // --- フォールバックへ ---
      }
    }

    // --- 非対応/許可不可: capture input へフォールバック ---
    try {
      cameraInput.click();
    } catch (_) {}
  });

  // --- Camera modal events ---
  if (cameraModal) {
    // --- 背景クリックで閉じる ---
    const backdrop = cameraModal.querySelector(".modal-backdrop");
    if (backdrop) {
      backdrop.addEventListener("click", () => {
        closeCameraModal();
      });
    }
  }

  if (cameraCancelButton) {
    cameraCancelButton.addEventListener("click", () => {
      closeCameraModal();
    });
  }

  if (cameraShotButton) {
    cameraShotButton.addEventListener("click", async () => {
      if (!cameraVideo) return;
      cameraShotButton.disabled = true;
      setCameraStatus("撮影中...", false);
      try {
        const file = await captureJpegFileFromVideo(cameraVideo);
        addAttachmentsFromFiles([file]);
        closeCameraModal();
      } catch (error) {
        setCameraStatus(String(error && error.message ? error.message : "撮影に失敗しました"), true);
      } finally {
        cameraShotButton.disabled = false;
      }
    });
  }

  // --- Camera controls (flip/zoom) ---
  if (cameraFlipButton) {
    cameraFlipButton.addEventListener("click", async () => {
      if (!cameraModal || cameraModal.classList.contains("hidden")) return;
      if (!canUseGetUserMedia()) return;
      if (cameraOpening) return;

      // --- イン/アウト切替（実際は facingMode を切り替えて再起動） ---
      cameraFacingMode = cameraFacingMode === "environment" ? "user" : "environment";
      setCameraStatus("カメラ切替中...", false);

      try {
        closeCameraModal();
        await openCameraModalWithPreview();
      } catch (_) {
        // --- 失敗した場合は capture input にフォールバック ---
        try {
          cameraInput.click();
        } catch (_) {}
      }
    });
  }

  if (cameraZoomSlider) {
    cameraZoomSlider.addEventListener("input", () => {
      if (!cameraZoomValue) return;
      const v = Number(cameraZoomSlider.value);
      if (Number.isFinite(v)) cameraZoomValue.textContent = `${v.toFixed(1)}×`;

      // --- input 連打を軽く間引く ---
      if (cameraZoomRafId != null) return;
      cameraZoomRafId = requestAnimationFrame(async () => {
        cameraZoomRafId = null;
        await applyCameraZoom(Number(cameraZoomSlider.value));
      });
    });
  }

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
    // --- モバイルのキーボード対策（ログイン前でも必要） ---
    installMobileViewportHeightSync();

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
