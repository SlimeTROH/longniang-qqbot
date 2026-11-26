"use strict";

const apiRoot = "/elevator/api";
const $ = id => document.getElementById(id);
const navOrder = ["station", "storage", "gear", "market", "atlas"];
const statNames = { hp: "最大生命", atk: "攻击", def: "防御", agi: "敏捷", luk: "幸运" };
const rarityNames = { white: "白", green: "绿", blue: "蓝", purple: "紫", gold: "金", red: "红", shining:"闪耀", rainbow: "虹彩" };
const mapSymbols = { mall: "▦", subway: "⊜", abyss: "✦", auction: "◇", tide: "≈", zenith: "✧" };
const eventNames = { ready: "可以深入", combat: "遭遇敌人", choice: "发现奇遇", relic: "发现遗物" };
const actionTitles = {
  start: "选择禁区", simulate: "选择模拟地图", battle_log: "战斗报告", lookup: "物品档案",
  help: "行动指南", quests: "每日悬赏", rank: "本群排行", all_rank: "全服排行",
  bag: "本局背包", field: "地图道具", field_guide: "地图道具手册", use: "使用道具",
  save_box: "存入安全箱", swap_box: "换出安全箱", shop: "商店", buy: "购买补给",
  warehouse: "仓库", sell: "出售物资", equipment: "装备详情", wear: "穿戴装备",
  reroll: "重铸词条", decode: "解码盲盒", black_market: "黑市", craft: "黑市兑换",
  gallery: "陈列室", atlas: "图鉴进度", rename: "游戏昵称", support: "支援群友",
  prophecy: "来自群星的寓言集", prophecy_rank: "群星寓言周榜", exchange: "无尽电梯交易所",
};
const rankActions = new Set(["rank", "all_rank", "prophecy_rank"]);
const historyLog = [];
const ui = { account: null, state: null, view: "station", market: "shop", storage: "warehouse",
  filter: "", busy: false, syncing: false, challenge: null, timer: 0, toast: 0, generation: 0,
  selectedMap: "", supplies: {}, runPane: "", stationNotice: "", stationSettlement: null,
  syncToken: "", maintenance: false, maintenanceChecking: false, maintenanceTimer: 0, pinnedStat: "" };

function node(tag, { className, text, ...attributes } = {}, ...children) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  for (const [key, value] of Object.entries(attributes)) {
    if (value !== null && value !== undefined) el.setAttribute(key, value);
  }
  el.append(...children.filter(child => child !== null && child !== undefined));
  return el;
}

function button(label, handler, style = "", attributes = {}) {
  const element = node("button", { type: "button", className: style, text: label, ...attributes });
  element.addEventListener("click", handler);
  return element;
}

function format(value) {
  return Number.isFinite(Number(value)) ? Number(value).toLocaleString("zh-CN") : "—";
}

function variant(label, action, argument = "") {
  return { label, action, argument };
}

function actionButton(choice, style = "", options = {}) {
  const revision = ui.state?.revision;
  const el = button(choice.label, () => perform(choice, { revision, ...options }), style, {
    "data-game-action": choice.action,
  });
  if (choice.description) el.append(node("span", { className: "action-note", text: choice.description }));
  return el;
}

function explain(message, error = false) {
  $("announce").textContent = message;
  if (!message) return;
  $("toast").textContent = message;
  $("toast").dataset.error = String(error);
  $("toast").hidden = false;
  clearTimeout(ui.toast);
  ui.toast = setTimeout(() => { $("toast").hidden = true; }, 3400);
}

function setError(id, message = "") {
  const el = $(id);
  el.textContent = message;
  el.hidden = !message;
}

async function request(path, body) {
  let response;
  try {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (path.startsWith("/game/")) {
      headers["X-Elevator-Sync"] = "1";
      if (ui.syncToken && body !== undefined) headers["X-Elevator-Base"] = ui.syncToken;
    }
    const direct = window.ElevatorRTC?.request(path, body, ui.syncToken);
    const viaWeb = () => fetch(apiRoot + path, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!direct) response = await viaWeb();
    else if (body === undefined) response = await direct.catch(viaWeb);
    else response = await direct;
  } catch {
    const error = new Error("暂时连不上网页服务，请检查网络。");
    error.code = "transport_error";
    throw error;
  }
  let data;
  try {
    data = await response.json();
  } catch {
    const error = new Error("未完整收到服务器回复。");
    error.code = "transport_error";
    throw error;
  }
  if (!response.ok || data.error) {
    const error = data.error;
    const problem = new Error(error?.message || (typeof error === "string" && error) || "操作暂时无法完成。");
    problem.status = response.status;
    problem.code = error?.code || data.code;
    problem.payload = data;
    if (problem.code === "maintenance") setMaintenance(true);
    throw problem;
  }
  if (path.startsWith("/game/") && !ui.maintenance) scheduleMaintenanceCheck();
  return data;
}

