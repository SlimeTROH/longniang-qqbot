"use strict";

const expeditionMoves = {
  down: ["深入一层", "E"],
  rush: ["连战", "Shift+E"],
  fight: ["攻击", "F"],
  flee: ["脱战", ""],
  extract: ["撤离", "R"],
  flare: ["呼叫支援", ""],
  rob: ["带走遗物", ""],
  rescue: ["寄还遗物", ""],
  leave: ["离开", ""],
};
const localMoves = new Set(["bag", "field", "battle_log", "save_box", "swap_box", "use"]);
const adventureOutcomes = { success: "成功", failure: "失败", ignored: "已无视" };

function gameControl(label, command, params = {}) {
  return { label, action: command, control: { command, params } };
}

function hotkeyLabel(key) {
  return key === "Shift+E" ? "⇧ E" : key;
}

function stationButton(choice, style = "", key = "", options = {}) {
  const revision = ui.state.revision;
  const control = button(choice.label, () => perform(choice, { revision, ...options }), style, {
    "data-game-action": choice.action, "aria-keyshortcuts": key || null,
  });
  if (choice.description) control.append(node("small", { className: "action-note", text: choice.description }));
  if (key) control.append(node("kbd", { text: hotkeyLabel(key), "aria-hidden": "true" }));
  return control;
}

function stationPickups(run) {
  const result = run.floor_result?.floor === run.floor ? run.floor_result : null;
  const items = [...(result?.loot || []), ...(result?.field || [])];
  const area = $("station-pickups");
  area.replaceChildren(
    node("div", { className: "pickup-heading" },
      node("h2", { text: "本层拾取" }),
      result && result.title !== "本层搜获" ? node("span", { text: result.title }) : null,
      items.length ? node("span", { className: "pickup-count", text: format(items.reduce((total, item) => total + (item.quantity ?? 1), 0)) + " 件" }) : null),
    items.length ? node("ul", { className: "pickup-grid" }, ...items.map(item => {
      const fieldItem = item.rarity === "field";
      const trigger = button("", () => {
        if (item.banked) {
          const stock = ui.state.warehouse?.find(entry => entry.id === item.id);
          window.ElevatorInventory.showItem(inventoryContext(), stock || item, "warehouse", trigger);
          return;
        }
        const bag = fieldItem ? run.field : run.bag;
        const offer = fieldItem && run.field_offer?.id === item.id ? run.field_offer : null;
        const matched = offer ? { ...offer, actions: offer.choices } : bag?.find(entry => entry.id === item.id &&
          (fieldItem || (entry.value === item.value && entry.name === item.name)));
        const secured = fieldItem ? null : run.safe?.find(entry =>
          entry.id === item.id && entry.value === item.value && entry.name === item.name);
        window.ElevatorInventory.showItem(inventoryContext(), { ...item, ...(matched || secured) },
          fieldItem ? "field" : secured && !matched ? "safe" : "bag", trigger);
      }, "pickup-tile", { "data-rarity": item.rarity });
      trigger.append(node("strong", { text: item.name }),
        node("small", { text: [
          fieldItem ? "局内道具" : rarityNames[item.rarity] || "物资",
          "×" + format(item.quantity ?? 1),
          ...(item.banked ? ["已入仓"] : fieldItem || item.value == null ? [] : [format(item.value) + " 金"]),
        ].join(" · ") }));
      return node("li", {}, trigger);
    }))
      : node("p", { className: "empty pickup-empty", text: result?.star || (result ? "本层暂未拾取物品。" : "本层暂无拾取记录。继续深入后会显示新拾取的物品。") }),
    ...(items.length && result?.star ? [node("p", { className: "item-facts", text: result.star })] : []),
  );
}

function stationEncounter(run) {
  const { event } = run;
  const enemy = event?.enemy;
  const panel = $("event-panel");
  panel.hidden = event?.kind === "ready" || !event;
  if (panel.hidden) {
    panel.replaceChildren();
    return;
  }
  if (enemy) {
    panel.replaceChildren(...[
      node("div", { className: "enemy-main" },
        node("h2", { text: enemy.name }),
        node("span", { className: "tag" + (enemy.boss || enemy.ambush ? " hot" : ""), text: enemy.boss ? "首领" : enemy.ambush ? "伏击" : enemy.elite ? "精英" : "普通" })),
      meter("敌方生命", enemy.hp, enemy.max_hp, "enemy"),
      node("div", { className: "enemy-attributes" }, ...["atk", "def", "agi"].filter(k => enemy[k] !== undefined)
        .map(k => node("span", { text: `${statNames[k]} ${format(enemy[k])}` }))),
      event.boss_rules ? node("p", { className: "item-facts",
        text: `已交锋 ${format(enemy.rounds_fought || 0)} 回合 · 每 ${event.boss_rules.exchange} 回合重击 ×${event.boss_rules.heavy}，随后可用药整备`
          + (run.simulation || run.prophecy ? "" : ` · 红装 ${(event.boss_rules.red_gear_rate * 100).toFixed(2)}% · 闪耀 ${(event.boss_rules.shining_rate * 100).toFixed(2)}%`) }) : enemy.elite ?
            node("p",{className:"item-facts",text:`精英交锋 ${enemy.rounds_fought || 0}/16 回合 · 第 8 回合可整备`}) : null,
      run.signal ? node("p", { className: "signal-status", text: "等待群友支援" }) : null,
    ].filter(Boolean));
  } else {
    panel.replaceChildren(...[node("h2", { text: event.name || eventNames[event.kind] }),
      event.text ? node("p", { text: event.text }) : null].filter(Boolean));
  }
}

