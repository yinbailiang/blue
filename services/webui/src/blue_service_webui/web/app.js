"use strict";

(() => {
  const els = {
    body: document.body,
    sidebar: document.getElementById("sidebar"),
    overlay: document.getElementById("overlay"),
    menuToggle: document.getElementById("menu-toggle"),
    newChat: document.getElementById("new-chat"),
    refreshSessions: document.getElementById("refresh-sessions"),
    resetChat: document.getElementById("reset-chat"),
    themeToggle: document.getElementById("theme-toggle"),
    sessionList: document.getElementById("session-list"),
    toolList: document.getElementById("tool-list"),
    toolCount: document.getElementById("tool-count"),
    systemDot: document.getElementById("system-dot"),
    systemText: document.getElementById("system-text"),
    systemLine: document.getElementById("system-line"),
    chatScroll: document.getElementById("chat-scroll"),
    chatTitle: document.getElementById("chat-title"),
    welcome: document.getElementById("welcome"),
    welcomeTools: document.getElementById("welcome-tools"),
    messageList: document.getElementById("message-list"),
    typing: document.getElementById("typing"),
    typingLabel: document.getElementById("typing-label"),
    composer: document.getElementById("composer"),
    attachments: document.getElementById("composer-attachments"),
    attach: document.getElementById("attach"),
    attachmentInput: document.getElementById("attachment-input"),
    prompt: document.getElementById("prompt"),
    send: document.getElementById("send"),
    sendIcon: document.getElementById("send-icon"),
    sendLabel: document.getElementById("send-label"),
    socketPill: document.getElementById("socket-pill"),
    toastRegion: document.getElementById("toast-region"),
  };

  const state = {
    sessionId: null,
    sessions: [],
    tools: [],
    messages: [],
    titles: readJson("blue.titles", {}),
    busy: false,
    socket: null,
    socketPromise: null,
    stickToBottom: true,
    liveMessage: null,
    streamRenderTimer: null,
    pendingAttachments: [],
  };

  const MAX_ATTACHMENTS = 4;
  const MAX_ATTACHMENT_EDGE = 1568;
  const ATTACHMENT_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];

  /*
   * 增量渲染缓存：state.messages 的每一项对应一个已挂载节点。
   * 流式增量只 patch 变化的节点，避免整表 innerHTML 重建导致
   * 滚动抖动、折叠状态丢失与复制按钮复位。
   */
  const view = {
    messages: [],
    nodes: [],
    signatures: [],
    parts: new WeakMap(),
  };

  function resetView() {
    view.messages = [];
    view.nodes = [];
    view.signatures = [];
    els.messageList.replaceChildren();
  }

  function readJson(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch {
      return fallback;
    }
  }

  function saveJson(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* ignore quota errors */
    }
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function uuid() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
  }

  function shortId(id) {
    return String(id || "").replace(/^s-/, "").slice(0, 8) || "new";
  }

  function titleFor(id) {
    return state.titles[id] || `会话 ${shortId(id)}`;
  }

  function rememberTitle(id, content) {
    const title = String(content || "").replace(/\s+/g, " ").trim().slice(0, 34);
    if (!id || !title) return;
    state.titles[id] = title;
    saveJson("blue.titles", state.titles);
    if (id === state.sessionId) els.chatTitle.textContent = title;
  }

  function initialsFor(id) {
    const title = titleFor(id).replace(/[^\p{L}\p{N}]/gu, "");
    return (title.slice(0, 2) || "BL").toUpperCase();
  }

  function relativeTime(seconds) {
    const value = Number(seconds || 0);
    if (!value) return "刚刚";
    const elapsed = Math.max(0, Date.now() / 1000 - value);
    if (elapsed < 60) return "刚刚";
    if (elapsed < 3600) return `${Math.floor(elapsed / 60)} 分钟前`;
    if (elapsed < 86400) return `${Math.floor(elapsed / 3600)} 小时前`;
    if (elapsed < 86400 * 7) return `${Math.floor(elapsed / 86400)} 天前`;
    return new Date(value * 1000).toLocaleDateString("zh-CN");
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        "content-type": "application/json",
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      let detail = `请求失败（${response.status}）`;
      try {
        const payload = await response.json();
        if (payload?.detail) detail = String(payload.detail);
      } catch {
        /* ignore */
      }
      throw new Error(detail);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function toast(message, kind = "info") {
    const node = document.createElement("div");
    node.className = `toast toast-${kind}`;
    node.textContent = String(message);
    els.toastRegion.appendChild(node);
    window.setTimeout(() => {
      node.classList.add("leaving");
      window.setTimeout(() => node.remove(), 220);
    }, 3800);
  }

  function showWelcome(show) {
    els.welcome.hidden = !show;
    els.messageList.hidden = show;
    if (show) resetView();
  }

  function setBusy(busy, label = "Agent 正在思考…") {
    state.busy = busy;
    els.typing.hidden = !busy;
    els.typingLabel.textContent = label;
    els.send.classList.toggle("loading", busy);
    els.sendIcon.setAttribute("href", busy ? "#i-target" : "#i-send");
    els.sendLabel.textContent = busy ? "停止" : "发送";
    els.send.setAttribute("aria-label", busy ? "停止生成" : "发送消息");
    updateSendState();
  }

  function updateSendState() {
    // 生成中按钮用于中断，此时即使输入框为空也应可点
    const hasContent = Boolean(els.prompt.value.trim()) || state.pendingAttachments.length > 0;
    els.send.disabled = state.busy ? false : !hasContent;
  }

  function setSocketOnline(online, label) {
    els.socketPill.classList.toggle("online", online);
    els.socketPill.innerHTML = `<span class="status-dot ${online ? "ok" : ""}"></span>${escapeHtml(
      label || (online ? "实时连接" : "离线"),
    )}`;
  }

  function scrollToBottom(force = false) {
    if (!force && !state.stickToBottom) return;
    requestAnimationFrame(() => {
      els.chatScroll.scrollTop = els.chatScroll.scrollHeight;
    });
  }

  function isSourceNearBottom() {
    const node = els.chatScroll;
    return node.scrollHeight - node.scrollTop - node.clientHeight < 160;
  }

  function scheduleStreamRender() {
    if (state.streamRenderTimer) return;
    state.streamRenderTimer = window.setTimeout(() => {
      state.streamRenderTimer = null;
      renderMessages({ scroll: true, force: state.stickToBottom });
    }, 40);
  }

  function resizePrompt() {
    els.prompt.style.height = "auto";
    els.prompt.style.height = `${Math.min(els.prompt.scrollHeight, 210)}px`;
    updateSendState();
  }

  /* ------------------------------------------------------------------ */
  /* Markdown                                                           */
  /* ------------------------------------------------------------------ */
  function inlineMarkdown(value) {
    return value
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
      .replace(
        /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
      )
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  }

  function renderCodeBlock(block) {
    return `<div class="code-block"><div class="code-head"><span>${escapeHtml(
      block.lang || "code",
    )}</span><button class="copy-button" type="button" data-copy><svg><use href="#i-copy"></use></svg>复制</button></div><pre><code>${escapeHtml(
      block.code,
    )}</code></pre></div>`;
  }

  function renderMarkdownLegacy(source) {
    const codeBlocks = [];
    let text = escapeHtml(source || "");
    text = text.replace(/```([\w+-]*)\r?\n?([\s\S]*?)```/g, (_, lang, code) => {
      const index = codeBlocks.push({ lang: lang || "code", code }) - 1;
      return `@@CODE${index}@@`;
    });

    const lines = inlineMarkdown(text).split("\n");
    const output = [];
    let paragraph = [];
    let listType = null;

    const flushParagraph = () => {
      if (!paragraph.length) return;
      output.push(`<p>${paragraph.join("<br>")}</p>`);
      paragraph = [];
    };
    const closeList = () => {
      if (!listType) return;
      output.push(`</${listType}>`);
      listType = null;
    };

    for (const line of lines) {
      if (!line.trim()) {
        flushParagraph();
        closeList();
        continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flushParagraph();
        closeList();
        const level = Math.min(4, heading[1].length + 1);
        output.push(`<h${level}>${heading[2]}</h${level}>`);
        continue;
      }
      if (/^>\s?/.test(line)) {
        flushParagraph();
        closeList();
        output.push(`<blockquote>${line.replace(/^>\s?/, "")}</blockquote>`);
        continue;
      }
      if (/^\s*[-*]\s+/.test(line)) {
        flushParagraph();
        if (listType !== "ul") {
          closeList();
          output.push("<ul>");
          listType = "ul";
        }
        output.push(`<li>${line.replace(/^\s*[-*]\s+/, "")}</li>`);
        continue;
      }
      if (/^\s*\d+\.\s+/.test(line)) {
        flushParagraph();
        if (listType !== "ol") {
          closeList();
          output.push("<ol>");
          listType = "ol";
        }
        output.push(`<li>${line.replace(/^\s*\d+\.\s+/, "")}</li>`);
        continue;
      }
      closeList();
      paragraph.push(line);
    }
    flushParagraph();
    closeList();

    return output
      .join("\n")
      .replace(/@@CODE(\d+)@@/g, (_, index) => renderCodeBlock(codeBlocks[Number(index)]));
  }

  /*
   * 完整 Markdown 与公式：优先使用 vendor 内的 marked + KaTeX。
   * 任一库缺失时退回上面的简易解析器，保证页面不会白屏。
   */
  function safeHref(href) {
    const value = String(href ?? "").trim();
    if (!value) return "#";
    if (/^(https?:\/\/|mailto:|#|\/|\.\/|\.\.\/)/i.test(value)) return value;
    return "#";
  }

  function renderMath(tex, display) {
    const katex = window.katex;
    if (!katex) return `<code class="math-fallback">${escapeHtml(tex)}</code>`;
    try {
      return katex.renderToString(tex, {
        displayMode: display,
        throwOnError: false,
        strict: "ignore",
        trust: false,
      });
    } catch {
      return `<code class="math-fallback">${escapeHtml(tex)}</code>`;
    }
  }

  // 块级公式：$$...$$ 或 \[...\]，必须独占一行开头
  const MATH_BLOCK = {
    name: "mathBlock",
    level: "block",
    start(src) {
      const index = src.search(/\$\$|\\\[/);
      return index < 0 ? undefined : index;
    },
    tokenizer(src) {
      const dollar = /^\$\$([\s\S]+?)\$\$[ \t]*(?:\n+|$)/.exec(src);
      if (dollar) return { type: "mathBlock", raw: dollar[0], text: dollar[1].trim() };
      const bracket = /^\\\[([\s\S]+?)\\\][ \t]*(?:\n+|$)/.exec(src);
      if (bracket) return { type: "mathBlock", raw: bracket[0], text: bracket[1].trim() };
      return undefined;
    },
    renderer(token) {
      return `<div class="math-block">${renderMath(token.text, true)}</div>`;
    },
  };

  // 行内公式：$...$ 或 \(...\)，要求首尾非空白且不跨行，避免误伤货币写法
  const MATH_INLINE = {
    name: "mathInline",
    level: "inline",
    start(src) {
      const index = src.search(/\$|\\\(/);
      return index < 0 ? undefined : index;
    },
    tokenizer(src) {
      const dollar = /^\$([^\s$](?:[^$\n]*[^\s$])?)\$/.exec(src);
      if (dollar) return { type: "mathInline", raw: dollar[0], text: dollar[1] };
      const paren = /^\\\(([\s\S]+?)\\\)/.exec(src);
      if (paren) return { type: "mathInline", raw: paren[0], text: paren[1].trim() };
      return undefined;
    },
    renderer(token) {
      return renderMath(token.text, false);
    },
  };

  let markdownReady = false;

  function configureMarkdown() {
    if (markdownReady || !window.marked) return;
    window.marked.use({
      gfm: true,
      breaks: true,
      extensions: [MATH_BLOCK, MATH_INLINE],
      renderer: {
        // 模型输出可能夹带 HTML，一律转义，不做原始标记透传
        html(token) {
          return escapeHtml(token.text);
        },
        code(token) {
          const lang = String(token.lang || "").trim().split(/\s+/)[0];
          const code = String(token.text || "").replace(/\n$/, "");
          return renderCodeBlock({ lang, code });
        },
        link(token) {
          const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
          const text = this.parser.parseInline(token.tokens);
          return `<a href="${escapeHtml(safeHref(token.href))}"${title} target="_blank" rel="noopener noreferrer">${text}</a>`;
        },
        image(token) {
          const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
          return `<img src="${escapeHtml(safeHref(token.href))}" alt="${escapeHtml(
            token.text || "",
          )}"${title} loading="lazy" />`;
        },
      },
    });
    markdownReady = true;
  }

  function renderMarkdown(source) {
    const text = String(source || "");
    if (!markdownReady) return renderMarkdownLegacy(text);
    try {
      const html = window.marked.parse(text);
      return typeof html === "string" ? html : renderMarkdownLegacy(text);
    } catch {
      return renderMarkdownLegacy(text);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Rendering                                                          */
  /* ------------------------------------------------------------------ */
  function sessionItem(session, active) {
    const id = session.session_id;
    const count = Number(session.message_count || 0);
    return `<div class="session-item ${active ? "active" : ""}" data-session="${escapeHtml(
      id,
    )}" role="button" tabindex="0" aria-current="${active ? "page" : "false"}">
      <span class="session-avatar">${escapeHtml(initialsFor(id))}</span>
      <span class="session-copy">
        <span class="session-name">${escapeHtml(titleFor(id))}</span>
        <span class="session-meta">${count} 条消息 · ${relativeTime(session.updated_at)}</span>
      </span>
      <button class="session-delete" type="button" data-delete="${escapeHtml(
        id,
      )}" aria-label="删除会话"><svg><use href="#i-trash"></use></svg></button>
    </div>`;
  }

  function renderSessions() {
    const items = state.sessions.slice().sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    const hasActive = state.sessionId && items.some((item) => item.session_id === state.sessionId);
    let html = "";
    if (state.sessionId && !hasActive) {
      html += sessionItem(
        { session_id: state.sessionId, message_count: state.messages.length, updated_at: Date.now() / 1000 },
        true,
      );
    }
    html += items.map((item) => sessionItem(item, item.session_id === state.sessionId)).join("");
    if (!html) {
      html = '<div class="side-empty">还没有会话。发出第一条消息，Agent 会为你创建会话。</div>';
    }
    els.sessionList.innerHTML = html;
  }

  function renderTools() {
    els.toolCount.textContent = String(state.tools.length);
    if (!state.tools.length) {
      els.toolList.innerHTML = '<span class="tool-chip"><svg><use href="#i-tool"></use></svg>等待工具上线</span>';
      els.welcomeTools.textContent = "工具目录加载中…";
      return;
    }
    els.toolList.innerHTML = state.tools
      .map((tool) => {
        const fn = tool.function || {};
        return `<span class="tool-chip" title="${escapeHtml(fn.description || "")}"><svg><use href="#i-tool"></use></svg>${escapeHtml(
          fn.name || "tool",
        )}</span>`;
      })
      .join("");
    els.welcomeTools.textContent = `${state.tools.length} 个工具已连接`;
  }

  function renderSystem(system) {
    if (!system?.available) {
      els.systemDot.className = "status-dot";
      els.systemText.textContent = "控制面状态不可见";
      els.systemLine.title = "以独立模式运行时无法读取 supervisor 状态";
      return;
    }
    const services = system.services || [];
    const ready = services.filter((service) => service.status === "Ready").length;
    const allReady = services.length > 0 && ready === services.length;
    els.systemDot.className = `status-dot ${allReady ? "ok" : "warn"}`;
    els.systemText.textContent = `控制面 · ${ready}/${services.length} 服务就绪`;
    els.systemLine.title = services
      .map(
        (service) =>
          `${service.name} ${service.package_version || "-"}: ${service.status}`,
      )
      .join("\n");
  }

  const CHEVRON = '<svg class="chevron"><use href="#i-chevron"></use></svg>';

  function messageSignature(message) {
    const content = String(message.content || "");
    const reasoning = String(message.reasoning_content || "");
    const calls = message.tool_calls || [];
    return [
      message.role,
      message.live ? 1 : 0,
      content.length,
      content.slice(-24),
      reasoning.length,
      reasoning.slice(-24),
      calls
        .map((call) => `${call.name}|${call.status || ""}|${JSON.stringify(call.arguments || {})}`)
        .join("\u00a7"),
    ].join("|");
  }

  function prettyArguments(call) {
    try {
      return JSON.stringify(call.arguments ?? {}, null, 2);
    } catch {
      return String(call.arguments ?? "");
    }
  }

  function compactArguments(call) {
    try {
      return JSON.stringify(call.arguments ?? {});
    } catch {
      return String(call.arguments ?? "");
    }
  }

  function toolBrief(text) {
    const line = String(text || "")
      .split("\n")
      .find((item) => item.trim());
    return (line || "").trim().slice(0, 120) || "（无输出）";
  }

  function updateToolCallNode(node, parts, call) {
    parts.name.textContent = call.name || "tool";
    const preview = compactArguments(call);
    if (preview !== parts.previewText) {
      parts.previewText = preview;
      parts.preview.textContent = preview;
      parts.args.textContent = prettyArguments(call);
    }
    node.classList.toggle("running", call.status === "running");
  }

  function createToolCallNode(call) {
    const node = document.createElement("details");
    node.className = "tool-call";
    node.innerHTML = `<summary>
        <span class="tool-call-icon"><svg><use href="#i-tool"></use></svg></span>
        <span class="tool-call-name"></span>
        <span class="tool-call-preview"></span>
        ${CHEVRON}
      </summary>
      <pre class="tool-call-args"></pre>`;
    const parts = {
      name: node.querySelector(".tool-call-name"),
      preview: node.querySelector(".tool-call-preview"),
      args: node.querySelector(".tool-call-args"),
      previewText: null,
    };
    view.parts.set(node, parts);
    updateToolCallNode(node, parts, call);
    return node;
  }

  function updateToolCallList(container, calls) {
    container.hidden = calls.length === 0;
    while (container.children.length > calls.length) {
      container.lastElementChild?.remove();
    }
    calls.forEach((call, index) => {
      const existing = container.children[index];
      if (!existing) {
        container.appendChild(createToolCallNode(call));
        return;
      }
      const parts = view.parts.get(existing);
      if (parts) updateToolCallNode(existing, parts, call);
    });
  }

  function updateAssistantNode(parts, message) {
    parts.label.textContent = `BLUE${message.name ? ` · ${message.name}` : ""}`;

    const reasoning = String(message.reasoning_content || "");
    parts.reasoning.hidden = !reasoning;
    if (reasoning) parts.reasoningText.textContent = reasoning;

    const content = String(message.content || "");
    const calls = message.tool_calls || [];
    parts.content.hidden = !content;
    if (content) {
      const html = renderMarkdown(content);
      if (html !== parts.contentHtml) {
        parts.contentHtml = html;
        parts.content.innerHTML = html;
      }
    }

    parts.placeholder.hidden = Boolean(content) || calls.length > 0;
    if (!parts.placeholder.hidden) {
      parts.placeholder.textContent = message.live ? "正在生成…" : "正在调用工具…";
    }

    updateToolCallList(parts.calls, calls);
  }

  function createAssistantNode(message) {
    const wrapper = document.createElement("div");
    wrapper.className = "message message-assistant";
    wrapper.innerHTML = `
      <div class="avatar avatar-agent"><svg><use href="#i-spark"></use></svg></div>
      <div class="message-main">
        <div class="message-label">BLUE</div>
        <div class="bubble">
          <details class="reasoning" hidden><summary>查看思考过程</summary><p></p></details>
          <div class="bubble-content md" hidden></div>
          <div class="message-label placeholder" hidden>正在生成…</div>
          <div class="tool-call-list" hidden></div>
        </div>
      </div>`;
    const parts = {
      label: wrapper.querySelector(".message-label"),
      reasoning: wrapper.querySelector(".reasoning"),
      reasoningText: wrapper.querySelector(".reasoning p"),
      content: wrapper.querySelector(".bubble-content"),
      placeholder: wrapper.querySelector(".placeholder"),
      calls: wrapper.querySelector(".tool-call-list"),
      contentHtml: "",
    };
    view.parts.set(wrapper, parts);
    updateAssistantNode(parts, message);
    return wrapper;
  }

  function updateToolNode(parts, message, name) {
    parts.label.textContent = `TOOL · ${name}`;
    parts.name.textContent = name;
    const text = String(message.content || "");
    if (text !== parts.text) {
      parts.text = text;
      parts.body.textContent = text || "（无输出）";
      parts.brief.textContent = toolBrief(text);
    }
  }

  function createToolNode(message, name) {
    const wrapper = document.createElement("div");
    wrapper.className = "message message-tool";
    wrapper.innerHTML = `
      <div class="avatar avatar-tool"><svg><use href="#i-tool"></use></svg></div>
      <div class="message-main">
        <div class="message-label">TOOL</div>
        <details class="tool-result">
          <summary>
            <span class="status-dot ok"></span>
            <span class="tool-result-name"></span>
            <span class="tool-result-brief"></span>
            ${CHEVRON}
          </summary>
          <pre></pre>
        </details>
      </div>`;
    const parts = {
      label: wrapper.querySelector(".message-label"),
      name: wrapper.querySelector(".tool-result-name"),
      brief: wrapper.querySelector(".tool-result-brief"),
      body: wrapper.querySelector(".tool-result pre"),
      text: null,
    };
    view.parts.set(wrapper, parts);
    updateToolNode(parts, message, name);
    return wrapper;
  }

  function updateUserNode(parts, message) {
    const text = String(message.content || "");
    const images = (message.parts || []).filter(
      (part) => part.type === "image" && part.url,
    );
    const signature = text + "|" + images.map((part) => part.url).join(",");
    if (signature === parts.signature) return;
    parts.signature = signature;
    parts.text = text;
    parts.content.textContent = "";
    if (text) parts.content.appendChild(document.createTextNode(text));
    for (const part of images) {
      const image = document.createElement("img");
      image.className = "bubble-image";
      image.src = part.url;
      image.alt = "附件图片";
      image.loading = "lazy";
      parts.content.appendChild(image);
    }
  }

  function createUserNode(message) {
    const wrapper = document.createElement("div");
    wrapper.className = "message message-user";
    wrapper.innerHTML = `
      <div class="avatar avatar-user"><svg><use href="#i-user"></use></svg></div>
      <div class="message-main">
        <div class="message-label">YOU</div>
        <div class="bubble"><div class="bubble-content"></div></div>
      </div>`;
    const parts = {
      content: wrapper.querySelector(".bubble-content"),
      text: null,
    };
    view.parts.set(wrapper, parts);
    updateUserNode(parts, message);
    return wrapper;
  }

  function toolNameIndex(messages) {
    const names = new Map();
    for (const message of messages) {
      if (message.role !== "assistant") continue;
      for (const call of message.tool_calls || []) names.set(call.id, call.name);
    }
    return names;
  }

  function createMessageNode(message, names) {
    if (message.role === "assistant") return createAssistantNode(message);
    if (message.role === "tool") {
      const name = names.get(message.tool_call_id) || message.name || "tool";
      return createToolNode(message, name);
    }
    return createUserNode(message);
  }

  function updateMessageNode(node, message, names) {
    const parts = view.parts.get(node);
    if (!parts) return;
    if (message.role === "assistant") updateAssistantNode(parts, message);
    else if (message.role === "tool") {
      const name = names.get(message.tool_call_id) || message.name || "tool";
      updateToolNode(parts, message, name);
    } else updateUserNode(parts, message);
  }

  function mountMessage(message, names) {
    const node = createMessageNode(message, names);
    els.messageList.appendChild(node);
    view.nodes.push(node);
    view.messages.push(message);
    view.signatures.push(messageSignature(message));
  }

  function syncMessages(options = {}) {
    const messages = state.messages.filter((message) => message.role !== "system");
    if (!messages.length) {
      showWelcome(true);
      return;
    }
    showWelcome(false);
    els.messageList.hidden = false;

    let common = 0;
    while (
      common < messages.length &&
      common < view.messages.length &&
      messages[common] === view.messages[common]
    ) {
      common += 1;
    }

    const names = toolNameIndex(messages);
    if (common < view.messages.length) {
      resetView();
      messages.forEach((message) => mountMessage(message, names));
    } else {
      for (let index = common; index < messages.length; index += 1) {
        mountMessage(messages[index], names);
      }
      for (let index = 0; index < common; index += 1) {
        const signature = messageSignature(messages[index]);
        if (signature === view.signatures[index]) continue;
        view.signatures[index] = signature;
        updateMessageNode(view.nodes[index], messages[index], names);
      }
    }

    const live = state.messages.find((message) => message.live);
    if (live && (live.content || live.reasoning_content || live.tool_calls?.length)) {
      els.typing.hidden = true;
    }
    if (options.scroll) scrollToBottom(options.force === true);
  }

  function renderMessages(options = {}) {
    syncMessages(options);
  }

  function updateTitleFromMessages() {
    const user = state.messages.find(
      (message) => message.role === "user" && String(message.content || "").trim(),
    );
    if (user) rememberTitle(state.sessionId, user.content);
  }

  function applySnapshot(snapshot, options = {}) {
    if (!snapshot) return;
    state.sessionId = snapshot.session_id;
    state.messages = Array.isArray(snapshot.messages) ? snapshot.messages : [];
    state.liveMessage = null;
    try {
      localStorage.setItem("blue.session", snapshot.session_id);
    } catch {
      /* ignore */
    }
    updateTitleFromMessages();
    els.chatTitle.textContent = titleFor(snapshot.session_id);
    renderMessages({ scroll: true, force: options.force === true });
    const existing = state.sessions.find((item) => item.session_id === snapshot.session_id);
    const next = {
      session_id: snapshot.session_id,
      message_count: snapshot.message_count,
      updated_at: snapshot.updated_at,
    };
    if (existing) Object.assign(existing, next);
    else state.sessions.push(next);
    renderSessions();
  }

  function appendAssistantText(text) {
    if (!text) return;
    const last = state.messages[state.messages.length - 1];
    if (last?.role === "assistant" && last.content === text) return;
    state.messages.push({ role: "assistant", content: text, tool_calls: [] });
    renderMessages({ scroll: true, force: true });
  }

  /* ------------------------------------------------------------------ */
  /* Data loading                                                       */
  /* ------------------------------------------------------------------ */
  async function loadTools() {
    try {
      const payload = await api("/api/tools");
      state.tools = payload.tools || [];
    } catch (error) {
      state.tools = [];
      toast(`工具目录：${error.message}`, "error");
    }
    renderTools();
  }

  async function loadSystem() {
    try {
      renderSystem(await api("/api/system"));
    } catch {
      renderSystem(null);
    }
  }

  async function refreshSessions(showError = false) {
    try {
      const payload = await api("/api/sessions");
      state.sessions = payload.sessions || [];
      renderSessions();
    } catch (error) {
      if (showError) toast(`会话列表：${error.message}`, "error");
    }
  }

  async function loadSession(sessionId, options = {}) {
    if (state.busy) {
      toast("请等待当前回复完成", "info");
      return;
    }
    try {
      const snapshot = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
      applySnapshot(snapshot, { force: true });
      closeSidebar();
    } catch (error) {
      if (options.silent) {
        localStorage.removeItem("blue.session");
        return;
      }
      toast(`读取会话失败：${error.message}`, "error");
    }
  }

  function newChat(close = true) {
    if (state.busy) {
      toast("请等待当前回复完成", "info");
      return;
    }
    state.sessionId = null;
    state.messages = [];
    state.liveMessage = null;
    state.stickToBottom = true;
    try {
      localStorage.removeItem("blue.session");
    } catch {
      /* ignore */
    }
    els.chatTitle.textContent = "新对话";
    showWelcome(true);
    renderSessions();
    if (close) closeSidebar();
    els.prompt.focus();
  }

  function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("读取图片失败"));
      reader.readAsDataURL(file);
    });
  }

  function loadImageElement(src) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error("图片解码失败"));
      image.src = src;
    });
  }

  async function prepareAttachment(file) {
    if (!ATTACHMENT_TYPES.includes(file.type)) {
      throw new Error("仅支持 PNG / JPEG / GIF / WebP");
    }
    const dataUrl = await readFileAsDataUrl(file);
    const base64 = dataUrl.split(",")[1] || "";
    const image = await loadImageElement(dataUrl);
    const longest = Math.max(image.width, image.height);
    if (longest <= MAX_ATTACHMENT_EDGE) {
      return { mime: file.type, data: base64, name: file.name || null };
    }
    // 浏览器端降采样：既省 token，也避免超大请求体
    const scale = MAX_ATTACHMENT_EDGE / longest;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(image.width * scale);
    canvas.height = Math.round(image.height * scale);
    const context = canvas.getContext("2d");
    if (!context) return { mime: file.type, data: base64, name: file.name || null };
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const mime = file.type === "image/png" ? "image/png" : "image/jpeg";
    const scaled = canvas.toDataURL(mime, 0.9);
    return { mime, data: scaled.split(",")[1] || "", name: file.name || null };
  }

  async function addAttachments(files) {
    for (const file of files) {
      if (state.pendingAttachments.length >= MAX_ATTACHMENTS) {
        toast(`一次最多附带 ${MAX_ATTACHMENTS} 张图片`, "info");
        break;
      }
      try {
        state.pendingAttachments.push(await prepareAttachment(file));
      } catch (error) {
        toast(error.message || "图片处理失败", "error");
      }
    }
    renderPendingAttachments();
    updateSendState();
  }

  function renderPendingAttachments() {
    if (!els.attachments) return;
    els.attachments.textContent = "";
    els.attachments.hidden = state.pendingAttachments.length === 0;
    state.pendingAttachments.forEach((attachment, index) => {
      const chip = document.createElement("div");
      chip.className = "attachment-chip";
      const image = document.createElement("img");
      image.src = `data:${attachment.mime};base64,${attachment.data}`;
      image.alt = attachment.name || "待发送图片";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "attachment-remove";
      remove.textContent = "×";
      remove.setAttribute("aria-label", "移除图片");
      remove.addEventListener("click", () => {
        state.pendingAttachments.splice(index, 1);
        renderPendingAttachments();
        updateSendState();
      });
      chip.append(image, remove);
      els.attachments.appendChild(chip);
    });
  }

  async function sendViaRest(sessionId, text, attachments = state.pendingAttachments) {
    const reply = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        text,
        attachments,
      }),
    });
    appendAssistantText(reply.text);
    setBusy(false);
    await refreshSessions();
    try {
      await loadSession(sessionId);
    } catch {
      /* snapshot is best effort */
    }
  }

  async function sendPrompt(rawText) {
    const text = String(rawText || "").trim();
    const attachments = state.pendingAttachments.slice();
    if ((!text && !attachments.length) || state.busy) return;
    const sessionId = state.sessionId || uuid();
    if (!state.sessionId) {
      state.sessionId = sessionId;
      try {
        localStorage.setItem("blue.session", sessionId);
      } catch {
        /* ignore */
      }
    }
    rememberTitle(sessionId, text || (attachments.length ? "[图片]" : ""));
    state.liveMessage = null;
    const localParts = [];
    if (text) localParts.push({ type: "text", text });
    attachments.forEach((attachment) => {
      localParts.push({
        type: "image",
        url: `data:${attachment.mime};base64,${attachment.data}`,
      });
    });
    state.messages.push({
      role: "user",
      content: text,
      parts: localParts,
      tool_calls: [],
    });
    state.pendingAttachments = [];
    renderPendingAttachments();
    showWelcome(false);
    renderMessages({ scroll: true, force: true });
    renderSessions();
    setBusy(true, "Agent 正在思考…");
    els.prompt.value = "";
    resizePrompt();

    try {
      const socket = await ensureSocket();
      socket.send(
        JSON.stringify({
          type: "prompt",
          session_id: sessionId,
          text,
          attachments,
        }),
      );
    } catch {
      try {
        await sendViaRest(sessionId, text, attachments);
      } catch (error) {
        setBusy(false);
        state.messages.push({
          role: "assistant",
          content: `⚠️ ${error.message || "发送失败"}`,
          tool_calls: [],
        });
        renderMessages({ scroll: true, force: true });
        toast(error.message || "发送失败", "error");
      }
    }
  }

  function cancelPrompt() {
    if (!state.busy) return;
    const socket = state.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      toast("实时连接不可用，无法中断", "error");
      setBusy(false);
      return;
    }
    socket.send(
      JSON.stringify({ type: "cancel", session_id: state.sessionId || "" }),
    );
    els.typingLabel.textContent = "正在中断…";
  }

  /* ------------------------------------------------------------------ */
  /* WebSocket                                                          */
  /* ------------------------------------------------------------------ */
  function ensureSocket() {
    if (state.socket?.readyState === WebSocket.OPEN) return Promise.resolve(state.socket);
    if (state.socketPromise) return state.socketPromise;
    state.socketPromise = new Promise((resolve, reject) => {
      const protocol = window.location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${protocol}://${window.location.host}/ws/chat`);
      const timer = window.setTimeout(() => {
        socket.close();
        state.socketPromise = null;
        reject(new Error("实时连接超时"));
      }, 5000);
      socket.onopen = () => {
        window.clearTimeout(timer);
        state.socket = socket;
        state.socketPromise = null;
        setSocketOnline(true, "实时连接");
        resolve(socket);
      };
      socket.onmessage = handleSocketMessage;
      socket.onerror = () => {
        window.clearTimeout(timer);
        state.socketPromise = null;
        setSocketOnline(false, "离线");
        reject(new Error("实时连接失败"));
      };
      socket.onclose = () => {
        window.clearTimeout(timer);
        state.socket = null;
        state.socketPromise = null;
        setSocketOnline(false, "离线");
        if (state.liveMessage) state.liveMessage.live = false;
        state.liveMessage = null;
        if (state.busy) {
          setBusy(false);
          toast("实时连接已断开，请重试", "error");
        }
      };
    });
    return state.socketPromise;
  }

  function currentLiveMessage() {
    if (state.liveMessage && state.messages.includes(state.liveMessage)) {
      return state.liveMessage;
    }
    const last = state.messages.at(-1);
    if (last?.role === "assistant" && last.live) {
      state.liveMessage = last;
      return last;
    }
    return null;
  }

  function beginStreamStep() {
    const current = currentLiveMessage();
    if (
      current &&
      !current.content &&
      !current.reasoning_content &&
      !(current.tool_calls || []).length
    ) {
      return current;
    }
    const message = {
      role: "assistant",
      content: "",
      reasoning_content: "",
      tool_calls: [],
      live: true,
    };
    state.messages.push(message);
    state.liveMessage = message;
    els.typing.hidden = true;
    scheduleStreamRender();
    return message;
  }

  function markToolCallStatus(callId, status) {
    if (!callId) return;
    for (let index = state.messages.length - 1; index >= 0; index -= 1) {
      const message = state.messages[index];
      if (message.role !== "assistant") continue;
      const call = (message.tool_calls || []).find((item) => item.id === callId);
      if (call) {
        call.status = status;
        return;
      }
    }
  }

  function handleStreamEvent(event) {
    if (!state.busy) return;
    const kind = event.kind;
    if (kind === "step_start") {
      beginStreamStep();
      return;
    }
    if (kind === "thinking") {
      const live = currentLiveMessage() || beginStreamStep();
      live.reasoning_content = `${live.reasoning_content || ""}${event.text || ""}`;
      scheduleStreamRender();
      return;
    }
    if (kind === "text") {
      const live = currentLiveMessage() || beginStreamStep();
      live.content = `${live.content || ""}${event.text || ""}`;
      els.typing.hidden = true;
      scheduleStreamRender();
      return;
    }
    if (kind === "tool_calls") {
      const live = currentLiveMessage() || beginStreamStep();
      live.tool_calls = (event.tool_calls || []).map((call) => ({
        id: call.id,
        name: call.name,
        arguments: call.arguments || {},
      }));
      scheduleStreamRender();
      return;
    }
    if (kind === "tool_start") {
      const call = event.tool_call || {};
      markToolCallStatus(call.id, "running");
      els.typingLabel.textContent = `正在执行 ${call.name || "工具"}…`;
      scheduleStreamRender();
      return;
    }
    if (kind === "tool_result") {
      const call = event.tool_call || {};
      markToolCallStatus(call.id, "done");
      state.messages.push({
        role: "tool",
        content: event.text || "",
        tool_call_id: call.id || null,
        name: call.name || null,
      });
      scheduleStreamRender();
      return;
    }
    if (kind === "done") {
      els.typing.hidden = true;
      return;
    }
    if (kind === "error") {
      if (
        state.liveMessage &&
        !state.liveMessage.content &&
        !state.liveMessage.reasoning_content &&
        !(state.liveMessage.tool_calls || []).length
      ) {
        state.messages = state.messages.filter(
          (message) => message !== state.liveMessage,
        );
      }
      state.liveMessage = null;
      scheduleStreamRender();
    }
  }

  function handleSocketMessage(event) {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch {
      return;
    }
    if (frame.type === "ready") {
      setSocketOnline(true, "实时连接");
      return;
    }
    if (frame.type === "status") {
      setBusy(true, frame.label || "Agent 正在思考…");
      return;
    }
    if (frame.type === "stream") {
      handleStreamEvent(frame.event || {});
      return;
    }
    if (frame.type === "progress") {
      applySnapshot(frame.session, { force: state.stickToBottom });
      const last = state.messages.filter((message) => message.role !== "system").at(-1);
      if (last?.role === "tool") setBusy(true, "工具执行完成，继续思考…");
      else if (last?.role === "assistant" && last.tool_calls?.length)
        setBusy(true, "正在调用工具…");
      return;
    }
    if (frame.type === "cancelled") {
      if (state.liveMessage?.live) state.liveMessage.live = false;
      state.liveMessage = null;
      setBusy(false);
      toast("已中断生成", "info");
      renderMessages({ scroll: true, force: state.stickToBottom });
      return;
    }
    if (frame.type === "reply") {
      setBusy(false);
      if (frame.success === false) {
        toast(frame.text || "Agent 处理失败", "error");
      } else {
        if (!state.liveMessage?.content) appendAssistantText(frame.text);
        toast(`已完成 · ${frame.steps || 0} 个模型步骤`, "success");
      }
      return;
    }
    if (frame.type === "session") {
      applySnapshot(frame.session, { force: state.stickToBottom });
      setBusy(false);
      void refreshSessions();
      return;
    }
    if (frame.type === "error") {
      setBusy(false);
      if (state.liveMessage?.live) state.liveMessage.live = false;
      state.liveMessage = null;
      const message = frame.message || "请求失败";
      const last = state.messages.at(-1);
      if (!(last?.role === "assistant" && last.content?.startsWith("⚠️"))) {
        state.messages.push({
          role: "assistant",
          content: `⚠️ ${message}`,
          tool_calls: [],
        });
        renderMessages({ scroll: true, force: true });
      }
      toast(message, "error");
    }
  }

  /* ------------------------------------------------------------------ */
  /* Theme / sidebar                                                    */
  /* ------------------------------------------------------------------ */
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("blue.theme", theme);
    } catch {
      /* ignore */
    }
  }

  function initTheme() {
    const saved = localStorage.getItem("blue.theme");
    const preferred = window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
    applyTheme(saved === "light" || saved === "dark" ? saved : preferred);
  }

  function closeSidebar() {
    els.body.classList.remove("sidebar-open");
  }

  function toggleSidebar() {
    els.body.classList.toggle("sidebar-open");
  }

  /* ------------------------------------------------------------------ */
  /* Events                                                             */
  /* ------------------------------------------------------------------ */
  function bindEvents() {
    els.newChat.addEventListener("click", () => newChat());
    els.refreshSessions.addEventListener("click", () => void refreshSessions(true));
    els.resetChat.addEventListener("click", async () => {
      if (state.busy) {
        toast("请等待当前回复完成", "info");
        return;
      }
      if (!state.sessionId) {
        newChat(false);
        return;
      }
      if (!window.confirm("清空当前会话的历史记录？")) return;
      try {
        await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`, { method: "DELETE" });
        toast("会话已清空", "success");
        newChat(false);
        await refreshSessions();
      } catch (error) {
        toast(`清空失败：${error.message}`, "error");
      }
    });

    els.themeToggle.addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
    });

    els.menuToggle.addEventListener("click", toggleSidebar);
    els.overlay.addEventListener("click", closeSidebar);

    els.composer.addEventListener("submit", (event) => {
      event.preventDefault();
      if (state.busy) {
        cancelPrompt();
        return;
      }
      void sendPrompt(els.prompt.value);
    });

    els.prompt.addEventListener("input", resizePrompt);
    if (els.attach && els.attachmentInput) {
      els.attach.addEventListener("click", () => els.attachmentInput.click());
      els.attachmentInput.addEventListener("change", () => {
        const files = Array.from(els.attachmentInput.files || []);
        els.attachmentInput.value = "";
        void addAttachments(files);
      });
    }
    els.prompt.addEventListener("paste", (event) => {
      const items = Array.from(event.clipboardData?.items || []);
      const files = items
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter((file) => file !== null);
      if (files.length) {
        event.preventDefault();
        void addAttachments(files);
      }
    });
    els.composer.addEventListener("dragover", (event) => {
      event.preventDefault();
    });
    els.composer.addEventListener("drop", (event) => {
      const files = Array.from(event.dataTransfer?.files || []);
      if (!files.length) return;
      event.preventDefault();
      void addAttachments(files);
    });
    els.prompt.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (state.busy) {
          cancelPrompt();
          return;
        }
        void sendPrompt(els.prompt.value);
      }
    });

    els.chatScroll.addEventListener("scroll", () => {
      state.stickToBottom = isSourceNearBottom();
    });

    document.querySelectorAll("[data-prompt]").forEach((button) => {
      button.addEventListener("click", () => {
        els.prompt.value = button.dataset.prompt || "";
        resizePrompt();
        els.prompt.focus();
      });
    });

    els.sessionList.addEventListener("click", (event) => {
      const deleteButton = event.target.closest("[data-delete]");
      if (deleteButton) {
        event.stopPropagation();
        void deleteSession(deleteButton.dataset.delete);
        return;
      }
      const item = event.target.closest("[data-session]");
      if (item) void loadSession(item.dataset.session);
    });

    els.sessionList.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const item = event.target.closest("[data-session]");
      if (!item) return;
      event.preventDefault();
      void loadSession(item.dataset.session);
    });

    els.messageList.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-copy]");
      if (!button) return;
      const code = button.closest(".code-block")?.querySelector("code")?.innerText || "";
      try {
        await navigator.clipboard.writeText(code);
        button.innerHTML = '<svg><use href="#i-check"></use></svg>已复制';
        window.setTimeout(() => {
          button.innerHTML = '<svg><use href="#i-copy"></use></svg>复制';
        }, 1400);
      } catch {
        toast("复制失败，请手动选择代码", "error");
      }
    });

    document.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        newChat(false);
      }
      if (event.key === "Escape") closeSidebar();
    });
  }

  async function deleteSession(sessionId) {
    if (!sessionId) return;
    if (state.busy) {
      toast("请等待当前回复完成", "info");
      return;
    }
    if (!window.confirm("删除这个会话及其历史记录？")) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      if (sessionId === state.sessionId) newChat(false);
      await refreshSessions();
      toast("会话已删除", "success");
    } catch (error) {
      toast(`删除失败：${error.message}`, "error");
    }
  }

  /* ------------------------------------------------------------------ */
  /* Bootstrap                                                          */
  /* ------------------------------------------------------------------ */
  async function bootstrap() {
    initTheme();
    configureMarkdown();
    bindEvents();
    resizePrompt();
    els.chatTitle.textContent = "新对话";
    renderSessions();
    renderTools();

    await Promise.allSettled([loadTools(), loadSystem(), refreshSessions()]);
    const saved = localStorage.getItem("blue.session");
    if (saved) {
      await loadSession(saved, { silent: true });
    }
    if (!state.sessionId) showWelcome(true);
    void ensureSocket().catch(() => {
      setSocketOnline(false, "离线");
    });
  }

  void bootstrap();
})();