function setMaintenance(paused) {
  const wasPaused = ui.maintenance;
  ui.maintenance = paused;
  $("maintenance-banner").hidden = !paused;
  $("play-area").inert = paused;
  if (paused && !wasPaused) {
    window.ElevatorInventory?.closeDetail();
    if ($("game-dialog").open) closeDialog("game-dialog");
  }
  if (!paused && wasPaused) {
    explain("维护结束，探险已恢复。");
    if (ui.account?.bound) refresh();
  }
}

function scheduleMaintenanceCheck() {
  clearTimeout(ui.maintenanceTimer);
  ui.maintenanceTimer = setTimeout(checkMaintenance, 30000);
}

async function checkMaintenance() {
  if (ui.maintenanceChecking) return;
  if (document.hidden) { scheduleMaintenanceCheck(); return; }
  ui.maintenanceChecking = true;
  try {
    const result = await request("/maintenance");
    if (typeof result.paused === "boolean") setMaintenance(result.paused);
  } catch {
    // A transient timeout has no bearing on whether the game is paused.
  } finally {
    ui.maintenanceChecking = false;
    scheduleMaintenanceCheck();
  }
}

function openDialog(id) {
  const dialog = $(id);
  if (!dialog.open) dialog.showModal();
}

function closeDialog(id) {
  const dialog = $(id);
  if (dialog.open) dialog.close();
}

function chooseAuth(mode) {
  setError("auth-error");
  for (const tab of document.querySelectorAll(".auth-tabs [data-auth]")) {
    tab.setAttribute("aria-pressed", String(tab.dataset.auth === mode));
  }
  document.querySelector(".auth-tabs").hidden = mode === "bind" || mode === "reset";
  for (const name of ["login", "register", "reset", "bind"]) {
    $(name + "-form").hidden = name !== mode;
  }
}

async function applySession(account, initial = null) {
  ui.account = account;
  window.ElevatorRTC?.setAccount(account?.rtc_trial ? account.username : "");
  $("loading").hidden = true;
  $("gate").hidden = Boolean(account?.bound);
  $("game").hidden = !account?.bound;
  $("identity").hidden = !account?.bound;
  if (account?.bound) {
    if (initial?.maintenance) setMaintenance(true);
    else if (initial?.state) applyState(initial.state, initial.sync_token);
    else if (!ui.state) await refresh();
  } else {
    ui.state = null;
    ui.syncToken = "";
    ui.pinnedStat = "";
    chooseAuth(account ? "bind" : "login");
    if (account) $("bind-description").textContent = `网页账号 ${account.username} 待绑定。请使用要绑定的 QQ 进行群验证。`;
  }
}

function secretForm(id, path, result, { errorId = "auth-error", prepare, onError } = {}) {
  $(id).addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('button[type="submit"]');
    if (submit.disabled) return;
    setError(errorId);
    submit.disabled = true;
    try {
      const fields = Object.fromEntries(new FormData(form));
      prepare?.(fields, form);
      const data = await request(path, fields);
      form.reset();
      await result(data);
    } catch (error) {
      setError(errorId, error.message);
      onError?.(error);
    } finally {
      submit.disabled = false;
    }
  });
}

function showLoginType(visible) {
  const type = $("login-type");
  $("login-type-field").hidden = !visible;
  type.disabled = !visible;
  if (visible && !$("login-form").hidden) type.focus();
  if (!visible) type.value = "";
}

function stopChallenge() {
  clearTimeout(ui.timer);
  ui.challenge = null;
  closeDialog("challenge-dialog");
  $("challenge-content").replaceChildren();
}

function challengeMessage(purpose) {
  return {
    bind: ["验证 QQ 绑定", "用你填写的 QQ 账号，将完整指令发到开通电梯游戏的 QQ 群。机器人确认后，本页将自动载入你的存档。"],
    change: ["确认修改密码", "用当前网页账号绑定的 QQ，将完整指令发到开通电梯游戏的群。机器人确认后，用新密码登录。"],
    reset: ["确认重置密码", "用原来绑定的 QQ，把完整指令发到开通电梯游戏的群。收到机器人确认后，用新密码登录。"],
  }[purpose];
}

async function copyCommand(command, code) {
  try {
    await navigator.clipboard.writeText(command);
    $("challenge-copy").textContent = "已复制";
  } catch {
    const range = document.createRange();
    range.selectNodeContents(code);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    $("challenge-copy").textContent = "已选中，请手动复制";
  }
}