function expeditionChoices(state) {
  const controls = state.controls || (state.actions || []).filter(choice => !localMoves.has(choice.action))
    .map(choice => choice.control ? { ...choice.control, label: choice.label, description: choice.description } : {
      command: choice.action,
      params: choice.action === "event" ? { option: Number(choice.argument) } : {},
      label: choice.label,
      description: choice.description,
    });
  const choiceRank = control => {
    if (["down", "fight", "event", "rob"].includes(control.command)) return 0;
    if (control.command === "extract") return 2;
    return 1;
  };
  return controls.filter(control => !localMoves.has(control.command))
    .sort((a, b) => choiceRank(a) - choiceRank(b));
}

function loadoutToggle(mode) {
  ui.runPane = ui.runPane === mode ? "" : mode;
  renderScene();
  const next = $("run-secondary").querySelector(`[data-run-pane="${mode}"]`);
  if (next) next.focus({ preventScroll: true });
}

function loaderItem(item, choices, info = "", kind = "bag", canSwap = false) {
  const openDetails = () => window.ElevatorInventory.showItem(inventoryContext(), item, kind, details);
  const details = button("", openDetails, "run-item-detail");
  details.append(node("strong", { text: item.name }));
  if (info) details.append(node("small", { text: info }));
  const controls = choices.map(choice => stationButton(choice, "small", "", { inline: true }));
  if (canSwap) controls.push(button("换入安全箱", openDetails, "small"));
  return node("article", { className: "run-item", "data-rarity": item.rarity || "field" },
    details,
    node("div", { className: "run-item-actions" }, ...controls),
  );
}

function runBag(run) {
  const inventory = window.ElevatorInventory;
  const carried = inventory.carriedBag(run);
  const groups = inventory.bagGroups(inventoryContext(), carried, item => {
    if (item.kind === "field") return loaderFieldItem(item);
    const actions = (item.actions || []).filter(choice => ["use", "save_box", "field_use"].includes(choice.control?.command || choice.action))
      .map(choice => choice.control || choice.action === "field_use" ? choice : gameControl(choice.label, choice.action, { index: item.index }));
    const multiple = item.quantity > 1;
    const info = [multiple ? "×" + format(item.quantity) : null,
      item.value == null ? null : format(item.value) + (multiple ? " 金 / 件" : " 金")].filter(Boolean).join(" · ");
    const canSwap = item.can_protect && run.safe?.length > 0 && run.safe.length >= run.safe_capacity;
    return loaderItem(item, actions, info, "bag", canSwap);
  }, "run-items", "station");
  const secured = (run.safe || []).map(item => loaderItem(item,
    (item.actions || []).filter(choice => choice.action === "take_box"),
    format(item.value) + " 金", "safe"));
  return [
    node("h3", { text: "背包" }),
    groups.length ? node("div", { className: "bag-rarities" }, ...groups) : node("p", { className: "empty", text: "背包为空。" }),
    node("h3", { text: `安全箱 ${format(run.safe?.length || 0)} / ${format(run.safe_capacity)}` }),
    secured.length ? node("div", { className: "run-items" }, ...secured) : node("p", { className: "empty", text: "安全箱为空。" }),
  ];
}

function loaderFieldItem(item) {
  return loaderItem(item, item.actions || [],
    [`×${format(item.quantity ?? 1)}`, item.description].filter(Boolean).join(" · "), "field");
}

function runField(run) {
  const effects = window.ElevatorInventory.activeFieldItems(run).map(item =>
    loaderItem(item, item.actions, [item.status, item.description].filter(Boolean).join(" · "), "field"));
  const carried = (run.field || []).map(loaderFieldItem);
  return [
    ...(effects.length ? [node("h3", { text: "生效中" }), node("div", { className: "run-items" }, ...effects)] : []),
    node("h3", { text: "临时道具" }),
    carried.length ? node("div", { className: "run-items" }, ...carried) : node("p", { className: "empty", text: "本局暂未拾取临时道具。" }),
  ];
}

function settlementLoot(items, label) {
  if (!items?.length) return null;
  return node("div", { className: "settlement-rewards" }, node("h3", { text: label }),
    node("ul", { className: "settlement-lines" }, ...items.map(item =>
      node("li", {}, node("strong", { text: item.name }),
        node("span", { text: [
          item.rarity === "field" ? "局内道具" : rarityNames[item.rarity],
          "×" + format(item.quantity ?? 1),
          item.banked ? "已入仓" : item.value == null || item.rarity === "field" ? null : format(item.value) + " 金",
        ].filter(Boolean).join(" · ") })),
    )),
  );
}

function battleDetails(battle) {
  const outcome = { victory: "胜利", defeat: "战败", draw: "仍在交战" }[battle.outcome] || "战斗结算";
  const stats = [
    ["回合", battle.rounds], ["造成伤害", battle.dealt], ["受到伤害", battle.taken],
    ["暴击", battle.crits], ["闪避", battle.dodges],
  ].filter(([, value]) => value != null);
  const rounds = (battle.log || []).filter(line => typeof line === "string" && line.trim());
  return [
    node("h2", { text: [outcome, battle.enemy].filter(Boolean).join(" · ") }),
    node("div", { className: "battle-stats" }, ...stats.map(([label, value]) => infoPair(label, format(value)))),
    settlementLoot(battle.loot, "战利品"),
    rounds.length ? node("details", { className: "settlement-rounds", open: "" },
      node("summary", { text: "逐回合战报" }),
      node("ul", { className: "settlement-lines", tabindex: "0", "aria-label": "逐回合战报明细" },
        ...rounds.map(text => node("li", { text })))) : null,
  ];
}

function runBattle(run) {
  const battle = run.floor_result?.floor === run.floor ? run.floor_result.battle : null;
  return battle ? battleDetails(battle).filter(Boolean)
    : [node("p", { className: "empty", text: "本层还没有战斗记录。" })];
}

