from django.http import HttpResponse, JsonResponse

from tenants.origins import log_origin_block, resolve_public_tenant, validate_tenant_origin
from tenants.services.widget_config import build_disabled_widget_config, build_widget_config_for_tenant


def widget_js(request):
    content = """(function () {
  const currentScript = document.currentScript;
  if (!currentScript) {
    console.error("[Lívia] Não foi possível localizar o script do widget.");
    return;
  }

  const tenant = (currentScript.getAttribute("data-tenant") || "").trim();
  if (!tenant || tenant === "default") {
    console.error("[Lívia] Atributo data-tenant é obrigatório.");
    return;
  }

  const initRegistry = window.__liviaWidgetInit || (window.__liviaWidgetInit = {});
  if (initRegistry[tenant]) {
    console.error("[Lívia] Widget já inicializado para o tenant:", tenant);
    return;
  }
  initRegistry[tenant] = true;

  const sessionStorageKey = "livia_session_id_" + tenant;
  const apiUrl = resolveApiUrl(currentScript);
  const configUrl = resolveConfigUrl(currentScript);
  const requestTimeoutMs = 10000;
  const maxSendAttempts = 3;
  const retryDelayMs = 650;
  const inProgressDelayMs = 900;
  const defaultConfig = {
    assistant_name: "Lívia",
    widget_title: "Lívia",
    launcher_label: "Fale com a Lívia",
    initial_message: "Olá! Sou a Lívia. Como posso te ajudar?",
    primary_color: "#2563eb",
    position: "bottom_right",
    placeholder_text: "Digite sua mensagem...",
    show_branding: true,
    is_widget_enabled: true,
    human_handoff_enabled: false,
    human_handoff_channel: "disabled",
    handoff_whatsapp_label: "Falar com um especialista"
  };

  function appendTenantParam(rawUrl) {
    try {
      const url = new URL(rawUrl, window.location.href);
      url.searchParams.set("tenant", tenant);
      return url.href;
    } catch (error) {
      const separator = String(rawUrl || "").indexOf("?") === -1 ? "?" : "&";
      return String(rawUrl || "/api/chat/") + separator + "tenant=" + encodeURIComponent(tenant);
    }
  }

  function resolveApiUrl(scriptEl) {
    if (scriptEl) {
      const configuredUrl = scriptEl.getAttribute("data-api-url");
      if (configuredUrl) {
        return appendTenantParam(configuredUrl);
      }
      if (scriptEl.src) {
        try {
          return appendTenantParam(new URL("/api/chat/", scriptEl.src).href);
        } catch (error) {
          return appendTenantParam("/api/chat/");
        }
      }
    }
    return appendTenantParam("/api/chat/");
  }

  function resolveConfigUrl(scriptEl) {
    if (scriptEl && scriptEl.src) {
      try {
        const url = new URL("/api/widget/config/", scriptEl.src);
        url.searchParams.set("tenant", tenant);
        return url.href;
      } catch (error) {
        return "/api/widget/config/?tenant=" + encodeURIComponent(tenant);
      }
    }
    return "/api/widget/config/?tenant=" + encodeURIComponent(tenant);
  }

  function getSessionId() {
    try {
      const existing = window.localStorage.getItem(sessionStorageKey);
      if (existing) {
        return existing;
      }
      const generated = generateSessionId();
      window.localStorage.setItem(sessionStorageKey, generated);
      return generated;
    } catch (error) {
      return generateSessionId();
    }
  }

  function generateRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }

    const timestamp = Date.now().toString(16);
    const randomPart = Math.random().toString(16).slice(2, 14);
    const extraPart = Math.random().toString(16).slice(2, 14);
    return "00000000-0000-4000-8000-" + (timestamp + randomPart + extraPart).slice(0, 12).padEnd(12, "0");
  }

  function generateSessionId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }

    const timestamp = Date.now().toString(16);
    const randomPart = Math.random().toString(16).slice(2, 14);
    return "livia-" + timestamp + "-" + randomPart;
  }

  function delay(ms) {
    return new Promise(function (resolve) {
      window.setTimeout(resolve, ms);
    });
  }

  function fetchWithTimeout(url, options, timeoutMs) {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const requestOptions = Object.assign({}, options || {});
    if (controller) {
      requestOptions.signal = controller.signal;
    }
    return new Promise(function (resolve, reject) {
      let settled = false;
      const timer = window.setTimeout(function () {
        if (settled) {
          return;
        }
        settled = true;
        if (controller) {
          controller.abort();
        }
        reject(new Error("request_timeout"));
      }, timeoutMs);
      fetch(url, requestOptions)
        .then(function (response) {
          if (settled) {
            return;
          }
          settled = true;
          window.clearTimeout(timer);
          resolve(response);
        })
        .catch(function (error) {
          if (settled) {
            return;
          }
          settled = true;
          window.clearTimeout(timer);
          reject(error);
        });
    });
  }

  function injectStyles() {
    if (document.getElementById("livia-widget-styles")) {
      return;
    }

    const style = document.createElement("style");
    style.id = "livia-widget-styles";
    style.textContent = [
      "#livia-launcher { position: fixed; right: 20px; bottom: 20px; z-index: 2147483000; border: 0; border-radius: 999px; padding: 12px 18px; cursor: pointer; background: var(--livia-primary, #2563eb); color: #fff; box-shadow: 0 12px 30px rgba(15, 23, 42, .25); font: 600 14px/1.2 Arial, sans-serif; }",
      "#livia-launcher.livia-hidden { display: none; }",
      "#livia-whatsapp-handoff { position: fixed; right: 20px; bottom: 88px; z-index: 2147483001; display: none; align-items: center; gap: 8px; font: 600 13px/1.2 Arial, sans-serif; }",
      "#livia-whatsapp-handoff.livia-visible { display: flex; animation: livia-handoff-in .18s ease-out; }",
      "#livia-whatsapp-handoff.livia-bottom-left { left: 20px; right: auto; flex-direction: row-reverse; }",
      "#livia-whatsapp-handoff.livia-bottom-right { right: 20px; left: auto; }",
      "#livia-whatsapp-link { min-width: 44px; min-height: 44px; width: 44px; height: 44px; border-radius: 999px; background: #25d366; color: #fff; display: inline-flex; align-items: center; justify-content: center; box-shadow: 0 12px 30px rgba(15, 23, 42, .22); text-decoration: none; }",
      "#livia-whatsapp-link:focus-visible, #livia-whatsapp-close:focus-visible { outline: 3px solid rgba(37, 211, 102, .35); outline-offset: 3px; }",
      "#livia-whatsapp-link svg { width: 24px; height: 24px; fill: currentColor; }",
      "#livia-whatsapp-close { width: 28px; height: 28px; border-radius: 999px; border: 1px solid rgba(15, 23, 42, .12); background: #fff; color: #0f172a; cursor: pointer; box-shadow: 0 8px 20px rgba(15, 23, 42, .16); font: 700 18px/1 Arial, sans-serif; }",
      "@keyframes livia-handoff-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }",
      "@media (prefers-reduced-motion: reduce) { #livia-whatsapp-handoff.livia-visible { animation: none; } }",
      "#livia-panel { position: fixed; right: 20px; bottom: 76px; width: min(360px, calc(100vw - 40px)); height: 520px; max-height: calc(100vh - 110px); z-index: 2147483000; display: none; flex-direction: column; background: #fff; border: 1px solid rgba(15, 23, 42, .12); border-radius: 18px; box-shadow: 0 18px 45px rgba(15, 23, 42, .2); overflow: hidden; font: 14px/1.4 Arial, sans-serif; }",
      "#livia-panel.livia-open { display: flex; }",
      ".livia-bottom-left#livia-launcher { left: 20px; right: auto; }",
      ".livia-bottom-left#livia-panel { left: 20px; right: auto; }",
      ".livia-bottom-right#livia-launcher { right: 20px; left: auto; }",
      ".livia-bottom-right#livia-panel { right: 20px; left: auto; }",
      "@media (max-width: 480px) { #livia-launcher { right: 16px; bottom: 16px; } #livia-panel { right: 16px; bottom: 72px; width: calc(100vw - 32px); max-height: calc(100vh - 96px); } #livia-whatsapp-handoff { right: 16px; bottom: 84px; } .livia-bottom-left#livia-launcher { left: 16px; right: auto; } .livia-bottom-left#livia-panel { left: 16px; right: auto; } .livia-bottom-left#livia-whatsapp-handoff { left: 16px; right: auto; } .livia-bottom-right#livia-launcher { right: 16px; left: auto; } .livia-bottom-right#livia-panel { right: 16px; left: auto; } .livia-bottom-right#livia-whatsapp-handoff { right: 16px; left: auto; } }",
      "#livia-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 16px; background: var(--livia-primary, #2563eb); color: #fff; }",
      "#livia-header strong { font-size: 15px; }",
      "#livia-close { border: 0; background: transparent; color: #fff; font-size: 24px; line-height: 1; cursor: pointer; padding: 0; }",
      "#livia-messages { flex: 1; overflow-y: auto; padding: 14px; background: #f8fafc; display: flex; flex-direction: column; gap: 10px; }",
      ".livia-message { max-width: 85%; padding: 10px 12px; border-radius: 14px; white-space: pre-wrap; word-break: break-word; }",
      ".livia-message.user { align-self: flex-end; background: var(--livia-primary, #2563eb); color: #fff; border-bottom-right-radius: 6px; }",
      ".livia-message.assistant { align-self: flex-start; background: #fff; color: #0f172a; border: 1px solid rgba(15, 23, 42, .08); border-bottom-left-radius: 6px; }",
      ".livia-message.system { align-self: center; background: transparent; color: #64748b; font-size: 12px; padding: 0; }",
      ".livia-typing-bubble { display: inline-flex; align-items: center; gap: 5px; min-width: 44px; min-height: 18px; }",
      ".livia-typing-dot { width: 6px; height: 6px; border-radius: 999px; background: #64748b; opacity: .75; animation: livia-typing-wave 1.05s ease-in-out infinite; }",
      ".livia-typing-dot:nth-child(2) { animation-delay: .14s; }",
      ".livia-typing-dot:nth-child(3) { animation-delay: .28s; }",
      "@keyframes livia-typing-wave { 0%, 70%, 100% { transform: translateY(0); opacity: .55; } 35% { transform: translateY(-5px); opacity: 1; } }",
      "@media (prefers-reduced-motion: reduce) { .livia-typing-dot { animation: none; } }",
      "#livia-branding { color: #64748b; font-size: 11px; padding: 0 12px 10px; text-align: center; background: #fff; }",
      "#livia-footer { display: flex; gap: 8px; padding: 12px; border-top: 1px solid rgba(15, 23, 42, .08); background: #fff; }",
      "#livia-input { flex: 1; border: 1px solid rgba(15, 23, 42, .15); border-radius: 12px; padding: 10px 12px; outline: none; font: inherit; min-width: 0; }",
      "#livia-send { border: 0; border-radius: 12px; padding: 10px 14px; background: var(--livia-primary, #2563eb); color: #fff; cursor: pointer; font: 600 14px/1 Arial, sans-serif; }",
      "#livia-send:disabled, #livia-input:disabled { opacity: .65; cursor: not-allowed; }"
    ].join("\\n");
    document.head.appendChild(style);
  }

  function createButton(label, id) {
    const button = document.createElement("button");
    button.id = id;
    button.type = "button";
    button.textContent = label;
    return button;
  }

  function createMessageElement(role, text) {
    const bubble = document.createElement("div");
    bubble.className = "livia-message " + role;
    bubble.textContent = text;
    return bubble;
  }

  function createTypingIndicator(assistantLabel) {
    const typing = document.createElement("div");
    typing.id = "livia-typing";
    typing.className = "livia-message assistant livia-typing-bubble";
    typing.setAttribute("role", "status");
    typing.setAttribute("aria-live", "polite");
    typing.setAttribute("aria-label", (assistantLabel || "Lívia") + " está respondendo");
    for (let index = 0; index < 3; index += 1) {
      const dot = document.createElement("span");
      dot.className = "livia-typing-dot";
      dot.setAttribute("aria-hidden", "true");
      typing.appendChild(dot);
    }
    return typing;
  }

  function appendMessage(messagesEl, role, text) {
    const message = createMessageElement(role, text);
    messagesEl.appendChild(message);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return message;
  }

  function setLoading(inputEl, sendButton, loading) {
    inputEl.disabled = loading;
    sendButton.disabled = loading;
    sendButton.textContent = loading ? "..." : "Enviar";
  }

  function isHexColor(value) {
    return /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(String(value || ""));
  }

  function buildWidget() {
    injectStyles();

    const launcher = createButton(defaultConfig.launcher_label, "livia-launcher");
    const panel = document.createElement("div");
    panel.id = "livia-panel";

    const handoffButton = document.createElement("div");
    handoffButton.id = "livia-whatsapp-handoff";

    const handoffLink = document.createElement("a");
    handoffLink.id = "livia-whatsapp-link";
    handoffLink.target = "_blank";
    handoffLink.rel = "noopener noreferrer";
    handoffLink.setAttribute("aria-label", defaultConfig.handoff_whatsapp_label);
    handoffLink.title = defaultConfig.handoff_whatsapp_label;
    handoffLink.innerHTML = '<svg aria-hidden="true" viewBox="0 0 32 32"><path d="M16.02 3.2A12.73 12.73 0 0 0 5.15 22.55L3.2 29l6.62-1.9A12.72 12.72 0 1 0 16.02 3.2Zm0 2.34a10.38 10.38 0 1 1-5.3 19.3l-.38-.23-3.72 1.07 1.1-3.58-.25-.4A10.38 10.38 0 0 1 16.02 5.54Zm-5.2 5.58c-.25 0-.66.1-1 .47-.34.37-1.3 1.27-1.3 3.1s1.33 3.6 1.52 3.85c.18.25 2.56 4.1 6.34 5.58 3.14 1.23 3.78.98 4.46.92.68-.06 2.2-.9 2.5-1.77.32-.87.32-1.62.23-1.77-.1-.16-.35-.25-.73-.44-.37-.18-2.2-1.08-2.54-1.2-.34-.13-.59-.19-.84.18-.25.38-.96 1.2-1.18 1.45-.22.25-.43.28-.8.1-.38-.19-1.58-.58-3-1.85-1.12-1-1.87-2.22-2.09-2.6-.22-.37-.02-.57.17-.75.17-.17.37-.44.56-.66.19-.22.25-.37.38-.62.12-.25.06-.47-.03-.65-.1-.19-.84-2.02-1.15-2.77-.3-.72-.6-.62-.84-.63l-.72-.01Z"></path></svg>';

    const handoffClose = createButton("×", "livia-whatsapp-close");
    handoffClose.setAttribute("aria-label", "Ocultar botão do WhatsApp");

    const header = document.createElement("div");
    header.id = "livia-header";

    const title = document.createElement("strong");
    title.textContent = defaultConfig.widget_title;

    const closeButton = createButton("×", "livia-close");

    const messages = document.createElement("div");
    messages.id = "livia-messages";

    const footer = document.createElement("form");
    footer.id = "livia-footer";
    footer.autocomplete = "off";

    const input = document.createElement("input");
    input.id = "livia-input";
    input.type = "text";
    input.placeholder = defaultConfig.placeholder_text;
    input.setAttribute("aria-label", "Mensagem para a Lívia");

    const sendButton = createButton("Enviar", "livia-send");
    sendButton.type = "submit";

    const branding = document.createElement("div");
    branding.id = "livia-branding";
    branding.textContent = "Atendimento por Lívia";

    const sessionId = getSessionId();
    const handoffStoragePrefix = "livia_handoff_" + tenant + "_" + sessionId + "_";
    let assistantName = defaultConfig.assistant_name;
    let typingIndicator = null;
    let isOpen = false;
    let widgetEnabled = true;
    let isSending = false;

    function applyConfig(rawConfig) {
      const config = Object.assign({}, defaultConfig, rawConfig || {});
      const color = isHexColor(config.primary_color) ? config.primary_color : defaultConfig.primary_color;
      const position = config.position === "bottom_left" ? "bottom_left" : "bottom_right";
      const positionClass = position === "bottom_left" ? "livia-bottom-left" : "livia-bottom-right";
      const otherPositionClass = position === "bottom_left" ? "livia-bottom-right" : "livia-bottom-left";

      document.documentElement.style.setProperty("--livia-primary", color);
      launcher.classList.remove(otherPositionClass);
      panel.classList.remove(otherPositionClass);
      handoffButton.classList.remove(otherPositionClass);
      launcher.classList.add(positionClass);
      panel.classList.add(positionClass);
      handoffButton.classList.add(positionClass);

      assistantName = String(config.assistant_name || defaultConfig.assistant_name).trim() || defaultConfig.assistant_name;
      title.textContent = String(config.widget_title || assistantName).trim() || assistantName;
      launcher.textContent = String(config.launcher_label || defaultConfig.launcher_label).trim() || defaultConfig.launcher_label;
      input.placeholder = String(config.placeholder_text || defaultConfig.placeholder_text).trim() || defaultConfig.placeholder_text;
      setWhatsAppLabel(String(config.handoff_whatsapp_label || defaultConfig.handoff_whatsapp_label).trim() || defaultConfig.handoff_whatsapp_label);

      const firstMessage = messages.querySelector(".livia-message.assistant");
      const initialMessage = String(config.initial_message || defaultConfig.initial_message).trim() || defaultConfig.initial_message;
      if (firstMessage) {
        firstMessage.textContent = initialMessage;
      }

      branding.style.display = config.show_branding === false ? "none" : "block";
      widgetEnabled = config.is_widget_enabled !== false;
      if (!widgetEnabled) {
        launcher.classList.add("livia-hidden");
        closePanel();
        hideWhatsAppHandoff();
      } else {
        launcher.classList.remove("livia-hidden");
      }
    }

    function sessionGet(key) {
      try {
        return window.sessionStorage.getItem(handoffStoragePrefix + key);
      } catch (error) {
        return null;
      }
    }

    function sessionSet(key, value) {
      try {
        window.sessionStorage.setItem(handoffStoragePrefix + key, value);
      } catch (error) {}
    }

    function sessionRemove(key) {
      try {
        window.sessionStorage.removeItem(handoffStoragePrefix + key);
      } catch (error) {}
    }

    function isSafeWhatsAppUrl(url) {
      return /^https:\\/\\/wa\\.me\\/\\d{8,15}(?:\\?|$)/.test(String(url || ""));
    }

    function setWhatsAppLabel(label) {
      const safeLabel = String(label || defaultConfig.handoff_whatsapp_label).trim() || defaultConfig.handoff_whatsapp_label;
      handoffLink.setAttribute("aria-label", safeLabel);
      handoffLink.title = safeLabel;
    }

    function showWhatsAppHandoff(url, label) {
      if (!isSafeWhatsAppUrl(url)) {
        return;
      }
      handoffLink.href = url;
      setWhatsAppLabel(label);
      handoffButton.classList.add("livia-visible");
    }

    function hideWhatsAppHandoff() {
      handoffButton.classList.remove("livia-visible");
    }

    function restoreWhatsAppHandoff() {
      if (sessionGet("closed") === "1") {
        return;
      }
      const storedUrl = sessionGet("url");
      if (sessionGet("visible") === "1" && isSafeWhatsAppUrl(storedUrl)) {
        showWhatsAppHandoff(storedUrl, sessionGet("label") || defaultConfig.handoff_whatsapp_label);
      }
    }

    function handleHumanHandoff(payload) {
      if (!payload || payload.active !== true || payload.channel !== "whatsapp") {
        return;
      }
      if (!isSafeWhatsAppUrl(payload.url)) {
        return;
      }
      const label = String(payload.label || defaultConfig.handoff_whatsapp_label).trim() || defaultConfig.handoff_whatsapp_label;
      sessionSet("visible", "1");
      sessionSet("url", payload.url);
      sessionSet("label", label);
      sessionRemove("closed");
      showWhatsAppHandoff(payload.url, label);
    }

    function closeWhatsAppHandoff() {
      hideWhatsAppHandoff();
      sessionSet("closed", "1");
      sessionRemove("visible");
    }

    function updateAssistantProfile(data) {
      const nextName = String((data && data.assistant_name) || "").trim();
      if (nextName) {
        assistantName = nextName;
      }
    }

    function refocusInput() {
      if (!isOpen || !widgetEnabled) {
        return;
      }
      const focusInput = function () {
        if (!isOpen || !widgetEnabled || input.disabled) {
          return;
        }
        try {
          input.focus({ preventScroll: true });
        } catch (error) {
          try {
            input.focus();
          } catch (fallbackError) {
            /* ignore focus errors on unsupported browsers */
          }
        }
      };
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(function () {
          requestAnimationFrame(focusInput);
        });
      } else {
        setTimeout(focusInput, 0);
      }
    }

    function openPanel() {
      if (!widgetEnabled) {
        return;
      }
      panel.classList.add("livia-open");
      isOpen = true;
      input.focus();
    }

    function closePanel() {
      panel.classList.remove("livia-open");
      isOpen = false;
    }

    function togglePanel() {
      if (isOpen) {
        closePanel();
        return;
      }
      openPanel();
    }

    function ensureTyping() {
      if (!typingIndicator) {
        typingIndicator = createTypingIndicator(assistantName);
        messages.appendChild(typingIndicator);
        messages.scrollTop = messages.scrollHeight;
      }
    }

    function removeTyping() {
      if (typingIndicator && typingIndicator.parentNode) {
        typingIndicator.parentNode.removeChild(typingIndicator);
      }
      typingIndicator = null;
    }

    async function loadConfig() {
      try {
        const response = await fetch(configUrl, { method: "GET", headers: { "X-Livia-Tenant": tenant } });
        const data = await response.json().catch(function () {
          return {};
        });
        if (!response.ok && data.is_widget_enabled !== false) {
          console.error("[Lívia] Configuração do widget recusada:", response.status);
          return;
        }
        applyConfig(data);
      } catch (error) {
        console.error("[Lívia] Falha ao carregar configuração do widget.");
        applyConfig(defaultConfig);
      }
    }

    async function postChatMessage(message, requestId) {
      return fetchWithTimeout(apiUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Livia-Tenant": tenant,
          "X-Livia-Request-ID": requestId
        },
        body: JSON.stringify({
          tenant: tenant,
          session_key: sessionId,
          session_id: sessionId,
          request_id: requestId,
          message: message,
          source_page: window.location.href
        })
      }, requestTimeoutMs);
    }

    async function sendMessage(rawMessage) {
      const message = String(rawMessage || "").trim();
      if (!message || !widgetEnabled || isSending) {
        return;
      }

      const requestId = generateRequestId();
      isSending = true;
      appendMessage(messages, "user", message);
      input.value = "";
      setLoading(input, sendButton, true);

      try {
        ensureTyping();
        let response = null;
        let data = {};
        for (let attempt = 1; attempt <= maxSendAttempts; attempt += 1) {
          try {
            response = await postChatMessage(message, requestId);
            data = await response.json().catch(function () {
              return {};
            });
            if (response.status === 409 && data.error === "request_in_progress" && attempt < maxSendAttempts) {
              await delay(inProgressDelayMs);
              continue;
            }
            break;
          } catch (error) {
            if (attempt >= maxSendAttempts) {
              throw error;
            }
            await delay(retryDelayMs);
          }
        }

        removeTyping();
        updateAssistantProfile(data);

        if (!response || !response.ok) {
          appendMessage(messages, "assistant", data.reply || data.error || "Não consegui responder agora. Tente novamente em instantes.");
          return;
        }

        handleHumanHandoff(data.human_handoff);
        appendMessage(messages, "assistant", data.reply || "Recebi sua mensagem.");
      } catch (error) {
        removeTyping();
        appendMessage(messages, "assistant", "Houve um problema ao conectar com a Lívia. Tente novamente.");
      } finally {
        isSending = false;
        setLoading(input, sendButton, false);
        refocusInput();
      }
    }

    closeButton.addEventListener("click", closePanel);
    handoffClose.addEventListener("click", closeWhatsAppHandoff);
    launcher.addEventListener("click", togglePanel);
    footer.addEventListener("submit", function (event) {
      event.preventDefault();
      sendMessage(input.value);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage(input.value);
      }
    });

    header.appendChild(title);
    header.appendChild(closeButton);
    handoffButton.appendChild(handoffLink);
    handoffButton.appendChild(handoffClose);
    footer.appendChild(input);
    footer.appendChild(sendButton);
    panel.appendChild(header);
    panel.appendChild(messages);
    panel.appendChild(branding);
    panel.appendChild(footer);

    document.body.appendChild(handoffButton);
    document.body.appendChild(launcher);
    document.body.appendChild(panel);

    appendMessage(messages, "assistant", defaultConfig.initial_message);
    applyConfig(defaultConfig);
    restoreWhatsAppHandoff();
    loadConfig();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildWidget);
  } else {
    buildWidget();
  }
})();"""
    return HttpResponse(
        content,
        content_type="application/javascript; charset=utf-8",
    )