function showChallenge(challenge, fallbackPurpose) {
  stopChallenge();
  if (!challenge?.code) throw new Error("群验证密钥未返回，请重新获取。");
  const purpose = challenge.purpose || fallbackPurpose;
  ui.challenge = { ...challenge, purpose };
  const [title, copy] = challengeMessage(purpose) || challengeMessage(fallbackPurpose);
  const command = challenge.command || `/电梯验证 ${challenge.code}`;
  const code = node("code", { className: "challenge-code", text: command, tabindex: 0 });
  const expiry = node("p", { role: "status", id: "challenge-expiry" });
  const confirmLabel = purpose === "bind" ? "检查绑定结果" : purpose === "reset" ? "已确认，去登录" : "检查修改结果";
  const confirm = button(confirmLabel, () => {
    if (purpose === "reset") {
      stopChallenge();
      chooseAuth("login");
      $("login-form").querySelector('[name="username"]').focus();
    } else {
      checkChallenge(true);
    }
  });
  $("challenge-content").replaceChildren(
    node("h2", { text: title }),
    node("p", { text: copy }), code, expiry,
    node("div", { className: "button-row" }, button("复制群指令", () => copyCommand(command, code), "primary", { id: "challenge-copy" }), confirm),
  );
  openDialog("challenge-dialog");
  tickChallenge();
}

async function checkChallenge(explicit = false) {
  const active = ui.challenge;
  const stage = active?.purpose ?? (ui.account?.bound === false ? "bind" : null);
  if (stage !== "bind" && stage !== "change") return;
  try {
    const { account } = await request("/auth/session");
    if (active !== ui.challenge) return;
    if (stage === "bind" && account?.bound) {
      stopChallenge();
      await applySession(account);
      explain("QQ 绑定成功，可以开始游戏。");
    } else if (stage === "change" && !account) {
      await leaveSession("密码已确认，请使用新密码登录。");
    } else if (explicit) {
      const text = account ? "还未收到 QQ 群里的验证。请确认使用同一个 QQ 账号发送了完整指令。" : "登录已超时，请重新登录。";
      const expiry = $("challenge-expiry");
      if (expiry) expiry.textContent = text;
      else setError("auth-error", text);
    }
  } catch (error) {
    if (explicit) setError(ui.account?.bound ? "account-error" : "auth-error", error.message);
  }
}

function tickChallenge() {
  const current = ui.challenge;
  if (!current) return;
  const expires = Number(current.expires_at) || 0;
  const remaining = Math.max(0, Math.ceil(expires - Date.now() / 1000));
  const info = $("challenge-expiry");
  if (remaining && info) {
    const seconds = String(remaining % 60).padStart(2, "0");
    info.textContent = `密钥剩余 ${Math.floor(remaining / 60)}:${seconds}，只能使用一次。`;
  } else if (expires && info) {
    info.textContent = "这条密钥的有效时间已结束。若尚未完成，请关闭此窗口并重新申请。";
    $("challenge-copy").disabled = true;
    checkChallenge();
    return;
  }
  if (remaining % 4 === 0 && current.purpose !== "reset") checkChallenge();
  ui.timer = setTimeout(tickChallenge, 1000);
}

async function leaveSession(message = "") {
  ui.generation += 1;
  stopChallenge();
  closeDialog("account-dialog");
  closeDialog("game-dialog");
  window.ElevatorInventory?.closeDetail();
  delete document.body.dataset.play;
  $("game-dialog-content").replaceChildren();
  $("game").querySelectorAll("section").forEach(section => section.removeAttribute("aria-busy"));
  historyLog.splice(0);
  ui.view = "station";
  ui.storage = "warehouse";
  ui.market = "shop";
  ui.filter = "";
  ui.search = "";
  ui.selectedMap = "";
  ui.focusDraft = null;
  ui.supplies = {};
  ui.runPane = "";
  ui.stationNotice = "";
  ui.stationSettlement = null;
  $("login-form").reset();
  showLoginType(false);
  $("register-form").reset();
  $("bind-form").reset();
  $("reset-form").reset();
  $("change-form").reset();
  await applySession(null);
  if (message) explain(message);
}

async function logout() {
  try {
    await request("/auth/logout", {});
    await leaveSession("已退出网页账号。");
  } catch (error) {
    setError(ui.account?.bound ? "account-error" : "auth-error", error.message);
  }
}