function adventureDetails(adventure) {
  const floors = adventure.floor == null ? "" : "F" + format(adventure.floor);
  const destination = floors && adventure.destination != null && adventure.destination !== adventure.floor
    ? floors + " 到 F" + format(adventure.destination) : floors;
  const result = adventureOutcomes[adventure.outcome];
  const lines = (adventure.lines || []).filter(line => typeof line === "string" && line.trim());
  return [
    node("h2", { text: adventure.name || "奇遇结算" }),
    node("p", { className: "settlement-meta", text: [result, adventure.option, destination].filter(Boolean).join(" · ") }),
    lines.length ? node("ul", { className: "settlement-lines" }, ...lines.map(text => node("li", { text }))) : null,
    settlementLoot(adventure.loot, "获得物品"),
  ];
}

function stationFieldOffer(run) {
  const offer = run.field_offer;
  if (!offer) return null;
  const slots = window.ElevatorInventory.fieldSlots(run);
  const choices = (offer.choices || []).map(choice => {
    const slot = choice.control?.params?.slot;
    const existing = slots[slot - 1];
    const description = slot ? [`当前：${existing?.name || "空槽"}`, existing?.description].filter(Boolean).join(" · ") : choice.description;
    return stationButton({ ...choice, description }, slot ? "primary" : "secondary", "", { inline: true });
  });
  return node("section", { className: "panel settlement-card", "aria-label": "待选临时装备" },
    node("h2", { text: `发现临时装备 · ${offer.name}` }),
    offer.description ? node("p", { className: "settlement-lines", text: offer.description }) : null,
    node("p", { className: "settlement-meta", text: "选一个槽位穿戴，或放弃这件装备，再继续本层行动。" }),
    node("div", { className: "action-grid", role: "group", "aria-label": "选择临时装备槽位" }, ...choices));
}

function stationSettlement(run) {
  const current = ui.stationSettlement?.revision === ui.state.revision ? ui.stationSettlement : null;
  const sameFloor = run?.floor_result?.floor === run?.floor;
  const battle = current?.battle || (sameFloor ? run?.floor_result?.battle : null);
  const adventure = current?.adventure || (run && (run.adventure?.destination ?? run.adventure?.floor) === run.floor
    ? run.adventure : null);
  const offer = run ? stationFieldOffer(run) : null;
  const stars = (ui.state.controls || []).filter(c => c.command === "prophecy_pick");
  const rush = current?.rush;
  const target = $("station-settlement");
  target.replaceChildren(...[
    stars.length ? node("section", { className: "panel settlement-card", "aria-label": "本层星匣" },
      node("h2", { text: "本层星匣 · 任选一件" }),
      node("p", { className: "settlement-meta", text: "嵌入后整局生效，同款升阶，离层即错过。" }),
      node("div", { className: "action-grid" }, ...stars.map(c =>
        stationButton(gameControl(c.label, c.command, c.params), "primary", "", { inline: true })))) : null,
    offer,
    rush ? node("details", {className:"panel settlement-card", "aria-label":"连战记录"},
      node("summary",{text:`连战记录 · ${rush.floors.length} 层 · ${rush.reason}`}),
      node("ol", {className:"settlement-lines"}, ...rush.floors.map(row => {
        const loot = [...row.loot.map(item=>item.name), ...row.field];
        const battle = row.battle;
        const adventure = row.adventure;
        const encounter = [
          battle ? `${battle.enemy} · ${battle.rounds} 回合 · 造成 ${battle.dealt} / 承受 ${battle.taken}` : "",
          adventure ? [adventure.name, adventure.outcome === "ignored" ? "" : adventure.option, adventureOutcomes[adventure.outcome],
            ...(adventure.lines || [])].filter(Boolean).join(" · ") : "",
        ].filter(Boolean).join(" · ");
        return node("li",{text:[`F${row.floor}`, encounter || row.title,
          loot.length ? "拾取 "+loot.join("、") : ""].filter(Boolean).join(" · ")});
      }))) : null,
    battle ? node("section", { className: "panel settlement-card", "aria-label": "战斗结算" }, ...battleDetails(battle)) : null,
    adventure ? node("section", { className: "panel settlement-card", "aria-label": "奇遇结算" }, ...adventureDetails(adventure)) : null,
  ].filter(Boolean));
  target.hidden = !offer && !battle && !adventure && !rush;
}

function runTools(run) {
  const hasEffects = window.ElevatorInventory.activeFieldItems(run).length > 0;
  const fieldLabel = hasEffects ? "临装生效中" : `临装 ${(run.field || []).reduce((count, item) => count + (item.quantity || 0), 0)}`;
  const panes = [
    ["bag", `背包 ${(run.bag || []).length + (run.field || []).length}`, "Q"],
    ["field", fieldLabel, "T"],
    ["battle", "战报", "B"],
  ];
  const controls = panes.map(([mode, label, key]) => {
    const el = button(label, () => loadoutToggle(mode), "quiet", {
      "data-run-pane": mode, "aria-expanded": String(ui.runPane === mode), "aria-controls": "run-loadout", "aria-keyshortcuts": key,
    });
    el.append(node("kbd", { text: key, "aria-hidden": "true" }));
    return el;
  });
  $("run-secondary").replaceChildren(...controls);
  const loadout = $("run-loadout");
  loadout.hidden = !ui.runPane;
  const render = { bag: runBag, field: runField, battle: runBattle }[ui.runPane];
  if (render) loadout.replaceChildren(...render(run));
}

