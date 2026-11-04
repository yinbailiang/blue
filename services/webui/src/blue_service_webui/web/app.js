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
    outbox: document.getElementById("outbox"),
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

  /*
   * 状态分两层：
   * - state 只放「全局」的东西：会话列表、socket、工具目录、主题…
   * - 每个会话一份 SessionState（消息、树、忙碌、草稿、队列、未读），存在
   *   state.store 里；没选中会话时用 state.draft 兜底（「新对话」草稿槽）。
   * 渲染统一读 cur()，所以切会话只是换一个 SessionState，不会互相覆盖。
   */
  const state = {
    sessionId: null,
    sessions: [],
    store: new Map(),
    draft: createSessionState(null),
    tools: [],
    activities: {},
    socket: null,
    socketPromise: null,
    stickToBottom: true,
    streamRenderTimer: null,
  };

  /** 一个会话的全部界面状态。 */
  function createSessionState(id) {
    return {
      id,
      title: "",
      loaded: false,
      messages: [],
      nodes: [],
      chain: [],
      busy: false,
      busyLabel: "",
      liveMessage: null,
      cancelled: false,
      /* 已收尾的 request_id：晚到的增量直接丢弃，避免快照之后又追加一遍 */
      lastRequestId: "",
      closedRequests: new Set(),
      outbox: [],
      pendingAttachments: [],
      unread: 0,
      sentAny: false,
      /* 虚拟滚动：key → 真实像素高度；renderFrom/renderTo 是当前挂载的区间 */
      heights: new Map(),
      renderFrom: 0,
      renderTo: 0,
    };
  }

  /** 取（必要时创建）某个会话的界面状态。 */
  function store(id) {
    let session = state.store.get(id);
    if (!session) {
      session = createSessionState(id);
      state.store.set(id, session);
    }
    return session;
  }

  /** 当前正在看的会话状态；没有选中会话时是「新对话」草稿槽。 */
  function cur() {
    return state.sessionId ? store(state.sessionId) : state.draft;
  }

  /** 标题最大长度（与 session 服务保持一致：28 字）。 */
  const TITLE_HINT = 28;

  /** 首条消息先本地占位标题/摘要，服务端快照到了会被覆盖。 */
  function sessionPreview(sessionId, text) {
    const flat = String(text || "").replace(/\s+/g, " ").trim();
    if (!flat) return;
    const meta = metaOf(sessionId);
    if (meta) {
      if (!meta.title) meta.title = flat.slice(0, TITLE_HINT);
      meta.preview = flat.slice(0, 120);
    }
    const session = store(sessionId);
    if (!session.title) session.title = flat.slice(0, TITLE_HINT);
    if (sessionId === state.sessionId) els.chatTitle.textContent = titleFor(sessionId);
  }

  /** 侧栏里某条会话的元信息（标题、摘要、排队数…）。 */
  function metaOf(id) {
    return state.sessions.find((item) => item.session_id === id) || null;
  }

  /*
   * 对话树：cur().nodes 是整棵树（含未选中的分支），cur().messages 是当前链。
   * 每条消息气泡底部的「分支切换条 + 编辑/重试/删除」都由节点 id 驱动。
   */
  function nodeFor(message) {
    if (!message || !message.id) return null;
    return cur().nodes.find((node) => node.id === message.id) || null;
  }

  function siblingsOf(node) {
    if (!node) return [];
    return cur().nodes.filter((item) => item.parent === node.parent).map((item) => item.id);
  }

  /** 消息脚注：父节点下有多个分支时给切换条，否则只给操作按钮。 */
  function createFoot(message) {
    // 工具响应是「助手某一步」的产物，不是独立消息：
    // 既没有分支可言，也不该被单独重试/删除（要重试就重试那条提问）
    if (message.role !== "user" && message.role !== "assistant") return null;
    const node = nodeFor(message);
    if (!node) return null;
    const foot = document.createElement("div");
    foot.className = "message-foot";
    const siblings = siblingsOf(node);
    if (siblings.length > 1) {
      const index = siblings.indexOf(node.id);
      const bar = document.createElement("div");
      bar.className = "branch-bar";
      bar.innerHTML =
        '<button type="button" data-act="branch-prev" title="上一个分支">‹</button>' +
        `<span class="branch-count">${index + 1} / ${siblings.length}</span>` +
        '<button type="button" data-act="branch-next" title="下一个分支">›</button>';
      foot.appendChild(bar);
    }
    const acts = document.createElement("div");
    acts.className = "message-acts";
    const addAction = (act, label, title) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.act = act;
      button.title = title;
      button.textContent = label;
      acts.appendChild(button);
    };
    if (message.role === "user") {
      addAction("edit", "编辑", "改这条消息，生成一条新分支");
    }
    addAction("retry", "重试", "在这条消息下重新生成一条回复分支");
    addAction("delete", "删除分支", "删除这条消息及其后续");
    foot.appendChild(acts);
    return foot;
  }

  const MAX_ATTACHMENTS = 4;
  const MAX_ATTACHMENT_EDGE = 1568;
  const ATTACHMENT_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];

  /*
   * 增量渲染缓存：cur().messages 的每一项对应一个已挂载节点。
   * 流式增量只 patch 变化的节点，避免整表 innerHTML 重建导致
   * 滚动抖动、折叠状态丢失与复制按钮复位。
   */
  /*
   * 渲染缓存：消息 key → DOM 节点。服务端消息用树节点 id 做 key，本地乐观/流式
   * 消息用临时 key。这样「快照替换对象」「窗口滑动」「增量追加」都只是复用 / 新增 /
   * 移除个别节点，不会整屏重建——整屏重建会让所有气泡重放入场动画（肉眼=闪烁）。
   */
  const view = {
    byKey: new Map(),
    signatures: new Map(),
    parts: new WeakMap(),
  };

  let localKeySeq = 0;

  /** 消息的稳定 key：有树节点 id 用 id，没有就发一个本地 key。 */
  function messageKey(message) {
    const id = message.id;
    if (id !== undefined && id !== null && id !== "") return `n${id}`;
    if (!message.__key) {
      localKeySeq += 1;
      message.__key = `l${localKeySeq}`;
    }
    return message.__key;
  }

  /** 树结构（分支数、当前分支）变了要重画气泡脚注：复用节点后不会再整屏重建。 */
  function refreshMessageFeet() {
    for (const node of view.byKey.values()) {
      const message = node.__message;
      const main = node.querySelector(".message-main");
      if (!message || !main) continue;
      main.querySelector(".message-foot")?.remove();
      const foot = createFoot(message);
      if (foot) main.appendChild(foot);
    }
  }

  function resetView() {
    view.byKey.clear();
    view.signatures.clear();
    els.messageList.replaceChildren();
    // 虚拟滚动的留白属于「上一屏」，换会话/清空时必须归零
    els.messageList.style.paddingTop = "";
    els.messageList.style.paddingBottom = "";
  }

  /** 只移动位置不对的节点；已经在位的跳过，避免无谓的 DOM 抖动与动画重启。 */
  function placeInOrder(nodes) {
    let cursor = els.messageList.firstElementChild;
    for (const node of nodes) {
      if (node === cursor) {
        cursor = cursor.nextElementSibling;
        continue;
      }
      els.messageList.insertBefore(node, cursor || null);
    }
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

  /*
   * 会话标题由 session 服务维护（自动取首条用户消息，也可手改），
   * 浏览器只负责显示：换设备、清缓存都不会丢。
   */
  function titleFor(id) {
    const meta = metaOf(id);
    if (meta?.title) return meta.title;
    const cached = state.store.get(id);
    if (cached?.title) return cached.title;
    return `会话 ${shortId(id)}`;
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

  function activityLabel(activity) {
    const phase = activity?.phase || "";
    if (phase === "tool") return "执行工具中";
    if (phase === "streaming") return "生成中";
    if (phase === "thinking") return "思考中";
    return "运行中";
  }

  function sessionRunning(session) {
    if (!session) return false;
    return Boolean(session.running) || Boolean(state.activities[session.session_id]?.running);
  }

  function sendFrame(frame) {
    const socket = state.socket;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(frame));
      return true;
    }
    return false;
  }

  /*
   * 「接续」：另一个页面/设备（或刷新后的本页）订阅某个会话的实时流。
   * 不发起新一轮 prompt，只把正在生成的内容接上来。
   */
  /*
   * 多会话并行：同时订阅「当前会话 + 所有正在跑的会话」的实时流。
   * 后端一个连接本来就支持多个 watch，这里只是把前端该订的都订上。
   */
  const watchers = new Set();

  function ensureWatch(sessionId) {
    if (!sessionId || watchers.has(sessionId)) return;
    watchers.add(sessionId);
    sendFrame({ type: "watch", session_id: sessionId });
  }

  function dropWatch(sessionId) {
    if (!watchers.delete(sessionId)) return;
    sendFrame({ type: "unwatch", session_id: sessionId });
  }

  /** 校准订阅集合：当前会话 + 所有忙碌会话。 */
  function syncWatch() {
    const wanted = new Set();
    if (state.sessionId) wanted.add(state.sessionId);
    for (const [id, session] of state.store) {
      if (session.busy) wanted.add(id);
    }
    for (const id of [...watchers]) {
      if (!wanted.has(id)) dropWatch(id);
    }
    for (const id of wanted) ensureWatch(id);
  }

  /** 只刷新「当前会话」相关的忙碌 UI：按钮、输入态、提示条。 */
  /**
   * 思考药丸只在「会话忙、但还没有可见气泡」时出现。
   * 一旦 live 气泡建起来，气泡本身就是进度指示器；两个同时显示就成了「双消息」。
   */
  function refreshTyping() {
    const session = cur();
    const live = Boolean(
      session.liveMessage && session.messages.includes(session.liveMessage),
    );
    els.typing.hidden = !(session.busy && session.id !== null && !live);
    els.typingLabel.textContent = session.busyLabel || "Agent 正在思考…";
  }

  function applyBusyChrome(busy, label) {
    cur().busyLabel = label;
    refreshTyping();
    els.send.classList.toggle("loading", busy && Boolean(state.sessionId));
    els.sendIcon.setAttribute("href", busy && state.sessionId ? "#i-target" : "#i-send");
    els.sendLabel.textContent = busy && state.sessionId ? "停止" : "发送";
    els.send.setAttribute(
      "aria-label",
      busy && state.sessionId ? "停止生成" : "发送消息",
    );
    updateSendState();
  }

  /** 设置某个会话的忙碌状态；只有它是当前会话时才动界面。 */
  function setSessionBusy(session, busy, label = "Agent 正在思考…") {
    session.busy = busy;
    if (session === cur()) applyBusyChrome(busy, label);
    syncWatch();
  }

  function setBusy(busy, label = "Agent 正在思考…") {
    setSessionBusy(cur(), busy, label);
  }

  function updateSendState() {
    // 生成中按钮用于中断，此时即使输入框为空也应可点
    const hasContent = Boolean(els.prompt.value.trim()) || cur().pendingAttachments.length > 0;
    els.send.disabled = cur().busy ? false : !hasContent;
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
  /*
   * 侧栏一项：标题（服务端，双击可改）、摘要、消息数/时间、运行状态、
   * 排队角标、未读点。样式与 b64/chat 的会话列表保持一致。
   */
  function sessionItem(session, active) {
    const id = session.session_id;
    const count = Number(session.message_count || 0);
    const running = sessionRunning(session);
    const queued = Number(session.queued || 0);
    const unread = state.store.get(id)?.unread || 0;
    const meta = running
      ? `<span class="session-running"><span class="session-running-dot"></span>${escapeHtml(
          activityLabel(state.activities[id] || session),
        )}</span>`
      : "";
    const queueBadge = queued
      ? `<span class="session-queue" title="${queued} 条待发送">⬆ ${queued}</span>`
      : "";
    const preview = String(session.preview || "").trim();
    const subline = preview
      ? escapeHtml(preview)
      : `${count} 条消息 · ${relativeTime(session.updated_at)}`;
    return `<div class="session-item ${active ? "active" : ""} ${
      running ? "is-running" : ""
    } ${unread ? "is-unread" : ""}" data-session="${escapeHtml(
      id,
    )}" role="button" tabindex="0" aria-current="${active ? "page" : "false"}">
      <span class="session-avatar">${escapeHtml(initialsFor(id))}</span>
      <span class="session-copy">
        <span class="session-name" title="双击重命名">${escapeHtml(titleFor(id))}</span>
        <span class="session-meta">${subline}${meta}${queueBadge}${
          unread ? `<span class="session-unread-dot" title="有新内容"></span>` : ""
        }</span>
      </span>
      <button class="session-delete" type="button" data-delete="${escapeHtml(
        id,
      )}" aria-label="删除会话"><svg><use href="#i-trash"></use></svg></button>
    </div>`;
  }

  function renderSessions() {
    const items = state.sessions
      .slice()
      .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    const hasActive =
      state.sessionId && items.some((item) => item.session_id === state.sessionId);
    let html = "";
    if (state.sessionId && !hasActive) {
      // 刚发出第一条消息、列表还没刷到时先占个位
      html += sessionItem(
        {
          session_id: state.sessionId,
          message_count: cur().messages.length,
          updated_at: Date.now() / 1000,
          title: cur().title,
        },
        true,
      );
    }
    html += items
      .map((item) => sessionItem(item, item.session_id === state.sessionId))
      .join("");
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

  /** 折叠态的摘要：只留一行、够短，完整内容在展开后的 body 里。 */
  function toolBrief(text) {
    const line = String(text || "")
      .split("\n")
      .find((item) => item.trim());
    const flat = (line || "").trim().replace(/\s+/g, " ");
    if (!flat) return "（无输出）";
    return flat.length > 44 ? `${flat.slice(0, 44)}…` : flat;
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
    wrapper.dataset.node = message.id ? String(message.id) : "";
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
    const foot = createFoot(message);
    if (foot) wrapper.querySelector(".message-main").appendChild(foot);
    return wrapper;
  }

  function imageLink(url, className, alt) {
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.className = "image-link";
    const image = document.createElement("img");
    image.className = className;
    image.src = url;
    image.alt = alt;
    image.loading = "lazy";
    link.appendChild(image);
    return link;
  }

  function updateToolNode(parts, message, name) {
    parts.label.textContent = `TOOL · ${name}`;
    parts.name.textContent = name;
    const text = String(message.content || "");
    const images = (message.parts || []).filter(
      (part) => part.type === "image" && part.url,
    );
    const signature = text + "|" + images.map((part) => part.url).join(",");
    if (signature === parts.signature) return;
    parts.signature = signature;
    parts.text = text;
    parts.body.textContent = text || "（无输出）";
    parts.brief.textContent = toolBrief(text);
    parts.thumbs.replaceChildren();
    parts.images.replaceChildren();
    for (const part of images) {
      parts.thumbs.appendChild(imageLink(part.url, "tool-thumb", "工具返回的图片"));
      parts.images.appendChild(imageLink(part.url, "bubble-image", "工具返回的图片"));
    }
    // 一律默认折叠：内容多的时候整屏都是工具输出，展开与否交给用户点
    if (parts.details) parts.details.open = false;
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
            <span class="tool-result-thumbs"></span>
            ${CHEVRON}
          </summary>
          <div class="tool-body">
            <pre></pre>
            <div class="tool-images"></div>
          </div>
        </details>
      </div>`;
    const parts = {
      label: wrapper.querySelector(".message-label"),
      name: wrapper.querySelector(".tool-result-name"),
      brief: wrapper.querySelector(".tool-result-brief"),
      body: wrapper.querySelector(".tool-result pre"),
      thumbs: wrapper.querySelector(".tool-result-thumbs"),
      images: wrapper.querySelector(".tool-images"),
      details: wrapper.querySelector(".tool-result"),
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
      parts.content.appendChild(imageLink(part.url, "bubble-image", "附件图片"));
    }
  }

  function createUserNode(message) {
    const wrapper = document.createElement("div");
    wrapper.className = "message message-user";
    wrapper.dataset.node = message.id ? String(message.id) : "";
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
    const foot = createFoot(message);
    if (foot) wrapper.querySelector(".message-main").appendChild(foot);
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


  /** 一次渲染的尾部条数；更早的消息由「加载更早」按需追加。 */
  /*
   * 虚拟滚动：整段历史用「估算高度」撑出滚动条，只把视口附近的消息挂到 DOM。
   * 实测高度回填到 session.heights，估算值很快收敛成真值。
   */
  const VIRTUAL = {
    estimate: 96,
    overscan: 900,
    minItems: 20,
    maxItems: 200,
  };

  /** 参与渲染的消息（system 不进正文）。 */
  function visibleMessages() {
    return cur().messages.filter((message) => message.role !== "system");
  }

  /** 每条消息用的高度：有实测用实测，没有就先用估算。 */
  function messageHeights(all) {
    const list = [];
    for (const message of all) {
      list.push(cur().heights.get(messageKey(message)) || VIRTUAL.estimate);
    }
    return list;
  }

  /** 前缀和：offsets[i] 是第 i 条消息的顶部位置。 */
  function offsetsOf(heights) {
    const offsets = [0];
    for (let index = 0; index < heights.length; index += 1) {
      offsets.push(offsets[index] + heights[index]);
    }
    return offsets;
  }

  /** 由当前滚动位置算出要挂载的区间。 */
  function windowFor(offsets, total, scrollTop, viewport) {
    const count = offsets.length - 1;
    if (count <= VIRTUAL.maxItems) return { start: 0, end: count, total };
    const top = Math.max(0, scrollTop - VIRTUAL.overscan);
    const bottom = Math.min(total, scrollTop + viewport + VIRTUAL.overscan);
    let start = 0;
    while (start < count - 1 && offsets[start + 1] <= top) start += 1;
    let end = start;
    while (end < count && offsets[end] < bottom) end += 1;
    if (end - start < VIRTUAL.minItems) {
      end = Math.min(count, start + VIRTUAL.minItems);
    }
    if (end - start > VIRTUAL.maxItems) end = start + VIRTUAL.maxItems;
    return { start, end, total };
  }

  /** 读回挂载节点的真实高度，返回是否有变化。 */
  function measureHeights(messages) {
    let changed = false;
    const session = cur();
    for (const message of messages) {
      const key = messageKey(message);
      const node = view.byKey.get(key);
      if (!node) continue;
      const height = Math.round(node.getBoundingClientRect().height);
      if (!height) continue;
      if (Math.abs((session.heights.get(key) || 0) - height) > 1) {
        session.heights.set(key, height);
        changed = true;
      }
    }
    return changed;
  }

  /** 留白：把未挂载部分的高度补成 padding，滚动条才能代表整段历史。 */
  function applyPadding(offsets, start, end) {
    const list = els.messageList;
    const previous = Number.parseFloat(list.style.paddingTop || "0") || 0;
    const top = start <= 0 ? 0 : offsets[start];
    const total = offsets[offsets.length - 1];
    list.style.paddingTop = `${top}px`;
    list.style.paddingBottom = `${Math.max(0, total - offsets[end])}px`;
    return { previous, top };
  }

  /**
   * 上侧留白变了，内容会整体位移：小跨度（窗口顺带滑动一页）要补回 scrollTop，
   * 画面才不抖；大跨度是用户主动跳转（拖滚动条/跳到顶），补了反而跳回去。
   */
  function compensateScroll(delta, options) {
    if (options.scroll || Math.abs(delta) < 0.5) return;
    const viewport = els.chatScroll.clientHeight || 0;
    if (Math.abs(delta) > Math.max(viewport * 1.2, 240)) return;
    els.chatScroll.scrollTop += delta;
  }

  function syncMessages(options = {}) {
    const all = visibleMessages();
    if (!all.length) {
      resetView();
      showWelcome(true);
      return;
    }
    showWelcome(false);
    els.messageList.hidden = false;

    const session = cur();
    const scroller = els.chatScroll;
    // 贴底意图要在改留白之前判断：改完 scrollHeight 会变，判断就不准了
    const wasAtBottom =
      scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 48;
    const offsets = offsetsOf(messageHeights(all));
    const total = offsets[offsets.length - 1];
    const win = windowFor(offsets, total, scroller.scrollTop, scroller.clientHeight);
    session.renderFrom = win.start;
    session.renderTo = win.end;
    const messages = all.slice(win.start, win.end);

    const names = toolNameIndex(messages);
    const wanted = [];
    const keep = new Set();
    for (const message of messages) {
      const key = messageKey(message);
      keep.add(key);
      let node = view.byKey.get(key);
      if (!node) {
        node = createMessageNode(message, names);
        node.__message = message;
        view.byKey.set(key, node);
        view.signatures.set(node, messageSignature(message));
      } else {
        node.__message = message;
        const signature = messageSignature(message);
        if (view.signatures.get(node) !== signature) {
          view.signatures.set(node, signature);
          updateMessageNode(node, message, names);
        }
      }
      wanted.push(node);
    }

    for (const [key, node] of [...view.byKey]) {
      if (keep.has(key)) continue;
      node.remove();
      view.signatures.delete(node);
      view.byKey.delete(key);
    }
    placeInOrder(wanted);

    const pad = applyPadding(offsets, win.start, win.end);
    compensateScroll(pad.top - pad.previous, options);

    // 估算收敛成实测高度后再修一次留白
    if (measureHeights(messages)) {
      const before = Number.parseFloat(els.messageList.style.paddingTop || "0") || 0;
      const after = applyPadding(offsetsOf(messageHeights(all)), win.start, win.end);
      compensateScroll(after.top - before, options);
    }

    refreshTyping();
    if (options.scroll || wasAtBottom) {
      // 原本在底部（或明确要求）就重新贴底，别被估算误差留在半空
      state.stickToBottom = true;
      requestAnimationFrame(() => {
        scroller.scrollTop = scroller.scrollHeight;
      });
    }
  }

  /** 滚动到新位置后若挂载区间该变，重排一次（rAF 节流）。 */
  let scrollFrame = 0;

  function scheduleWindowSync() {
    if (scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = 0;
      const all = visibleMessages();
      if (!all.length) return;
      const session = cur();
      const offsets = offsetsOf(messageHeights(all));
      const total = offsets[offsets.length - 1];
      const scrollTop = els.chatScroll.scrollTop;
      const win = windowFor(offsets, total, scrollTop, els.chatScroll.clientHeight);
      if (win.start === session.renderFrom && win.end === session.renderTo) {
        if (measureHeights(all.slice(win.start, win.end))) syncMessages();
        return;
      }
      syncMessages();
    });
  }

  function renderMessages(options = {}) {
    syncMessages(options);
  }

  /*
   * 快照落到「它自己那个会话」的 store 里。后台会话的快照只更新缓存，
   * 不抢当前视图——这正是多会话并行不串台的关键。
   */
  /**
   * 快照替换消息数组时，把「本地乐观/流式气泡」的 DOM 节点过户给对应的服务端节点。
   *
   * 本地消息用临时 key，服务端消息用节点 id；不过户的话这一对会被判成「删旧建新」，
   * 气泡会重新播放入场动画——用户看到的就是每轮结束闪一下。
   */
  function adoptProvisionalNodes(session, nextMessages) {
    const locals = session.messages.filter(
      (message) => !message.id || message.id === "",
    );
    if (!locals.length) return;
    const used = new Set();
    for (const local of locals) {
      const localKey = messageKey(local);
      const node = view.byKey.get(localKey);
      if (!node) continue;
      const text = String(local.content || "");
      const target = nextMessages.find((message) => {
        if (!message.id || used.has(message.id)) return false;
        if (message.role !== local.role) return false;
        const other = String(message.content || "");
        if (!text) return other === "";
        return other === text || other.startsWith(text.slice(0, 24));
      });
      if (!target) continue;
      used.add(target.id);
      view.byKey.delete(localKey);
      view.byKey.set(messageKey(target), node);
      const signature = messageSignature(target);
      view.signatures.set(node, signature);
      node.__message = target;
    }
  }

  function applySnapshot(snapshot, options = {}) {
    if (!snapshot) return;
    const session = store(snapshot.session_id);
    session.title = String(snapshot.title || session.title || "");
    const nextMessages = Array.isArray(snapshot.messages) ? snapshot.messages : [];
    if (state.sessionId === snapshot.session_id) {
      adoptProvisionalNodes(session, nextMessages);
    }
    session.messages = nextMessages;
    session.nodes = Array.isArray(snapshot.nodes) ? snapshot.nodes : [];
    session.chain = Array.isArray(snapshot.current) ? snapshot.current : [];
    session.liveMessage = null;
    session.loaded = true;
    session.renderFrom = 0;
    session.renderTo = 0;

    const existing = metaOf(snapshot.session_id);
    const next = {
      session_id: snapshot.session_id,
      message_count: snapshot.message_count,
      updated_at: snapshot.updated_at,
      title: snapshot.title || existing?.title || "",
      preview: existing?.preview || "",
      queued: existing?.queued || 0,
      running: existing?.running || false,
      phase: existing?.phase || "",
      step: existing?.step || 0,
    };
    if (existing) Object.assign(existing, next);
    else state.sessions.push(next);

    if (state.sessionId === snapshot.session_id) {
      els.chatTitle.textContent = titleFor(snapshot.session_id);
      renderMessages({ scroll: true, force: options.force === true });
      refreshMessageFeet();
    }
    renderSessions();
  }

  function appendAssistantText(text) {
    if (!text) return;
    const last = cur().messages[cur().messages.length - 1];
    if (last?.role === "assistant" && last.content === text) return;
    cur().messages.push({ role: "assistant", content: text, tool_calls: [] });
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
      state.activities = {};
      for (const item of state.sessions) {
        if (item.running) state.activities[item.session_id] = item;
      }
      renderSessions();
    } catch (error) {
      if (showError) toast(`会话列表：${error.message}`, "error");
    }
  }

  /** 切到某个会话：先渲染缓存（立刻有内容），再后台刷新快照。 */
  async function loadSession(sessionId, options = {}) {
    if (!sessionId) return;
    switchSession(sessionId);
    closeSidebar();
    if (options.refresh === false) return;
    try {
      const snapshot = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
      applySnapshot(snapshot, { force: true });
      void refreshOutbox(sessionId);
    } catch (error) {
      if (options.silent) {
        localStorage.removeItem("blue.session");
        return;
      }
      toast(`读取会话失败：${error.message}`, "error");
    }
  }

  /** 切换视图到某个会话（纯界面动作，不等待网络）。 */
  function switchSession(sessionId) {
    // 草稿跟着会话走：切走时存下输入框，切回来再放回去
    if (state.sessionId && state.sessionId !== sessionId) {
      store(state.sessionId).draftText = els.prompt.value;
    }
    state.sessionId = sessionId;
    const session = store(sessionId);
    els.prompt.value = session.draftText || "";
    resizePrompt();
    session.unread = 0;
    state.stickToBottom = true;
    try {
      localStorage.setItem("blue.session", sessionId);
    } catch {
      /* ignore */
    }
    els.chatTitle.textContent = titleFor(sessionId);
    showWelcome(session.messages.length === 0 && !session.loaded ? true : false);
    if (!session.loaded) {
      // 还没加载过：先清空视图，避免残留上一个会话的内容
      session.messages = [];
      session.nodes = [];
      session.chain = [];
    }
    // 视图缓存是按当前会话建的，切会话一律重建
    resetView();
    renderMessages({ scroll: true, force: true });
    renderOutbox();
    applyBusyChrome(session.busy, session.busy ? "Agent 正在思考…" : "");
    renderSessions();
    syncWatch();
    if (session.loaded) void refreshOutbox();
  }

  /** 新建对话：切到草稿槽；其它会话的生成照常进行。 */
  function newChat(close = true) {
    state.sessionId = null;
    const draft = state.draft;
    draft.messages = [];
    draft.nodes = [];
    draft.chain = [];
    draft.liveMessage = null;
    draft.busy = false;
    draft.loaded = false;
    draft.outbox = [];
    state.stickToBottom = true;
    try {
      localStorage.removeItem("blue.session");
    } catch {
      /* ignore */
    }
    els.chatTitle.textContent = "新对话";
    resetView();
    showWelcome(true);
    applyBusyChrome(false, "");
    renderOutbox();
    renderSessions();
    syncWatch();
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
      if (cur().pendingAttachments.length >= MAX_ATTACHMENTS) {
        toast(`一次最多附带 ${MAX_ATTACHMENTS} 张图片`, "info");
        break;
      }
      try {
        cur().pendingAttachments.push(await prepareAttachment(file));
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
    els.attachments.hidden = cur().pendingAttachments.length === 0;
    cur().pendingAttachments.forEach((attachment, index) => {
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
        cur().pendingAttachments.splice(index, 1);
        renderPendingAttachments();
        updateSendState();
      });
      chip.append(image, remove);
      els.attachments.appendChild(chip);
    });
  }

  async function sendViaRest(
    sessionId,
    text,
    attachments = cur().pendingAttachments,
    options = {},
  ) {
    const reply = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        text,
        attachments,
        parent: options.parent ?? null,
        regenerate: options.regenerate === true,
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

  /*
   * 发一轮对话。options.parent 指定新消息挂在谁下面（开新分支），
   * options.regenerate 表示只重新生成回复（重试某条消息）。
   */
  async function sendPrompt(rawText, options = {}) {
    // 会话正忙：这一轮不会立刻开始，先按「排队」处理（消息进后端发件箱）
    const regenerate = options.regenerate === true;
    const parent = options.parent ?? null;
    const text = String(rawText || "").trim();
    // 编辑可以带上它自己的附件；不传就用输入框里攒的
    const attachments = regenerate
      ? []
      : (options.attachments ?? cur().pendingAttachments).slice();
    if (!regenerate && !text && !attachments.length) return;
    if (regenerate && !state.sessionId) return;
    const sessionId = state.sessionId || uuid();
    if (!state.sessionId) {
      state.sessionId = sessionId;
      try {
        localStorage.setItem("blue.session", sessionId);
      } catch {
        /* ignore */
      }
    }
    if (!regenerate) {
      // 新对话落成正式会话槽：草稿里攒的内容搬过去
      if (!state.sessionId) {
        const draft = state.draft;
        const session = store(sessionId);
        session.messages = draft.messages;
        session.loaded = true;
        state.sessionId = sessionId;
        draft.messages = [];
        state.draft = createSessionState(null);
        try {
          localStorage.setItem("blue.session", sessionId);
        } catch {
          /* ignore */
        }
      }
      const meta = metaOf(sessionId);
      if (meta && !meta.title) meta.title = text.slice(0, TITLE_HINT);
      sessionPreview(sessionId, text);
    }
    cur().liveMessage = null;
    if (!regenerate) {
      const localParts = [];
      if (text) localParts.push({ type: "text", text });
      attachments.forEach((attachment) => {
        localParts.push({
          type: "image",
          url: `data:${attachment.mime};base64,${attachment.data}`,
        });
      });
      cur().messages.push({
        role: "user",
        content: text,
        parts: localParts,
        tool_calls: [],
      });
    }
    if (!options.attachments) {
      cur().pendingAttachments = [];
      renderPendingAttachments();
    }
    showWelcome(false);
    renderMessages({ scroll: true, force: true });
    renderSessions();
    cur().cancelled = false;
    setBusy(true, regenerate ? "正在生成新分支…" : "Agent 正在思考…");
    cur().sentAny = true;
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
          parent,
          regenerate,
        }),
      );
    } catch {
      try {
        await sendViaRest(sessionId, text, attachments, { parent, regenerate });
      } catch (error) {
        setBusy(false);
        cur().messages.push({
          role: "assistant",
          content: `⚠️ ${error.message || "发送失败"}`,
          tool_calls: [],
        });
        renderMessages({ scroll: true, force: true });
        toast(error.message || "发送失败", "error");
      }
    }
  }

  /* ------------------------------------------------------------------ */
  /* 对话树操作                                                          */
  /* ------------------------------------------------------------------ */
  /** 切换到同父的相邻分支；只改后端指针，历史一条不丢。 */
  async function switchBranch(node, step) {
    if (cur().busy) {
      toast("请等待当前回复完成", "info");
      return;
    }
    const siblings = siblingsOf(node);
    if (siblings.length < 2) return;
    const index = siblings.indexOf(node.id);
    const target = siblings[(index + step + siblings.length) % siblings.length];
    if (target === node.id) return;
    try {
      const view_ = await api(
        `/api/sessions/${encodeURIComponent(state.sessionId)}/branches/${target}/activate`,
        { method: "POST" },
      );
      applySnapshot(view_, { force: false });
      void refreshSessions();
    } catch (error) {
      toast(`切换分支失败：${error.message}`, "error");
    }
  }

  /** 重试：再生成一条回复分支，旧回复原样保留。 */
  function retryNode(node) {
    if (cur().busy) {
      toast("请等待当前回复完成", "info");
      return;
    }
    // 用户消息：新回复挂在它下面；助手消息：新回复与它同级（回到父节点）
    const role = node.message?.role || "";
    const parent = role === "user" ? node.id : node.parent;
    void sendPrompt("", { parent, regenerate: true });
  }

  /** 删除一条分支：该消息与其后续一起消失，界面上立刻重渲染。 */
  async function deleteBranch(node) {
    if (cur().busy) {
      toast("请等待当前回复完成", "info");
      return;
    }
    if (!window.confirm("删除这条消息及其后续内容？")) return;
    try {
      const view_ = await api(
        `/api/sessions/${encodeURIComponent(state.sessionId)}/branches/${node.id}`,
        { method: "DELETE" },
      );
      applySnapshot(view_, { force: false });
      void refreshSessions();
      toast("分支已删除", "success");
    } catch (error) {
      toast(`删除失败：${error.message}`, "error");
    }
  }

  /** 内联编辑用户消息：保存时在同一个父节点下开新分支，再生成新回复。 */
  /** textarea 跟着内容长高（上限 55vh，再超出就内部滚动）。 */
  function autoGrowTextarea(area) {
    const max = Math.round(window.innerHeight * 0.55);
    area.style.height = "auto";
    area.style.height = `${Math.min(area.scrollHeight, max)}px`;
    area.style.overflowY = area.scrollHeight > max ? "auto" : "hidden";
  }

  /**
   * 把消息里的图片转回「可再次提交」的附件。
   * 历史消息里的图是 `/api/attachments/<hash>`，编辑后开新分支要重新上传，
   * 所以这里读回 base64。
   */
  async function attachmentFromUrl(url, name) {
    if (!url) return null;
    try {
      let mime = "image/png";
      let data = "";
      if (url.startsWith("data:")) {
        const [head, payload] = url.split(",");
        mime = head.slice(5).split(";")[0] || mime;
        data = payload || "";
      } else {
        const response = await fetch(url);
        if (!response.ok) return null;
        const blob = await response.blob();
        mime = blob.type || mime;
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result || ""));
          reader.onerror = () => reject(new Error("读取附件失败"));
          reader.readAsDataURL(blob);
        });
        data = dataUrl.split(",")[1] || "";
      }
      if (!data) return null;
      return { mime, data, name: name || null };
    } catch {
      return null;
    }
  }

  async function beginEdit(host, node) {
    if (cur().busy) {
      toast("请等待当前回复完成", "info");
      return;
    }
    if (host.querySelector(".editor")) return;
    const message = cur().messages.find((item) => item.id === node.id);
    const bubble = host.querySelector(".bubble");
    if (!message || !bubble) return;

    const editor = document.createElement("div");
    editor.className = "editor";
    editor.__attachments = [];

    const area = document.createElement("textarea");
    area.value = String(message.content || "");
    area.rows = 1;
    area.placeholder = "编辑这条消息…";
    area.addEventListener("input", () => autoGrowTextarea(area));

    const attachmentsRow = document.createElement("div");
    attachmentsRow.className = "editor-attachments";
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = ATTACHMENT_TYPES.join(",");
    fileInput.multiple = true;
    fileInput.hidden = true;
    const addButton = document.createElement("button");
    addButton.type = "button";
    addButton.className = "editor-add";
    addButton.textContent = "＋ 图片";
    addButton.title = `最多 ${MAX_ATTACHMENTS} 张`;

    const renderAttachments = () => {
      attachmentsRow.textContent = "";
      editor.__attachments.forEach((attachment, index) => {
        const chip = document.createElement("div");
        chip.className = "attachment-chip";
        const image = document.createElement("img");
        image.src = `data:${attachment.mime};base64,${attachment.data}`;
        image.alt = attachment.name || "附件图片";
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "attachment-remove";
        remove.textContent = "×";
        remove.setAttribute("aria-label", "移除图片");
        remove.addEventListener("click", () => {
          editor.__attachments.splice(index, 1);
          renderAttachments();
        });
        chip.append(image, remove);
        attachmentsRow.appendChild(chip);
      });
      attachmentsRow.appendChild(addButton);
    };

    addButton.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", async () => {
      const files = Array.from(fileInput.files || []);
      for (const file of files) {
        if (editor.__attachments.length >= MAX_ATTACHMENTS) {
          toast(`最多 ${MAX_ATTACHMENTS} 张图片`, "info");
          break;
        }
        try {
          editor.__attachments.push(await prepareAttachment(file));
        } catch (error) {
          toast(String(error.message || error), "error");
        }
      }
      fileInput.value = "";
      renderAttachments();
    });

    const actions = document.createElement("div");
    actions.className = "editor-actions";
    const hint = document.createElement("span");
    hint.className = "editor-hint";
    hint.textContent = "Ctrl/⌘+Enter 保存 · Esc 取消";
    const save = document.createElement("button");
    save.type = "button";
    save.dataset.act = "save-edit";
    save.textContent = "保存并生成新分支";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.dataset.act = "cancel-edit";
    cancel.textContent = "取消";
    actions.append(hint, save, cancel);

    editor.append(area, attachmentsRow, fileInput, actions);
    host.classList.add("editing");
    bubble.hidden = true;
    bubble.after(editor);
    renderAttachments();
    autoGrowTextarea(area);
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);
    area.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        save.click();
      } else if (event.key === "Escape") {
        event.preventDefault();
        cancel.click();
      }
    });

    // 原消息带的图片搬进编辑器（读回 base64，新分支才能重新上传）
    const images = (message.parts || []).filter(
      (part) => part.type === "image" && part.url,
    );
    if (images.length) {
      const restored = await Promise.all(
        images.map((part) => attachmentFromUrl(part.url, "原附件")),
      );
      for (const attachment of restored) {
        if (!attachment) continue;
        if (editor.__attachments.length >= MAX_ATTACHMENTS) break;
        editor.__attachments.push(attachment);
      }
      renderAttachments();
      autoGrowTextarea(area);
    }
  }

  function endEdit(host) {
    const editor = host?.querySelector(".editor");
    if (!editor) return;
    editor.remove();
    host.classList.remove("editing");
    const bubble = host.querySelector(".bubble");
    if (bubble) bubble.hidden = false;
  }

  /** 把某个会话的队列整体替换掉（REST 拉取或 session.outbox 推送都用它）。 */
  function applyOutbox(sessionId, messages) {
    if (!sessionId) return;
    const session = store(sessionId);
    session.outbox = Array.isArray(messages) ? messages : [];
    const meta = metaOf(sessionId);
    if (meta) meta.queued = session.outbox.length;
    if (session === cur()) renderOutbox();
    renderSessions();
  }

  /*
   * 发件箱：和输入框同宽同风格，放在它正上方。
   * 每条显示状态（待发/发送中/失败原因），可立刻发送、重试、取消，点正文可改。
   */
  function outboxStateLabel(item) {
    if (item.state === "sending") return "发送中";
    if (item.state === "failed") return "失败";
    return "待发送";
  }

  function renderOutbox() {
    if (!els.outbox) return;
    const items = cur().outbox;
    els.outbox.hidden = items.length === 0;
    els.outbox.innerHTML = "";
    if (!items.length) return;

    const busyCount = items.filter((item) => item.state === "sending").length;
    const head = document.createElement("div");
    head.className = "outbox-head";
    head.innerHTML = `
      <span class="outbox-title">发件箱 · ${items.length} 条${
        busyCount ? `（${busyCount} 条发送中）` : ""
      }</span>
      <span class="outbox-actions">
        <button type="button" data-act="outbox-send-now" title="把排在最前面的那条立刻发出去">立即发送</button>
        <button type="button" data-act="outbox-clear" title="取消全部待发送（发送中的保留）">全部取消</button>
      </span>`;
    els.outbox.appendChild(head);

    const list = document.createElement("ol");
    list.className = "outbox-list";
    items.forEach((item, index) => {
      const row = document.createElement("li");
      row.className = "outbox-item";
      row.dataset.queue = item.id;
      row.dataset.state = item.state || "pending";

      const number = document.createElement("span");
      number.className = "outbox-index";
      number.textContent = `${index + 1}`;

      const body = document.createElement("button");
      body.type = "button";
      body.className = "outbox-text";
      body.dataset.act = "outbox-edit";
      const preview = String(item.text || "").replace(/\s+/g, " ").trim();
      const images = (item.attachments || []).length;
      body.textContent = preview || (images ? "[图片]" : "(空消息)");
      if (images) body.textContent += ` · ${images} 张图`;
      body.title = "点击修改这条待发送消息";

      const state = document.createElement("span");
      state.className = "outbox-state";
      state.textContent = outboxStateLabel(item);
      if (item.state === "failed" && item.error) state.title = item.error;
      if (item.attempts > 1) state.textContent += ` · 第 ${item.attempts} 次`;

      const ops = document.createElement("span");
      ops.className = "outbox-ops";
      const addOp = (act, label, title) => {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.act = act;
        button.title = title;
        button.textContent = label;
        ops.appendChild(button);
      };
      if (item.state === "failed") addOp("outbox-retry", "重试", "重新排进待发送");
      if (item.state !== "sending") addOp("outbox-now", "立刻", "置顶并马上发送");
      addOp("outbox-drop", "✕", "取消这条");

      row.append(number, body, state, ops);
      list.appendChild(row);
    });
    els.outbox.appendChild(list);
  }

  /** 发件箱的写操作统一走这里：调接口 → 用返回的队列刷新。 */
  async function outboxAction(path, options = {}, successText = "") {
    const sessionId = state.sessionId;
    if (!sessionId) return;
    try {
      const payload = await api(
        `/api/sessions/${encodeURIComponent(sessionId)}${path}`,
        options,
      );
      applyOutbox(sessionId, payload.messages || []);
      if (successText) toast(successText, "success");
    } catch (error) {
      toast(`发件箱操作失败：${error.message}`, "error");
    }
  }

  /** 点正文就地改：Enter 保存、Esc 取消。 */
  function beginEditQueued(row, item) {
    if (row.querySelector("input.outbox-input")) return;
    const body = row.querySelector(".outbox-text");
    if (!body) return;
    const input = document.createElement("input");
    input.className = "outbox-input";
    input.type = "text";
    input.maxLength = 100_000;
    input.value = String(item.text || "");
    body.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const text = input.value.trim();
      if (!save || text === String(item.text || "")) {
        renderOutbox();
        return;
      }
      await outboxAction(
        `/outbox/${encodeURIComponent(item.id)}`,
        { method: "PATCH", body: JSON.stringify({ text }) },
      );
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        void finish(true);
      } else if (event.key === "Escape") {
        event.preventDefault();
        void finish(false);
      }
    });
    input.addEventListener("blur", () => void finish(true));
  }

  /** 拉取某个会话的队列；默认当前会话。 */
  async function refreshOutbox(sessionId = state.sessionId) {
    if (!sessionId) {
      renderOutbox();
      return;
    }
    try {
      const payload = await api(
        `/api/sessions/${encodeURIComponent(sessionId)}/outbox`,
      );
      applyOutbox(sessionId, payload.messages || []);
    } catch {
      // 网络抖动时保留上一次结果
    }
  }

  async function dropQueued(messageId) {
    const sessionId = state.sessionId;
    if (!sessionId) return;
    try {
      await api(
        `/api/sessions/${encodeURIComponent(sessionId)}/outbox/${encodeURIComponent(messageId)}`,
        { method: "DELETE" },
      );
    } catch (error) {
      toast(error.message || "取消失败", "error");
    }
    void refreshOutbox(sessionId);
  }

  function cancelPrompt() {
    if (!cur().busy) return;
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
  /** 断线后自动重连（1s→2s→4s，上限 8s）；重连成功会重新订阅会话并拉一次快照。 */
  function scheduleReconnect() {
    if (state.reconnectTimer) return;
    const delay = Math.min(state.reconnectDelay || 1000, 8000);
    state.reconnectDelay = delay * 2;
    state.reconnectTimer = window.setTimeout(() => {
      state.reconnectTimer = null;
      void ensureSocket().catch(() => {});
    }, delay);
  }

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
        const reconnected = state.everConnected === true;
        state.everConnected = true;
        state.socket = socket;
        state.socketPromise = null;
        state.reconnectDelay = 1000;
        window.clearTimeout(state.reconnectTimer);
        state.reconnectTimer = null;
        setSocketOnline(true, "实时连接");
        resolve(socket);
        if (reconnected) {
          // 断线期间的事件都丢了：重新订阅 + 后台拉一遍，别让界面停在旧状态
          watchers.clear();
          syncWatch();
          void refreshSessions();
          if (state.sessionId) void refreshSession(state.sessionId);
        }
      };
      socket.onmessage = handleSocketMessage;
      socket.onerror = () => {
        window.clearTimeout(timer);
        state.socketPromise = null;
        setSocketOnline(false, "离线");
        scheduleReconnect();
        reject(new Error("实时连接失败"));
      };
      socket.onclose = () => {
        window.clearTimeout(timer);
        state.socket = null;
        state.socketPromise = null;
        watchers.clear();
        setSocketOnline(false, "离线");
        let wasBusy = false;
        for (const session of state.store.values()) {
          if (session.liveMessage) session.liveMessage.live = false;
          session.liveMessage = null;
          if (session.busy) {
            session.busy = false;
            wasBusy = true;
          }
        }
        if (cur().busy) {
          cur().busy = false;
          wasBusy = true;
        }
        applyBusyChrome(false, "");
        renderSessions();
        if (wasBusy) toast("实时连接已断开，正在重连…", "error");
        scheduleReconnect();
      };
    });
    return state.socketPromise;
  }

  /** 某个会话「正在生成的 assistant 消息」（流式增量都累积在它身上）。 */
  function currentLiveMessage(session) {
    if (session.liveMessage && session.messages.includes(session.liveMessage)) {
      return session.liveMessage;
    }
    const last = session.messages.at(-1);
    if (last?.role === "assistant" && last.live) {
      session.liveMessage = last;
      return last;
    }
    return null;
  }

  /** 标记该会话当前这轮请求已收尾；之后同 request_id 的增量一律丢弃。 */
  function closeRequest(session) {
    const requestId = session.lastRequestId;
    if (!requestId) return;
    session.closedRequests.add(requestId);
    while (session.closedRequests.size > 8) {
      const oldest = session.closedRequests.values().next().value;
      session.closedRequests.delete(oldest);
    }
  }

  function beginStreamStep(session) {
    const current = currentLiveMessage(session);
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
    session.messages.push(message);
    session.liveMessage = message;
    // 有气泡了立刻收掉「Agent 正在思考…」，否则两个指示器会同时占屏
    if (session === cur()) refreshTyping();
    touchSession(session);
    return message;
  }

  function markToolCallStatus(session, callId, status) {
    if (!callId) return;
    for (let index = session.messages.length - 1; index >= 0; index -= 1) {
      const message = session.messages[index];
      if (message.role !== "assistant") continue;
      const call = (message.tool_calls || []).find((item) => item.id === callId);
      if (call) {
        call.status = status;
        return;
      }
    }
  }

  /* 某个会话的状态变了：当前会话直接重绘，后台会话只加未读角标。 */
  function touchSession(session) {
    if (session === cur()) {
      scheduleStreamRender();
      return;
    }
    session.unread += 1;
    renderSessions();
  }

  function handleStreamEvent(session, event) {
    const requestId = String(event.request_id || "");
    if (requestId) {
      if (session.closedRequests.has(requestId)) return;
      session.lastRequestId = requestId;
    }
    const kind = event.kind;
    if (kind === "queued") {
      // 同一会话已有请求在跑（可能是另一台设备/另一个标签页）：进入等待态
      setSessionBusy(session, true, event.text || "正在等待上一个回复结束…");
      return;
    }
    if (!session.busy && !watchers.has(session.id)) return;
    if (kind === "step_start") {
      beginStreamStep(session);
      return;
    }
    if (kind === "thinking") {
      const live = currentLiveMessage(session) || beginStreamStep(session);
      live.reasoning_content = `${live.reasoning_content || ""}${event.text || ""}`;
      touchSession(session);
      return;
    }
    if (kind === "text") {
      const live = currentLiveMessage(session) || beginStreamStep(session);
      live.content = `${live.content || ""}${event.text || ""}`;
      if (session === cur()) els.typing.hidden = true;
      touchSession(session);
      return;
    }
    if (kind === "tool_calls") {
      const live = currentLiveMessage(session) || beginStreamStep(session);
      live.tool_calls = (event.tool_calls || []).map((call) => ({
        id: call.id,
        name: call.name,
        arguments: call.arguments || {},
      }));
      touchSession(session);
      return;
    }
    if (kind === "tool_start") {
      const call = event.tool_call || {};
      markToolCallStatus(session, call.id, "running");
      if (session === cur()) {
        els.typingLabel.textContent = `正在执行 ${call.name || "工具"}…`;
      }
      touchSession(session);
      return;
    }
    if (kind === "tool_result") {
      const call = event.tool_call || {};
      markToolCallStatus(session, call.id, "done");
      const toolParts = [];
      if (event.text) toolParts.push({ type: "text", text: event.text });
      for (const hash of event.attachments || []) {
        toolParts.push({ type: "image", hash, url: `/api/attachments/${hash}` });
      }
      session.messages.push({
        role: "tool",
        content: event.text || "",
        parts: toolParts,
        tool_call_id: call.id || null,
        name: call.name || null,
      });
      touchSession(session);
      return;
    }
    if (kind === "done") {
      if (session === cur()) els.typing.hidden = true;
      return;
    }
    if (kind === "error") {
      const live = session.liveMessage;
      if (
        live &&
        !live.content &&
        !live.reasoning_content &&
        !(live.tool_calls || []).length
      ) {
        session.messages = session.messages.filter((message) => message !== live);
      }
      session.liveMessage = null;
      touchSession(session);
    }
  }

  /*
   * 运行状态刷新：谁在跑、跑到哪一步。
   * 当前会话由别处（另一台设备 / 刷新前的本页）发起时，自动接续它的实时流。
   */
  /*
   * 运行状态刷新：谁在跑、跑到哪一步。
   * 正在跑的会话都会被订阅，所以后台会话也能持续推进；切回去时内容已经在了。
   */
  function handleActivityFrame(frame) {
    const incoming = Array.isArray(frame.sessions) ? frame.sessions : [];
    for (const activity of incoming) {
      const id = activity?.session_id;
      if (!id) continue;
      const session = store(id);
      if (activity.running) {
        state.activities[id] = activity;
        ensureWatch(id);
        if (!session.busy) {
          setSessionBusy(session, true, `Agent ${activityLabel(activity)}…（已接续）`);
        }
      } else {
        const wasRunning = Boolean(state.activities[id]) || session.busy;
        delete state.activities[id];
        if (session.busy) {
          session.busy = false;
          session.liveMessage = null;
          if (session === cur()) {
            applyBusyChrome(false, "");
            void refreshSession(id);
          } else if (wasRunning) {
            session.unread += 1;
            void refreshSession(id);
          }
        }
      }
      const summary = metaOf(id);
      if (summary) {
        summary.running = Boolean(activity.running);
        summary.phase = activity.phase || "";
        summary.step = activity.step || 0;
      }
    }
    renderSessions();
    syncWatch();
  }

  /** 后台刷新某个会话的快照（不切换视图）。 */
  async function refreshSession(sessionId) {
    try {
      const snapshot = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
      applySnapshot(snapshot, { force: false });
    } catch {
      // 网络抖动：下一次推送/刷新还会来
    }
  }

  /*
   * 所有帧都按 session_id 路由到「它自己那个会话」，与当前看的是哪个无关。
   * 只有当前会话的帧会立刻重绘；后台会话攒着，切过去就在。
   */
  function frameSession(frame) {
    const id =
      frame.session_id ||
      frame.event?.session_id ||
      frame.session?.session_id ||
      null;
    return id ? store(id) : null;
  }

  function dropOptimisticUser(session) {
    for (let index = session.messages.length - 1; index >= 0; index -= 1) {
      const message = session.messages[index];
      if (message.role === "user" && (message.id === null || message.id === undefined)) {
        session.messages.splice(index, 1);
        return true;
      }
    }
    return false;
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
      syncWatch();
      return;
    }
    if (frame.type === "activity") {
      handleActivityFrame(frame);
      return;
    }
    if (frame.type === "status") {
      const session = frameSession(frame);
      if (session) setSessionBusy(session, true, frame.label || "Agent 正在思考…");
      return;
    }
    if (frame.type === "stream") {
      const session = frameSession(frame);
      if (session) handleStreamEvent(session, frame.event || {});
      return;
    }
    if (frame.type === "progress") {
      applySnapshot(frame.session, { force: state.stickToBottom });
      const session = store(frame.session?.session_id || state.sessionId);
      const last = session.messages.filter((m) => m.role !== "system").at(-1);
      if (last?.role === "tool") setSessionBusy(session, true, "工具执行完成，继续思考…");
      else if (last?.role === "assistant" && last.tool_calls?.length) {
        setSessionBusy(session, true, "正在调用工具…");
      }
      return;
    }
    if (frame.type === "queued_ack") {
      // 这一轮没有真正开始：消息进了后端队列，别在气泡区留一条幽灵消息
      const session = frameSession(frame) || cur();
      dropOptimisticUser(session);
      session.busy = false;
      if (session === cur()) {
        applyBusyChrome(false, "");
        renderMessages({ scroll: true, force: state.stickToBottom });
      }
      toast("会话忙，已加入发件箱", "info");
      void refreshOutbox(session.id);
      return;
    }
    if (frame.type === "cancelled") {
      const session = frameSession(frame) || cur();
      closeRequest(session);
      if (session.liveMessage) {
        // 本地那半截气泡交给服务端快照（它才是真相），别和「已中断」那条并存
        session.liveMessage.live = false;
        const index = session.messages.indexOf(session.liveMessage);
        if (index >= 0) session.messages.splice(index, 1);
      }
      session.liveMessage = null;
      session.cancelled = true;
      setSessionBusy(session, false);
      toast("已中断生成", "info");
      if (session === cur()) renderMessages({ scroll: true, force: state.stickToBottom });
      if (session.id) void refreshSession(session.id);
      return;
    }
    if (frame.type === "reply") {
      const session = frameSession(frame) || cur();
      closeRequest(session);
      setSessionBusy(session, false);
      const wasCancelled = session.cancelled;
      session.cancelled = false;
      if (wasCancelled) {
        // 中断提示已由 cancelled 帧给出，这里不再重复报错、也不再补气泡
      } else if (frame.success === false) {
        // 真失败才在对话里留一条记号，并且只留一条
        const reason = frame.text || "Agent 处理失败";
        const last = session.messages.at(-1);
        if (!(last?.role === "assistant" && String(last.content || "").startsWith("⚠️"))) {
          session.messages.push({
            role: "assistant",
            content: `⚠️ ${reason}`,
            tool_calls: [],
          });
          if (session === cur()) renderMessages({ scroll: true, force: true });
        }
        toast(reason, "error");
      } else {
        if (session === cur() && !session.liveMessage?.content) {
          appendAssistantText(frame.text);
        }
        toast(`已完成 · ${frame.steps || 0} 个模型步骤`, "success");
      }
      return;
    }
    if (frame.type === "session") {
      const id = frame.session?.session_id;
      const isCurrent = !id || id === state.sessionId;
      if (id) closeRequest(store(id));
      applySnapshot(frame.session, { force: state.stickToBottom });
      if (id) setSessionBusy(store(id), false);
      if (isCurrent && state.sessionId) applyBusyChrome(false, "");
      void refreshSessions();
      void refreshOutbox(id || state.sessionId);
      return;
    }
    if (frame.type === "outbox") {
      applyOutbox(frame.session_id, frame.messages || []);
      return;
    }
    if (frame.type === "error") {
      const session = frameSession(frame) || cur();
      setSessionBusy(session, false);
      if (session.liveMessage?.live) session.liveMessage.live = false;
      session.liveMessage = null;
      const message = frame.message || "请求失败";
      const last = session.messages.at(-1);
      if (!(last?.role === "assistant" && last.content?.startsWith("⚠️"))) {
        session.messages.push({
          role: "assistant",
          content: `⚠️ ${message}`,
          tool_calls: [],
        });
        if (session === cur()) renderMessages({ scroll: true, force: true });
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
      if (cur().busy) {
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
      // 提交只由按钮触发（回车已被 keydown 拦下）：忙时按钮是「停止」
      if (cur().busy) {
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
        // 回车永远是「发送」：会话忙时后端会把消息排进待发送队列，
        // 不再把回车复用成「中断」——那会让连续输入像被掐断。
        void sendPrompt(els.prompt.value);
      }
    });

    els.chatScroll.addEventListener("scroll", () => {
      state.stickToBottom = isSourceNearBottom();
      scheduleWindowSync();
    });
    // 图片是异步布局的：加载完要重新量高度，否则留白会算错
    els.messageList.addEventListener("load", () => scheduleWindowSync(), true);

    document.querySelectorAll("[data-prompt]").forEach((button) => {
      button.addEventListener("click", () => {
        els.prompt.value = button.dataset.prompt || "";
        resizePrompt();
        els.prompt.focus();
      });
    });

    /*
     * 侧栏：单击切换、双击标题改名、删除要点两次（自制确认，避免原生
     * confirm 被沙箱拦掉——与 b64/chat 一致）。
     */
    els.sessionList.addEventListener("click", (event) => {
      const deleteButton = event.target.closest("[data-delete]");
      if (deleteButton) {
        event.stopPropagation();
        void armDelete(deleteButton);
        return;
      }
      if (event.target.closest("input.rename")) return;
      const item = event.target.closest("[data-session]");
      if (item) void loadSession(item.dataset.session);
    });

    els.sessionList.addEventListener("dblclick", (event) => {
      const name = event.target.closest(".session-name");
      const item = event.target.closest("[data-session]");
      if (!name || !item || item.dataset.session !== state.sessionId) {
        // 双击非当前会话的标题：先切过去，再改名
        if (name && item) void loadSession(item.dataset.session);
        return;
      }
      event.preventDefault();
      beginRename(item.dataset.session, name);
    });

    els.sessionList.addEventListener("keydown", (event) => {
      if (event.key === "F2") {
        const item = event.target.closest("[data-session]");
        const name = item?.querySelector(".session-name");
        if (item && name && item.dataset.session === state.sessionId) {
          event.preventDefault();
          beginRename(item.dataset.session, name);
        }
        return;
      }
      if (event.key !== "Enter" && event.key !== " ") return;
      const item = event.target.closest("[data-session]");
      if (!item) return;
      event.preventDefault();
      void loadSession(item.dataset.session);
    });

    /*
     * 发件箱：全部取消 / 立即发送 / 单条立刻 / 重试 / 取消 / 改正文。
     * 用一处事件委托，队列每次重渲染也不用重新绑。
     */
    els.outbox.addEventListener("click", async (event) => {
      const action = event.target.closest("[data-act]");
      if (!action) return;
      const row = action.closest("[data-queue]");
      const messageId = row?.dataset.queue || "";
      const item = cur().outbox.find((entry) => entry.id === messageId);
      const act = action.dataset.act;
      if (act === "outbox-edit" && item && row) {
        beginEditQueued(row, item);
        return;
      }
      if (act === "outbox-clear") {
        if (!cur().outbox.length) return;
        await outboxAction("/outbox", { method: "DELETE" }, "已取消全部待发送");
        return;
      }
      if (act === "outbox-send-now") {
        const first = cur().outbox.find((entry) => entry.state !== "sending");
        if (!first) {
          toast("没有可发送的排队消息", "info");
          return;
        }
        await outboxAction(
          `/outbox/${encodeURIComponent(first.id)}/send-now`,
          { method: "POST" },
          "已置顶，马上发送",
        );
        return;
      }
      if (!messageId) return;
      if (act === "outbox-now") {
        await outboxAction(
          `/outbox/${encodeURIComponent(messageId)}/send-now`,
          { method: "POST" },
          "已置顶，马上发送",
        );
      } else if (act === "outbox-retry") {
        await outboxAction(
          `/outbox/${encodeURIComponent(messageId)}/retry`,
          { method: "POST" },
          "已重新排队",
        );
      } else if (act === "outbox-drop") {
        await outboxAction(
          `/outbox/${encodeURIComponent(messageId)}`,
          { method: "DELETE" },
        );
      }
    });

    els.messageList.addEventListener("click", async (event) => {
      const action = event.target.closest("[data-act]");
      if (action) {
        const host = action.closest("[data-node]");
        const nodeId = Number(host?.dataset.node || "");
        const node = cur().nodes.find((item) => item.id === nodeId) || null;
        if (!node) return;
        const act = action.dataset.act;
        if (act === "branch-prev") await switchBranch(node, -1);
        else if (act === "branch-next") await switchBranch(node, 1);
        else if (act === "edit") void beginEdit(host, node);
        else if (act === "retry") retryNode(node);
        else if (act === "delete") await deleteBranch(node);
        else if (act === "cancel-edit") endEdit(host);
        else if (act === "save-edit") {
          const editor = host.querySelector(".editor");
          const text = String(editor?.querySelector("textarea")?.value || "").trim();
          const attachments = (editor?.__attachments || []).slice();
          if (!text && !attachments.length) {
            toast("消息不能为空", "info");
            return;
          }
          endEdit(host);
          await sendPrompt(text, { parent: node.parent, attachments });
        }
        return;
      }
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

  /** 删除要点两次：第一次变「确认」，3 秒后退回（与 b64/chat 一致）。 */
  async function armDelete(button) {
    const sessionId = button.dataset.delete;
    if (!sessionId) return;
    if (button.dataset.arm) {
      delete button.dataset.arm;
      await deleteSession(sessionId);
      return;
    }
    button.dataset.arm = "1";
    button.classList.add("arm");
    button.textContent = "确认";
    toast(`再点一次「确认」删除会话「${titleFor(sessionId)}」`, "info");
    window.setTimeout(() => {
      if (!button.isConnected) return;
      delete button.dataset.arm;
      button.classList.remove("arm");
      button.innerHTML = '<svg><use href="#i-trash"></use></svg>';
    }, 3000);
  }

  async function deleteSession(sessionId) {
    if (!sessionId) return;
    if (store(sessionId).busy) {
      toast("该会话正在生成，先中断再删除", "info");
      return;
    }
    try {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      dropWatch(sessionId);
      state.store.delete(sessionId);
      if (sessionId === state.sessionId) newChat(false);
      await refreshSessions();
      toast("会话已删除", "success");
    } catch (error) {
      toast(`删除失败：${error.message}`, "error");
    }
  }

  /** 双击标题就地改名；留空回车 = 恢复自动标题（与服务端语义一致）。 */
  function beginRename(sessionId, nameEl) {
    if (!nameEl || !nameEl.isConnected) return;
    if (nameEl.parentElement?.querySelector("input.rename")) return;
    const input = document.createElement("input");
    input.className = "rename";
    input.type = "text";
    input.maxLength = 80;
    input.placeholder = "留空恢复自动标题";
    input.value = metaOf(sessionId)?.title || "";
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const value = input.value.trim();
      // 就地恢复标题节点，不重建整个列表（重建会把点击目标换掉）
      input.replaceWith(nameEl);
      if (save) await renameSession(sessionId, value);
    };
    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("dblclick", (event) => event.stopPropagation());
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        void finish(true);
      } else if (event.key === "Escape") {
        event.preventDefault();
        void finish(false);
      }
    });
    input.addEventListener("blur", () => void finish(true));
    nameEl.replaceWith(input);
    input.focus();
    input.select();
  }

  async function renameSession(sessionId, title) {
    try {
      const payload = await api(
        `/api/sessions/${encodeURIComponent(sessionId)}/title`,
        { method: "PATCH", body: JSON.stringify({ title }) },
      );
      const resolved = String(payload.title || "");
      const meta = metaOf(sessionId);
      if (meta) meta.title = resolved;
      const session = state.store.get(sessionId);
      if (session) session.title = resolved;
      if (sessionId === state.sessionId) {
        els.chatTitle.textContent = titleFor(sessionId);
      }
      renderSessions();
    } catch (error) {
      toast(`改名失败：${error.message}`, "error");
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
    if (saved && metaOf(saved)) {
      await loadSession(saved, { silent: true });
    } else if (saved) {
      // 会话已被删除：别再拿旧 id 去猜
      localStorage.removeItem("blue.session");
    }
    if (!state.sessionId) showWelcome(true);
    void refreshOutbox();
    // 队列变化由 session.outbox 广播实时推送，不再轮询
    void ensureSocket().catch(() => {
      setSocketOnline(false, "离线");
    });
  }

  void bootstrap();
})();