function focusView(view, focus = true) {
  if (!navOrder.includes(view)) return;
  const changed = ui.view !== view;
  if (changed) {
    ui.filter = "";
    ui.search = "";
    window.ElevatorInventory?.closeDetail();
  }
  ui.view = view;
  renderView();
  if (changed) {
    if (focus) $("view-" + view).querySelector("h1")?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }
  for (const tab of document.querySelectorAll("#main-nav button")) {
    if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
}

function infoPair(label, value, className = "status-pair") {
  return node("div", { className }, node("span", { text: label }), node("strong", { text: value }));
}

function meter(label, value, max, style = "") {
  const limit = Math.max(1, Number(max) || 1);
  const current = Math.min(limit, Math.max(0, Number(value) || 0));
  return node("div", { className: "meter-wrap" },
    node("div", { className: "meter-label" }, node("span", { text: label }), node("strong", { text: `${format(current)} / ${format(limit)}` })),
    node("progress", { className: "meter " + style, value: current, max: limit, "aria-label": label }),
  );
}

function cleanReply(text) {
  return String(text || "").replace(/^👉 (?:直接回序号[^\n]*|\d+[.、][^\n]*)$/gm, "").trim();
}

function writeLog(text) {
  const readable = cleanReply(text);
  if (!readable) return;
  const latest = historyLog[0];
  if (latest?.text !== readable) {
    historyLog.unshift({ text: readable, time: new Date() });
    if (historyLog.length > 20) historyLog.pop();
  }
  renderLog();
}

function renderLog() {
  const entries = historyLog.slice(0, 5).map(({ text, time }) => node("li", {},
    node("time", { datetime: time.toISOString(), text: time.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) }),
    node("span", { text: text.split("\n")[0].slice(0, 125) }),
  ));
  $("log-list").replaceChildren(...(entries.length ? entries : [node("li", { text: "本页的行动会记在这里。" })]));
  $("log-button").hidden = !entries.length;
}

function basicAttributes(values, sources) {
  const details = node("div", { id: "stat-source", className: "stat-source", hidden: "" });
  const grid = node("div", { className: "status-pairs" });
  const buttons = new Map();
  function show(key) {
    const rows = sources[key] || [];
    details.hidden = !rows.length;
    for (const [id, el] of buttons) el.setAttribute("aria-expanded", String(id === key && rows.length > 0));
    if (!rows.length) { details.replaceChildren(); return; }
    details.replaceChildren(node("strong", { text: (statNames[key] || key) + "来源" }),
      node("ul", {}, ...rows.map(row => {
        const value = format(row.value);
        const term = row.op === "multiply" ? "×" + value :
          row.op === "add" && row.value >= 0 ? "+" + value : value;
        return node("li", { text: `${row.source}　${term}${row.op === "base" ? "" : " → " + format(row.result)}` });
      })));
  }
  for (const [key, value] of Object.entries(values)) {
    const el = button("", () => {
      ui.pinnedStat = ui.pinnedStat === key ? "" : key;
      show(ui.pinnedStat);
    }, "stat-source-button status-pair", { "aria-controls": "stat-source", "aria-expanded": "false" });
    el.append(node("span", { text: statNames[key] || key }), node("strong", { text: format(value) }));
    el.addEventListener("pointerenter", event => { if (event.pointerType !== "touch") show(key); });
    el.addEventListener("pointerleave", () => show(ui.pinnedStat));
    el.addEventListener("focus", () => show(key));
    el.addEventListener("blur", () => show(ui.pinnedStat));
    grid.append(el);
    buttons.set(key, el);
  }
  show(ui.pinnedStat);
  return node("div", {}, grid, details);
}