function mapCard(map, player, selected) {
  const name = map.name || map.id;
  const locked = map.unlocked === false || (player.extracts ?? 0) < (map.extracts ?? 0);
  const fee = Number(map.entry) || 0;
  const detail = locked ? `撤离 ${map.extracts} 次解锁` : fee ? `${format(fee)} 金入场` : "免入场费";
  const select = button("", () => {
    ui.selectedMap = map.id;
    const panel = $("station-map");
    panel.querySelectorAll("[data-zone]").forEach(el => el.setAttribute("aria-pressed", String(el.dataset.zone === map.id)));
    trialPlan(map);
  }, "map-selector", { "data-zone": map.id, "aria-pressed": String(selected) });
  select.append(node("span", { className: "map-symbol", "aria-hidden": "true", text: mapSymbols[map.id] || "▦" }),
    node("span", {}, node("strong", { text: name }), node("small", { text: detail })));
  return select;
}

function trialPlan(map) {
  const { player, supplies } = ui.state;
  const locked = map.unlocked === false || player.extracts < map.extracts;
  const fee = Number(map.entry) || 0;
  const block = locked ? "地图尚未解锁" : player.tickets <= 0 ? "通行证不足" : player.gold < fee ? "金币不足" : "";
  const launch = button("下潜", () => begin(false), "primary", { "aria-keyshortcuts": "E", "data-game-action": "start" });
  launch.append(node("kbd", { text: "E", "aria-hidden": "true" }));
  launch.disabled = Boolean(block);
  launch.title = block;
  const simulate = button("模拟", () => begin(true), "secondary", { "data-game-action": "simulate" });
  async function begin(simulation) {
    const nickname = $("launch-nickname");
    if (nickname && !nickname.reportValidity()) return;
    const focus = ui.focusDraft || [];
    if (!simulation && (focus.filter(Boolean).length!==2 || focus[0]===focus[1])) {
      explain("请选择两套不同的追寻祝福。",true); return;
    }
    const stock = simulation ? [] : [...quantities.values()].map(item =>
      ({ id: item.id, quantity: Math.min(item.quantity, ui.supplies[item.id] || 0) })).filter(item => item.quantity > 0);
    const params = { map_id: map.id, ...(!simulation ? {focus} : {}), ...(stock.length ? { supplies: stock } : {}),
      ...(nickname?.value.trim() ? { nickname: nickname.value.trim() } : {}) };
    const anim = playDescentEntrance(map, simulation);
    const ok = await perform(gameControl(simulation ? "模拟" : "下潜", simulation ? "simulate" : "start", params), { inline: true });
    if (!ok) {
      cancelDescentEntrance();
      return;
    }
    await anim;
  }
  const quantities = new Map();
  for (const item of supplies || []) {
    const count = (quantities.get(item.id)?.quantity || 0) + item.quantity;
    quantities.set(item.id, { ...item, quantity: count });
  }
  const stash = Array.from(quantities.values()).map(item => {
    const limit = Math.min(item.quantity, 50);
    const input = node("input", { type: "number", inputmode: "numeric", min: 0, max: limit, value: Math.min(ui.supplies[item.id] || 0, limit), "aria-label": `${item.name}携带数量` });
    input.addEventListener("input", () => {
      const amount = Math.max(0, Math.min(limit, Math.floor(Number(input.value) || 0)));
      ui.supplies[item.id] = amount;
    });
    input.addEventListener("change", () => { input.value = ui.supplies[item.id] || 0; });
    return node("label", { className: "supply-choice" }, node("span", { text: `${item.name} · ${item.quantity}` }), input);
  });
  $("launch-plan").replaceChildren(...[
    !player.name_confirmed ? node("label", { className: "launch-nickname" }, "游戏昵称",
      node("input", { id: "launch-nickname", maxlength: 16, value: (player.nickname || "").slice(0, 16), autocomplete: "off" })) : null,
    focusPicker(),
    stash.length ? node("details", { className: "launch-supplies" }, node("summary", { text: "选择携带补给" }), node("div", { className: "supply-grid" }, ...stash)) : null,
    node("div", { className: "launch-controls" }, launch, simulate),
    block ? node("p", { className: "empty", text: block }) : null,
  ].filter(Boolean));
}

function focusPicker(saveNow = false) {
  const h = ui.state.heirlooms;
  const selected = ui.focusDraft || h.focus || [];
  const selects = [0,1].map(i => {
    const el=node("select",{"aria-label":`第 ${i+1} 套追寻祝福`},
      node("option",{value:"",text:`选择第 ${i+1} 套`}),
      ...Object.entries(h.sets).map(([id,s])=>node("option",{value:id,text:s.name})));
    el.value=selected[i] || "";
    return el;
  });
  function sync() {
    ui.focusDraft=selects.map(el=>el.value);
    for (const [i,el] of selects.entries()) {
      for(const option of el.options) option.disabled=Boolean(option.value && option.value===selects[1-i].value);
    }
  }
  selects.forEach(el=>el.addEventListener("change",sync));
  sync();
  return node("div",{className:"heirloom-focus"},node("strong",{text:saveNow?"本局补选追寻":"追寻祝福"}),
    ...selects,saveNow?button("确认追寻",()=>perform(gameControl("确认追寻","pursue",{focus:ui.focusDraft}),{inline:true})):null);
}

let _cancelDescent = null;

function cancelDescentEntrance() {
  if (_cancelDescent) { _cancelDescent(false); _cancelDescent = null; }
}

function triggerDescentUnfurl() {
  document.body.classList.remove("descent-unfurling");
  void document.body.offsetWidth;
  document.body.classList.add("descent-unfurling");
  setTimeout(() => document.body.classList.remove("descent-unfurling"), 1400);
}

