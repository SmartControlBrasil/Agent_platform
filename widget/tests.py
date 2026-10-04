import json
import subprocess

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from tenants.models import AssistantProfile, Tenant, TenantAllowedOrigin
from tenants.services.human_handoff import build_whatsapp_handoff_url


class WidgetTests(TestCase):
    def test_widget_js_contains_fetch(self):
        response = self.client.get("/widget.js")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/javascript; charset=utf-8")
        content = response.content.decode("utf-8")
        self.assertIn("fetch", content)
        self.assertIn('getAttribute("data-api-url")', content)
        self.assertIn('appendTenantParam', content)
        self.assertIn('url.searchParams.set("tenant", tenant)', content)
        self.assertIn('return appendTenantParam(configuredUrl);', content)
        self.assertIn('return appendTenantParam(new URL("/api/chat/", scriptEl.src).href);', content)
        self.assertIn("session_key: sessionId", content)
        self.assertIn("/api/widget/config/", content)
        self.assertIn("loadConfig", content)
        self.assertIn("primary_color", content)
        self.assertIn("bottom_left", content)
        self.assertIn("getAttribute(\"data-api-url\")", content)
        self.assertIn("#livia-whatsapp-handoff", content)
        self.assertIn("handleHumanHandoff(data.human_handoff)", content)
        self.assertIn("window.sessionStorage", content)
        self.assertIn("livia_handoff_", content)
        self.assertIn("https:\\/\\/wa\\.me", content)
        self.assertIn("prefers-reduced-motion", content)
        self.assertIn("noopener noreferrer", content)
        self.assertIn("generateRequestId", content)
        self.assertIn("crypto.randomUUID", content)
        self.assertIn("request_id: requestId", content)
        self.assertIn("X-Livia-Request-ID", content)
        self.assertIn("X-Livia-Tenant", content)
        self.assertIn("fetchWithTimeout", content)
        self.assertIn("AbortController", content)
        self.assertIn("controller.abort()", content)
        self.assertIn("maxSendAttempts", content)
        self.assertIn("request_in_progress", content)
        self.assertIn("isSending", content)
        self.assertIn("livia-typing-bubble", content)
        self.assertIn("livia-typing-dot", content)
        self.assertIn("livia-typing-wave", content)
        self.assertIn("prefers-reduced-motion: reduce", content)
        self.assertIn("aria-label", content)
        self.assertIn("está respondendo", content)
        self.assertIn("for (let index = 0; index < 3; index += 1)", content)
        self.assertIn("function createTypingIndicator(assistantLabel)", content)
        self.assertIn("createTypingIndicator(assistantName)", content)
        self.assertIn("ensureTyping();", content)
        self.assertNotIn('assistantName + " está respondendo"', content)
        self.assertNotIn("Digitando...", content)

    def test_widget_runtime_renders_success_and_clears_loading_on_success_and_error(self):
        source = self.client.get("/widget.js").content.decode("utf-8")
        runner = r"""
        const vm = require("vm");
        const source = WIDGET_SOURCE;

        class Element {
          constructor(tag, documentRef) {
            this.tagName = tag.toUpperCase();
            this.ownerDocument = documentRef;
            this.children = [];
            this.parentNode = null;
            this.attributes = {};
            this.listeners = {};
            this.style = {};
            this.disabled = false;
            this.value = "";
            this.textContent = "";
            this.innerHTML = "";
            this.className = "";
            this.type = "";
            this.id = "";
            this.scrollTop = 0;
            this.scrollHeight = 0;
            this.classList = {
              add: (...names) => {
                const current = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                names.forEach((name) => current.add(name));
                this.className = Array.from(current).join(" ");
              },
              remove: (...names) => {
                const remove = new Set(names);
                this.className = String(this.className || "").split(/\s+/).filter((name) => name && !remove.has(name)).join(" ");
              },
              contains: (name) => String(this.className || "").split(/\s+/).includes(name)
            };
          }
          appendChild(child) {
            child.parentNode = this;
            this.children.push(child);
            if (child.id) {
              this.ownerDocument.byId[child.id] = child;
            }
            return child;
          }
          removeChild(child) {
            this.children = this.children.filter((item) => item !== child);
            if (child.id) {
              delete this.ownerDocument.byId[child.id];
            }
            child.parentNode = null;
            return child;
          }
          setAttribute(name, value) { this.attributes[name] = String(value); }
          getAttribute(name) { return this.attributes[name] || null; }
          addEventListener(name, callback) { this.listeners[name] = callback; }
          dispatchEvent(name, event) { this.listeners[name] && this.listeners[name](event || { preventDefault() {} }); }
          focus() { this.focusCalls = (this.focusCalls || 0) + 1; }
          querySelector(selector) {
            const stack = [...this.children];
            while (stack.length) {
              const item = stack.shift();
              if (selector === ".livia-message.assistant" && String(item.className || "").includes("livia-message") && String(item.className || "").includes("assistant")) {
                return item;
              }
              stack.push(...item.children);
            }
            return null;
          }
        }

        function makeStorage() {
          const values = new Map();
          return { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, String(value)), removeItem: (key) => values.delete(key) };
        }

        function makeDocument() {
          const documentRef = { byId: {}, readyState: "complete" };
          documentRef.createElement = (tag) => new Element(tag, documentRef);
          documentRef.getElementById = (id) => documentRef.byId[id] || null;
          documentRef.addEventListener = () => {};
          documentRef.head = new Element("head", documentRef);
          documentRef.body = new Element("body", documentRef);
          documentRef.documentElement = { style: { setProperty() {} } };
          const script = new Element("script", documentRef);
          script.src = "https://livia.smartcontrolbrasil.com.br/widget.js";
          script.setAttribute("data-tenant", "granimarmores-pitondo");
          script.setAttribute("data-api-url", "https://livia.smartcontrolbrasil.com.br/api/chat/");
          documentRef.currentScript = script;
          return documentRef;
        }

        async function runScenario(fetchImpl) {
          const document = makeDocument();
          const fastSetTimeout = (callback, _ms) => setTimeout(callback, 0);
          const windowRef = {
            document,
            location: { href: "https://www.granimarmorespitondo.com.br/" },
            localStorage: makeStorage(),
            sessionStorage: makeStorage(),
            crypto: { randomUUID: () => "11111111-1111-4111-8111-111111111111" },
            setTimeout: fastSetTimeout,
            clearTimeout,
            AbortController,
            fetch: fetchImpl,
            console: { error() {}, log() {} }
          };
          const sandbox = { window: windowRef, document, fetch: fetchImpl, AbortController, URL, Promise, Error, String, Math, Date, setTimeout, clearTimeout, console: windowRef.console };
          vm.runInNewContext(source, sandbox);
          await new Promise((resolve) => setTimeout(resolve, 0));
          const launcher = document.getElementById("livia-launcher");
          if (launcher) {
            launcher.dispatchEvent("click", { preventDefault() {} });
          }
          const input = document.getElementById("livia-input");
          const send = document.getElementById("livia-send");
          const footer = document.getElementById("livia-footer");
          input.value = "Quero uma pia";
          footer.dispatchEvent("submit", { preventDefault() {} });
          await new Promise((resolve) => setTimeout(resolve, 35));
          return { document, input, send };
        }

        function response(status, payload) {
          return { status, ok: status >= 200 && status < 300, json: async () => payload };
        }

        (async () => {
          let chatUrl = "";
          const success = await runScenario(async (url) => {
            if (String(url).includes("/api/widget/config/")) return response(200, {});
            chatUrl = String(url);
            return response(200, { reply: "Resposta da Lívia" });
          });
          const messages = success.document.getElementById("livia-messages").children.map((item) => item.textContent).join("\\n");
          if (!chatUrl.includes("tenant=granimarmores-pitondo")) throw new Error("chat URL missing tenant query");
          if (!messages.includes("Resposta da Lívia")) throw new Error("assistant reply was not rendered");
          if (success.input.disabled || success.send.disabled || success.send.textContent !== "Enviar") throw new Error("loading was not cleared on success");
          if (success.document.getElementById("livia-typing")) throw new Error("typing indicator remained after success");
          if ((success.input.focusCalls || 0) < 2) throw new Error("input was not refocused after send");

          const failure = await runScenario(async (url) => {
            if (String(url).includes("/api/widget/config/")) return response(200, {});
            throw new Error("network down");
          });
          const failureMessages = failure.document.getElementById("livia-messages").children.map((item) => item.textContent).join("\\n");
          if (!failureMessages.includes("Houve um problema ao conectar")) throw new Error("network fallback was not rendered");
          if (failure.input.disabled || failure.send.disabled || failure.send.textContent !== "Enviar") throw new Error("loading was not cleared on error");
          if (failure.document.getElementById("livia-typing")) throw new Error("typing indicator remained after error");
        })().catch((error) => { console.error(error && error.stack || error); process.exit(1); });
        """.replace("WIDGET_SOURCE", json.dumps(source))
        completed = subprocess.run(["node", "-e", runner], text=True, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_widget_runtime_typing_indicator_does_not_block_chat_send(self):
        source = self.client.get("/widget.js").content.decode("utf-8")
        runner = r"""
        const vm = require("vm");
        const source = WIDGET_SOURCE;

        class Element {
          constructor(tag, documentRef) {
            this.tagName = tag.toUpperCase();
            this.ownerDocument = documentRef;
            this.children = [];
            this.parentNode = null;
            this.attributes = {};
            this.listeners = {};
            this.style = {};
            this.disabled = false;
            this.value = "";
            this.textContent = "";
            this.innerHTML = "";
            this.className = "";
            this.type = "";
            this.id = "";
            this.scrollTop = 0;
            this.scrollHeight = 0;
            this.classList = {
              add: (...names) => {
                const current = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                names.forEach((name) => current.add(name));
                this.className = Array.from(current).join(" ");
              },
              remove: (...names) => {
                const remove = new Set(names);
                this.className = String(this.className || "").split(/\s+/).filter((name) => name && !remove.has(name)).join(" ");
              },
              contains: (name) => String(this.className || "").split(/\s+/).includes(name)
            };
          }
          appendChild(child) {
            child.parentNode = this;
            this.children.push(child);
            if (child.id) this.ownerDocument.byId[child.id] = child;
            return child;
          }
          removeChild(child) {
            this.children = this.children.filter((item) => item !== child);
            if (child.id) delete this.ownerDocument.byId[child.id];
            child.parentNode = null;
            return child;
          }
          setAttribute(name, value) { this.attributes[name] = String(value); }
          getAttribute(name) { return this.attributes[name] || null; }
          addEventListener(name, callback) { this.listeners[name] = callback; }
          dispatchEvent(name, event) { this.listeners[name] && this.listeners[name](event || { preventDefault() {} }); }
          focus() { this.focusCalls = (this.focusCalls || 0) + 1; }
          querySelector(selector) {
            const stack = [...this.children];
            while (stack.length) {
              const item = stack.shift();
              if (selector === ".livia-message.assistant" && String(item.className || "").includes("livia-message") && String(item.className || "").includes("assistant")) {
                return item;
              }
              stack.push(...item.children);
            }
            return null;
          }
        }

        function makeStorage() {
          const values = new Map();
          return { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, String(value)), removeItem: (key) => values.delete(key) };
        }

        function makeDocument() {
          const documentRef = { byId: {}, readyState: "complete" };
          documentRef.createElement = (tag) => new Element(tag, documentRef);
          documentRef.getElementById = (id) => documentRef.byId[id] || null;
          documentRef.addEventListener = () => {};
          documentRef.head = new Element("head", documentRef);
          documentRef.body = new Element("body", documentRef);
          documentRef.documentElement = { style: { setProperty() {} } };
          const script = new Element("script", documentRef);
          script.src = "https://livia.smartcontrolbrasil.com.br/widget.js";
          script.setAttribute("data-tenant", "smart-control-brasil");
          script.setAttribute("data-api-url", "https://livia.smartcontrolbrasil.com.br/api/chat/");
          documentRef.currentScript = script;
          return documentRef;
        }

        function response(status, payload) {
          return { status, ok: status >= 200 && status < 300, json: async () => payload };
        }

        (async () => {
          const document = makeDocument();
          let chatFetches = 0;
          const fetchImpl = async (url) => {
            if (String(url).includes("/api/widget/config/")) return response(200, {});
            chatFetches += 1;
            return response(200, { reply: "Claro. Pode me contar um pouco mais?" });
          };
          const windowRef = {
            document,
            location: { href: "https://www.smartcontrolbrasil.com.br/" },
            localStorage: makeStorage(),
            sessionStorage: makeStorage(),
            crypto: { randomUUID: () => "22222222-2222-4222-8222-222222222222" },
            setTimeout: (callback, _ms) => setTimeout(callback, 0),
            clearTimeout,
            AbortController,
            fetch: fetchImpl,
            console: { error() {}, log() {} }
          };
          vm.runInNewContext(source, { window: windowRef, document, fetch: fetchImpl, AbortController, URL, Promise, Error, String, Math, Date, setTimeout, clearTimeout, console: windowRef.console });
          await new Promise((resolve) => setTimeout(resolve, 0));
          const input = document.getElementById("livia-input");
          const send = document.getElementById("livia-send");
          input.value = "gostaria de um site";
          document.getElementById("livia-footer").dispatchEvent("submit", { preventDefault() {} });
          await new Promise((resolve) => setTimeout(resolve, 35));
          const messages = document.getElementById("livia-messages").children.map((item) => item.textContent).join("\\n");
          if (chatFetches !== 1) throw new Error("expected exactly one chat fetch, got " + chatFetches);
          if (!messages.includes("gostaria de um site")) throw new Error("user message was not rendered");
          if (!messages.includes("Claro. Pode me contar um pouco mais?")) throw new Error("assistant reply was not rendered");
          if (input.disabled || send.disabled || send.textContent !== "Enviar") throw new Error("loading remained after success");
          if (document.getElementById("livia-typing")) throw new Error("typing indicator remained after success");
        })().catch((error) => { console.error(error && error.stack || error); process.exit(1); });
        """.replace("WIDGET_SOURCE", json.dumps(source))
        completed = subprocess.run(["node", "-e", runner], text=True, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_widget_runtime_clears_loading_if_typing_indicator_throws(self):
        source = self.client.get("/widget.js").content.decode("utf-8")
        runner = r"""
        const vm = require("vm");
        const source = WIDGET_SOURCE;

        class Element {
          constructor(tag, documentRef) {
            this.tagName = tag.toUpperCase();
            this.ownerDocument = documentRef;
            this.children = [];
            this.parentNode = null;
            this.attributes = {};
            this.listeners = {};
            this.style = {};
            this.disabled = false;
            this.value = "";
            this.textContent = "";
            this.innerHTML = "";
            this.className = "";
            this.type = "";
            this.id = "";
            this.scrollTop = 0;
            this.scrollHeight = 0;
            this.classList = {
              add: (...names) => {
                const current = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                names.forEach((name) => current.add(name));
                this.className = Array.from(current).join(" ");
              },
              remove: (...names) => {
                const remove = new Set(names);
                this.className = String(this.className || "").split(/\s+/).filter((name) => name && !remove.has(name)).join(" ");
              },
              contains: (name) => String(this.className || "").split(/\s+/).includes(name)
            };
          }
          appendChild(child) {
            child.parentNode = this;
            this.children.push(child);
            if (child.id) this.ownerDocument.byId[child.id] = child;
            return child;
          }
          removeChild(child) {
            this.children = this.children.filter((item) => item !== child);
            if (child.id) delete this.ownerDocument.byId[child.id];
            child.parentNode = null;
            return child;
          }
          setAttribute(name, value) {
            if (this.id === "livia-typing" && name === "aria-label") {
              throw new Error("unexpected typing failure");
            }
            this.attributes[name] = String(value);
          }
          getAttribute(name) { return this.attributes[name] || null; }
          addEventListener(name, callback) { this.listeners[name] = callback; }
          dispatchEvent(name, event) { this.listeners[name] && this.listeners[name](event || { preventDefault() {} }); }
          focus() {}
          querySelector() { return null; }
        }

        function makeStorage() {
          const values = new Map();
          return { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, String(value)), removeItem: (key) => values.delete(key) };
        }

        (async () => {
          const documentRef = { byId: {}, readyState: "complete" };
          documentRef.createElement = (tag) => new Element(tag, documentRef);
          documentRef.getElementById = (id) => documentRef.byId[id] || null;
          documentRef.addEventListener = () => {};
          documentRef.head = new Element("head", documentRef);
          documentRef.body = new Element("body", documentRef);
          documentRef.documentElement = { style: { setProperty() {} } };
          const script = new Element("script", documentRef);
          script.src = "https://livia.smartcontrolbrasil.com.br/widget.js";
          script.setAttribute("data-tenant", "smart-control-brasil");
          script.setAttribute("data-api-url", "https://livia.smartcontrolbrasil.com.br/api/chat/");
          documentRef.currentScript = script;
          let chatFetches = 0;
          const fetchImpl = async (url) => {
            if (String(url).includes("/api/widget/config/")) return { status: 200, ok: true, json: async () => ({}) };
            chatFetches += 1;
            return { status: 200, ok: true, json: async () => ({ reply: "nao deveria chegar" }) };
          };
          const windowRef = {
            document: documentRef,
            location: { href: "https://www.smartcontrolbrasil.com.br/" },
            localStorage: makeStorage(),
            sessionStorage: makeStorage(),
            crypto: { randomUUID: () => "33333333-3333-4333-8333-333333333333" },
            setTimeout: (callback, _ms) => setTimeout(callback, 0),
            clearTimeout,
            AbortController,
            fetch: fetchImpl,
            console: { error() {}, log() {} }
          };
          vm.runInNewContext(source, { window: windowRef, document: documentRef, fetch: fetchImpl, AbortController, URL, Promise, Error, String, Math, Date, setTimeout, clearTimeout, console: windowRef.console });
          await new Promise((resolve) => setTimeout(resolve, 0));
          const input = documentRef.getElementById("livia-input");
          const send = documentRef.getElementById("livia-send");
          input.value = "gostaria de um site";
          documentRef.getElementById("livia-footer").dispatchEvent("submit", { preventDefault() {} });
          await new Promise((resolve) => setTimeout(resolve, 35));
          const messages = documentRef.getElementById("livia-messages").children.map((item) => item.textContent).join("\\n");
          if (chatFetches !== 0) throw new Error("chat fetch should not run after typing failure");
          if (!messages.includes("Houve um problema ao conectar com a Lívia")) throw new Error("fallback was not rendered after typing failure");
          if (input.disabled || send.disabled || send.textContent !== "Enviar") throw new Error("loading remained after typing failure");
          if (send.textContent === "...") throw new Error("widget stayed stuck in loading");
        })().catch((error) => { console.error(error && error.stack || error); process.exit(1); });
        """.replace("WIDGET_SOURCE", json.dumps(source))
        completed = subprocess.run(["node", "-e", runner], text=True, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_demo_page_loads_widget_script(self):
        response = self.client.get("/demo/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('/widget.js" data-tenant="smart-control-brasil"', response.content.decode("utf-8"))


class WidgetConfigEndpointTests(TestCase):
    def test_widget_config_returns_active_tenant_config(self):
        tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil", domain="smartcontrolbrasil.com.br")
        TenantAllowedOrigin.objects.create(tenant=tenant, origin="https://www.smartcontrolbrasil.com.br")
        AssistantProfile.objects.create(
            tenant=tenant,
            name="Lívia",
            widget_title="Lívia Smart Control",
            launcher_label="Fale conosco",
            initial_message="Olá pela config.",
            primary_color="#123abc",
            position="bottom_left",
            placeholder_text="Digite aqui...",
            show_branding=False,
        )

        response = self.client.get("/api/widget/config/?tenant=smart-control-brasil", HTTP_ORIGIN="https://www.smartcontrolbrasil.com.br", HTTP_X_LIVIA_TENANT="smart-control-brasil")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["tenant"], "smart-control-brasil")
        self.assertEqual(data["api_contract_version"], "2026-08-23")
        self.assertEqual(data["widget_version"], "1.0")
        self.assertEqual(data["assistant_name"], "Lívia")
        self.assertEqual(data["widget_title"], "Lívia Smart Control")
        self.assertEqual(data["launcher_label"], "Fale conosco")
        self.assertEqual(data["initial_message"], "Olá pela config.")
        self.assertEqual(data["primary_color"], "#123abc")
        self.assertEqual(data["position"], "bottom_left")
        self.assertEqual(data["placeholder_text"], "Digite aqui...")
        self.assertFalse(data["show_branding"])
        self.assertTrue(data["is_widget_enabled"])
        self.assertFalse(data["human_handoff_enabled"])
        self.assertEqual(data["human_handoff_channel"], "disabled")
        self.assertEqual(data["handoff_whatsapp_label"], "Falar com um especialista")

    def test_widget_config_inactive_tenant_returns_disabled_config(self):
        tenant = Tenant.objects.create(
            name="Inactive",
            slug="inactive",
            domain="inactive.example",
            is_active=False,
        )
        AssistantProfile.objects.create(tenant=tenant, name="Lívia")

        response = self.client.get("/api/widget/config/?tenant=inactive")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["is_widget_enabled"])

    def test_widget_config_missing_tenant_returns_disabled_config(self):
        response = self.client.get("/api/widget/config/?tenant=missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["tenant"], "missing")
        self.assertFalse(response.json()["is_widget_enabled"])

    def test_widget_config_uses_defaults_and_contains_no_secrets(self):
        tenant = Tenant.objects.create(name="Defaults", slug="defaults", domain="defaults.example")
        AssistantProfile.objects.create(tenant=tenant, name="Lívia Defaults")
        TenantAllowedOrigin.objects.create(tenant=tenant, origin="https://defaults.example")

        response = self.client.get("/api/widget/config/?tenant=defaults", HTTP_ORIGIN="https://defaults.example", HTTP_X_LIVIA_TENANT="defaults")

        data = response.json()
        self.assertEqual(data["widget_title"], "Lívia Defaults")
        self.assertEqual(data["launcher_label"], "Fale com a Lívia")
        self.assertEqual(data["primary_color"], "#2563eb")
        self.assertEqual(data["position"], "bottom_right")
        self.assertEqual(data["placeholder_text"], "Digite sua mensagem...")
        payload_text = str(data)
        self.assertNotIn("secret", payload_text.lower())
        self.assertNotIn("token", payload_text.lower())
        self.assertNotIn("551151968525", payload_text)
        self.assertNotIn("wa.me", payload_text)

    def test_widget_config_disabled_profile_returns_safe_disabled_payload(self):
        tenant = Tenant.objects.create(name="Disabled Widget", slug="disabled-widget")
        TenantAllowedOrigin.objects.create(tenant=tenant, origin="https://disabled.example")
        AssistantProfile.objects.create(tenant=tenant, is_widget_enabled=False)

        response = self.client.get(
            "/api/widget/config/?tenant=disabled-widget",
            HTTP_ORIGIN="https://disabled.example",
            HTTP_X_LIVIA_TENANT="disabled-widget",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["is_widget_enabled"])
        self.assertEqual(data["api_contract_version"], "2026-08-23")
        self.assertNotIn("secret", str(data).lower())
        self.assertNotIn("token", str(data).lower())


class WidgetCorsMiddlewareTests(TestCase):
    def setUp(self):
        tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        TenantAllowedOrigin.objects.create(tenant=tenant, origin="https://www.smartcontrolbrasil.com.br")
    @override_settings(DEBUG=False, LIVIA_ALLOWED_WIDGET_ORIGINS=["https://www.smartcontrolbrasil.com.br"])
    def test_chat_options_allows_configured_origin(self):
        response = self.client.options(
            "/api/chat/",
            HTTP_ORIGIN="https://www.smartcontrolbrasil.com.br",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_X_LIVIA_TENANT="smart-control-brasil",
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://www.smartcontrolbrasil.com.br")
        self.assertIn("POST", response["Access-Control-Allow-Methods"])

    @override_settings(DEBUG=False, LIVIA_ALLOWED_WIDGET_ORIGINS=["https://www.smartcontrolbrasil.com.br"])
    def test_chat_options_does_not_allow_unknown_origin(self):
        response = self.client.options(
            "/api/chat/",
            HTTP_ORIGIN="https://evil.example",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_X_LIVIA_TENANT="smart-control-brasil",
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response)


class WidgetConfigCorsMiddlewareTests(TestCase):
    def setUp(self):
        tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        AssistantProfile.objects.create(tenant=tenant)
        TenantAllowedOrigin.objects.create(tenant=tenant, origin="https://www.smartcontrolbrasil.com.br")
    @override_settings(DEBUG=False, LIVIA_ALLOWED_WIDGET_ORIGINS=["https://www.smartcontrolbrasil.com.br"])
    def test_config_get_allows_configured_origin(self):
        response = self.client.get(
            "/api/widget/config/?tenant=smart-control-brasil",
            HTTP_ORIGIN="https://www.smartcontrolbrasil.com.br",
            HTTP_X_LIVIA_TENANT="smart-control-brasil",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://www.smartcontrolbrasil.com.br")

    @override_settings(DEBUG=False, LIVIA_ALLOWED_WIDGET_ORIGINS=["https://www.smartcontrolbrasil.com.br"])
    def test_config_options_allows_configured_origin(self):
        response = self.client.options(
            "/api/widget/config/",
            HTTP_ORIGIN="https://www.smartcontrolbrasil.com.br",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
            HTTP_X_LIVIA_TENANT="smart-control-brasil",
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://www.smartcontrolbrasil.com.br")
        self.assertIn("GET", response["Access-Control-Allow-Methods"])



class HumanHandoffConfigTests(TestCase):
    def test_defaults_are_disabled(self):
        tenant = Tenant.objects.create(name="Tenant", slug="tenant")
        profile = AssistantProfile.objects.create(tenant=tenant)

        self.assertFalse(profile.human_handoff_enabled)
        self.assertEqual(profile.human_handoff_channel, "disabled")
        self.assertFalse(profile.has_valid_whatsapp_handoff)

    def test_whatsapp_number_is_normalized_and_url_is_safe(self):
        tenant = Tenant.objects.create(name="Tenant", slug="tenant")
        profile = AssistantProfile.objects.create(
            tenant=tenant,
            human_handoff_enabled=True,
            human_handoff_channel="whatsapp",
            handoff_whatsapp_number="+55 (11) 51968-525",
            handoff_whatsapp_message="Olá, Lívia & equipe",
        )

        self.assertEqual(profile.handoff_whatsapp_number, "551151968525")
        self.assertEqual(build_whatsapp_handoff_url(profile), "https://wa.me/551151968525?text=Ol%C3%A1%2C+L%C3%ADvia+%26+equipe")

    def test_invalid_number_is_rejected_when_whatsapp_enabled(self):
        tenant = Tenant.objects.create(name="Tenant", slug="tenant")
        profile = AssistantProfile(
            tenant=tenant,
            human_handoff_enabled=True,
            human_handoff_channel="whatsapp",
            handoff_whatsapp_number="123",
        )

        with self.assertRaises(ValidationError):
            profile.full_clean()

    def test_public_config_contains_safe_handoff_fields_only(self):
        tenant = Tenant.objects.create(name="Tenant", slug="tenant")
        AssistantProfile.objects.create(
            tenant=tenant,
            human_handoff_enabled=True,
            human_handoff_channel="whatsapp",
            handoff_whatsapp_number="551151968525",
        )

        TenantAllowedOrigin.objects.create(tenant=tenant, origin="https://tenant.example")
        response = self.client.get("/api/widget/config/?tenant=tenant", HTTP_ORIGIN="https://tenant.example", HTTP_X_LIVIA_TENANT="tenant")

        data = response.json()
        self.assertTrue(data["human_handoff_enabled"])
        self.assertEqual(data["human_handoff_channel"], "whatsapp")
        payload_text = str(data)
        self.assertNotIn("551151968525", payload_text)
        self.assertNotIn("wa.me", payload_text)