function secondaryPanel(stats, run) {
  if (!stats) return null;
  const pct = value => format(Number((value * 100).toFixed(2))) + "%";
  const labels = {hit:"命中率",dodge:"闪避率",crit:"暴击率",crit_multiplier:"暴击伤害",pierce:"穿透率",combo:"追击率",
    leech:"吸血率",boss_bonus:"首领增伤",mitigation:"打击减伤",flee:"逃跑率",ring_shield_limit:run?"本层星环盾上限":"首层星环盾上限"};
  const box=node("section",{className:"secondary-stats","aria-label":"二级战斗属性"},node("h3",{text:"二级战斗属性"}));
  const grid=node("div",{className:"status-pairs"});
  const detail=node("div",{className:"stat-source","aria-live":"polite",id:"secondary-source"});
  const controls=new Map();
  const display=(key,v)=>v==null?(stats.enemy?"当前不可用":"遇敌后显示"):key==="crit_multiplier"?"×"+format(+v.toFixed(2)):
    key==="ring_shield_limit"?String(v):pct(v);
  function show(key) {
    controls.forEach((b,k)=>b.setAttribute("aria-expanded",String(k===key)));
    detail.hidden=!key;
    if(!key) { detail.replaceChildren();return; }
    const conditions=stats.condition||{}, enemy=stats.enemy;
    const notes=[];
    if(key==="hit")notes.push(enemy?"当前敌人 "+enemy:"遇敌后按对方敏捷计算通常命中率。",conditions.full==="B01"?
      "回描（本场）"+(conditions.state?.redraw?"已使用":"可用"):"");
    if(key==="crit")notes.push("当前生命 "+conditions.hp+" / "+conditions.max_hp);
    if(key==="leech")notes.push("常规吸血每回合最多回复最大生命的 10%，并受当前缺血量限制。");
    if(key==="pierce") {
      if(conditions.depth_cap)notes.push("本层祝福深层破甲 "+pct(conditions.depth_shred)+"，随楼层增长，最高 "+pct(conditions.depth_cap)+"。");
      if(enemy && conditions.effective_enemy_def!=null)notes.push("敌方原防御 "+conditions.enemy_def+"，退火裂纹 "+(conditions.state?.cracks||0)+"/2，主攻和追击的实际敌防 "+format(+conditions.effective_enemy_def.toFixed(2)));
    }
    if(key==="mitigation")notes.push("常规减伤后计算格挡 "+pct(conditions.guard||0)+"、抢拍减伤、双向伤害 ×"+(conditions.damage_multiplier||1)+"、压舱及护盾。");
    if(key==="boss_bonus")notes.push(conditions.boss?"当前首领增伤生效":"遇到首领时生效。");
    if(key==="flee" && stats.flee==null)notes.push(enemy?"当前遭遇或待处理操作限制普通逃跑。":"大厅没有逃跑判定。");
    if(key==="ring_shield_limit")notes.push("星环剩余 "+(stats.shield_split?.[0]||0)+"，普通盾 "+(stats.shield_split?.[1]||0)+"，总盾 "+(run?.shield||0));
    const steps=stats.sources?.[key]||[];
    const list=node("ul",{},...steps.map(step=>node("li",{text:step.name+"　"+(step.before!=null?
      display(key,step.before)+" → ":"")+(step.after==null?"":display(key,step.after))+(step.change==null?"":"（变化 "+(step.change>0?"+":"")+
      (key==="crit_multiplier"?step.change.toFixed(2):(100*step.change).toFixed(2)+" 个百分点")+"）")})));
    detail.replaceChildren(node("strong",{text:labels[key]+" · "+display(key,stats[key])}),list,...notes.filter(Boolean).map(text=>node("p",{text})));
  }
  for(const [key,label] of Object.entries(labels)){
    const b=button("",()=>{ui.secondaryPinned=ui.secondaryPinned===key?"":key;show(ui.secondaryPinned);},"stat-source-button",
      {"aria-controls":"secondary-source","aria-expanded":"false"});
    b.append(node("span",{text:label}),node("strong",{text:display(key,stats[key])}));
    b.addEventListener("pointerenter",e=>{if(e.pointerType==="mouse")show(key);});
    b.addEventListener("pointerleave",()=>show(ui.secondaryPinned));
    b.addEventListener("focus",()=>show(key));b.addEventListener("blur",()=>show(ui.secondaryPinned));
    b.addEventListener("keydown",e=>{if(e.key==="Escape"){ui.secondaryPinned="";show("");}});
    controls.set(key,b);grid.append(b);
  }
  show(ui.secondaryPinned);
  box.append(grid,detail);
  return box;
}

function showFormulas() {
  const groups = ui.state?.formulas || [];
  $("game-dialog-content").replaceChildren(node("h2", {text:"计算公式"}), ...groups.map((group,index) =>
    node("details", {className:"formula-section", open:index === 0 ? "" : null},
      node("summary",{text:group.title}), node("ul",{}, ...group.lines.map(text => node("li",{text}))))));
  openDialog("game-dialog");
}

function renderStatus() {
  const { player, run, atlas, notices } = ui.state;
  $("player-name").textContent = player.nickname || ui.account.username;
  $("resources").replaceChildren(
    infoPair("钱包 · 金", format(player.gold), "resource"),
    infoPair("通行证", `${format(player.tickets)} / ${format(player.ticket_cap)}`, "resource"),
    infoPair("图鉴", `${format(atlas?.known ?? 0)} / ${atlas?.total == null ? "—" : format(atlas.total)}`, "resource"),
  );
  const base = basicAttributes(run?.effective_stats || run?.stats || player.stats || {}, player.stat_sources || {});
  const effects = run ? window.ElevatorInventory.activeFieldItems(run) : [];
  $("status-tag").textContent = run?.prophecy ? "星兆试炼" : run?.simulation || run?.sim ? "模拟" : run ? "下潜中" : "地面";
  $("status-body").replaceChildren(
    ...(run ? [node("p", { className: "item-facts", text: `安全箱 ${format(run.safe?.length || 0)} / ${format(run.safe_capacity ?? player.safebox)}` })] : []),
    base,
    ...(run ? [infoPair("物资估值", format(run.value))] : []),
    ...(player.secondary ? [secondaryPanel(player.secondary, run)] : []),
    ...(run?.recovery ? [node("p", { className: "item-facts",
      text: `缓释再生液 · 进层回复 ${Math.round(run.recovery.fraction * 100)}% · 剩余 ${run.recovery.left} 次` })] : []),
    ...(run?.damage_multiplier > 1 ? [node("p", { className: "item-facts",
      text: `伤害结算 · 对怪物 ×${format(run.damage_multiplier)} · 承受怪物伤害 ×${format(run.damage_multiplier)}` })] : []),
    ...(effects.length ? [node("p", { className: "item-facts", text: "本局生效 · " +
      effects.map(item => item.name + "（" + item.status + "）").join("、") })] : []),
    ...(notices?.length ? [node("details", { className: "notices" }, node("summary", { text: "最近通知" }),
      ...notices.map(notice => node("p", { text: notice.text })))] : []),
  );
}