function playDescentEntrance(map, simulation = false) {
  const portal = $("descent-portal");
  if (!portal) return Promise.resolve();
  cancelDescentEntrance();
  const mapId = map?.id || "mall";
  const mapName = map?.name || "禁区井道";
  portal.dataset.map = simulation ? "simulation" : mapId;
  const badgeEl = $("descent-mode-badge");
  const depthEl = $("descent-depth");
  const arrowEl = $("descent-arrow");
  const floorEl = $("descent-floor");
  const nameEl = $("descent-map-name");
  const statusEl = $("descent-status");
  const skipBtn = $("descent-skip");
  if (badgeEl) badgeEl.textContent = simulation ? "SIMULATION · 演习模拟" : "DIVE · 正式下潜";
  if (nameEl) nameEl.textContent = mapName;
  if (arrowEl) arrowEl.textContent = "▼";
  if (floorEl) floorEl.textContent = "G";
  if (depthEl) depthEl.textContent = "▼ -0 m";
  if (statusEl) statusEl.textContent = "DOOR SEALING · 轿厢门锁死";
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  portal.hidden = false;
  portal.classList.remove("active", "phase-drop", "phase-brake", "phase-open", "leaving");
  void portal.offsetWidth;
  portal.classList.add("active");
  if (reduced) {
    if (floorEl) floorEl.textContent = "F1";
    return new Promise(resolve => setTimeout(() => {
      portal.classList.add("phase-open", "leaving");
      triggerDescentUnfurl();
      setTimeout(() => { portal.classList.remove("active", "phase-open", "leaving"); portal.hidden = true; resolve(); }, 220);
    }, 320));
  }
  return new Promise(resolve => {
    const timers = [];
    let rafId = 0;
    let settled = false;
    const finish = (unfurl = true) => {
      if (settled) return;
      settled = true;
      _cancelDescent = null;
      timers.forEach(clearTimeout);
      cancelAnimationFrame(rafId);
      if (skipBtn) skipBtn.onclick = null;
      if (unfurl) {
        portal.classList.add("phase-open", "leaving");
        triggerDescentUnfurl();
        setTimeout(() => {
          portal.classList.remove("active", "phase-drop", "phase-brake", "phase-open", "leaving");
          portal.hidden = true;
          resolve();
        }, 380);
      } else {
        portal.classList.remove("active", "phase-drop", "phase-brake", "phase-open", "leaving");
        portal.hidden = true;
        resolve();
      }
    };
    _cancelDescent = finish;
    if (skipBtn) skipBtn.onclick = () => finish(true);
    // Act 2: Heavy downward drop (0.42s -> 1.95s)
    timers.push(setTimeout(() => {
      portal.classList.add("phase-drop");
      if (statusEl) statusEl.textContent = "DESCENDING · 井道高速下潜";
      const t0 = performance.now();
      const dropMs = 1520;
      const floorSteps = ["G", "B1", "B2", "B3", "B5", "B8", "F1"];
      function tick(now) {
        if (settled || portal.hidden) return;
        const p = Math.min(1, (now - t0) / dropMs);
        const eased = p < 0.75 ? Math.pow(p / 0.75, 1.5) * 0.86 : 0.86 + 0.14 * (1 - Math.pow(1 - (p - 0.75) / 0.25, 2));
        const depth = Math.round(eased * 140);
        if (depthEl) depthEl.textContent = `▼ -${depth} m`;
        if (floorEl) floorEl.textContent = floorSteps[Math.min(floorSteps.length - 1, Math.floor(p * floorSteps.length))];
        if (p < 1) rafId = requestAnimationFrame(tick);
      }
      rafId = requestAnimationFrame(tick);
    }, 420));
    // Act 3: Hydraulic brake jolt + door seam glow (1.95s)
    timers.push(setTimeout(() => {
      portal.classList.remove("phase-drop");
      portal.classList.add("phase-brake");
      if (arrowEl) arrowEl.textContent = "■";
      if (floorEl) floorEl.textContent = "F1";
      if (depthEl) depthEl.textContent = "■ -140 m";
      if (statusEl) statusEl.textContent = "ARRIVED · 液压制动完成，准备开门";
    }, 1950));
    // Act 4: Twin steel doors slowly slide apart revealing F1 stage (2.55s -> 3.65s)
    timers.push(setTimeout(() => {
      if (settled) return;
      settled = true;
      _cancelDescent = null;
      if (statusEl) statusEl.textContent = "DOORS OPENING · 禁区大门开启";
      portal.classList.add("phase-open");
      triggerDescentUnfurl();
      timers.push(setTimeout(() => portal.classList.add("leaving"), 520));
      timers.push(setTimeout(() => {
        cancelAnimationFrame(rafId);
        if (skipBtn) skipBtn.onclick = null;
        portal.classList.remove("active", "phase-drop", "phase-brake", "phase-open", "leaving");
        portal.hidden = true;
        resolve();
      }, 1050));
    }, 2550));
  });
}

function triggerProphecyUnfurl() {
  document.body.classList.remove("prophecy-unfurling");
  void document.body.offsetWidth;
  document.body.classList.add("prophecy-unfurling");
  setTimeout(() => document.body.classList.remove("prophecy-unfurling"), 1500);
}

