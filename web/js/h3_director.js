// ComfyUI-H3Director · 侧栏面板（插件版）
// 只做三件事：注册一个独立侧栏标签、内嵌导演台页面、显示服务在线状态。
// 不修改 ComfyUI 任何全局状态；与已有 "H3 导演台" 侧栏标签共存（标题/ID 均不同）。
(function registerH3DirectorPanel() {
  const TAB_ID = "h3DirectorPanel";
  const TAB_TITLE = "H3 制片台";
  function retry(n) {
    if (n <= 0) { console.warn("[H3Director] comfyAPI 未就绪，放弃注册"); return; }
    setTimeout(() => boot(n - 1), 1000);
  }

  async function boot(tries) {
    const api = window.comfyAPI;
    const app = api?.app?.app || api?.app;
    const sidebar = app?.extensionManager?.sidebarTab;
    if (!app || !sidebar?.registerSidebarTab) { retry(tries); return; }
    if (sidebar.getSidebarTab?.(TAB_ID)) return; // 已注册，避免重复

    let cfg = { url: "http://127.0.0.1:8898", version: "" };
    try {
      const r = await fetch("/h3director/config", { cache: "no-store" });
      if (r.ok) cfg = await r.json();
    } catch (e) { /* 保持默认 URL */ }

    const style = document.createElement("style");
    style.id = "h3director-style";
    style.textContent = `
      #h3director-panel{display:flex;flex-direction:column;height:100%;background:#0d0f12;color:#e8edf3}
      #h3director-panel .bar{flex:none;display:flex;align-items:center;gap:8px;padding:7px 10px;
        border-bottom:1px solid #2a313b;background:#15191f;font:12px/1.4 "Microsoft YaHei UI",sans-serif}
      #h3director-panel .dot{width:8px;height:8px;border-radius:50%;background:#8d9aaa;flex:none}
      #h3director-panel .dot.on{background:#7fb37a;box-shadow:0 0 6px #7fb37a}
      #h3director-panel .dot.off{background:#d96c5f}
      #h3director-panel .url{opacity:.55;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      #h3director-panel .sp{flex:1}
      #h3director-panel button{background:none;border:1px solid #2e3742;border-radius:5px;color:#c9d3df;
        cursor:pointer;padding:3px 8px;font-size:11px}
      #h3director-panel button:hover{border-color:#e2a33c;color:#e2a33c}
      #h3director-panel iframe{flex:1;width:100%;border:0;background:#0d0f12}
    `;
    if (!document.getElementById("h3director-style")) document.head.appendChild(style);

    let panel = null, dot = null, timer = null;

    async function probe() {
      if (!dot) return;
      try {
        const r = await fetch(cfg.url.replace(/\/$/, "") + "/status", { cache: "no-store" });
        dot.className = "dot " + (r.ok ? "on" : "off");
        dot.title = r.ok ? "导演台在线" : ("导演台返回 " + r.status);
      } catch (e) {
        dot.className = "dot off";
        dot.title = "导演台未启动";
      }
    }

    sidebar.registerSidebarTab({
      id: TAB_ID,
      title: TAB_TITLE,
      icon: "icon-[lucide--clapperboard]",
      tooltip: "H3 导演台（插件版）· 只读嵌入",
      type: "custom",
      render(el) {
        panel = el;
        el.id = "h3director-panel";
        el.innerHTML = `
          <div class="bar">
            <span class="dot off" title="检测中"></span>
            <span>H3 导演台</span>
            <span class="url">${cfg.url}</span>
            <span class="sp"></span>
            <button data-act="refresh" title="重新加载面板">刷新</button>
            <button data-act="open" title="在浏览器打开">新窗口</button>
          </div>
          <iframe src="${cfg.url}/?embed=1" title="H3 导演台"></iframe>`;
        dot = el.querySelector(".dot");
        el.querySelector('[data-act="refresh"]').onclick = () => {
          const f = el.querySelector("iframe");
          if (f) f.src = cfg.url + "/?embed=1&t=" + Date.now();
          probe();
        };
        el.querySelector('[data-act="open"]').onclick = () => window.open(cfg.url + "/", "_blank");
        probe();
        timer = setInterval(probe, 5000);
      },
      destroy() {
        if (timer) clearInterval(timer);
        timer = null; panel = null; dot = null;
      },
    });
  }

  boot(30);
})();