function resourceList(actions, options) {
  return node("div", { className: "item-actions" }, ...actions.map(choice => actionButton(choice, "", options)));
}

function inventoryContext() {
  return { state: ui.state, selection: ui, node, button, infoPair, format, rarityNames, statNames,
    variant, execute: perform, explain, focusView };
}

function renderView() {
  if (!ui.state) return;
  for (const view of navOrder) $("view-" + view).hidden = ui.view !== view;
  if (ui.view === "station") {
    renderScene();
    return;
  }
  const method = { storage: "showStorage", gear: "showGear", market: "showMarket", atlas: "showAtlas" }[ui.view];
  $(ui.view + "-content").replaceChildren(window.ElevatorInventory[method](inventoryContext()));
}

function applyState(state, token = "") {
  if (!state?.player) throw new Error("当前存档内容不可用，请尝试刷新。");
  ui.state = state;
  ui.syncToken = token;
  document.body.dataset.play = "true";
  renderStatus();
  renderView();
  renderLog();
}

async function refresh(notify = false) {
  if (ui.syncing || ui.busy) return;
  const generation = ui.generation;
  ui.syncing = true;
  $("sync").disabled = true;
  try {
    const data = await request("/game/state");
    if (ui.generation !== generation) return;
    applyState(data.state || data, data.sync_token);
    if (notify) explain("已同步当前 QQ 存档。");
  } catch (error) {
    if (error.status === 401 || error.code === "qq_unbound") {
      await leaveSession("登录已失效，请重新登录。");
    } else {
      explain(error.message, true);
    }
  } finally {
    ui.syncing = false;
    $("sync").disabled = false;
  }
}

function showReply(title, reply, rankChoice = null) {
  const body = cleanReply(reply?.text);
  const heading = node("h2", { text: title, tabindex: rankChoice ? -1 : null });
  const choices = rankChoice ? (reply?.choices || []).filter(choice => rankActions.has(choice.action)) : [];
  let current = null;
  const controls = choices.map(choice => {
    const selected = choice.action === rankChoice.action && choice.argument === rankChoice.argument;
    const control = actionButton(choice, selected ? "primary" : "", { revision: null });
    if (selected) {
      control.setAttribute("aria-current", "true");
      current = control;
    }
    return control;
  });
  const navigation = controls.length ? node("nav", { className: "item-actions", "aria-label": "切换排行" }, ...controls) : null;
  const content = body ? node("div", { className: "reply-text", text: body }) : node("p", { text: "当前没有记录。" });
  $("game-dialog-content").replaceChildren(...[heading, navigation, content].filter(Boolean));
  openDialog("game-dialog");
  if (rankChoice) {
    $("game-dialog").scrollTop = 0;
    (current || heading).focus();
  }
}

function confirmAction(choice, options, title, text, confirmLabel="确认", onCancel=null) {
  $("game-dialog-content").replaceChildren(
    node("h2", { text: title }), node("p", { text }),
    node("div", { className: "button-row" },
      button(confirmLabel, () => {
        closeDialog("game-dialog");
        perform(choice, { ...options, confirmed: true });
      }, "primary"),
      button("取消", () => {
        closeDialog("game-dialog");
        if(onCancel) onCancel();
      }),
    ),
  );
  openDialog("game-dialog");
}