function playProphecyEntrance(prophecy) {
  const portal = $("prophecy-portal");
  if (!portal) return Promise.resolve();
  $("portal-title").textContent = prophecy?.title || "来自群星的寓言集";
  $("portal-subtitle").textContent = [prophecy?.week, prophecy?.name, "F1 / " + format(prophecy?.max_floor || 2000)].filter(Boolean).join(" · ");
  const altEl = $("portal-altitude");
  const tagEl = $("portal-phase-tag");
  const gEl = $("portal-gforce");
  const velEl = $("portal-vel");
  const skipBtn = $("portal-skip");
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;

  portal.hidden = false;
  portal.classList.remove("active", "phase-boost", "phase-break", "phase-reveal", "leaving");
  void portal.offsetWidth;
  portal.classList.add("active");

  if (reduced) {
    if (altEl) altEl.textContent = "✦ 星穹轨道 · F1";
    return new Promise(resolve => setTimeout(() => {
      portal.classList.add("leaving");
      triggerProphecyUnfurl();
      setTimeout(() => { portal.classList.remove("active", "leaving"); portal.hidden = true; resolve(); }, 180);
    }, 450));
  }

  return new Promise(resolve => {
    const timers = [];
    let rafId = 0;
    let done = false;

    const cleanup = () => {
      timers.forEach(clearTimeout);
      cancelAnimationFrame(rafId);
      if (skipBtn) skipBtn.onclick = null;
      portal.classList.remove("active", "phase-boost", "phase-break", "phase-reveal", "leaving");
      portal.hidden = true;
    };

    const finishEarly = () => {
      if (done) return;
      done = true;
      timers.forEach(clearTimeout);
      cancelAnimationFrame(rafId);
      portal.classList.add("leaving");
      triggerProphecyUnfurl();
      setTimeout(() => { cleanup(); resolve(); }, 320);
    };

    if (skipBtn) skipBtn.onclick = finishEarly;

    const startTs = performance.now();
    const ascentMs = 2650;
    function tick(now) {
      if (done || portal.hidden) return;
      const elapsed = now - startTs;
      const t = Math.min(1, elapsed / ascentMs);
      // Heavy launch curve: slow weighty ignition -> violent upward acceleration -> weightless orbit
      const eased = t < 0.35 ? 0.16 * Math.pow(t / 0.35, 2.2) : 0.16 + 0.84 * Math.pow((t - 0.35) / 0.65, 1.85);
      const altMeters = Math.round(eased * 120000);
      const vel = t < 1 ? (0.4 + Math.pow(t, 2.1) * 11.4) : 0;
      const gLoad = t < 0.35 ? (1.2 + (t / 0.35) * 2.4) : t < 0.96 ? (3.6 + ((t - 0.35) / 0.61) * 3.8) : 0.0;

      if (altEl) {
        if (t < 0.38) altEl.textContent = `↑ 重力井点火 · ${altMeters.toLocaleString()} m`;
        else if (t < 0.94) altEl.textContent = `↑ 突破大气层 · ${altMeters.toLocaleString()} m`;
        else altEl.textContent = "✦ 越过卡门线 · 星穹视界已展开";
      }
      if (tagEl) tagEl.textContent = t < 0.38 ? "IGNITION · HEAVY LIFT" : t < 0.94 ? "OVERDRIVE · ASCENT" : "ORBIT · WEIGHTLESS";
      if (gEl) gEl.textContent = `LOAD ${gLoad.toFixed(1)} G`;
      if (velEl) velEl.textContent = t < 1 ? `VEL ${vel.toFixed(1)} km/s` : "SYNC · LOCKED";

      if (t < 1) rafId = requestAnimationFrame(tick);
    }
    rafId = requestAnimationFrame(tick);

    // Act 2: Heavy overdrive boost (1.05s)
    timers.push(setTimeout(() => portal.classList.add("phase-boost"), 1050));
    // Act 3: Break atmosphere — shockwave, silence, giant eclipse + astrolabe loom in (2.55s)
    timers.push(setTimeout(() => portal.classList.add("phase-break"), 2550));
    // Act 4: Title solemn reveal over eclipse (3.05s)
    timers.push(setTimeout(() => portal.classList.add("phase-reveal"), 3050));
    // Act 5: Portal slowly parts & dissolves while the Fable stage unfurls smoothly underneath (4.55s -> 5.45s)
    timers.push(setTimeout(() => {
      if (done) return;
      done = true;
      portal.classList.add("leaving");
      triggerProphecyUnfurl();
      timers.push(setTimeout(() => { cleanup(); resolve(); }, 880));
    }, 4550));
  });
}

function showProphecyDetails(prophecy) {
  if (!prophecy) return;
  const gearList = node("ul", { className: "prophecy-detail-list" },
    ...(prophecy.gear || []).map(g => node("li", {},
      node("strong", { text: `[${rarityNames[g.rarity] || g.rarity} · ${g.slot}] ${g.name}` }),
      node("span", { text: " · " + Object.entries(g.stats || {}).map(([k, v]) => `${statNames[k] || k}${v >= 0 ? "+" : ""}${v}`).join(" / ") }),
      g.affixes?.length ? node("small", { text: " ｜ 词条：" + g.affixes.map(a => a.name).join("、") }) : null,
    )));
  function boardBlock(title, rows) {
    if (!rows?.length) return node("div", { className: "prophecy-board-box" }, node("h3", { text: title }), node("p", { className: "empty", text: "暂无留名记录。" }));
    const medals = ["🥇", "🥈", "🥉"];
    return node("div", { className: "prophecy-board-box" },
      node("h3", { text: title }),
      node("ol", { className: "prophecy-rank-list" }, ...rows.map((r, i) => node("li", {},
        node("span", { className: "rank-medal", text: medals[i] || `#${i + 1}` }),
        node("strong", { text: r.nickname }),
        node("span", { className: "rank-score", text: `通过 F${format(r.cleared_floor)}（推进 F${format(r.max_floor)}）` }),
        node("small", { text: `${r.cleared_floor >= (prophecy.max_floor || 2000) ? "👑登顶" : r.extracted ? "安全撤离" : "力竭止步"} · 挑战 ${format(r.attempts)} 次` }),
      ))));
  }
  const s = prophecy.stats || {};
  $("game-dialog-content").replaceChildren(
    node("div", { className: "prophecy-modal-head" },
      node("span", { className: "eyebrow", text: `✦ ${prophecy.week} · ${prophecy.name}` }),
      node("h2", { text: prophecy.title || "来自群星的寓言集" }),
      node("p", { text: prophecy.omen || "" }),
    ),
    node("p", { className: "item-facts", text: `统一面板：生命 ${format(s.hp)} · 攻击 ${format(s.atk)} · 防御 ${format(s.def)} · 敏捷 ${format(s.agi)} · 幸运 ${format(s.luk)} ｜ 上限 F${format(prophecy.max_floor)} ｜ 0 费用 · 零带出` }),
    node("h3", { text: "本周固定 4 槽装备" }), gearList,
    node("p", { className: "item-facts", text: `✨ 固定祝福（${(prophecy.blessings || []).length}槽）：${(prophecy.blessing_names || []).join("、") || "无"}` }),
    node("p", { className: "item-facts", text: `🌈 固定虹彩（${(prophecy.rainbows || []).length}件）：${(prophecy.rainbow_names || []).join("、") || "无"}` }),
    node("p", { className: "item-facts", text: `🎒 统一补给：${(prophecy.supplies || []).map(x => `${x.name}×${x.quantity}`).join("、") || "无"}` }),
    node("p", { className: "item-facts", text: prophecy.star_rule }),
    ...(ui.state.run?.star_relics || []).map(r => node("p", { className: "item-facts",
      text: `${r.name} · ${r.rank} 阶 · ${r.label} +${format(r.bonus)}` })),
    boardBlock(`本周全服实时榜（${prophecy.week}）`, prophecy.leaderboard),
    prophecy.last_week ? boardBlock(`上周最终结算榜（${prophecy.last_week.week} · ${prophecy.last_week.name}）`, prophecy.last_week.leaderboard) : null,
  );
  openDialog("game-dialog");
}

