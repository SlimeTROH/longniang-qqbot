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
      : node("p", { className: "empty pickup-empty", text: result ? "本层暂未拾取物品。" : "本层暂无拾取记录。继续深入后会显示新拾取的物品。" }),
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
          + (run.simulation ? "" : ` · 红装 ${(event.boss_rules.red_gear_rate * 100).toFixed(2)}% · 闪耀 ${(event.boss_rules.shining_rate * 100).toFixed(2)}%`) }) : enemy.elite ?
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
  const rush = current?.rush;
  const target = $("station-settlement");
  target.replaceChildren(...[
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
  function begin(simulation) {
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
    perform(gameControl(simulation ? "模拟" : "下潜", simulation ? "simulate" : "start", params), { inline: true });
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

function renderScene() {
  const { run, maps, player, signals } = ui.state;
  const mapRows = Array.isArray(maps) ? maps : Object.values(maps || {});
  const event = run?.event;
  const boss = Boolean(event?.enemy?.boss);
  const tag = boss ? "首领房" : event ? eventNames[event.kind] || "探索中" : "地面大厅";
  $("scene").dataset.event = boss ? "boss" : event?.kind || "surface";
  $("scene").classList.toggle("active", Boolean(run));
  $("scene-label").replaceChildren(node("i"), document.createTextNode(run?.simulation ? " 模拟 · " + tag : " " + tag));
  $("station-title").textContent = run ? run.map_name || mapRows.find(item => item.id === run.map)?.name || "探索中" : "选择下潜区域";
  $("scene-description").textContent = run?.radar_scan
    ? `📡 热成像雷达 · F${run.radar_scan.floor} 侦测到「${run.radar_scan.room}」`
    : run ? run.next_floor || `下个首领在 F${run.next_boss}` : "每局消耗 1 张通行证。模拟免费。";
  $("rush-floors").value = String(player.rush_floors || 3);
  $("rush-event").value = player.rush_event_mode || "pause";
  $("rush-auto-elite").checked = Boolean(player.rush_auto_elite);
  $("rush-skip").checked = Boolean(player.rush_skip_valuable);
  $("floor-display").querySelector("strong").textContent = run ? format(run.floor) : "G";
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
    );
    stationPickups(run);
    if (ui.state.heirlooms.needs_focus) $("station-vitals").append(focusPicker(true));
    stationEncounter(run);
    const choices = run.field_offer ? [] : expeditionChoices(ui.state);
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
    $("station-map").replaceChildren(node("div", { className: "zone-grid", role: "group", "aria-label": "探索地图" },
      ...mapRows.map(item => mapCard(item, player, item.id === selected.id))), node("div", { id: "launch-plan", className: "panel launch-plan" }));
    trialPlan(selected);
  }
  const utilities = [
    variant("悬赏", "quests"), variant("本群排行", "rank"), variant("全服排行", "all_rank"),
    variant("首领规则", "help", "首领"), variant("道具手册", "field_guide"),
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