async function perform(choice, options = {}) {
  if (ui.maintenance || ui.busy || !ui.state || !choice?.action) return false;
  const control = choice.control;
  const command = control?.command || choice.action;
  if (ui.syncing) {
    const message = "正在同步存档，请等同步完成。";
    options.onError?.(message);
    explain(message);
    return false;
  }
  if (options.revision && options.revision !== ui.state.revision) {
    const message = "存档已更新，请重新选择。";
    options.onError?.(message);
    explain(message, true);
    return false;
  }
  if (!options.confirmed && command === "blessing") {
    confirmAction(choice, options, "开启全群金雨", "这会消耗金币，为绑定群开启一小时金雨。");
    return false;
  }
  if (!options.confirmed && command === "reroll") {
    confirmAction(choice, options, "确认重铸装备", "重铸会消耗金币和洗练材料，并替换当前词条。");
    return false;
  }
  const generation = ui.generation;
  ui.busy = true;
  const active = document.activeElement;
  $("play-area").setAttribute("aria-busy", "true");
  const pendingNotice = control && ui.view === "station" ? $(ui.state.run ? "run-notice" : "station-receipt") : null;
  const pendingText = command === "down" && ui.state.run
    ? `正在前往 F${ui.state.run.floor + 1}…` : `正在${choice.label}…`;
  if (pendingNotice) {
    pendingNotice.textContent = pendingText;
    pendingNotice.hidden = false;
  }
  try {
    const revision = ui.state.revision ?? ui.state.version;
    const previousFloor = ui.state.run?.floor;
    const data = control
      ? await request("/game/command", { command, params: control.params || {}, revision })
      : await request("/game/action", { action: command, argument: choice.argument ?? "", revision });
    if (generation !== ui.generation) return false;
    if (data.delta) {
      const patch = data.delta;
      if (patch.base === ui.syncToken && patch.base_revision === ui.state.revision) {
        data.state = { ...ui.state, ...patch.fields };
        for (const [key, changed] of Object.entries(patch.sections)) {
          data.state[key] = { ...data.state[key], ...changed };
        }
        for (const [name, update] of Object.entries(patch.lists || {})) {
          const original = ui.state[name];
          const byId = new Map(original.map(row => [row[update.key], row]));
          for (const row of update.upsert) byId.set(row[update.key], row);
          const order = update.order || original.map(row => row[update.key]);
          data.state[name] = order.map(id => byId.get(id));
        }
      } else {
        const fresh = await request("/game/state");
        if (generation !== ui.generation) return false;
        data.state = fresh.state || fresh;
        data.sync_token = fresh.sync_token;
      }
    }
    if (control) {
      const nextFloor = data.state?.run?.floor;
      const moved = nextFloor !== previousFloor;
      if (moved) ui.runPane = "";
      const { battle, adventure, rush, radar_scan: scan, summary, limit_notices: limitNotices = [] } = data.result || {};
      ui.stationSettlement = battle || adventure || rush ? { battle, adventure, rush, revision: data.state?.revision } : null;
      if (scan) explain(summary);
      if (ui.view === "station") {
        ui.stationNotice = [(battle || adventure) && !rush ? "" : summary, ...limitNotices].filter(Boolean).join(" · ");
      } else if (limitNotices.length) {
        explain(limitNotices.join(" · "));
      }
      if (summary) writeLog(summary);
      if (limitNotices.length) writeLog(limitNotices.join(" · "));
      if (data.state) applyState(data.state, data.sync_token);
      const wish = data.result?.wish_preview;
      if (wish) {
        const label = "确认清空并击杀";
        const cost=wish.bag||wish.safe ? `清空背包 ${wish.bag} 件、安全箱 ${wish.safe} 件本局拾取（估值 ${format(wish.value)} 金）。` :
          "背包和安全箱没有本局拾取。";
        confirmAction(gameControl(label,"wish_confirm",{token:wish.token}), {revision:ui.state.revision},
          "发动「为了我所期待的」？",
          `${cost}消耗 1 件临装并立即击杀「${wish.enemy}」。入场补给、其余临装及已入库物品保留。`,label,
          () => perform(gameControl("取消发动","wish_cancel"),{inline:true,quiet:true}));
      }
      if (ui.view === "station") {
        if (moved) window.scrollTo(0, 0);
        else if (battle || adventure) {
          const settlement = $("station-settlement");
          if (!settlement.hidden && settlement.getBoundingClientRect().top < 60) {
            settlement.scrollIntoView({ block: "start" });
          }
        }
      }
      return true;
    }
    if (data.state) applyState(data.state, data.sync_token);
    const reply = data.reply;
    if (options.quiet) return true;
    const ranking = rankActions.has(command);
    if (reply?.text || (ranking && reply?.choices?.length)) {
      showReply(actionTitles[command] || choice.label, reply, ranking ? choice : null);
    }
    return true;
  } catch (error) {
    if (generation !== ui.generation) return false;
    if (error.payload?.state) applyState(error.payload.state, error.payload.sync_token);
    if (error.code === "transport_error") {
      try {
        const saved = await request("/game/state");
        if (generation !== ui.generation) return false;
        applyState(saved.state || saved, saved.sync_token);
        error.message = "操作回包中断，已重新读取服务器存档，请核对当前状态。";
      } catch { /* Preserve the original error while the connection is unavailable. */ }
    }
    if (error.status === 401) await leaveSession("登录已失效，请重新登录。");
    else {
      if (error.status === 409 && !error.payload?.state) setTimeout(refresh, 0);
      options.onError?.(error.message);
      explain(error.message, true);
      if ($("game-dialog").open) {
        const old = $("game-dialog-content").querySelector(".form-error");
        if (old) old.remove();
        $("game-dialog-content").append(node("p", { className: "form-error", text: error.message, role: "alert" }));
      }
    }
    return false;
  } finally {
    ui.busy = false;
    $("play-area").removeAttribute("aria-busy");
    if (pendingNotice && pendingNotice.textContent === pendingText) {
      pendingNotice.textContent = ui.stationNotice;
      pendingNotice.hidden = !ui.stationNotice;
    }
    if (active?.isConnected && !$("game-dialog").open && document.activeElement === document.body) active.focus({ preventScroll: true });
  }
}

function openAccount() {
  setError("account-error");
  $("account-title").textContent = ui.account?.username || "网页账号";
  $("account-state").textContent = ui.account?.bound ? "QQ 已绑定。网页操作使用你的电梯存档。" : "仍需确认 QQ 绑定。";
  openDialog("account-dialog");
}