function prophecyBanner(prophecy, player) {
  if (!prophecy) return null;
  const unlock = prophecy.unlock || {};
  const unlocked = Boolean(unlock.unlocked);
  const top = prophecy.leaderboard?.[0];
  const me = prophecy.my_record;
  const enterBtn = button("✦ 踏入星兆", async () => {
    const nickname = $("launch-nickname");
    if (nickname && !nickname.reportValidity()) return;
    const params = nickname?.value.trim() ? { nickname: nickname.value.trim() } : {};
    const anim = playProphecyEntrance(prophecy);
    const ok = await perform(gameControl("踏入星兆", "prophecy_start", params), { inline: true });
    await anim;
    if (!ok) $("prophecy-portal").hidden = true;
  }, "prophecy-enter-btn", { "data-game-action": "prophecy_start" });
  enterBtn.disabled = !unlocked;
  if (!unlocked) enterBtn.title = `需集齐全部红、金藏品图鉴（${unlock.owned}/${unlock.total}）`;
  const detailBtn = button("星兆详情与周榜", () => showProphecyDetails(prophecy), "prophecy-ghost-btn");
  const chips = [
    `⚔️ ${(prophecy.gear || []).map(g => g.name).join(" / ")}`,
    `✨ 祝福 ${(prophecy.blessings || []).length} 件 · ${(prophecy.blessing_names || []).slice(0, 2).join("、")}${(prophecy.blessing_names || []).length > 2 ? "…" : ""}`,
    `🌈 虹彩 ${(prophecy.rainbows || []).length} 件 · ${(prophecy.rainbow_names || []).join("、") || "无"}`,
    `🏆 上限 F${format(prophecy.max_floor || 2000)} · 零费用 · 零带出`,
  ];
  return node("section", { className: "prophecy-card", "aria-label": "来自群星的寓言集" },
    node("div", { className: "prophecy-bg", "aria-hidden": "true" },
      node("span", { className: "prophecy-orb orb-a" }),
      node("span", { className: "prophecy-orb orb-b" }),
      node("span", { className: "prophecy-sigil" }),
    ),
    node("div", { className: "prophecy-body" },
      node("div", { className: "prophecy-head" },
        node("span", { className: "prophecy-badge", text: `✦ 每周固定随机高难 · ${prophecy.week}` }),
        node("span", { className: "prophecy-status" + (unlocked ? " unlocked" : ""),
          text: unlocked ? `✦ 资格已解锁（红/金图鉴 ${unlock.owned}/${unlock.total}）` : `🔒 红/金藏品图鉴 ${unlock.owned}/${unlock.total}` }),
      ),
      node("h2", { className: "prophecy-title", text: prophecy.title || "来自群星的寓言集" }),
      node("p", { className: "prophecy-sub", text: `「${prophecy.name}」— ${prophecy.omen}` }),
      node("div", { className: "prophecy-chips" }, ...chips.map(text => node("span", { className: "prophecy-chip", text }))),
      node("div", { className: "prophecy-meta-row" },
        top ? node("span", { text: `👑 本周榜首：${top.nickname}（通过 F${format(top.cleared_floor)}）` }) : node("span", { text: "🌌 本周尚无人留名，虚位以待" }),
        me ? node("span", { text: `🏅 我的纪录：第 ${me.rank} 名 · 通过 F${format(me.cleared_floor)}` }) : null,
      ),
      !unlocked && unlock.missing_preview?.length
        ? node("p", { className: "prophecy-lock-note", text: `还差 ${unlock.missing_count} 件红/金藏品：${unlock.missing_preview.join("、")}${unlock.missing_count > unlock.missing_preview.length ? " 等" : ""}` })
        : null,
      node("div", { className: "prophecy-actions" }, enterBtn, detailBtn),
    ),
  );
}