def widget_config(request):
    resolution = resolve_public_tenant(request)
    tenant_slug = resolution.slug
    if resolution.error == "tenant_required":
        payload = build_disabled_widget_config(tenant_slug)
        payload["error"] = "tenant_required"
        return JsonResponse(payload, status=400)
    if resolution.error == "tenant_mismatch":
        return JsonResponse({"error": "tenant_mismatch"}, status=400)
    if resolution.error == "tenant_unavailable":
        payload = build_disabled_widget_config(tenant_slug)
        payload["error"] = "tenant_unavailable"
        return JsonResponse(payload, status=403 if resolution.tenant is not None else 404)

    result = validate_tenant_origin(request, resolution.tenant)
    if not result.allowed:
        log_origin_block(resolution.tenant, result)
        return JsonResponse({"error": "origin_not_allowed"}, status=403)
    request.livia_validated_origin = result.origin
    return JsonResponse(build_widget_config_for_tenant(resolution.tenant))


def demo_page(request):
    content = """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Demo da Lívia</title>
</head>
<body style="font-family: Arial, sans-serif; margin: 0; min-height: 100vh; background: #f8fafc;">
  <main style="max-width: 900px; margin: 0 auto; padding: 48px 24px;">
    <h1>Demo da Lívia</h1>
    <p>Use esta página para testar o widget localmente.</p>
  </main>
  <script src="/widget.js" data-tenant="smart-control-brasil"></script>
</body>
</html>
"""
    return HttpResponse(content, content_type="text/html; charset=utf-8")