function renameDialog() {
  const input = node("input", { name: "nickname", maxlength: 16, value: ui.state?.player.nickname || "", required: "" });
  const form = node("form", { className: "form-pane" }, node("label", {}, "游戏昵称（1–16 字）", input),
    node("button", { type: "submit", className: "primary", text: "保存昵称" }),
  );
  const revision = ui.state.revision;
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value || /^__keep_(?:start|lobby)__$/.test(value)) return explain("请输入有效的游戏昵称。", true);
    if (await perform(variant("保存昵称", "rename", value), { revision, quiet: true })) {
      closeDialog("game-dialog");
      explain("游戏昵称已保存。");
    }
  });
  closeDialog("account-dialog");
  $("game-dialog-content").replaceChildren(node("h2", { text: "修改游戏昵称" }), form);
  openDialog("game-dialog");
}

function enableSwipes() {
  let gesture = null;
  $("views").addEventListener("touchstart", event => {
    gesture = null;
    if (event.touches.length !== 1 || event.target.closest("button, input, select, a, summary, .pickup-grid")) return;
    const touch = event.touches[0];
    gesture = { x: touch.clientX, y: touch.clientY, time: Date.now() };
  }, { passive: true });
  $("views").addEventListener("touchcancel", () => { gesture = null; }, { passive: true });
  $("views").addEventListener("touchend", event => {
    if (!gesture || !event.changedTouches[0]) return;
    const { clientX, clientY } = event.changedTouches[0];
    const dx = clientX - gesture.x;
    const dy = clientY - gesture.y;
    const elapsed = Date.now() - gesture.time;
    gesture = null;
    if (elapsed > 650 || Math.abs(dx) < 64 || Math.abs(dx) < 1.4 * Math.abs(dy)) return;
    const next = navOrder[navOrder.indexOf(ui.view) + (dx < 0 ? 1 : -1)];
    if (next) focusView(next, false);
  }, { passive: true });
}

function setup() {
  document.querySelectorAll("[data-auth]").forEach(tab => tab.addEventListener("click", () => chooseAuth(tab.dataset.auth)));
  $("forgot-button").addEventListener("click", () => chooseAuth("reset"));
  $("bind-check").addEventListener("click", () => checkChallenge(true));
  $("bind-logout").addEventListener("click", logout);
  $("logout").addEventListener("click", logout);
  $("sync").addEventListener("click", () => refresh(true));
  $("formula-button").addEventListener("click", showFormulas);
  $("rush-form").addEventListener("submit", event => {
    event.preventDefault();
    perform(gameControl("保存连战设置", "rush_settings", {
      floors: Number($("rush-floors").value), skip_valuable: $("rush-skip").checked,
      event_mode: $("rush-event").value, auto_elite: $("rush-auto-elite").checked,
    }), { inline: true });
  });
  scheduleMaintenanceCheck();
  $("account-button").addEventListener("click", openAccount);
  $("nickname-button").addEventListener("click", renameDialog);
  $("account-dialog").addEventListener("close", () => $("change-form").reset());
  $("challenge-dialog").addEventListener("close", () => {
    if (!$("challenge-dialog").open) {
      clearTimeout(ui.timer);
      $("challenge-content").replaceChildren();
    }
  });
  $("log-button").addEventListener("click", () => showReply("行动记录", { text: historyLog.map(row => row.text).join("\n\n") }));
  document.querySelectorAll("#main-nav button").forEach(tab => tab.addEventListener("click", () => focusView(tab.dataset.view)));
  document.addEventListener("keydown", expeditionKeys);
  $("login-form").elements.username.addEventListener("input", () => showLoginType(false));
  secretForm("login-form", "/auth/login", async data => {
    showLoginType(false);
    await applySession(data.account);
  }, {
    prepare: (fields, form) => { fields.remember = form.elements.remember.checked; },
    onError: error => { if (error.code === "ambiguous_login") showLoginType(true); },
  });
  secretForm("register-form", "/auth/register", async data => {
    await applySession(data.account);
    showChallenge(data.challenge, "bind");
  });
  secretForm("bind-form", "/auth/bind", data => showChallenge(data.challenge, "bind"));
  secretForm("reset-form", "/auth/reset", data => showChallenge(data.challenge, "reset"));
  secretForm("change-form", "/auth/change", data => {
    closeDialog("account-dialog");
    showChallenge(data.challenge, "change");
  }, { errorId: "account-error" });
  enableSwipes();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") checkMaintenance();
    if (document.visibilityState === "visible" && ui.account?.bound && !ui.busy && !$("game-dialog").open) refresh();
  });
  request("/auth/session?game_state=1").then(data => applySession(data.account, data)).catch(error => {
    applySession(null);
    setError("auth-error", error.message);
  });
}

setup();