function renderScene() {
  const { run, maps, player, signals, prophecy } = ui.state;
  const mapRows = Array.isArray(maps) ? maps : Object.values(maps || {});
  const event = run?.event;
  const boss = Boolean(event?.enemy?.boss);
  const isProphecy = Boolean(run?.prophecy);
  const tag = boss ? "首领房" : event ? eventNames[event.kind] || "探索中" : "地面大厅";
  const sceneMode = isProphecy ? "prophecy" : run?.simulation ? "simulation" : "normal";
  $("scene").dataset.event = boss ? "boss" : event?.kind || "surface";
  $("scene").dataset.mode = sceneMode;
  document.body.dataset.mode = sceneMode;
  const floorCap = $("floor-display").querySelector("span");
  if (floorCap) floorCap.textContent = isProphecy ? "星阶 ↑" : "楼层";
  $("scene").classList.toggle("active", Boolean(run));
  $("scene-label").replaceChildren(node("i"), document.createTextNode(
    isProphecy ? ` ✦ 星兆试炼 · ${run.prophecy_week} · ` + tag
      : run?.simulation ? " 模拟 · " + tag : " " + tag
  ));
  $("station-title").textContent = run ? run.map_name || mapRows.find(item => item.id === run.map)?.name || "探索中" : "选择下潜区域";
  $("scene-description").textContent = run?.radar_scan
    ? `📡 热成像雷达 · F${run.radar_scan.floor} 侦测到「${run.radar_scan.room}」`
    : isProphecy ? `${run.prophecy_name || ""} · ${run.next_floor || `下个首领在 F${run.next_boss}`}（上限 F${format(run.max_floor || 2000)} · 零带出）`
    : run ? run.next_floor || `下个首领在 F${run.next_boss}` : "每局消耗 1 张通行证。模拟免费。";
  $("rush-floors").value = String(player.rush_floors || 3);
  $("rush-event").value = player.rush_event_mode || "pause";
  $("rush-auto-elite").checked = Boolean(player.rush_auto_elite);
  $("rush-skip").checked = Boolean(player.rush_skip_valuable);
  $("floor-display").querySelector("strong").textContent = run ? (isProphecy ? `${format(run.floor)}/${format(run.max_floor || 2000)}` : format(run.floor)) : "G";
  $("station-vitals").hidden = !run;
  $("trip-controls").hidden = !run;
  $("station-map").hidden = Boolean(run);
  $("station-receipt").hidden = Boolean(run) || !ui.stationNotice;
  $("station-receipt").textContent = ui.stationNotice;
  $("run-notice").hidden = !run || !ui.stationNotice;
  $("run-notice").textContent = ui.stationNotice;
  stationSettlement(run);
  if (run) {
    $("station-vitals").replaceChildren(
      meter("生命", run.hp, run.max_hp),
      node("span", { text: `盾 ${format(run.shield ?? 0)}` }),
      node("span", { text: `${format(run.value)} 金` }),
      ...(isProphecy ? [button("本周星兆构筑与榜", () => showProphecyDetails(prophecy), "quiet small")] : []),
    );
    stationPickups(run);
    if (ui.state.heirlooms.needs_focus) $("station-vitals").append(focusPicker(true));
    stationEncounter(run);
    const choices = run.field_offer ? [] : expeditionChoices(ui.state).filter(c => c.command !== "prophecy_pick");
    $("run-actions").replaceChildren(...choices.map((control, index) => {
      const { command, params } = control;
      const [label, key] = expeditionMoves[command] || [control.label, ""];
      const choice = { ...gameControl(command === "fight" && boss ? "挑战首领" : ["flee","rush"].includes(command) ? control.label : label,
        command, params), description: control.description };
      return stationButton(choice, command === "extract" ? "danger" : index === 0 ? "primary" : "", key, { inline: true });
    }));
    runTools(run);
  } else if (mapRows.length) {
    const selected = mapRows.find(item => item.id === ui.selectedMap) || mapRows[0];
    ui.selectedMap = selected.id;
    $("station-map").replaceChildren(
      prophecyBanner(prophecy, player),
      node("div", { className: "zone-grid", role: "group", "aria-label": "探索地图" },
        ...mapRows.map(item => mapCard(item, player, item.id === selected.id))),
      node("div", { id: "launch-plan", className: "panel launch-plan" }),
    );
    trialPlan(selected);
  }
  const utilities = [
    variant("悬赏", "quests"), variant("本群排行", "rank"), variant("全服排行", "all_rank"),
    variant("✦ 寓言周榜", "prophecy_rank"), variant("首领规则", "help", "首领"), variant("道具手册", "field_guide"),
  ];
  if (!run && !ui.state.group_blessing_until) utilities.push(gameControl("开启金雨", "blessing"));
  const support = (signals || []).map(signal => ({
    ...signal.action,
    label: signal.nickname ? `支援 ${signal.nickname}` : signal.action.label,
    control: { command: "support", params: { owner_id: signal.action.argument } },
  }));
  $("station-tools").replaceChildren(...[
    ui.state.group_blessing_until ? node("p", { className: "signal-status", text: "绑定群金雨已开启" }) : null,
    resourceList([...support, ...utilities]),
  ].filter(Boolean));
}

function expeditionKeys(event) {
  if (!ui.state || ui.busy || event.repeat || event.altKey || event.metaKey || event.ctrlKey ||
      event.target.closest?.("input,textarea,select,[contenteditable],dialog") || document.querySelector("dialog[open]")) return;
  const key = event.key.toLowerCase();
  if (!event.shiftKey && /^[1-5]$/.test(key)) {
    event.preventDefault();
    focusView(navOrder[Number(key) - 1]);
    return;
  }
  if (ui.view !== "station") return;
  const action = key === "e" ? (ui.state.run ? event.shiftKey ? "rush" : "down" : "start")
    : key === "f" ? "fight" : key === "r" ? "extract" : "";
  const control = action ? $("view-station").querySelector(`button[data-game-action="${action}"]:not(:disabled)`)
    : key === "q" ? $("run-secondary").querySelector('[data-run-pane="bag"]')
    : key === "t" ? $("run-secondary").querySelector('[data-run-pane="field"]')
    : key === "b" ? $("run-secondary").querySelector('[data-run-pane="battle"]') : null;
  if (control && !control.closest("[hidden]")) {
    event.preventDefault();
    control.click();
  }
}
