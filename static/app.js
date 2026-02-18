/**
 * CocoroGhost Web UI
 * - CocoroUI.png 互換レイアウト（ゼロベース）
 *
 * 目的:
 * - /api/auth/login で Cookie セッションを張る
 * - /api/chat の SSE を逐次表示する
 * - /api/settings をタブ形式のフォームで編集/保存できるようにする
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
  const apiSettingsPath = "/api/settings";
  // NOTE: CocoroGhost は HTTPS 必須（自己署名TLS）なので WebSocket も wss を使う。
  const webSocketEventsUrl = `wss://${location.host}/api/events/stream`;

  // --- DOM refs ---
  const panelLogin = document.getElementById("panel-login");
  const panelChat = document.getElementById("panel-chat");
  const panelSettings = document.getElementById("panel-settings");
  const inputPanel = document.getElementById("input-panel");

  const loginTokenInput = document.getElementById("login-token");
  const loginStatus = document.getElementById("login-status");
  const loginButton = document.getElementById("btn-login");

  const micButton = document.getElementById("btn-mic");
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
  const settingsStatus = document.getElementById("settings-status");
  const settingsBackButton = document.getElementById("btn-settings-back");
  const settingsReloadButton = document.getElementById("btn-settings-reload");
  const settingsSaveButton = document.getElementById("btn-settings-save");
  const settingsLogoutButton = document.getElementById("btn-settings-logout");
  const settingsTabButtons = Array.from(document.querySelectorAll(".settings-tab"));
  const settingsPageNodes = Array.from(document.querySelectorAll("[data-tab-page]"));

  const settingsActiveLlmSelect = document.getElementById("settings-active-llm");
  const llmAddButton = document.getElementById("btn-llm-add");
  const llmDuplicateButton = document.getElementById("btn-llm-duplicate");
  const llmDeleteButton = document.getElementById("btn-llm-delete");
  const llmNameInput = document.getElementById("llm-name");
  const llmModelInput = document.getElementById("llm-model");
  const llmApiKeyInput = document.getElementById("llm-api-key");
  const llmBaseUrlInput = document.getElementById("llm-base-url");
  const llmReasoningEffortInput = document.getElementById("llm-reasoning-effort");
  const llmMaxTurnsWindowInput = document.getElementById("llm-max-turns-window");
  const llmMaxTokensInput = document.getElementById("llm-max-tokens");
  const llmReplyWebSearchEnabledInput = document.getElementById("llm-reply-web-search-enabled");
  const llmImageModelInput = document.getElementById("llm-image-model");
  const llmImageApiKeyInput = document.getElementById("llm-image-api-key");
  const llmImageBaseUrlInput = document.getElementById("llm-image-base-url");
  const llmMaxTokensVisionInput = document.getElementById("llm-max-tokens-vision");
  const llmImageTimeoutSecondsInput = document.getElementById("llm-image-timeout-seconds");

  const settingsMemoryEnabledInput = document.getElementById("settings-memory-enabled");
  const settingsActiveEmbeddingSelect = document.getElementById("settings-active-embedding");
  const embeddingAddButton = document.getElementById("btn-embedding-add");
  const embeddingDuplicateButton = document.getElementById("btn-embedding-duplicate");
  const embeddingDeleteButton = document.getElementById("btn-embedding-delete");
  const embeddingNameInput = document.getElementById("embedding-name");
  const embeddingModelInput = document.getElementById("embedding-model");
  const embeddingApiKeyInput = document.getElementById("embedding-api-key");
  const embeddingBaseUrlInput = document.getElementById("embedding-base-url");
  const embeddingDimensionInput = document.getElementById("embedding-dimension");
  const embeddingSimilarEpisodesLimitInput = document.getElementById("embedding-similar-episodes-limit");

  const settingsActivePersonaSelect = document.getElementById("settings-active-persona");
  const personaAddButton = document.getElementById("btn-persona-add");
  const personaDuplicateButton = document.getElementById("btn-persona-duplicate");
  const personaDeleteButton = document.getElementById("btn-persona-delete");
  const personaNameInput = document.getElementById("persona-name");
  const personaSecondPersonLabelInput = document.getElementById("persona-second-person-label");
  const personaTextInput = document.getElementById("persona-text");

  const settingsActiveAddonSelect = document.getElementById("settings-active-addon");
  const addonAddButton = document.getElementById("btn-addon-add");
  const addonDuplicateButton = document.getElementById("btn-addon-duplicate");
  const addonDeleteButton = document.getElementById("btn-addon-delete");
  const addonNameInput = document.getElementById("addon-name");
  const addonTextInput = document.getElementById("addon-text");

  const desktopWatchEnabledInput = document.getElementById("desktop-watch-enabled");
  const desktopWatchIntervalSecondsInput = document.getElementById("desktop-watch-interval-seconds");
  const desktopWatchTargetClientIdInput = document.getElementById("desktop-watch-target-client-id");

  // --- Camera modal DOM refs ---
  const cameraModal = document.getElementById("camera-modal");
  const cameraPreview = document.getElementById("camera-preview");
  const cameraVideo = document.getElementById("camera-video");
  const cameraModeLabel = document.getElementById("camera-mode-label");
  const cameraCancelButton = document.getElementById("btn-camera-cancel");
  const cameraShotButton = document.getElementById("btn-camera-shot");
  const cameraStatus = document.getElementById("camera-status");
  const cameraFlipButton = document.getElementById("btn-camera-flip");
  const cameraZoomWrap = document.getElementById("camera-zoom");
  const cameraZoomSlider = document.getElementById("camera-zoom-slider");
  const cameraZoomValue = document.getElementById("camera-zoom-value");
  const cameraRatioButtons = Array.from(document.querySelectorAll(".ratio-btn"));

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
  /** @type {any|null} */
  let settingsDraft = null;
  /** @type {"chat"|"memory"|"persona"|"addon"|"system"} */
  let settingsCurrentTab = "chat";

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
  /** @type {"3:4"|"1:1"|"16:9"} */
  let cameraAspectKey = "3:4";

  // --- Voice input (Web Speech API) state ---
  /** @type {SpeechRecognition|null} */
  let speechRecognition = null;
  /** @type {boolean} */
  let speechListening = false;
  /** @type {boolean} */
  let speechWantedOn = false;
  /** @type {boolean} */
  let speechHadError = false;
  /** @type {number|null} */
  let speechSilenceTimerId = null;
  /** @type {string} */
  let speechBaseText = "";
  /** @type {string} */
  let speechFinalText = "";
  /** @type {string} */
  let speechInterimText = "";
  /** @type {string} */
  let speechSavedStatusRight = "";
  /** @type {string[]} */
  let speechSendQueue = [];

  // --- Camera: preview crop fallback ---
  // NOTE:
  // - 端末によっては aspectRatio の要求が通らず、選択比率と実映像比率がズレることがある。
  // - その場合、プレビューは「選択比率でクロップ表示」に切り替えて、見た目と撮影結果を一致させる。
  if (cameraVideo) {
    // --- 初回に実寸が確定したタイミング ---
    cameraVideo.addEventListener("loadedmetadata", () => {
      syncCameraPreviewCropMode();
    });

    // --- 端末回転などで実寸が変わるタイミング（対応ブラウザのみ） ---
    cameraVideo.addEventListener("resize", () => {
      syncCameraPreviewCropMode();
    });
  }

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

  function setSettingsStatus(text, isError) {
    settingsStatus.textContent = text || "";
    settingsStatus.classList.toggle("error", !!isError);
  }

  function setStatusBar(leftText, rightText) {
    statusLeft.textContent = leftText || "";
    statusRight.textContent = rightText || "";
  }

  function showLogin() {
    // --- 念のため、開いているカメラがあれば閉じる ---
    closeCameraModal();
    stopVoiceInput();
    panelLogin.classList.remove("hidden");
    panelChat.classList.add("hidden");
    panelSettings.classList.add("hidden");
    inputPanel.classList.add("hidden");
    setSettingsStatus("", false);
    setStatusBar("状態: ログイン待ち", "");
  }

  function showChat() {
    panelSettings.classList.add("hidden");
    panelLogin.classList.add("hidden");
    panelChat.classList.remove("hidden");
    inputPanel.classList.remove("hidden");
    refreshSendButtonEnabled();
    setStatusBar("状態: 正常動作中", "");
  }

  function showSettings() {
    // --- 設定編集中は音声入力を停止する ---
    stopVoiceInput();
    panelLogin.classList.add("hidden");
    panelChat.classList.add("hidden");
    panelSettings.classList.remove("hidden");
    inputPanel.classList.add("hidden");
    setSettingsTab(settingsCurrentTab);
    setSettingsStatus("", false);
    setStatusBar("状態: 設定編集中", "");
  }

  function scrollToBottom() {
    chatScroll.scrollTop = chatScroll.scrollHeight;
  }

  // --- Voice helpers ---
  /**
   * Web Speech API（SpeechRecognition）が利用可能かを判定する。
   *
   * NOTE:
   * - 実装状況はブラウザ依存（Chrome 系は webkitSpeechRecognition のことがある）。
   * - ここでは「利用できるなら使う」だけで、品質や言語は環境任せ。
   */
  function canUseSpeechRecognition() {
    const w = window;
    return !!(w && (w.SpeechRecognition || w.webkitSpeechRecognition));
  }

  /**
   * SpeechRecognition のコンストラクタを取得する。
   */
  function getSpeechRecognitionCtor() {
    const w = window;
    return w.SpeechRecognition || w.webkitSpeechRecognition || null;
  }

  /**
   * 音声入力のUI状態を更新する。
   */
  function syncVoiceUiState() {
    // --- ON/OFF は「ユーザーがトグルした状態」を優先して表示する ---
    if (micButton) micButton.classList.toggle("listening", !!speechWantedOn);

    // --- 音声入力ONの間は入力欄を固定（途中編集で意図しない送信になりやすいため） ---
    if (textInput) textInput.disabled = !!speechWantedOn;
    if (sendButton) sendButton.disabled = !!speechWantedOn || !!chatAbortController;

    // --- 右側ステータスは音声入力の表示に使う（左側の接続状態などを壊さない） ---
    if (statusRight) {
      statusRight.textContent = speechWantedOn ? "音声入力ON" : String(speechSavedStatusRight || "");
    }
  }

  /**
   * 音声認識の途中結果を input に反映する（ユーザーが見えるフィードバック用）。
   */
  function renderSpeechTextToInput() {
    const finalText = String(speechFinalText || "").trim();
    const interimText = String(speechInterimText || "").trim();
    const combined = `${finalText}${interimText ? (finalText ? " " : "") + interimText : ""}`.trim();

    if (!textInput) return;

    // --- 音声入力の可視化用に、現在の発話だけを表示する（既存の入力は speechBaseText に退避） ---
    textInput.value = combined;
    autoResizeTextarea();
    refreshSendButtonEnabled();
  }

  /**
   * 無音（結果の更新が止まった）とみなして、現在の発話を「自動送信」するタイマーをセットする。
   *
   * NOTE:
   * - SpeechRecognition の発話区切りは環境差が大きい。
   * - ここでは「最後の onresult から一定時間更新が無ければ送信」として安定動作を狙う。
   */
  function scheduleSpeechSilenceFlush() {
    if (speechSilenceTimerId != null) {
      try {
        clearTimeout(speechSilenceTimerId);
      } catch (_) {}
      speechSilenceTimerId = null;
    }
    if (!speechWantedOn) return;

    // --- 体感: 0.9〜1.3秒くらいが「話し終わり」として自然 ---
    const ms = 1100;
    speechSilenceTimerId = setTimeout(() => {
      // --- 発話の区切りとして、認識セッションを一度止める ---
      // NOTE:
      // - continuous=true で流しっぱなしにすると、環境によって結果が累積して重複しやすい。
      // - ここでは「無音で stop() → onend で送信 → wantedOn なら再起動」で安定させる。
      if (!speechWantedOn) return;

      if (speechRecognition) {
        try {
          speechRecognition.stop();
          return;
        } catch (_) {}
      }

      // --- stop() できない/セッションが無い場合は、今ある結果だけ送って再起動する ---
      flushSpeechUtteranceToQueue(null);
      if (speechWantedOn && !speechHadError) {
        try {
          setTimeout(() => startSpeechRecognitionSession(), 250);
        } catch (_) {}
      }
    }, ms);
  }

  /**
   * キューに溜まっている音声メッセージを、送信可能なタイミングで順次送る。
   */
  function drainSpeechSendQueue() {
    // --- 送信中なら終わってから ---
    if (chatAbortController) return;
    const next = Array.isArray(speechSendQueue) && speechSendQueue.length ? speechSendQueue.shift() : "";
    const text = String(next || "").trim();
    if (!text) {
      return;
    }
    if (!textInput) return;

    // --- sendChat は textInput を読むので、ここで入れて呼ぶ ---
    textInput.value = text;
    autoResizeTextarea();
    refreshSendButtonEnabled();

    // --- 実際の送信 ---
    try {
      sendChat();
    } catch (_) {}
  }

  /**
   * 現在の発話（final/interim）をキューに積んで、送信処理を回す。
   */
  function flushSpeechUtteranceToQueue(options) {
    // --- options ---
    // - force: boolean（OFFに切り替えるタイミングでも残りを送る）
    const force = !!(options && options.force);
    if (!speechWantedOn && !force) return;

    const finalText = String(speechFinalText || "").trim();
    const interimText = String(speechInterimText || "").trim();

    // --- final があるときは final を優先（final+interim を連結すると重複しやすい） ---
    const combined = (finalText || interimText).trim();
    if (!combined) return;

    // --- キューへ積む ---
    speechSendQueue.push(combined);

    // --- 現在の発話をリセット ---
    speechFinalText = "";
    speechInterimText = "";
    renderSpeechTextToInput();

    // --- 送れるなら送る ---
    drainSpeechSendQueue();
  }

  /**
   * SpeechRecognition のセッションを開始する（ON の間は必要に応じて自動再起動する）。
   */
  function startSpeechRecognitionSession() {
    if (!speechWantedOn) return;
    if (!canUseSpeechRecognition()) return;

    const Ctor = getSpeechRecognitionCtor();
    if (!Ctor) return;

    // --- インスタンスは都度作る（環境差で再利用が不安定なことがある） ---
    /** @type {SpeechRecognition} */
    const rec = new Ctor();
    speechRecognition = rec;
    speechHadError = false;

    // --- 設定 ---
    // NOTE:
    // - continuous=false: 無音検出で stop() → onend → 再起動、のループで安定させる
    // - interimResults=true: 入力欄に途中結果を出してフィードバック
    try {
      rec.lang = "ja-JP";
    } catch (_) {}
    rec.continuous = false;
    rec.interimResults = true;
    rec.maxAlternatives = 1;

    rec.onstart = () => {
      speechListening = true;
      syncVoiceUiState();
      renderSpeechTextToInput();
    };

    rec.onresult = (event) => {
      // --- 途中/確定の結果を取り出す ---
      // NOTE:
      // - resultIndex を信頼して「追記」すると、環境によって同じ断片が再送され重複しやすい。
      // - ここでは毎回 event.results 全体から再構築する（1セッション=1発話なので十分速い）。
      const finals = [];
      const interims = [];
      try {
        for (let i = 0; i < event.results.length; i += 1) {
          const res = event.results[i];
          const alt = res && res[0] ? res[0] : null;
          const t = String(alt && alt.transcript != null ? alt.transcript : "").trim();
          if (!t) continue;
          if (res.isFinal) finals.push(t);
          else interims.push(t);
        }
      } catch (_) {}

      speechFinalText = finals.join(" ").trim();
      speechInterimText = interims.join(" ").trim();

      // --- 入力欄へ反映 ---
      renderSpeechTextToInput();

      // --- 無音タイマー更新（無音になったら自動送信） ---
      scheduleSpeechSilenceFlush();
    };

    rec.onerror = (event) => {
      speechHadError = true;
      const err = event && event.error ? String(event.error) : "unknown";
      setStatusBar(String(statusLeft ? statusLeft.textContent || "" : "状態:"), `音声入力エラー: ${err}`);

      // --- エラー時はONを解除（無限再起動を避ける） ---
      speechWantedOn = false;
      syncVoiceUiState();

      try {
        rec.stop();
      } catch (_) {}
    };

    rec.onend = () => {
      speechListening = false;
      speechRecognition = null;

      // --- タイマー停止 ---
      if (speechSilenceTimerId != null) {
        try {
          clearTimeout(speechSilenceTimerId);
        } catch (_) {}
        speechSilenceTimerId = null;
      }

      // --- 終了時に、残っている発話があれば送信 ---
      flushSpeechUtteranceToQueue(null);

      // --- ON のままなら自動で再起動（環境によっては onend が頻繁に来る） ---
      if (speechWantedOn && !speechHadError) {
        try {
          setTimeout(() => startSpeechRecognitionSession(), 250);
        } catch (_) {}
      }

      // --- OFF の場合は下書きを復元（送信が無く、入力欄が空のときだけ） ---
      if (!speechWantedOn) {
        const hasQueued = Array.isArray(speechSendQueue) && speechSendQueue.length > 0;
        const hasInput = !!(textInput && String(textInput.value || "").trim());
        if (!hasQueued && !hasInput && textInput) {
          textInput.value = speechBaseText;
          autoResizeTextarea();
          refreshSendButtonEnabled();
        }
      }

      syncVoiceUiState();
    };

    try {
      rec.start();
    } catch (_) {
      // --- start に失敗したらON解除 ---
      speechRecognition = null;
      speechWantedOn = false;
      syncVoiceUiState();
    }
  }

  /**
   * 音声入力を開始する（無音検出で自動送信、再度押すまでON）。
   */
  async function startVoiceInputAndAutoSend() {
    // --- チャット画面以外では送らない ---
    if (!panelChat || panelChat.classList.contains("hidden")) {
      alert("音声入力はログイン後に利用できます。");
      return;
    }
    if (!canUseSpeechRecognition()) {
      alert("このブラウザでは音声認識（Web Speech API）が利用できません。");
      return;
    }
    if (chatAbortController) {
      alert("送信中のため、音声入力を開始できません。");
      return;
    }
    if (speechWantedOn) return;

    // --- state リセット ---
    speechHadError = false;
    // NOTE: 既存の入力は「下書き」として退避し、音声入力中は上書き表示する。
    speechBaseText = String(textInput ? textInput.value || "" : "");
    speechFinalText = "";
    speechInterimText = "";
    speechSavedStatusRight = String(statusRight ? statusRight.textContent || "" : "");
    speechSendQueue = [];

    // --- ON に切り替え ---
    speechWantedOn = true;
    speechListening = false;
    syncVoiceUiState();

    // --- 入力欄は音声入力用にクリア ---
    if (textInput) {
      textInput.value = "";
      autoResizeTextarea();
      refreshSendButtonEnabled();
    }

    // --- セッション開始 ---
    startSpeechRecognitionSession();
  }

  /**
   * 音声入力を停止する（停止時に残っている発話があれば送信し、入力欄は下書きを復元する）。
   */
  function stopVoiceInput() {
    if (speechSilenceTimerId != null) {
      try {
        clearTimeout(speechSilenceTimerId);
      } catch (_) {}
      speechSilenceTimerId = null;
    }

    // --- OFF に切り替え ---
    speechWantedOn = false;
    syncVoiceUiState();
    try {
      if (micButton) micButton.blur();
    } catch (_) {}

    // --- 残っている発話があれば送信（OFFでも送信自体は続ける） ---
    flushSpeechUtteranceToQueue({ force: true });
    drainSpeechSendQueue();

    if (!speechRecognition) {
      // --- 下書き復元（送信キューが空で、発話も空のときだけ） ---
      if (textInput && !String(textInput.value || "").trim()) {
        textInput.value = speechBaseText;
        autoResizeTextarea();
        refreshSendButtonEnabled();
      }
      return;
    }
    try {
      speechRecognition.stop();
    } catch (_) {}
  }

  // --- Camera helpers ---
  /**
   * カメラ（getUserMedia）が利用可能かを判定する。
   */
  function canUseGetUserMedia() {
    return !!(navigator && navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === "function");
  }

  /**
   * 現在の設定（イン/アウト・比率）から getUserMedia の video constraints を作る。
   *
   * NOTE:
   * - 端末/ブラウザによっては aspectRatio/width/height の要求が無視されることがある。
   * - `ideal` は「希望」で、満たせない環境もある。
   * - UI は「比率キー」を常に保ち、実出力が合わない場合はクロップ表示でフォールバックする。
   */
  function buildCameraVideoConstraints(options) {
    // --- options ---
    // - aspectMode: "ideal" | "exact"
    const aspectMode = options && options.aspectMode ? String(options.aspectMode) : "ideal";

    // --- 比率 ---
    const ar = getCameraAspectRatio();
    const targetAspect = Number(ar.w) / Number(ar.h);
    const aspectRatio = aspectMode === "exact" ? { exact: targetAspect } : { ideal: targetAspect };

    // --- 比率ごとの「出やすい」解像度（ideal） ---
    // NOTE: ここは強制ではなく、端末が選びやすい候補を出すだけ。
    let idealWidth = 1280;
    let idealHeight = 720;
    if (cameraAspectKey === "3:4") {
      idealWidth = 720;
      idealHeight = 960;
    } else if (cameraAspectKey === "1:1") {
      idealWidth = 720;
      idealHeight = 720;
    } else if (cameraAspectKey === "16:9") {
      idealWidth = 1280;
      idealHeight = 720;
    }

    return {
      facingMode: { ideal: cameraFacingMode },
      aspectRatio,
      width: { ideal: idealWidth },
      height: { ideal: idealHeight },
    };
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
   * プレビュー/撮影で使う比率を UI と DOM に反映する。
   *
   * NOTE:
   * - まずはカメラ側に比率変更（constraints）を要求する。
   * - 端末都合で実出力が合わない場合は、プレビューはクロップ表示に切り替え、
   *   撮影も同じ比率で切り抜いて「見た目と実体」を一致させる。
   */
  function setCameraAspectRatioKey(nextKey) {
    const key = String(nextKey || "").trim();
    if (key !== "3:4" && key !== "1:1" && key !== "16:9") return;
    cameraAspectKey = key;

    // --- UI: 選択状態 ---
    for (const btn of cameraRatioButtons) {
      const k = String(btn && btn.dataset ? btn.dataset.ratio : "");
      btn.classList.toggle("selected", k === cameraAspectKey);
    }

    // --- DOM: プレビュー枠の aspect-ratio を更新 ---
    if (!cameraPreview) return;
    if (cameraAspectKey === "1:1") cameraPreview.style.aspectRatio = "1 / 1";
    if (cameraAspectKey === "3:4") cameraPreview.style.aspectRatio = "3 / 4";
    if (cameraAspectKey === "16:9") cameraPreview.style.aspectRatio = "16 / 9";

    // --- 既定は表示なし（切り抜きにフォールバックしたときだけ表示する） ---
    setCameraPreviewModeLabel("");
  }

  /**
   * プレビュー枠の左下に「現在どの方式で比率を合わせているか」を表示する。
   *
   * 表示例:
   * - "3:4"
   * - "16:9（切り抜き）"
   */
  function setCameraPreviewModeLabel(modeText) {
    if (!cameraModeLabel) return;
    if (modeText == null) {
      cameraModeLabel.textContent = "";
      return;
    }

    const mode = String(modeText || "").trim();
    cameraModeLabel.textContent = mode ? `${cameraAspectKey}（${mode}）` : `${cameraAspectKey}`;
  }

  /**
   * 実際の映像比率が選択比率と合わない場合、プレビューをクロップ表示に切り替える。
   *
   * 目的:
   * - カメラ出力比率は端末依存で変えられないことがある。
   * - その場合でも、プレビュー/撮影結果が「選択比率」で統一されるようにする（スマホ標準カメラ寄せ）。
   */
  function syncCameraPreviewCropMode() {
    if (!cameraPreview || !cameraVideo) return;

    // --- video の実寸（メタデータ確定後に入る） ---
    const vw = Number(cameraVideo.videoWidth || 0);
    const vh = Number(cameraVideo.videoHeight || 0);
    if (!vw || !vh) {
      setCameraPreviewModeLabel("");
      return;
    }

    // --- 目標比率 ---
    const ar = getCameraAspectRatio();
    const targetAspect = Number(ar.w) / Number(ar.h);

    // --- 実比率と比較 ---
    const actualAspect = vw / vh;

    // --- 小さな誤差は許容（端末側の丸めで僅かにズレることがある） ---
    const tolerance = 0.03;
    const shouldCrop = Math.abs(actualAspect - targetAspect) > tolerance;
    cameraPreview.classList.toggle("is-crop", shouldCrop);

    // --- ラベル（どっちを使っているか） ---
    setCameraPreviewModeLabel(shouldCrop ? "切り抜き" : "");
  }

  /**
   * 現在のストリームを維持したまま、getUserMedia で新しいストリームを取り直して差し替える。
   *
   * NOTE:
   * - 取り直しに失敗したときも、既存ストリームを残す（真っ黒を避ける）。
   */
  async function restartCameraStreamWithPreview(options) {
    if (!cameraVideo) throw new Error("camera video not found");
    if (!canUseGetUserMedia()) throw new Error("getUserMedia not supported");
    if (cameraOpening) throw new Error("camera opening");

    cameraOpening = true;

    const constraints = {
      audio: false,
      video: {
        ...buildCameraVideoConstraints(options || null),
      },
    };

    let nextStream = null;
    try {
      nextStream = await navigator.mediaDevices.getUserMedia(constraints);
    } finally {
      cameraOpening = false;
    }

    const prevStream = cameraStream;
    cameraStream = nextStream;

    // --- video 差し替え ---
    cameraVideo.srcObject = cameraStream;
    try {
      await cameraVideo.play();
    } catch (_) {}

    // --- track/zoom UI 更新 ---
    try {
      const track = cameraStream.getVideoTracks && cameraStream.getVideoTracks()[0];
      initCameraZoomUiFromTrack(track || null);
    } catch (_) {
      initCameraZoomUiFromTrack(null);
    }

    // --- メタデータ確定後にクロップモード判定 ---
    // NOTE: ここでは即時も当てる（イベントは遅れることがある）。
    syncCameraPreviewCropMode();
    try {
      requestAnimationFrame(() => syncCameraPreviewCropMode());
    } catch (_) {}

    // --- 前のストリームを停止 ---
    if (prevStream) {
      try {
        for (const t of prevStream.getTracks()) {
          try {
            t.stop();
          } catch (_) {}
        }
      } catch (_) {}
    }
  }

  /**
   * 現在の比率キーから数値比率を返す。
   */
  function getCameraAspectRatio() {
    if (cameraAspectKey === "1:1") return { w: 1, h: 1 };
    if (cameraAspectKey === "16:9") return { w: 16, h: 9 };
    return { w: 3, h: 4 };
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
   * カメラストリームを停止して解放する（モーダルは閉じない）。
   *
   * NOTE:
   * - イン/アウト切替や比率変更で「ストリームだけ」差し替えたいときに使う。
   */
  function stopCameraStreamOnly() {
    // --- ズーム関連の state をリセット ---
    cameraVideoTrack = null;
    cameraZoomCaps = null;
    if (cameraZoomRafId != null) {
      try {
        cancelAnimationFrame(cameraZoomRafId);
      } catch (_) {}
      cameraZoomRafId = null;
    }
    if (cameraZoomWrap) cameraZoomWrap.classList.add("hidden");

    // --- video からストリームを外す ---
    if (cameraVideo) {
      try {
        cameraVideo.pause();
      } catch (_) {}
      try {
        cameraVideo.srcObject = null;
      } catch (_) {}
    }

    // --- tracks を停止 ---
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
    setCameraAspectRatioKey(cameraAspectKey);

    // --- カメラ起動（イン/アウト・比率を要求） ---
    const constraints = {
      audio: false,
      video: {
        ...buildCameraVideoConstraints({ aspectMode: "ideal" }),
      },
    };

    try {
      cameraStream = await navigator.mediaDevices.getUserMedia(constraints);
      cameraVideo.srcObject = cameraStream;

      // --- iOS などで play() が必要なことがある ---
      try {
        await cameraVideo.play();
      } catch (_) {}

      // --- 実際の映像比率が取れたらクロップ表示へフォールバック（必要な場合のみ） ---
      // NOTE: loadedmetadata 前だと videoWidth/Height が 0 のことがあるため、イベント側でも再同期する。
      syncCameraPreviewCropMode();

      // --- ズームUI（対応していれば表示） ---
      try {
        const track = cameraStream.getVideoTracks && cameraStream.getVideoTracks()[0];
        initCameraZoomUiFromTrack(track || null);

        // --- 可能なら `exact` で比率を寄せる（ダメならクロップで吸収） ---
        // NOTE: getUserMedia の ideal が無視される環境向けに、追加で try する。
        if (track && typeof track.applyConstraints === "function") {
          try {
            const vcExact = buildCameraVideoConstraints({ aspectMode: "exact" });
            await track.applyConstraints({ aspectRatio: vcExact.aspectRatio, width: vcExact.width, height: vcExact.height });
          } catch (_) {}
        }
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
    // --- ストリームだけ止める ---
    stopCameraStreamOnly();

    if (cameraModal) cameraModal.classList.add("hidden");
    setCameraStatus("", false);
    setCameraPreviewModeLabel(null);
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

    // --- プレビュー枠と同じ比率で切り抜く（スマホ標準カメラ寄せ） ---
    const ar = getCameraAspectRatio();
    const targetAspect = Number(ar.w) / Number(ar.h);
    const sourceAspect = vw / vh;

    // --- 切り抜き領域（中心） ---
    let sx = 0;
    let sy = 0;
    let sw = vw;
    let sh = vh;
    // --- 小さな誤差は許容（端末側の丸めで僅かにズレることがある） ---
    const tolerance = 0.03;

    if (Math.abs(sourceAspect - targetAspect) <= tolerance) {
      // --- ほぼ一致: 切り抜かない（プレビューの contain と揃える） ---
      sx = 0;
      sy = 0;
      sw = vw;
      sh = vh;
    } else if (sourceAspect > targetAspect) {
      // --- ソースが横長: 左右を切る ---
      sh = vh;
      sw = Math.max(1, Math.round(vh * targetAspect));
      sx = Math.round((vw - sw) / 2);
      sy = 0;
    } else if (sourceAspect < targetAspect) {
      // --- ソースが縦長: 上下を切る ---
      sw = vw;
      sh = Math.max(1, Math.round(vw / targetAspect));
      sx = 0;
      sy = Math.round((vh - sh) / 2);
    }

    // --- 5MB制限に近づきにくいように縮小（最大辺 1280px） ---
    const maxSide = 1280;
    const scale = Math.min(1, maxSide / Math.max(sw, sh));
    const tw = Math.max(1, Math.round(sw * scale));
    const th = Math.max(1, Math.round(sh * scale));

    const canvas = document.createElement("canvas");
    canvas.width = tw;
    canvas.height = th;

    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("canvas ctx not available");

    ctx.drawImage(video, sx, sy, sw, sh, 0, 0, tw, th);

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

  function cloneJson(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function createUuidLikeId() {
    return typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }

  function getPresetMeta(kind) {
    // --- kind 別の設定キー定義 ---
    const metaByKind = {
      llm: {
        arrayKey: "llm_preset",
        activeKey: "active_llm_preset_id",
        idKey: "llm_preset_id",
        nameKey: "llm_preset_name",
      },
      embedding: {
        arrayKey: "embedding_preset",
        activeKey: "active_embedding_preset_id",
        idKey: "embedding_preset_id",
        nameKey: "embedding_preset_name",
      },
      persona: {
        arrayKey: "persona_preset",
        activeKey: "active_persona_preset_id",
        idKey: "persona_preset_id",
        nameKey: "persona_preset_name",
      },
      addon: {
        arrayKey: "addon_preset",
        activeKey: "active_addon_preset_id",
        idKey: "addon_preset_id",
        nameKey: "addon_preset_name",
      },
    };
    return metaByKind[kind] || null;
  }

  function getPresetList(kind) {
    const meta = getPresetMeta(kind);
    if (!meta || !settingsDraft) return [];
    return Array.isArray(settingsDraft[meta.arrayKey]) ? settingsDraft[meta.arrayKey] : [];
  }

  function getActivePreset(kind) {
    const meta = getPresetMeta(kind);
    if (!meta || !settingsDraft) return null;
    const activeId = String(settingsDraft[meta.activeKey] || "");
    const presets = getPresetList(kind);
    for (const preset of presets) {
      if (String(preset[meta.idKey] || "") === activeId) return preset;
    }
    return null;
  }

  function renderPresetSelect(selectEl, kind) {
    if (!selectEl || !settingsDraft) return;
    const meta = getPresetMeta(kind);
    if (!meta) return;
    const presets = getPresetList(kind);
    const activeId = String(settingsDraft[meta.activeKey] || "");

    selectEl.innerHTML = "";
    for (const preset of presets) {
      const opt = document.createElement("option");
      const presetId = String(preset[meta.idKey] || "");
      const presetName = String(preset[meta.nameKey] || "");
      opt.value = presetId;
      opt.textContent = presetName || "(no name)";
      if (presetId === activeId) opt.selected = true;
      selectEl.appendChild(opt);
    }
  }

  function readNumberInput(inputEl) {
    const n = Number(inputEl.value);
    if (!Number.isFinite(n)) return null;
    return Math.trunc(n);
  }

  function setSettingsTab(nextTab) {
    settingsCurrentTab = /** @type {"chat"|"memory"|"persona"|"addon"|"system"} */ (nextTab);
    for (const btn of settingsTabButtons) {
      btn.classList.toggle("active", String(btn.dataset.tab || "") === settingsCurrentTab);
    }
    for (const page of settingsPageNodes) {
      page.classList.toggle("hidden", String(page.dataset.tabPage || "") !== settingsCurrentTab);
    }
  }

  function createDefaultPreset(kind) {
    const id = createUuidLikeId();
    if (kind === "llm") {
      return {
        llm_preset_id: id,
        llm_preset_name: "new-llm-preset",
        llm_api_key: "",
        llm_model: "openrouter/google/gemini-3-flash-preview",
        reasoning_effort: "",
        reply_web_search_enabled: true,
        llm_base_url: "",
        max_turns_window: 50,
        max_tokens: 4096,
        image_model_api_key: "",
        image_model: "openrouter/google/gemini-3-flash-preview",
        image_llm_base_url: "",
        max_tokens_vision: 4096,
        image_timeout_seconds: 60,
      };
    }
    if (kind === "embedding") {
      return {
        embedding_preset_id: id,
        embedding_preset_name: "new-embedding-preset",
        embedding_model_api_key: "",
        embedding_model: "openrouter/google/gemini-embedding-001",
        embedding_base_url: "",
        embedding_dimension: 3072,
        similar_episodes_limit: 60,
      };
    }
    if (kind === "persona") {
      return {
        persona_preset_id: id,
        persona_preset_name: "new-persona-preset",
        persona_text: "",
        second_person_label: "マスター",
      };
    }
    return {
      addon_preset_id: id,
      addon_preset_name: "new-addon-preset",
      addon_text: "",
    };
  }

  function addPreset(kind) {
    if (!settingsDraft) return;
    const meta = getPresetMeta(kind);
    if (!meta) return;
    const list = getPresetList(kind);
    const newPreset = createDefaultPreset(kind);
    list.push(newPreset);
    settingsDraft[meta.activeKey] = String(newPreset[meta.idKey] || "");
    renderSettingsForm();
    setSettingsStatus("プリセットを追加しました。", false);
  }

  function duplicatePreset(kind) {
    if (!settingsDraft) return;
    const meta = getPresetMeta(kind);
    if (!meta) return;
    const source = getActivePreset(kind);
    if (!source) return;

    const list = getPresetList(kind);
    const copied = cloneJson(source);
    copied[meta.idKey] = createUuidLikeId();
    copied[meta.nameKey] = `${String(source[meta.nameKey] || "")} copy`;
    list.push(copied);
    settingsDraft[meta.activeKey] = String(copied[meta.idKey] || "");
    renderSettingsForm();
    setSettingsStatus("プリセットを複製しました。", false);
  }

  function deletePreset(kind) {
    if (!settingsDraft) return;
    const meta = getPresetMeta(kind);
    if (!meta) return;
    const list = getPresetList(kind);
    if (list.length <= 1) {
      setSettingsStatus("最後の1件は削除できません。", true);
      return;
    }

    const activeId = String(settingsDraft[meta.activeKey] || "");
    const idx = list.findIndex((x) => String(x[meta.idKey] || "") === activeId);
    if (idx < 0) return;

    list.splice(idx, 1);
    settingsDraft[meta.activeKey] = String(list[Math.min(idx, list.length - 1)][meta.idKey] || "");
    renderSettingsForm();
    setSettingsStatus("プリセットを削除しました。", false);
  }

  function renderSettingsForm() {
    if (!settingsDraft) return;

    // --- タブ状態 ---
    setSettingsTab(settingsCurrentTab);

    // --- プリセット選択 ---
    renderPresetSelect(settingsActiveLlmSelect, "llm");
    renderPresetSelect(settingsActiveEmbeddingSelect, "embedding");
    renderPresetSelect(settingsActivePersonaSelect, "persona");
    renderPresetSelect(settingsActiveAddonSelect, "addon");

    // --- 会話設定（LLM） ---
    const llmPreset = getActivePreset("llm");
    if (llmPreset) {
      llmNameInput.value = String(llmPreset.llm_preset_name || "");
      llmModelInput.value = String(llmPreset.llm_model || "");
      llmApiKeyInput.value = String(llmPreset.llm_api_key || "");
      llmBaseUrlInput.value = String(llmPreset.llm_base_url || "");
      llmReasoningEffortInput.value = String(llmPreset.reasoning_effort || "");
      llmMaxTurnsWindowInput.value = String(llmPreset.max_turns_window);
      llmMaxTokensInput.value = String(llmPreset.max_tokens);
      llmReplyWebSearchEnabledInput.checked = !!llmPreset.reply_web_search_enabled;
      llmImageModelInput.value = String(llmPreset.image_model || "");
      llmImageApiKeyInput.value = String(llmPreset.image_model_api_key || "");
      llmImageBaseUrlInput.value = String(llmPreset.image_llm_base_url || "");
      llmMaxTokensVisionInput.value = String(llmPreset.max_tokens_vision);
      llmImageTimeoutSecondsInput.value = String(llmPreset.image_timeout_seconds);
    }

    // --- 記憶設定（Embedding + memory_enabled） ---
    settingsMemoryEnabledInput.checked = !!settingsDraft.memory_enabled;
    const embeddingPreset = getActivePreset("embedding");
    if (embeddingPreset) {
      embeddingNameInput.value = String(embeddingPreset.embedding_preset_name || "");
      embeddingModelInput.value = String(embeddingPreset.embedding_model || "");
      embeddingApiKeyInput.value = String(embeddingPreset.embedding_model_api_key || "");
      embeddingBaseUrlInput.value = String(embeddingPreset.embedding_base_url || "");
      embeddingDimensionInput.value = String(embeddingPreset.embedding_dimension);
      embeddingSimilarEpisodesLimitInput.value = String(embeddingPreset.similar_episodes_limit);
    }

    // --- ペルソナ設定 ---
    const personaPreset = getActivePreset("persona");
    if (personaPreset) {
      personaNameInput.value = String(personaPreset.persona_preset_name || "");
      personaSecondPersonLabelInput.value = String(personaPreset.second_person_label || "");
      personaTextInput.value = String(personaPreset.persona_text || "");
    }

    // --- アドオン設定 ---
    const addonPreset = getActivePreset("addon");
    if (addonPreset) {
      addonNameInput.value = String(addonPreset.addon_preset_name || "");
      addonTextInput.value = String(addonPreset.addon_text || "");
    }

    // --- システム設定 ---
    desktopWatchEnabledInput.checked = !!settingsDraft.desktop_watch_enabled;
    desktopWatchIntervalSecondsInput.value = String(settingsDraft.desktop_watch_interval_seconds);
    desktopWatchTargetClientIdInput.value = String(settingsDraft.desktop_watch_target_client_id || "");
  }

  async function apiGetSettings() {
    // --- 現在設定を取得する ---
    const response = await fetch(apiSettingsPath, { method: "GET" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return await response.json();
  }

  async function apiPutSettings(payload) {
    // --- JSON設定を全量更新する ---
    const response = await fetch(apiSettingsPath, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      let detail = "";
      try {
        detail = await response.text();
      } catch (_) {
        detail = "";
      }
      throw new Error(`HTTP ${response.status}${detail ? ` ${detail}` : ""}`);
    }
    return await response.json();
  }

  async function reloadSettingsForm() {
    setSettingsStatus("設定を読み込み中...", false);
    const payload = await apiGetSettings();
    settingsDraft = cloneJson(payload);
    renderSettingsForm();
    setSettingsStatus("設定を読み込みました。", false);
  }

  async function saveSettingsForm() {
    if (!settingsDraft) return;
    setSettingsStatus("設定を保存中...", false);
    const latest = await apiPutSettings(settingsDraft);
    settingsDraft = cloneJson(latest);
    renderSettingsForm();
    setSettingsStatus("設定を保存しました。", false);
  }

  function syncLlmFormToDraft() {
    const preset = getActivePreset("llm");
    if (!preset) return;
    const maxTurns = readNumberInput(llmMaxTurnsWindowInput);
    const maxTokens = readNumberInput(llmMaxTokensInput);
    const maxTokensVision = readNumberInput(llmMaxTokensVisionInput);
    const imageTimeout = readNumberInput(llmImageTimeoutSecondsInput);
    if (maxTurns == null || maxTokens == null || maxTokensVision == null || imageTimeout == null) {
      setSettingsStatus("会話設定の数値項目が不正です。", true);
      return;
    }

    preset.llm_preset_name = String(llmNameInput.value || "");
    preset.llm_model = String(llmModelInput.value || "");
    preset.llm_api_key = String(llmApiKeyInput.value || "");
    preset.llm_base_url = String(llmBaseUrlInput.value || "");
    preset.reasoning_effort = String(llmReasoningEffortInput.value || "");
    preset.max_turns_window = maxTurns;
    preset.max_tokens = maxTokens;
    preset.reply_web_search_enabled = !!llmReplyWebSearchEnabledInput.checked;
    preset.image_model = String(llmImageModelInput.value || "");
    preset.image_model_api_key = String(llmImageApiKeyInput.value || "");
    preset.image_llm_base_url = String(llmImageBaseUrlInput.value || "");
    preset.max_tokens_vision = maxTokensVision;
    preset.image_timeout_seconds = imageTimeout;
  }

  function syncEmbeddingFormToDraft() {
    if (!settingsDraft) return;
    const preset = getActivePreset("embedding");
    if (!preset) return;
    const dimension = readNumberInput(embeddingDimensionInput);
    const similarLimit = readNumberInput(embeddingSimilarEpisodesLimitInput);
    if (dimension == null || similarLimit == null) {
      setSettingsStatus("記憶設定の数値項目が不正です。", true);
      return;
    }

    settingsDraft.memory_enabled = !!settingsMemoryEnabledInput.checked;
    preset.embedding_preset_name = String(embeddingNameInput.value || "");
    preset.embedding_model = String(embeddingModelInput.value || "");
    preset.embedding_model_api_key = String(embeddingApiKeyInput.value || "");
    preset.embedding_base_url = String(embeddingBaseUrlInput.value || "");
    preset.embedding_dimension = dimension;
    preset.similar_episodes_limit = similarLimit;
  }

  function syncPersonaFormToDraft() {
    const preset = getActivePreset("persona");
    if (!preset) return;
    preset.persona_preset_name = String(personaNameInput.value || "");
    preset.second_person_label = String(personaSecondPersonLabelInput.value || "");
    preset.persona_text = String(personaTextInput.value || "");
  }

  function syncAddonFormToDraft() {
    const preset = getActivePreset("addon");
    if (!preset) return;
    preset.addon_preset_name = String(addonNameInput.value || "");
    preset.addon_text = String(addonTextInput.value || "");
  }

  function syncSystemFormToDraft() {
    if (!settingsDraft) return;
    const interval = readNumberInput(desktopWatchIntervalSecondsInput);
    if (interval == null) {
      setSettingsStatus("システム設定の数値項目が不正です。", true);
      return;
    }
    settingsDraft.desktop_watch_enabled = !!desktopWatchEnabledInput.checked;
    settingsDraft.desktop_watch_interval_seconds = interval;
    settingsDraft.desktop_watch_target_client_id = String(desktopWatchTargetClientIdInput.value || "");
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

      // --- 音声入力キューがあれば続けて送る ---
      drainSpeechSendQueue();
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

  // --- Icons ---
  micButton.addEventListener("click", async () => {
    // --- トグル（再度押すまでON） ---
    if (speechWantedOn) {
      stopVoiceInput();
      return;
    }
    await startVoiceInputAndAutoSend();
  });
  gearButton.addEventListener("click", async () => {
    if (!panelLogin.classList.contains("hidden")) return;
    showSettings();
    try {
      await reloadSettingsForm();
    } catch (error) {
      setSettingsStatus(`設定の読込に失敗しました: ${String(error && error.message ? error.message : error)}`, true);
    }
  });

  for (const btn of settingsTabButtons) {
    btn.addEventListener("click", () => {
      setSettingsTab(String(btn.dataset.tab || "chat"));
    });
  }

  settingsBackButton.addEventListener("click", () => {
    showChat();
  });

  settingsReloadButton.addEventListener("click", async () => {
    try {
      await reloadSettingsForm();
    } catch (error) {
      setSettingsStatus(`設定の読込に失敗しました: ${String(error && error.message ? error.message : error)}`, true);
    }
  });

  settingsSaveButton.addEventListener("click", async () => {
    try {
      await saveSettingsForm();
    } catch (error) {
      setSettingsStatus(`設定の保存に失敗しました: ${String(error && error.message ? error.message : error)}`, true);
    }
  });

  // --- Settings: active preset switches ---
  settingsActiveLlmSelect.addEventListener("change", () => {
    if (!settingsDraft) return;
    settingsDraft.active_llm_preset_id = String(settingsActiveLlmSelect.value || "");
    renderSettingsForm();
  });
  settingsActiveEmbeddingSelect.addEventListener("change", () => {
    if (!settingsDraft) return;
    settingsDraft.active_embedding_preset_id = String(settingsActiveEmbeddingSelect.value || "");
    renderSettingsForm();
  });
  settingsActivePersonaSelect.addEventListener("change", () => {
    if (!settingsDraft) return;
    settingsDraft.active_persona_preset_id = String(settingsActivePersonaSelect.value || "");
    renderSettingsForm();
  });
  settingsActiveAddonSelect.addEventListener("change", () => {
    if (!settingsDraft) return;
    settingsDraft.active_addon_preset_id = String(settingsActiveAddonSelect.value || "");
    renderSettingsForm();
  });

  // --- Settings: preset operations ---
  llmAddButton.addEventListener("click", () => addPreset("llm"));
  llmDuplicateButton.addEventListener("click", () => duplicatePreset("llm"));
  llmDeleteButton.addEventListener("click", () => deletePreset("llm"));
  embeddingAddButton.addEventListener("click", () => addPreset("embedding"));
  embeddingDuplicateButton.addEventListener("click", () => duplicatePreset("embedding"));
  embeddingDeleteButton.addEventListener("click", () => deletePreset("embedding"));
  personaAddButton.addEventListener("click", () => addPreset("persona"));
  personaDuplicateButton.addEventListener("click", () => duplicatePreset("persona"));
  personaDeleteButton.addEventListener("click", () => deletePreset("persona"));
  addonAddButton.addEventListener("click", () => addPreset("addon"));
  addonDuplicateButton.addEventListener("click", () => duplicatePreset("addon"));
  addonDeleteButton.addEventListener("click", () => deletePreset("addon"));

  // --- Settings: form bindings ---
  llmNameInput.addEventListener("input", syncLlmFormToDraft);
  llmModelInput.addEventListener("input", syncLlmFormToDraft);
  llmApiKeyInput.addEventListener("input", syncLlmFormToDraft);
  llmBaseUrlInput.addEventListener("input", syncLlmFormToDraft);
  llmReasoningEffortInput.addEventListener("input", syncLlmFormToDraft);
  llmMaxTurnsWindowInput.addEventListener("input", syncLlmFormToDraft);
  llmMaxTokensInput.addEventListener("input", syncLlmFormToDraft);
  llmReplyWebSearchEnabledInput.addEventListener("change", syncLlmFormToDraft);
  llmImageModelInput.addEventListener("input", syncLlmFormToDraft);
  llmImageApiKeyInput.addEventListener("input", syncLlmFormToDraft);
  llmImageBaseUrlInput.addEventListener("input", syncLlmFormToDraft);
  llmMaxTokensVisionInput.addEventListener("input", syncLlmFormToDraft);
  llmImageTimeoutSecondsInput.addEventListener("input", syncLlmFormToDraft);

  settingsMemoryEnabledInput.addEventListener("change", syncEmbeddingFormToDraft);
  embeddingNameInput.addEventListener("input", syncEmbeddingFormToDraft);
  embeddingModelInput.addEventListener("input", syncEmbeddingFormToDraft);
  embeddingApiKeyInput.addEventListener("input", syncEmbeddingFormToDraft);
  embeddingBaseUrlInput.addEventListener("input", syncEmbeddingFormToDraft);
  embeddingDimensionInput.addEventListener("input", syncEmbeddingFormToDraft);
  embeddingSimilarEpisodesLimitInput.addEventListener("input", syncEmbeddingFormToDraft);

  personaNameInput.addEventListener("input", syncPersonaFormToDraft);
  personaSecondPersonLabelInput.addEventListener("input", syncPersonaFormToDraft);
  personaTextInput.addEventListener("input", syncPersonaFormToDraft);

  addonNameInput.addEventListener("input", syncAddonFormToDraft);
  addonTextInput.addEventListener("input", syncAddonFormToDraft);

  desktopWatchEnabledInput.addEventListener("change", syncSystemFormToDraft);
  desktopWatchIntervalSecondsInput.addEventListener("input", syncSystemFormToDraft);
  desktopWatchTargetClientIdInput.addEventListener("input", syncSystemFormToDraft);

  settingsLogoutButton.addEventListener("click", async () => {
    const ok = confirm("ログアウトしますか？");
    if (!ok) return;

    closeCameraModal();
    stopEventsSocket();
    await apiLogout();
    showLogin();
    setLoginStatus("", false);
    setSettingsStatus("", false);
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
        // --- モーダルは維持して、ストリームだけ差し替える ---
        stopCameraStreamOnly();
        await openCameraModalWithPreview();
      } catch (_) {
        // --- 失敗した場合は capture input にフォールバック ---
        try {
          closeCameraModal();
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

  // --- Camera aspect ratio controls ---
  for (const btn of cameraRatioButtons) {
    btn.addEventListener("click", async () => {
      const k = String(btn && btn.dataset ? btn.dataset.ratio : "");
      setCameraAspectRatioKey(k);

      // --- プレビューのクロップモードを即時反映（メタデータがあれば） ---
      syncCameraPreviewCropMode();

      // --- 可能ならカメラ側へ「比率変更」を要求する ---
      // NOTE:
      // - まずは `exact` を要求し、通れば「実映像そのもの」が選択比率になる。
      // - 通らない端末では、プレビュー/撮影を「クロップ」で選択比率に合わせてフォールバックする。
      if (!cameraModal || cameraModal.classList.contains("hidden")) return;
      if (!canUseGetUserMedia()) return;
      if (cameraOpening) return;

      setCameraStatus("比率を変更中...", false);

      const track = cameraVideoTrack;
      if (track && typeof track.applyConstraints === "function") {
        const vcExact = buildCameraVideoConstraints({ aspectMode: "exact" });

        // --- まずは `exact`（できない場合は例外になる） ---
        try {
          await track.applyConstraints({
            aspectRatio: vcExact.aspectRatio,
            width: vcExact.width,
            height: vcExact.height,
          });
          syncCameraPreviewCropMode();
          setCameraStatus("", false);
          return;
        } catch (_) {}

        // --- advanced でも試す（実装差分の吸収） ---
        try {
          await track.applyConstraints({
            width: vcExact.width,
            height: vcExact.height,
            advanced: [{ aspectRatio: vcExact.aspectRatio && vcExact.aspectRatio.exact != null ? vcExact.aspectRatio.exact : undefined }],
          });
          syncCameraPreviewCropMode();
          setCameraStatus("", false);
          return;
        } catch (_) {}
      }

      // --- applyConstraints が効かない場合は、ストリーム取り直しで試す ---
      try {
        await restartCameraStreamWithPreview({ aspectMode: "exact" });
        syncCameraPreviewCropMode();
        setCameraStatus("", false);
      } catch (_) {
        // --- 変更できない端末もあるので、ここではエラー扱いにせず続行 ---
        // NOTE: 以降はクロップ表示/クロップ撮影で「選択比率」に合わせる。
        syncCameraPreviewCropMode();
        setCameraStatus("※この端末ではカメラ出力比率の変更ができない可能性があります（プレビュー/撮影は切り抜きで合わせます）", false);
      }
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
