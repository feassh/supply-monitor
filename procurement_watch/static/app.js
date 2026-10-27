"use strict";

const $ = (selector) => document.querySelector(selector);
const csrf = $('meta[name="csrf-token"]').content;
const icon = (name, cls = "") => `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
const escapeHtml = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
let toastTimer;
function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast visible${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("visible"), 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {credentials: "same-origin", ...options, headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf, ...options.headers}});
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && document.body.dataset.page !== "login") {
    window.location.assign("/login");
    throw new Error("登录已过期");
  }
  if (!response.ok) throw new Error(data.error || "操作未完成，请稍后重试");
  return data;
}
const post = (path, data = {}) => api(path, {method:"POST", body:JSON.stringify(data)});

function dateParts(value) {
  if (!value) return ["—", ""];
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return ["—", ""];
  const options = {timeZone:"Asia/Shanghai", hour12:false};
  return [date.toLocaleDateString("sv-SE", options), date.toLocaleTimeString("en-GB", options)];
}
function shortDate(value) {
  const [day, time] = dateParts(value);
  if (day === "—") return "尚未安排";
  const [today] = dateParts(new Date().toISOString());
  return `${day === today ? "今天" : day.slice(5)} ${time.slice(0,5)}`;
}
function safeLink(value) {
  try { const url = new URL(value); return ["http:","https:"].includes(url.protocol) ? url.href : "#"; }
  catch { return "#"; }
}

if (document.body.dataset.page === "login") {
  $("#login-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const setup = event.currentTarget.dataset.setup === "true";
    const password = $("#password").value;
    const error = $("#login-error");
    error.hidden = true;
    if (setup && password !== $("#confirm-password").value) {error.textContent = "两次输入的密码不一致"; error.hidden = false; return;}
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    try {
      await post(setup ? "/api/setup" : "/api/login", {password,setup_token:$("#setup-token")?.value || ""});
      window.location.assign("/");
    } catch (err) {error.textContent = err.message; error.hidden = false; button.disabled = false;}
  });
} else if (document.body.dataset.page === "push") {
  let saved = null;
  let dirty = false;
  let testing = false;
  function updateTestButton() {
    $("#test-push").disabled = testing || dirty || !saved?.url || !saved?.group_id;
    $("#test-help").textContent = dirty ? "有未保存的修改，请先保存配置。" : saved?.url && saved?.group_id ?
      `向群 ${saved.group_id} 发送一条测试消息，不影响自动推送开关。` : "请先保存完整配置，再测试连接。";
  }
  function renderChannel(data) {
    const configured = Boolean(data.url && data.group_id);
    $("#push-badge").textContent = data.enabled ? "自动推送已启用" : "自动推送已关闭";
    $("#push-badge").className = `badge ${data.enabled ? "green" : "neutral"}`;
    $("#channel-title").textContent = data.enabled ? "自动推送已启用" : configured ? "已配置，待启用" : "等待配置";
    $("#channel-subtitle").textContent = data.enabled ? "新增匹配公告将自动发送" : "可先发送测试消息验证连接";
    $("#channel-indicator").classList.toggle("enabled", data.enabled);
    $("#channel-group").textContent = data.group_id || "未设置";
    $("#channel-pending").textContent = `${data.pending_count} 条`;
    $("#queue-note").textContent = data.pending_count ? `当前有 ${data.pending_count} 条待发送公告，启用后会自动发送到此群组。` : "启用后，新增且标题匹配的公告将自动推送。";
    $("#channel-test").textContent = data.last_test ? `${data.last_test.ok ? "成功" : "失败"} · ${shortDate(data.last_test.at)}` : "尚未测试";
    const result = $("#test-result");
    result.hidden = !data.last_test;
    result.className = `test-result${data.last_test?.ok ? " success" : ""}`;
    result.textContent = data.last_test ? data.last_test.ok ? "测试消息发送成功，请在 QQ 群中查看。" : data.last_test.error : "";
  }
  function fillForm(data) {
    saved = data;
    dirty = false;
    $("#bot-url").value = data.url;
    $("#bot-group").value = data.group_id;
    $("#bot-token").value = "";
    $("#bot-token").disabled = false;
    $("#bot-token").placeholder = data.has_token ? "已保存访问令牌，留空保持不变" : "填写 Bearer Token，无需添加 Bearer 前缀";
    $("#token-status").textContent = data.has_token ? data.token_source === "environment" ? "使用环境变量" : "已加密保存" : "未设置";
    $("#clear-token").checked = false;
    $("#push-enabled").checked = data.enabled;
    renderChannel(data);
    updateTestButton();
  }
  $("#push-form").addEventListener("input", () => {
    dirty = true;
    $("#save-state").textContent = "有未保存的修改";
    updateTestButton();
  });
  $("#clear-token").addEventListener("change", () => {
    $("#bot-token").disabled = $("#clear-token").checked;
    if ($("#clear-token").checked) $("#bot-token").value = "";
  });
  $("#push-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#save-push");
    button.disabled = true;
    try {
      const data = await post("/api/push-config", {url:$("#bot-url").value.trim(), group_id:$("#bot-group").value.trim(),
        token:$("#bot-token").value, clear_token:$("#clear-token").checked, enabled:$("#push-enabled").checked});
      fillForm(data);
      $("#save-state").textContent = "配置已保存，下次推送立即生效。";
      toast("推送配置已保存");
    } catch (err) {toast(err.message,true);} finally {button.disabled = false;}
  });
  $("#test-push").addEventListener("click", async () => {
    testing = true;
    updateTestButton();
    $("#test-push").innerHTML = icon("refresh","loading-spin") + "正在发送…";
    try {await post("/api/push-test"); toast("测试消息发送成功");}
    catch(err) {toast(err.message,true);}
    finally {
      testing = false;
      $("#test-push").innerHTML = icon("play") + "发送测试消息";
      try {saved = await api("/api/push-config"); renderChannel(saved);} catch(err) {toast(err.message,true);}
      updateTestButton();
    }
  });
  $("#logout").addEventListener("click", async () => {
    try {await post("/api/logout"); window.location.assign("/login");} catch(err) {toast(err.message,true);}
  });
  api("/api/push-config").then(fillForm).catch(err => {toast(err.message,true); $("#save-push").disabled = true;});
} else {
  const state = {overview:null, articles:[], page:1, total:0, tab:"all", query:"", source:"", loading:false};
  let requestVersion = 0;
  const unitLabels = {minutes:"分钟",hours:"小时",days:"天"};
  const params = (withPage = true) => {
    const query = new URLSearchParams();
    if (withPage) query.set("page", state.page);
    if (state.query) query.set("q", state.query);
    if (state.source) query.set("source", state.source);
    if (state.tab === "matched") query.set("matched", "1");
    return query.toString();
  };
  function renderKeywords(keywords) {
    $("#keywords").innerHTML = keywords.length ? keywords.map(k => `<span class="keyword-tag">${escapeHtml(k)}<button aria-label="删除关键词 ${escapeHtml(k)}" data-remove="${escapeHtml(k)}">${icon("x")}</button></span>`).join("") : '<span class="small-note">添加关键词，开启标题匹配</span>';
  }
  function renderOverview(data) {
    const first = data.sources.find(s => s.enabled) || data.sources[0];
    const latest = data.sources.map(s => s.last_success_at).filter(Boolean).sort().at(-1);
    const [date, time] = dateParts(latest);
    $("#stat-articles").innerHTML = `${data.articles}<small>条</small>`;
    $("#stat-keywords").innerHTML = `${data.keywords.length}<small>个</small>`;
    $("#stat-last").textContent = time ? time.slice(0,5) : "—";
    $("#stat-last-date").textContent = time ? `${date} · 北京时间` : "等待首次采集";
    $("#stat-notify").textContent = data.notification_enabled ? "已启用" : "待配置";
    $("#stat-notify-foot").textContent = `${data.notification_enabled ? "QQ 群机器人" : "QQ 群机器人未启用"} · ${data.notifications.pending || 0} 条待发送`;
    $("#source-name").textContent = first?.name || "暂无采集来源";
    const disabled = !data.sources.some(s => s.enabled);
    const badge = $("#task-status");
    const status = data.running ? ["正在采集","green"] : disabled ? ["未启用","neutral"] : data.paused ? ["已暂停","neutral"] : first?.last_error ? ["等待重试","amber"] : ["监控中","green"];
    badge.className = `badge ${status[1]}`;
    badge.textContent = status[0];
    $("#source-frequency").textContent = `每 ${data.schedule.value} ${unitLabels[data.schedule.unit]}`;
    $("#source-next").textContent = data.paused ? "已暂停" : data.running ? "采集中…" : first?.next_run_at ? shortDate(first.next_run_at) : "即将采集";
    const error = $("#source-error");
    error.hidden = !first?.last_error;
    error.textContent = first?.last_error || "";
    $("#pause").innerHTML = icon(data.paused ? "play" : "pause") + `<span>${data.paused ? "恢复采集" : "暂停采集"}</span>`;
    $("#collect").disabled = data.running || !data.worker_online || disabled;
    $("#collect").innerHTML = icon("refresh", data.running ? "loading-spin" : "") + `<span>${data.running ? "正在采集…" : "立即采集"}</span>`;
    $("#pause").disabled = disabled;
    if (JSON.stringify(state.overview?.keywords) !== JSON.stringify(data.keywords)) renderKeywords(data.keywords);
    if (!state.overview || JSON.stringify(state.overview.schedule) !== JSON.stringify(data.schedule)) {
      $("#schedule-value").value = data.schedule.value;
      $("#schedule-unit").value = data.schedule.unit;
    }
    const signature = JSON.stringify(data.sources.map(s => [s.id, s.name]));
    if (signature !== JSON.stringify(state.overview?.sources.map(s => [s.id, s.name]))) {
      $("#source-filter").innerHTML = '<option value="">全部来源</option>' + data.sources.map(s => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.name)}</option>`).join("");
      $("#source-filter").value = state.source;
    }
    renderLogs(data.recent_runs);
  }
  async function refreshOverview() {
    const data = await api("/api/overview");
    const finished = state.overview?.running && !data.running;
    renderOverview(data);
    state.overview = data;
    if (finished) {
      await loadArticles();
      toast(data.recent_runs[0]?.status === "failed" ? "采集未完成，可在运行日志中查看原因" : "本轮采集已完成", data.recent_runs[0]?.status === "failed");
    }
  }
  function highlightedTitle(item) {
    if (!item.matched_keywords.length) return escapeHtml(item.title);
    const ordered = [...item.matched_keywords].sort((a,b) => b.length-a.length);
    const expression = new RegExp(`(${ordered.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
    return item.title.split(expression).map((part, i) => i % 2 ? `<mark class="matched-text">${escapeHtml(part)}</mark>` : escapeHtml(part)).join("");
  }
  async function loadArticles() {
    const version = ++requestVersion;
    const data = await api(`/api/articles?${params()}`);
    if (version !== requestVersion) return;
    if (data.page > 1 && !data.items.length) {state.page = Math.max(1,Math.ceil(data.total / 10)); return loadArticles();}
    state.articles = data.items;
    state.total = data.total;
    $("#record-count").textContent = data.total;
    $("#article-rows").innerHTML = data.items.length ? data.items.map(item => {
      const [day,time] = dateParts(item.published_at);
      return `<tr><td><button class="article-title" data-detail="${item.id}" title="${escapeHtml(item.title)}">${highlightedTitle(item)}</button><div class="article-meta">${escapeHtml(item.buyer || "采购人未提供")}</div></td><td><span class="type-tag">${escapeHtml(item.notice_type || "未分类")}</span></td><td>${escapeHtml(item.region || "—")}</td><td><div class="date-main">${day}</div><div class="date-sub">${time}</div></td><td><a href="${escapeHtml(safeLink(item.url))}" target="_blank" rel="noopener noreferrer" class="external-link" title="打开原文" aria-label="打开原文：${escapeHtml(item.title)}">${icon("arrow")}</a></td></tr>`;
    }).join("") : `<tr><td colspan="5" class="empty-cell"><strong>${state.query || state.tab === "matched" ? "没有找到匹配的公告" : "还没有采集记录"}</strong>${state.query ? "试试其他搜索词，或清空搜索条件。" : state.tab === "matched" ? "设置关注的关键词，匹配结果将在这里显示。" : "点击“立即采集”，开始收集你关注的采购信息。"}</td></tr>`;
    $("#page-summary").textContent = data.total ? `共 ${data.total} 条，显示第 ${(state.page-1)*10+1}–${Math.min(state.page*10,data.total)} 条` : "共 0 条记录";
    $("#page-number").textContent = state.page;
    $("#prev-page").disabled = state.page <= 1;
    $("#next-page").disabled = state.page * 10 >= data.total;
    $("#export").href = `/api/export?${params(false)}`;
  }
  function renderLogs(runs) {
    $("#logs-content").innerHTML = runs.length ? runs.map(run => `<div class="log-item"><div class="log-top"><span class="badge ${run.status === "success" ? "green" : "amber"}">${run.status === "success" ? "采集成功" : "采集失败"}</span><span>${dateParts(run.finished_at).join(" ")}</span></div><p>${escapeHtml(run.source_id)}<br>${run.status === "success" ? `读取 ${run.fetched} 条 · 新增 ${run.new_count} 条 · 通知入队 ${run.queued} 条` : escapeHtml(run.error)}</p></div>`).join("") : '<div class="empty-cell"><strong>暂无运行日志</strong>首次采集后将在这里展示运行结果。</div>';
  }
  function showDetail(id) {
    const item = state.articles.find(a => a.id === Number(id));
    if (!item) return;
    const fields = [["发布时间",dateParts(item.published_at).join(" ")],["采购人",item.buyer],["代理机构",item.agency],["公告类型",item.notice_type],["地区",item.region],["品目",item.category],["命中关键词",item.matched_keywords.join("、") || "未匹配"],["通知状态",{pending:"待发送",sent:"已发送"}[item.notification_status] || "未入队"],["首次发现",dateParts(item.first_seen_at).join(" ")]];
    $("#detail-content").innerHTML = `<h2 class="detail-title">${escapeHtml(item.title)}</h2><dl class="detail-grid">${fields.map(([key,value]) => `<dt>${key}</dt><dd>${escapeHtml(value || "未提供")}</dd>`).join("")}</dl>${item.summary ? `<p class="detail-summary">${escapeHtml(item.summary)}</p>` : ""}<div class="detail-actions"><a class="button primary" href="${escapeHtml(safeLink(item.url))}" target="_blank" rel="noopener noreferrer">打开公告原文 ${icon("arrow")}</a></div>`;
    $("#detail-dialog").showModal();
  }
  function guarded(action) { return async (event) => {try {await action(event);} catch (err) {toast(err.message,true);}}; }
  $("#keyword-form").addEventListener("submit", guarded(async event => {
    event.preventDefault();
    const keyword = $("#keyword-input").value.trim();
    if (!keyword) return;
    await post("/api/keywords", {action:"add",keyword});
    $("#keyword-input").value = "";
    await refreshOverview(); await loadArticles(); toast("关键词已添加");
  }));
  $("#keywords").addEventListener("click", guarded(async event => {
    const button = event.target.closest("[data-remove]");
    if (!button) return;
    await post("/api/keywords", {action:"remove",keyword:button.dataset.remove});
    await refreshOverview(); await loadArticles(); toast("关键词已移除");
  }));
  $("#schedule-form").addEventListener("submit", guarded(async event => {
    event.preventDefault();
    await post("/api/schedule", {value:Number($("#schedule-value").value),unit:$("#schedule-unit").value});
    await refreshOverview(); toast("采集频率已保存");
  }));
  $("#pause").addEventListener("click", guarded(async () => {
    const paused = !state.overview?.paused;
    await post("/api/pause", {paused});
    await refreshOverview(); toast(paused ? "自动采集已暂停，仍可手动采集" : "自动采集已恢复");
  }));
  $("#collect").addEventListener("click", guarded(async () => {
    $("#collect").disabled = true;
    try {await post("/api/collect"); await refreshOverview(); toast("采集任务已开始");}
    catch(err) {$("#collect").disabled = false; throw err;}
  }));
  $("#logout").addEventListener("click", guarded(async () => {await post("/api/logout"); window.location.assign("/login");}));
  $("#show-logs").addEventListener("click", () => $("#logs-dialog").showModal());
  document.querySelectorAll(".close-dialog").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
  document.querySelectorAll("dialog").forEach(dialog => dialog.addEventListener("click", event => {if (event.target === dialog && (event.offsetX < 0 || event.offsetX > dialog.offsetWidth || event.offsetY < 0 || event.offsetY > dialog.offsetHeight)) dialog.close();}));
  $("#article-rows").addEventListener("click", event => {const button = event.target.closest("[data-detail]"); if (button) showDetail(button.dataset.detail);});
  document.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", guarded(async () => {
    state.tab = button.dataset.tab; state.page = 1;
    document.querySelectorAll("[data-tab]").forEach(tab => {const active = tab === button; tab.classList.toggle("active",active); tab.setAttribute("aria-selected",active);});
    await loadArticles();
  })));
  let searchTimer;
  $("#search-input").addEventListener("input", () => {
    clearTimeout(searchTimer);
    $("#search-clear").hidden = !$("#search-input").value;
    searchTimer = setTimeout(guarded(async () => {state.query = $("#search-input").value.trim(); state.page = 1; await loadArticles();}),250);
  });
  $("#search-clear").addEventListener("click", guarded(async () => {clearTimeout(searchTimer); $("#search-input").value=""; $("#search-clear").hidden=true; state.query=""; state.page=1; await loadArticles();}));
  $("#source-filter").addEventListener("change", guarded(async event => {state.source=event.target.value; state.page=1; await loadArticles();}));
  $("#prev-page").addEventListener("click", guarded(async () => {if(state.page>1) {state.page--; await loadArticles();}}));
  $("#next-page").addEventListener("click", guarded(async () => {if(state.page*10<state.total) {state.page++; await loadArticles();}}));
  (async () => {
    try {await refreshOverview(); await loadArticles();}
    catch(err) {toast(err.message,true); $("#article-rows").innerHTML = '<tr><td colspan="5" class="empty-cell"><strong>读取失败</strong>请刷新页面重试。</td></tr>';}
  })();
  setInterval(() => {if (!document.hidden) refreshOverview().catch(err => toast(err.message,true));},5000);
}
