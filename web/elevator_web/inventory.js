"use strict";

window.ElevatorInventory = (() => {
  const order = ["rainbow", "shining", "red", "gold", "purple", "blue", "green", "white", "field"];
  const ranks = new Map(order.map((grade, index) => [grade, index]));
  const healingItems = new Set(["coffee", "ice_tea"]);
  const usableActions = new Set(["use", "field_use"]);
  const bagGroupLabels = { healing: "回血道具", usable: "其他可用道具" };
  const bagGroupOrder = ["healing", "usable", ...order, "neutral"];
  const slots = ["武器", "护甲", "挂件", "核心"];
  const pageSize = 72;
  const readableActions = new Set(["lookup", "equipment", "gallery", "atlas"]);
  const quantityActions = new Set(["buy", "sell", "decode", "craft"]);
  let detailDialog;

  function grade(item) {
    return ranks.has(item.rarity) ? item.rarity : "neutral";
  }

  function gradeName(ctx, item) {
    return grade(item) === "field" ? "局内" : ctx.rarityNames[grade(item)] || "";
  }

  function equipmentReference(item) {
    return Number.isInteger(item.equip_id) ? "#" + item.equip_id : "";
  }

  function fieldSlots(run) {
    const worn = run.field_slots ?? [run.field_worn];
    return [0, 1].map(index => {
      const item = worn[index];
      return item ? { ...item, status: `临装槽 ${index + 1} · 已穿戴`, actions: item.actions || [item.action].filter(Boolean) } : null;
    });
  }

  function lastingFieldItems(run) {
    return [
      ...(run.field_buffs || []).map(item => ({ ...item, status: "持续至 F" + item.end_floor, actions: [item.action] })),
      ...(run.field_charges || []).map(item => ({ ...item, status: "剩余 " + item.left + " 次", actions: [] })),
    ];
  }

  function activeFieldItems(run) {
    return [...fieldSlots(run).filter(Boolean), ...lastingFieldItems(run)];
  }

  function sorted(items) {
    return items.slice().sort((a, b) => (ranks.get(grade(a)) ?? order.length) -
      (ranks.get(grade(b)) ?? order.length) || Number(b.value ?? 0) - Number(a.value ?? 0) ||
      (a.name || a.id || "").localeCompare(b.name || b.id || "", "zh-CN"));
  }

  function carriedBag(run) {
    const indexed = (run.bag || []).map((item, offset) => {
      if (item.kind === "field") return item;
      const index = Number(item.index ?? item.reference);
      return { ...item, index: Number.isInteger(index) && index > 0 ? index : offset + 1 };
    });
    return [...indexed, ...(run.field || [])];
  }

  function bagGroup(item) {
    if (item.healing === true || healingItems.has(item.id)) return "healing";
    if (item.kind === "field" || item.actions?.some(choice => usableActions.has(choice.control?.command || choice.action))) return "usable";
    return grade(item);
  }

  function bagGroupName(ctx, id) {
    return bagGroupLabels[id] || (gradeName(ctx, { rarity: id }) || "其他") + "品质";
  }

  function bagGroups(ctx, items, renderItem, itemClass, view) {
    const { node, format, selection } = ctx;
    const groups = new Map();
    for (const item of items) {
      const id = bagGroup(item);
      if (!groups.has(id)) groups.set(id, { items: [], count: 0, value: 0 });
      const group = groups.get(id);
      const quantity = Number(item.quantity ?? 1);
      group.items.push(item);
      group.count += quantity;
      group.value += Number(item.value ?? 0) * quantity;
    }
    const expanded = (selection.bagGroups ||= {})[view] ||= new Set();
    return bagGroupOrder.filter(id => groups.has(id)).map(id => {
      const { items: members, count, value } = groups.get(id);
      const details = node("details", { className: "bag-rarity", open: expanded.has(id) ? "" : null },
        node("summary", {}, node("strong", { text: bagGroupName(ctx, id) }),
          node("span", { className: "bag-rarity-meta", text: `${format(count)} 件 · ${format(value)} 金` })),
        node("div", { className: itemClass }, ...members.map(renderItem)));
      details.addEventListener("toggle", () => {
        if (details.open) expanded.add(id);
        else expanded.delete(id);
      });
      return details;
    });
  }

  function sortAtlas(items) {
    return items.slice().sort((a, b) => {
      const aRes = a.id === "prism_resonance" ? 1 : 0;
      const bRes = b.id === "prism_resonance" ? 1 : 0;
      if (aRes !== bRes) return aRes - bRes;
      const aLit = a.unlocked ? 0 : 1;
      const bLit = b.unlocked ? 0 : 1;
      if (aLit !== bLit) return aLit - bLit;
      return (ranks.get(grade(a)) ?? order.length) - (ranks.get(grade(b)) ?? order.length) ||
        Number(b.value ?? 0) - Number(a.value ?? 0) ||
        (a.name || a.id || "").localeCompare(b.name || b.id || "", "zh-CN");
    });
  }

  function control(label, command, params = {}) {
    return { label, action: command, control: { command, params } };
  }

  function closeDetail() {
    if (detailDialog?.open) detailDialog.close();
  }

  function getDialog(ctx) {
    if (detailDialog) return detailDialog;
    detailDialog = ctx.node("dialog", {
      className: "inv-detail", "aria-labelledby": "inventory-detail-title",
    });
    detailDialog.addEventListener("click", event => {
      if (event.target !== detailDialog) return;
      const bounds = detailDialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right ||
          event.clientY < bounds.top || event.clientY > bounds.bottom) closeDetail();
    });
    document.body.append(detailDialog);
    return detailDialog;
  }

  function priceLabel(ctx, item, market) {
    if (item.kind === "field" || item.value === null || item.value === undefined) return null;
    const label = market === "shop" ? "售价" : "单件估值";
    return ctx.node("div", { className: "inv-detail-fact" },
      ctx.node("span", { text: label }), ctx.node("strong", { text: ctx.format(item.price ?? item.value) + " 金" }));
  }

  function itemActions(ctx, item, kind) {
    const actions = (item.actions || []).filter(choice => !readableActions.has(choice.action));
    const index = Number(item.index ?? item.reference);
    const run = ctx.state.run;
    if (kind !== "bag" || !run || !item.can_protect || !Number.isInteger(index) || index < 1) return actions;
    const safe = run.safe || [];
    if (!safe.length || safe.length < run.safe_capacity) return actions;
    return actions.concat(safe.map((other, offset) => {
      const safeIndex = Number(other.index ?? other.reference ?? offset + 1);
      const value = other.value == null ? "" : " · " + ctx.format(other.value) + " 金";
      return {
        ...control("换入，取出「" + (other.name || other.id) + "」", "swap_box", { index, safe_index: safeIndex }),
        description: "安全箱第 " + safeIndex + " 格" + value,
      };
    }));
  }

  function quantityLimit(ctx, item, command, params) {
    if (command === "buy") {
      const price = item.price ?? item.value;
      let desired = 99;
      if (item.id === "safebox") desired = 1;
      if (item.id === "ticket") desired = item.max_quantity ?? 0;
      return price > 0 ? Math.max(0, Math.min(desired, Math.floor(ctx.state.player.gold / price))) : desired;
    }
    if (command === "sell" && params.equip_id !== undefined) return 1;
    if (command === "sell" || command === "decode") return Math.min(9999, Math.max(1, Number(item.quantity) || 1));
    return 1;
  }

  function showDetail(ctx, item, kind, source) {
    const { node, button, format } = ctx;
    const dialog = getDialog(ctx);
    const label = gradeName(ctx, item);
    const title = item.name || item.id;
    const error = node("p", { className: "inv-detail-error", hidden: true, role: "alert" });
    const actions = itemActions(ctx, item, kind);
    const revision = ctx.state.revision;
    let working = false;

    async function submit(choice, quantity) {
      if (working) return;
      const params = { ...choice.control?.params };
      const command = choice.control?.command;
      if (quantity !== undefined && command && quantityActions.has(command)) params.quantity = quantity;
      const payload = command ? { ...choice, control: { command, params } } : choice;
      const buttons = Array.from(dialog.querySelectorAll(".inv-detail-action"));
      const previouslyDisabled = buttons.map(el => el.disabled);
      working = true;
      error.hidden = true;
      buttons.forEach(el => { el.disabled = true; });
      try {
        let failure = "";
        const success = await ctx.execute(payload, {
          quiet: true, inline: true, confirmed: true, revision,
          onError: message => { failure = message; },
        });
        if (success) closeDetail();
        else {
          error.textContent = failure || "操作未完成，请核对物资或余额后重试。";
          error.hidden = false;
        }
      } catch (issue) {
        error.textContent = issue?.message || "操作未完成。";
        error.hidden = false;
      } finally {
        working = false;
        buttons.forEach((el, index) => { el.disabled = previouslyDisabled[index]; });
      }
    }

    function actionRow(choice) {
      const command = choice.control?.command || choice.action;
      const max = quantityLimit(ctx, item, command, choice.control?.params || {});
      const price = item.price ?? item.value;
      let quantity = 1;
      const btn = button(choice.label, () => submit(choice, choice.control && quantityActions.has(command) ? quantity : undefined),
        "inv-detail-action" + (command === "sell" ? " inv-detail-sell" : ""));
      const controls = node("div", { className: "inv-detail-order" }, btn);
      if (command === "swap_box" && choice.description) {
        controls.append(node("span", { className: "inv-order-total", text: choice.description }));
      }
      if (max === 0) {
        btn.disabled = true;
        btn.textContent = item.id === "ticket" && item.max_quantity === 0 ? "已达今日限购或已购票存量上限" : "金币不足";
        return controls;
      }
      if (command === "buy" || command === "sell") {
        const amount = node("span", { className: "inv-order-total", "aria-live": "polite" });
        btn.textContent = command === "buy" ? "确认购买" : "确认出售";
        if (price !== undefined) controls.append(amount);
        amount.textContent = (command === "buy" ? "花费 " : "获得 ") + format(price) + " 金";
        if (max > 1 && choice.control) controls.prepend(stepper(next => {
          quantity = next;
          amount.textContent = (command === "buy" ? "花费 " : "获得 ") + format(price * quantity) + " 金";
        }, max));
      } else if (max > 1 && choice.control && quantityActions.has(command)) {
        controls.prepend(stepper(next => { quantity = next; }, max));
      }
      return controls;
    }

    function stepper(set, max) {
      let value = 1;
      const count = node("input", {
        type: "number", inputmode: "numeric", min: 1, max, step: 1, value: 1,
        "aria-label": "操作数量",
      });
      const add = button("＋", () => update(value + 1), "inv-detail-action", { "aria-label": "增加数量" });
      const subtract = button("−", () => update(value - 1), "inv-detail-action", { "aria-label": "减少数量" });
      function update(next, showValue = true) {
        value = Math.max(1, Math.min(max, next));
        subtract.disabled = value === 1;
        add.disabled = value === max;
        if (showValue) count.value = value;
        set(value);
      }
      count.addEventListener("input", () => {
        if (Number.isInteger(count.valueAsNumber)) update(count.valueAsNumber, false);
      });
      count.addEventListener("change", () => update(Number.isInteger(count.valueAsNumber) ? count.valueAsNumber : value));
      update(1);
      return node("div", { className: "inv-stepper", role: "group", "aria-label": "数量" }, subtract, count, add);
    }

    const affixes = (item.affixes || []).map(affix => typeof affix === "string" ?
      node("li", { text: affix }) :
      node("li", {}, node("strong", { text: affix.name }), affix.description ? node("span", { text: affix.description }) : null));
    const stats = Object.entries(item.stats || {}).map(([id, value]) =>
      node("div", { className: "inv-detail-fact" }, node("span", { text: ctx.statNames[id] || id }),
        node("strong", { text: (Number(value) > 0 ? "+" : "") + format(value) })));
    const locked = item.unlocked === false;
    const headline = node("div", { className: "inv-detail-header", "data-grade": grade(item), ...(locked ? { "data-locked": "true" } : {}) },
      node("div", {},
        node("p", { className: "inv-detail-meta", text: [label && label + "品质", item.slot, item.status || (item.equipped ? "已穿戴" : null), locked ? "未点亮" : null].filter(Boolean).join(" · ") }),
        node("h2", { id: "inventory-detail-title", text: title })),
      button("关闭", closeDetail, "inv-detail-close", { "aria-label": "关闭物品详情" }),
    );
    const statusText = item.owned !== undefined && item.total !== undefined
      ? `${item.owned}/${item.total}${item.unlocked ? " · 已觉醒" : " · 待齐聚"}`
      : kind === "stages" && item.unlocked !== undefined ? (item.unlocked ? "已解锁" : "待解锁")
      : item.unlocked !== undefined ? (item.unlocked ? (kind === "rainbow" ? "已点亮 · 永久生效" : "已点亮") : "未点亮") : null;
    const hideQty = kind === "shop" || kind === "market" || kind === "atlas" || kind === "rainbow" || kind === "stages";
    const isTicketPurchase = kind === "shop" && item.id === "ticket";
    const { paid_tickets: paidTickets, paid_ticket_cap: paidTicketCap } = ctx.state.player;
    const reference = equipmentReference(item);
    const facts = [
      reference && node("div", { className: "inv-detail-fact" },
        node("span", { text: "装备编号" }), node("strong", { text: reference })),
      statusText && node("div", { className: "inv-detail-fact" },
        node("span", { text: "图鉴状态" }), node("strong", { text: statusText })),
      item.quantity !== undefined && !hideQty && node("div", { className: "inv-detail-fact" },
        node("span", { text: "数量" }), node("strong", { text: format(item.quantity) })),
      priceLabel(ctx, item, kind),
      isTicketPurchase && Number.isInteger(item.max_quantity) &&
        node("div", { className: "inv-detail-fact" }, node("span", { text: "当前可加购" }),
          node("strong", { text: format(item.max_quantity) + " 张" })),
      isTicketPurchase && Number.isInteger(paidTickets) && Number.isInteger(paidTicketCap) &&
        node("div", { className: "inv-detail-fact" }, node("span", { text: "结转票存量" }),
          node("strong", { text: format(paidTickets) + " / " + format(paidTicketCap) + " 张" })),
      item.set && node("div", { className: "inv-detail-fact" }, node("span", { text: "所属收藏" }), node("strong", { text: item.set })),
      ...stats,
    ].filter(Boolean);
    dialog.replaceChildren(headline,
      node("div", { className: "inv-detail-body" },
        item.description ? node("p", { className: "inv-detail-desc", text: item.description }) : null,
        item.lore ? node("section", { className: "inv-detail-lore", "data-grade": grade(item) },
          node("h3", { text: "档案逸闻" }),
          node("p", { text: item.lore }),
        ) : null,
        facts.length ? node("div", { className: "inv-detail-facts" }, ...facts) : null,
        item.source ? node("section", { className: "inv-affixes" }, node("h3", { text: "获取方式" }),
          node("p", { text: item.source })) : null,
        affixes.length ? node("section", { className: "inv-affixes" }, node("h3", { text: "词条" }), node("ul", {}, ...affixes)) : null,
        item.requirements ? node("section", { className: "inv-affixes" }, node("h3", { text: "兑换材料" }),
          node("p", { text: item.requirements })) : null,
        actions.length ? node("div", { className: "inv-detail-actions" }, ...actions.map(actionRow)) : null,
        error,
      ),
    );
    const onClosed = () => {
      if (source?.isConnected) source.focus({ preventScroll: true });
      else document.querySelector(".view:not([hidden]) .inv-filter input")?.focus({ preventScroll: true });
    };
    dialog.addEventListener("close", onClosed, { once: true });
    if (!dialog.open) dialog.showModal();
  }

  function card(ctx, item, kind) {
    const { node, button, format } = ctx;
    const rarity = gradeName(ctx, item);
    const title = item.name || item.id;
    const price = item.kind === "field" ? null : item.price ?? item.value;
    const locked = item.unlocked === false;
    const type = item.status || (item.equipped ? "已穿戴" : item.slot || (
      item.owned !== undefined && item.total !== undefined
        ? `${item.owned}/${item.total}${item.unlocked ? " · 已觉醒" : " · 套装共鸣"}`
        : kind === "stages" && item.unlocked !== undefined ? (item.unlocked ? "已解锁" : "待解锁")
        : item.unlocked === undefined ? null : item.unlocked ? "已点亮" : "未点亮"
    ));
    const hideQty = kind === "shop" || kind === "market" || kind === "atlas" || kind === "rainbow" || kind === "stages";
    const count = equipmentReference(item) || (item.quantity !== undefined && !hideQty ? "×" + format(item.quantity) : null);
    const el = button("", () => showDetail(ctx, item, kind, el), "inv-card", {
      "aria-haspopup": "dialog", "aria-label": [title, rarity && rarity + "品质", type, count, "查看详情"].filter(Boolean).join("，"),
      "data-grade": grade(item),
      ...(locked ? { "data-locked": "true" } : {}),
    });
    el.append(
      node("span", { className: "inv-card-head" },
        node("span", { className: "inv-card-name", text: title }),
        rarity ? node("span", { className: "inv-grade", text: rarity }) : null,
      ),
      node("span", { className: "inv-card-meta" },
        node("span", { text: [count, type].filter(Boolean).join(" · ") }),
        price === null || price === undefined ? null : node("span", { className: "inv-card-value", text: format(price) + " 金" }),
      ),
    );
    return el;
  }

  function itemGroups(ctx, items, renderItem, property, categories, fallback) {
    const names = [...categories, fallback];
    const groups = new Map(names.map(name => [name, []]));
    for (const item of items) {
      groups.get(groups.has(item[property]) ? item[property] : fallback).push(item);
    }
    return names.filter(name => groups.get(name).length).map(name =>
      ctx.node("section", { className: "inv-list", "aria-label": name },
        ctx.node("h3", { text: name }),
        ctx.node("div", { className: "inv-card-grid" }, ...groups.get(name).map(renderItem))));
  }

  function section(ctx, key, title, items, { filter = false, kind = key, empty = "这里还没有物品。", sort = sorted, paginate = true, countText = null } = {}) {
    const { node, button, format } = ctx;
    const bag = kind === "bag";
    const gear = kind === "gear";
    const shop = kind === "shop";
    const category = bag ? bagGroup : grade;
    const values = sort(items || []);
    const stored = ctx.selection.inventoryFilters ||= {};
    const current = stored[key] ||= { query: "", grade: "", page: 1 };
    const list = node("div", { className: bag ? "bag-rarities" : gear || shop ? "inv-content" : "inv-card-grid" });
    const count = node("span", { className: "inv-list-count" });
    const pages = node("div", { className: "inv-pages" });
    const heading = node("div", { className: "inv-list-head" }, node("h2", { text: title }), count);
    const group = node("section", { className: "inv-list" }, heading);

    function render() {
      const needle = filter ? current.query.trim().toLowerCase() : "";
      const equipmentId = gear && /^#?\d+$/.test(needle) ? Number(needle.replace(/^#/, "")) : null;
      const results = values.filter(item => (!filter || !current.grade || category(item) === current.grade) &&
        (!needle || (equipmentId === null
          ? [item.name, item.id, item.slot, item.kind, item.category, item.set_name].filter(Boolean).join(" ").toLowerCase().includes(needle)
          : item.equip_id === equipmentId)));
      const total = paginate ? Math.max(1, Math.ceil(results.length / pageSize)) : 1;
      current.page = Math.min(current.page, total);
      const start = paginate ? (current.page - 1) * pageSize : 0;
      const range = paginate ? results.slice(start, start + pageSize) : results;
      count.textContent = countText ? countText(results) : format(results.length) + " 项";
      const renderItem = item => card(ctx, item, bag && item.kind === "field" ? "field" : kind);
      let rows;
      if (bag) rows = bagGroups(ctx, range, renderItem, "inv-card-grid", "storage");
      else if (gear) rows = itemGroups(ctx, range, renderItem, "slot", slots, "其他装备");
      else if (shop) rows = itemGroups(ctx, range, renderItem, "category", ["装备箱","回复药剂","战术补给","通行与扩容"], "其他");
      else rows = range.map(renderItem);
      list.replaceChildren(...(rows.length ? rows :
        [node("p", { className: "inv-empty", text: values.length ? "没有符合条件的物品。" : empty })]));
      pages.hidden = total < 2;
      if (total < 2) { pages.replaceChildren(); return; }
      function jump(to) {
        current.page = Math.min(total, Math.max(1, to));
        render();
        group.scrollIntoView({ block: "start", behavior: "auto" });
      }
      pages.replaceChildren(
        button("上一页", () => jump(current.page - 1), "", current.page === 1 ? { disabled: "" } : {}),
        node("span", { text: format(current.page) + " / " + format(total) }),
        button("下一页", () => jump(current.page + 1), "", current.page === total ? { disabled: "" } : {}),
      );
    }
    if (filter && values.length) {
      const search = node("input", { type: "search", value: current.query,
        placeholder: gear ? "搜索名称、种类或编号" : "搜索物品", "aria-label": "搜索" + title });
      const grades = (bag ? bagGroupOrder : order).filter(id => values.some(item => category(item) === id));
      const rarity = node("select", { "aria-label": title + (bag ? "分类" : "品质") },
        node("option", { value: "", text: bag ? "全部分类" : "全部品质" }),
        ...grades.map(id => node("option", { value: id, text: bag ? bagGroupName(ctx, id) : gradeName(ctx, { rarity: id }) })));
      if (!grades.includes(current.grade)) current.grade = "";
      rarity.value = current.grade;
      const update = event => {
        if (event.isComposing) return;
        current.query = search.value;
        current.grade = rarity.value;
        current.page = 1;
        render();
      };
      search.addEventListener("input", update);
      search.addEventListener("compositionend", update);
      rarity.addEventListener("change", update);
      group.append(node("div", { className: "inv-filter" }, search, rarity));
    }
    group.append(list, pages);
    render();
    return group;
  }

  function tabs(ctx, modes, selected, update) {
    const strip = ctx.node("nav", { className: "inv-tabs", "aria-label": "物品分类" });
    for (const [mode, label] of modes) {
      const tab = ctx.button(label, () => {
        for (const peer of strip.children) peer.setAttribute("aria-pressed", String(peer === tab));
        update(mode);
      }, "", { "aria-pressed": String(selected === mode) });
      strip.append(tab);
    }
    return strip;
  }

  function showStorage(ctx) {
    const { run, warehouse } = ctx.state;
    const { node, selection } = ctx;
    const body = node("div", { className: "inv-content" });
    if (!run) selection.storage = "warehouse";
    function render(mode) {
      selection.storage = mode;
      const stocked = warehouse || [];
      if (mode === "warehouse") {
        body.replaceChildren(section(ctx, "warehouse", "仓库物资", stocked, { filter: true }));
        return;
      }
      const effects = activeFieldItems(run);
      const groups = [
        section(ctx, "bag", "背包", carriedBag(run), { filter: true, paginate: false, empty: "背包目前没有物资。" }),
        section(ctx, "safe", `安全箱 ${run.safe?.length || 0}/${run.safe_capacity ?? ctx.state.player.safebox}`, run.safe,
          { empty: "在背包点选物资可存入安全箱。" }),
      ];
      if (effects.length) groups.push(section(ctx, "effects", "生效中的道具", effects));
      body.replaceChildren(...groups);
    }
    render(selection.storage || "warehouse");
    return node("div", { className: "inventory" },
      run ? tabs(ctx, [["warehouse", "跨群仓库"], ["bag", "本局物资"]], selection.storage, render) : null,
      body,
    );
  }

  function heirloomCabinet(ctx, atlas) {
    const {node,button,state}=ctx, h=state.heirlooms;
    if (!h) return null;
    const equipped = h.slots.filter(Boolean);
    const body=node("section",{className:"inv-list heirloom-cabinet"},
      node("h2",{text:atlas?"闻岑的八套遗物":"闪耀祝福 · 四槽"}));
    if (!atlas) {
      body.append(node("div",{className:"inv-card-grid inv-equipment-slots"}, ...h.slots.map((key,i)=>{
        const label=`槽 ${i+1} · ${key?h.pieces[key].name:"空"}`;
        if(!key || state.run) return node("div",{className:"inv-empty-slot",text:label});
        return button(label+" · 卸下",()=>ctx.execute(control("卸下祝福","equip_blessing",{slot:i+1,piece:null}),{inline:true}),"inv-utility");
      })));
    }
    for (const [code,suit] of Object.entries(h.sets)) {
      const parts=Object.entries(h.pieces).filter(([,p])=>p.set===code);
      const worn=equipped.filter(key=>h.pieces[key].set===code).length;
      const owned=parts.filter(([key])=>h.owned[key]).length;
      const details=node("details",{className:"heirloom-set"},
        node("summary",{text:`${suit.name} · 收藏 ${owned}/4 · 装配 ${worn}/4`}),
        node("p",{text:suit.short}), node("p",{text:suit.lore}),
        node("p",{text:`两件${worn>=2?"（生效）":""}　${suit.two}`}),node("p",{text:`四件${worn===4?"（生效）":""}　${suit.four}`}));
      for(const [key,p] of parts) {
        const line=node("details",{className:"heirloom-piece","data-grade":"shining"},
          node("summary",{text:`${key}　${p.name} · ${ctx.statNames[p.stats[0]]} +${p.stats[1]} · 持有 ${h.owned[key]||0}`}),
          node("p",{text:p.lore}));
        if(!atlas && !state.run && h.owned[key] && !equipped.includes(key)){
          const select=node("select",{"aria-label":p.name+"的祝福槽位"},...h.slots.map((old,i)=>node("option",{value:i+1,
            text:`槽 ${i+1} · ${old?h.pieces[old].name:"空"}`})));
          select.value=String(Math.max(0,h.slots.indexOf(null))+1);
          line.append(select,button("装入此槽",()=>ctx.execute(control("装配祝福","equip_blessing",
            {slot:Number(select.value),piece:key}),{inline:true}),"inv-utility"));
        }
        details.append(line);
      }
      body.append(details);
    }
    return body;
  }

  function showGear(ctx) {
    const { node, button } = ctx;
    const { run } = ctx.state;
    const items = ctx.state.equipment || [];
    const equipped = items.filter(item => item.equipped);
    const wornSets = new Map();
    for (const item of equipped) {
      if (item.set_name) wornSets.set(item.set_name, (wornSets.get(item.set_name) || 0) + 1);
    }
    const effects = run ? lastingFieldItems(run) : [];
    return node("div", { className: "inventory inv-content" },
      node("section", { className: "inv-list" },
        node("div", { className: "inv-list-head" }, node("h2", { text: "当前配装" }),
          !run && items.length ? button("一键配装",
            () => ctx.execute(control("一键配装", "autoequip"), { quiet: true, inline: true }),
            "inv-utility") : null),
        node("div", { className: "inv-card-grid inv-equipment-slots" }, ...slots.map(slot => {
          const item = equipped.find(candidate => candidate.slot === slot);
          return item ? card(ctx, item, "gear") :
            node("div", { className: "inv-empty-slot" }, node("span", { text: slot }), node("small", { text: "空槽" }));
        })),
        ...Array.from(wornSets, ([name, count]) => node("p", { className: "item-facts",
          text: `${name} · ${count}/4 件${count >= 4 ? " · 四件效果生效" : count >= 2 ? " · 两件效果生效" : ""}` })),
      ),
      heirloomCabinet(ctx,false),
      run ? node("section", { className: "inv-list" },
        node("div", { className: "inv-list-head" }, node("h2", { text: "本局临时装备" }),
          run.field_offer ? button("处理待选装备", () => ctx.focusView("station"), "inv-utility") : null),
        node("div", { className: "inv-card-grid" }, ...fieldSlots(run).map((item, index) =>
          item ? card(ctx, item, "field") :
            node("div", { className: "inv-empty-slot" }, node("span", { text: `临装槽 ${index + 1}` }), node("small", { text: "空槽" })))),
      ) : null,
      run ? section(ctx, "effects", "本局生效增益", effects, { empty: "当前没有生效的时限或充能道具。" }) : null,
      section(ctx, "gear", "装备库", items, { filter: true, empty: "暂无装备。可在商店购买装备箱。" }),
    );
  }

  function exchangePanel(ctx) {
    const { node, button, format, state, selection } = ctx;
    const ex = state.exchange || { listings: [], tradable: { inventory: [], equipment: [], shining: [] }, barter_catalog: {} };
    const inRun = Boolean(state.run);
    const wrap = node("div", { className: "exchange-panel" });

    const ASSET_LABELS = { inventory: "仓库物资", equipment: "闲置装备", shining: "闪耀祝福" };
    const GRADE_ORDER = ["rainbow", "shining", "red", "gold", "purple", "blue", "green", "white"];

    function itemUid(assetType, it) {
      if (!it) return "";
      if (assetType === "inventory") return `inv:${it.stock_id}`;
      if (assetType === "equipment") return `eq:${it.equip_id}`;
      return `sh:${it.piece}`;
    }

    function allTradableEntries() {
      const out = [];
      for (const aType of ["inventory", "equipment", "shining"]) {
        for (const it of (ex.tradable?.[aType] || [])) {
          out.push({ assetType: aType, item: it, uid: itemUid(aType, it) });
        }
      }
      return out;
    }

    function formatGearSummary(it) {
      const parts = [];
      if (it.stats) {
        parts.push(Object.entries(it.stats).map(([k, v]) => `${ctx.statNames[k] || k}${v >= 0 ? "+" : ""}${v}`).join(" "));
      }
      if (it.affixes?.length) {
        parts.push("词条：" + it.affixes.map(a => a.name).join("、"));
      }
      return parts.join(" ｜ ");
    }

    function entrySearchText(entry) {
      const it = entry.item;
      return [
        it.name, it.id, it.slot, it.category_label, it.rarity_label, it.quality,
        it.description, ASSET_LABELS[entry.assetType],
        it.equip_id ? `#${it.equip_id}` : "",
        it.piece || "",
        ...(it.affixes || []).map(a => `${a.name} ${a.description || ""}`),
      ].filter(Boolean).join(" ").toLowerCase();
    }

    // Persistent form draft across state refreshes
    const draft = selection.exDraft ||= {
      scope: "public",
      target: "",
      selectedUid: "",
      quantity: 1,
      allowGold: true,
      allowBarter: false,
      priceGold: "",
      wantId: "",
      wantName: "",
    };

    const allEntries = allTradableEntries();
    function getSelectedEntry() {
      if (!draft.selectedUid) return null;
      return allEntries.find(e => e.uid === draft.selectedUid) || null;
    }

    // --- Reusable Secondary Modal Picker (with search, category tabs & rarity filter) ---
    function openSearchPicker({
      title, subtitle = "", entries, selectedKey = "", showAssetTabs = false,
      showGradeFilter = false, topOption = null, renderCardMeta, onSelect, searchPlaceholder = "搜索名称、品质、部位、词条或 #编号…",
    }) {
      const dialog = getDialog(ctx);
      let activeAsset = "all";
      let activeGrade = "";
      let query = "";

      const headCount = node("span", { className: "inv-list-count" });
      const searchInput = node("input", {
        type: "search", className: "exchange-picker-search",
        placeholder: searchPlaceholder, "aria-label": title + "搜索",
      });

      const tabBar = showAssetTabs ? node("div", { className: "inv-tabs exchange-picker-tabs" }) : null;
      const gradeSel = showGradeFilter ? node("select", { className: "exchange-picker-grade", "aria-label": "按品质筛选" }) : null;
      const grid = node("div", { className: "exchange-picker-grid" });

      if (showGradeFilter && gradeSel) {
        const presentGrades = GRADE_ORDER.filter(g => entries.some(e => (e.item?.rarity || e.rarity) === g));
        const gradeLabels = {
          rainbow: "🌈 虹彩", shining: "✨ 闪耀", red: "🟥 传世红", gold: "🟨 大金",
          purple: "🟪 紫色", blue: "🟦 蓝色", green: "🟩 绿色", white: "⬜ 白色",
        };
        gradeSel.replaceChildren(
          node("option", { value: "", text: "全部品质" }),
          ...presentGrades.map(g => node("option", { value: g, text: gradeLabels[g] || g })),
        );
      }

      function renderTabs() {
        if (!tabBar) return;
        const counts = {
          all: entries.length,
          inventory: entries.filter(e => e.assetType === "inventory").length,
          equipment: entries.filter(e => e.assetType === "equipment").length,
          shining: entries.filter(e => e.assetType === "shining").length,
        };
        const tabsDef = [
          ["all", `全部 (${counts.all})`],
          ["inventory", `仓库物资 (${counts.inventory})`],
          ["equipment", `闲置装备 (${counts.equipment})`],
          ["shining", `闪耀祝福 (${counts.shining})`],
        ];
        tabBar.replaceChildren(...tabsDef.map(([id, label]) =>
          button(label, () => { activeAsset = id; renderTabs(); renderGrid(); }, "inv-tab", { "aria-pressed": String(id === activeAsset) }),
        ));
      }

      function renderGrid() {
        const needle = query.trim().toLowerCase();
        const filtered = entries.filter(e => {
          if (showAssetTabs && activeAsset !== "all" && e.assetType !== activeAsset) return false;
          const r = e.item?.rarity || e.rarity || "";
          if (showGradeFilter && activeGrade && r !== activeGrade) return false;
          if (needle && !e.searchText.includes(needle)) return false;
          return true;
        });
        headCount.textContent = `共 ${format(filtered.length)} 项`;
        const cards = [];
        if (topOption && !needle) {
          const isSel = selectedKey === topOption.key;
          const topBtn = button("", () => { onSelect(topOption.value); closeDetail(); },
            "inv-card exchange-pick-card exchange-pick-any" + (isSel ? " is-selected" : ""),
            { "data-grade": topOption.grade || "neutral" });
          topBtn.append(
            node("span", { className: "inv-card-head" },
              node("span", { className: "inv-card-name", text: topOption.title }),
              isSel ? node("span", { className: "inv-grade", text: "✓ 已选" }) : null,
            ),
            node("span", { className: "inv-card-meta" }, node("span", { text: topOption.meta || "" })),
          );
          cards.push(topBtn);
        }
        for (const entry of filtered) {
          const info = renderCardMeta(entry);
          const isSel = entry.key === selectedKey;
          const btn = button("", () => { onSelect(entry.value); closeDetail(); },
            "inv-card exchange-pick-card" + (isSel ? " is-selected" : ""),
            { "data-grade": info.grade || "neutral" });
          btn.append(
            node("span", { className: "inv-card-head" },
              node("span", { className: "inv-card-name", text: info.name }),
              node("span", { className: "inv-grade", text: isSel ? "✓ 已选" : info.badge }),
            ),
            info.sub ? node("span", { className: "exchange-pick-sub", text: info.sub }) : null,
            node("span", { className: "inv-card-meta" },
              node("span", { text: info.metaLeft || "" }),
              info.metaRight ? node("span", { className: "inv-card-value", text: info.metaRight }) : null,
            ),
          );
          cards.push(btn);
        }
        grid.replaceChildren(...(cards.length ? cards : [node("p", { className: "inv-empty", text: "没有找到匹配的物品，试试换个关键词。" })]));
      }

      const onSearch = ev => {
        if (ev.isComposing) return;
        query = searchInput.value;
        renderGrid();
      };
      searchInput.addEventListener("input", onSearch);
      searchInput.addEventListener("compositionend", onSearch);
      if (gradeSel) gradeSel.addEventListener("change", () => { activeGrade = gradeSel.value; renderGrid(); });

      const controlsRow = node("div", { className: "exchange-picker-controls" }, searchInput, gradeSel);
      dialog.replaceChildren(
        node("div", { className: "inv-detail-header", "data-grade": "neutral" },
          node("div", {},
            node("p", { className: "inv-detail-meta", text: subtitle || "点击任意卡片即可选择" }),
            node("h2", { id: "inventory-detail-title", text: title }),
          ),
          button("关闭", closeDetail, "inv-detail-close", { "aria-label": "关闭选择器" }),
        ),
        node("div", { className: "inv-detail-body exchange-picker-body" },
          tabBar,
          node("div", { className: "exchange-picker-toolbar" }, controlsRow, headCount),
          grid,
        ),
      );
      renderTabs();
      renderGrid();
      if (!dialog.open) dialog.showModal();
      setTimeout(() => searchInput.focus({ preventScroll: true }), 20);
    }

    // --- 1. Create Listing Form ---
    const formDetails = node("details", { className: "inv-list exchange-create", open: selection.exCreateOpen ? "" : null });
    formDetails.addEventListener("toggle", () => { selection.exCreateOpen = formDetails.open; });

    const scopeSel = node("select", { "aria-label": "上架范围" },
      node("option", { value: "public", text: "📢 公开售卖（72小时）" }),
      node("option", { value: "direct", text: "🤝 1对1定向交易（24小时）" }));
    scopeSel.value = draft.scope;

    const targetInput = node("input", {
      type: "text", inputmode: "numeric", placeholder: "输入对方 QQ 号",
      value: draft.target || "", hidden: draft.scope !== "direct", "aria-label": "对方 QQ 号",
    });

    // Selected listing item preview box + trigger button
    const itemBox = node("div", { className: "exchange-selected-box" });
    const qtyWrap = node("div", { className: "exchange-qty-wrap", hidden: true });
    const qtyInput = node("input", { type: "number", min: 1, max: 1, value: String(draft.quantity || 1), "aria-label": "上架数量" });
    const qtyMinus = button("−", () => {
      qtyInput.value = String(Math.max(1, (Math.floor(Number(qtyInput.value) || 1) - 1)));
      draft.quantity = Number(qtyInput.value);
      syncFormState();
    }, "quiet small", { "aria-label": "减少数量" });
    const qtyPlus = button("＋", () => {
      const maxQ = Number(qtyInput.max) || 1;
      qtyInput.value = String(Math.min(maxQ, (Math.floor(Number(qtyInput.value) || 1) + 1)));
      draft.quantity = Number(qtyInput.value);
      syncFormState();
    }, "quiet small", { "aria-label": "增加数量" });
    qtyWrap.append(node("span", { className: "exchange-field-label", text: "上架数量" }),
      node("div", { className: "inv-stepper" }, qtyMinus, qtyInput, qtyPlus));

    // Trade mode toggles: Gold, Barter, or Both
    const goldToggle = node("input", { type: "checkbox", checked: draft.allowGold ? "" : null });
    const barterToggle = node("input", { type: "checkbox", checked: draft.allowBarter ? "" : null });
    const goldLabel = node("label", { className: "exchange-mode-chip" }, goldToggle, node("span", { text: "💰 金币买卖" }));
    const barterLabel = node("label", { className: "exchange-mode-chip" }, barterToggle, node("span", { text: "🔄 以物换物" }));
    const modeRow = node("div", { className: "exchange-mode-group" },
      node("span", { className: "exchange-field-label", text: "交易方式（可单选或两个都选）" }),
      node("div", { className: "exchange-mode-chips" }, goldLabel, barterLabel),
    );

    // Gold pricing panel
    const priceInput = node("input", {
      type: "number", min: 500, max: 100000000, step: 100,
      placeholder: "输入总金币价格", value: draft.priceGold || "", "aria-label": "金币总价",
    });
    const fillMinBtn = button("填入底价", () => {
      if (priceInput.min) {
        priceInput.value = priceInput.min;
        draft.priceGold = priceInput.value;
      }
    }, "quiet small");
    const goldRow = node("div", { className: "exchange-subpanel" },
      node("span", { className: "exchange-field-label", text: "金币总价" }),
      node("div", { className: "exchange-row" }, priceInput, fillMinBtn),
    );

    // Barter target picker panel
    const wantBox = node("div", { className: "exchange-want-box" });
    const barterRow = node("div", { className: "exchange-subpanel" },
      node("span", { className: "exchange-field-label", text: "希望换取的物品（同品质同分类）" }),
      wantBox,
    );

    const hint = node("p", { className: "item-facts exchange-hint" });
    const formErr = node("p", { className: "form-error", hidden: true });

    function openTradablePicker() {
      const pickerEntries = allEntries.map(e => ({
        key: e.uid,
        assetType: e.assetType,
        item: e.item,
        value: e,
        searchText: entrySearchText(e),
      }));
      openSearchPicker({
        title: "选择要上架的交易物",
        subtitle: "支持跨分类搜索物品名、品质、槽位、词条或装备 #编号（穿戴中装备与唯一虹彩不可挂单）",
        entries: pickerEntries,
        selectedKey: draft.selectedUid,
        showAssetTabs: true,
        showGradeFilter: true,
        renderCardMeta: e => {
          const it = e.item;
          const isEq = e.assetType === "equipment";
          return {
            name: isEq ? `${it.name} #${it.equip_id}` : it.name,
            badge: `${it.rarity_label}${isEq ? "·" + it.slot : ""}`,
            grade: it.rarity,
            sub: isEq ? formatGearSummary(it) : (it.quality ? `品相：${it.quality}` : (it.category === "rainbow" ? "重复副本（可交易）" : it.category_label)),
            metaLeft: isEq ? "闲置装备" : `可用 ×${format(it.quantity || 1)}`,
            metaRight: `底价 ${format(it.min_gold || 500)} 金`,
          };
        },
        onSelect: chosenEntry => {
          const prev = getSelectedEntry();
          draft.selectedUid = chosenEntry.uid;
          draft.quantity = 1;
          qtyInput.value = "1";
          // Reset specific barter target if category/rarity changed
          if (!prev || prev.assetType !== chosenEntry.assetType ||
              prev.item.category !== chosenEntry.item.category || prev.item.rarity !== chosenEntry.item.rarity) {
            draft.wantId = "";
            draft.wantName = "";
          }
          draft.priceGold = "";
          syncFormState();
        },
      });
    }

    function openWantPicker(entry) {
      const chosen = entry.item;
      const catalogKey = entry.assetType === "equipment"
        ? `equipment:${chosen.rarity === "red" ? "red" : "standard"}`
        : `${chosen.category}:${chosen.rarity}`;
      const candidates = (ex.barter_catalog?.[catalogKey] || []).filter(c => !(
        (chosen.category === "rainbow" || chosen.category === "shining") && c.id === chosen.id
      ));
      const pickerEntries = candidates.map(c => ({
        key: c.id,
        item: c,
        rarity: chosen.rarity,
        value: { id: c.id, name: c.slot ? `${chosen.rarity_label}·${c.name}(${c.slot})` : c.name },
        searchText: [c.name, c.id, c.slot, c.category_label, c.rarity_label, c.description].filter(Boolean).join(" ").toLowerCase(),
      }));
      openSearchPicker({
        title: "指定想要交换的物品",
        subtitle: `须与上架物同品质同分类：仅限「${chosen.rarity_label}${chosen.category_label}」`,
        entries: pickerEntries,
        selectedKey: draft.wantId || "__ANY__",
        topOption: {
          key: "__ANY__",
          value: { id: "", name: "" },
          title: `✨ 任意「${chosen.rarity_label}${chosen.category_label}」（不指定具体物品）`,
          meta: "买家可用任意同品质同分类物品与你交换",
          grade: chosen.rarity,
        },
        searchPlaceholder: "搜索目标物品名、部位或关键词…",
        renderCardMeta: e => {
          const c = e.item;
          return {
            name: c.name,
            badge: c.slot || chosen.rarity_label,
            grade: chosen.rarity,
            sub: c.description || c.category_label || "",
            metaLeft: `指定换取`,
            metaRight: "",
          };
        },
        onSelect: picked => {
          draft.wantId = picked.id || "";
          draft.wantName = picked.name || "";
          syncFormState();
        },
      });
    }

    const submitBtn = button("确认上架", async () => {
      formErr.hidden = true;
      const entry = getSelectedEntry();
      if (!entry) {
        formErr.textContent = "请先点击「选择交易物」挑选要上架的物品。";
        formErr.hidden = false;
        return;
      }
      if (!draft.allowGold && !draft.allowBarter) {
        formErr.textContent = "请至少勾选一种交易方式（金币买卖 或 以物换物）。";
        formErr.hidden = false;
        return;
      }
      const mode = draft.allowGold && draft.allowBarter ? "both" : draft.allowGold ? "gold" : "barter";
      const params = {
        scope: scopeSel.value,
        target_user_id: scopeSel.value === "direct" ? targetInput.value.trim() : "",
        mode,
        asset_type: entry.assetType,
      };
      if (entry.assetType === "inventory") {
        params.stock_id = entry.item.stock_id;
        params.quantity = Math.max(1, Math.min(entry.item.quantity, Math.floor(Number(qtyInput.value) || 1)));
      } else if (entry.assetType === "equipment") {
        params.equip_id = entry.item.equip_id;
      } else {
        params.piece = entry.item.piece;
      }
      if (mode === "gold" || mode === "both") {
        params.price_gold = Math.floor(Number(priceInput.value) || 0);
      }
      if (mode === "barter" || mode === "both") {
        params.want_item_id = draft.wantId || "";
      }
      const ok = await ctx.execute(control("发布挂单", "exchange_list", params), {
        inline: true,
        onError: msg => { formErr.textContent = msg; formErr.hidden = false; },
      });
      if (ok) {
        draft.selectedUid = "";
        draft.quantity = 1;
        draft.priceGold = "";
        draft.wantId = "";
        draft.wantName = "";
      }
    }, "primary");

    function syncFormState() {
      draft.scope = scopeSel.value;
      draft.target = targetInput.value;
      targetInput.hidden = scopeSel.value !== "direct";
      goldLabel.classList.toggle("is-active", draft.allowGold);
      barterLabel.classList.toggle("is-active", draft.allowBarter);

      const entry = getSelectedEntry();
      if (!entry) {
        itemBox.replaceChildren(
          node("div", { className: "exchange-selected-empty" },
            node("span", { text: allEntries.length ? `仓库/装备/祝福共 ${allEntries.length} 件可上架` : "当前暂无可上架物品（穿戴中装备与唯一虹彩不可挂单）" }),
            button("🔍 选择交易物", openTradablePicker, "primary small", { disabled: allEntries.length ? null : "" }),
          ),
        );
        qtyWrap.hidden = true;
        wantBox.replaceChildren(node("span", { className: "item-facts", text: "请先选择要上架的交易物，再指定想要换取的目标物品。" }));
        hint.textContent = "支持选择「金币买卖」「以物换物」或两个同时开启；点击「选择交易物」可在弹窗中按分类与关键词搜索。";
        goldRow.hidden = !draft.allowGold;
        barterRow.hidden = !draft.allowBarter;
        submitBtn.disabled = inRun || !entry;
        return;
      }

      const chosen = entry.item;
      const isInv = entry.assetType === "inventory" && chosen.category !== "rainbow";
      qtyWrap.hidden = !isInv;
      qtyInput.max = String(chosen.quantity || 1);
      const qty = isInv ? Math.max(1, Math.min(chosen.quantity, Math.floor(Number(qtyInput.value) || 1))) : 1;
      qtyInput.value = String(qty);
      draft.quantity = qty;

      const summaryLine = entry.assetType === "equipment"
        ? formatGearSummary(chosen)
        : [chosen.category_label, chosen.quality ? `品相：${chosen.quality}` : "", `库存 ×${format(chosen.quantity || 1)}`].filter(Boolean).join(" · ");

      itemBox.replaceChildren(
        node("div", { className: "exchange-selected-card", "data-grade": chosen.rarity },
          node("div", { className: "exchange-selected-info" },
            node("div", { className: "exchange-selected-head" },
              node("span", { className: "inv-grade-pill", "data-grade": chosen.rarity, text: `${chosen.rarity_label}${chosen.slot ? "·" + chosen.slot : ""}` }),
              node("strong", { text: entry.assetType === "equipment" ? `${chosen.name} #${chosen.equip_id}` : chosen.name }),
            ),
            summaryLine ? node("small", { className: "item-facts", text: summaryLine }) : null,
          ),
          button("🔍 更换交易物", openTradablePicker, "quiet small"),
        ),
      );

      const minGold = (chosen.min_gold || 500) * qty;
      priceInput.min = String(minGold);
      if (!Number(priceInput.value) || Number(priceInput.value) < minGold) {
        priceInput.value = String(minGold);
        draft.priceGold = priceInput.value;
      }
      fillMinBtn.textContent = `填入底价 (${format(minGold)} 金)`;

      const wantLabel = draft.wantId
        ? `🎯 指定换取：${draft.wantName || draft.wantId}${isInv ? ` ×${qty}` : ""}`
        : `✨ 任意同品质同类：${chosen.rarity_label}${chosen.category_label}${isInv ? ` ×${qty}` : ""}`;
      wantBox.replaceChildren(
        node("div", { className: "exchange-want-row" },
          node("span", { className: "exchange-want-text", text: wantLabel }),
          node("div", { className: "exchange-want-actions" },
            button("🔍 指定交换物品", () => openWantPicker(entry), "quiet small"),
            draft.wantId ? button("不指定", () => { draft.wantId = ""; draft.wantName = ""; syncFormState(); }, "quiet small") : null,
          ),
        ),
      );

      goldRow.hidden = !draft.allowGold;
      barterRow.hidden = !draft.allowBarter;

      if (draft.allowGold && draft.allowBarter) {
        hint.textContent = `✨ 双模式挂单：买家既可用 ≥${format(minGold)} 金直接买走，也可用「${draft.wantId ? (draft.wantName || draft.wantId) : `任意${chosen.rarity_label}${chosen.category_label}`}」${isInv ? `×${qty}` : ""} 交换。`;
      } else if (draft.allowGold) {
        hint.textContent = `💰 金币售卖底价：${format(minGold)} 金（虹彩底价 ${format(ex.min_floors?.rainbow || 1500000)} 金，仅允许挂售重复副本并保留 1 件在仓）。`;
      } else {
        hint.textContent = `🔄 同品质同分类以物换物：仅接受「${draft.wantId ? (draft.wantName || draft.wantId) : `任意${chosen.rarity_label}${chosen.category_label}`}」${isInv ? `×${qty}` : ""}。`;
      }
      submitBtn.disabled = inRun || (!draft.allowGold && !draft.allowBarter);
    }

    scopeSel.addEventListener("change", syncFormState);
    targetInput.addEventListener("input", () => { draft.target = targetInput.value; });
    qtyInput.addEventListener("input", syncFormState);
    priceInput.addEventListener("input", () => { draft.priceGold = priceInput.value; });
    goldToggle.addEventListener("change", () => {
      if (!goldToggle.checked && !barterToggle.checked) {
        goldToggle.checked = true;
        return;
      }
      draft.allowGold = goldToggle.checked;
      syncFormState();
    });
    barterToggle.addEventListener("change", () => {
      if (!barterToggle.checked && !goldToggle.checked) {
        barterToggle.checked = true;
        return;
      }
      draft.allowBarter = barterToggle.checked;
      syncFormState();
    });
    syncFormState();

    formDetails.append(
      node("summary", { text: inRun ? "发布新挂单（撤离回大厅后可操作）" : "＋ 发布新挂单（支持搜索选物 / 金币 / 以物换物 / 双选）" }),
      node("div", { className: "exchange-form" },
        node("div", { className: "exchange-row" }, scopeSel, targetInput),
        itemBox,
        qtyWrap,
        modeRow,
        goldRow,
        barterRow,
        hint, formErr, submitBtn,
      ),
    );
    wrap.append(formDetails);

    // --- 2. Barter Offer Picker Dialog (upgraded to searchable card modal!) ---
    function openBarterDialog(listing) {
      const candidates = (ex.tradable?.[listing.asset_type] || []).filter(item => {
        if (item.category !== listing.category || item.rarity !== listing.rarity) return false;
        if (listing.asset_type === "inventory" && (item.quantity || 1) < listing.quantity) return false;
        if (listing.want_item_id && item.id !== listing.want_item_id) return false;
        if ((listing.category === "rainbow" || listing.category === "shining") && item.id === listing.item_id) return false;
        return true;
      });

      const dialog = getDialog(ctx);
      const err = node("p", { className: "inv-detail-error", hidden: true });
      const searchInput = node("input", {
        type: "search", className: "exchange-picker-search",
        placeholder: "搜索你仓库中符合条件的物品、词条或 #编号…", "aria-label": "搜索交换物品",
      });
      const grid = node("div", { className: "exchange-picker-grid" });
      let pickedIdx = candidates.length ? 0 : -1;
      let query = "";

      const confirmBtn = button("确认以物换物", async () => {
        const chosen = candidates[pickedIdx];
        if (!chosen) return;
        err.hidden = true;
        const params = { listing_id: listing.listing_id, pay_mode: "barter" };
        if (listing.asset_type === "inventory") params.offer_stock_id = chosen.stock_id;
        else if (listing.asset_type === "equipment") params.offer_equip_id = chosen.equip_id;
        else params.offer_piece = chosen.piece;
        const ok = await ctx.execute(control("以物换物", "exchange_buy", params), {
          inline: true,
          onError: msg => { err.textContent = msg; err.hidden = false; },
        });
        if (ok) closeDetail();
      }, "primary");

      function renderCandidates() {
        const needle = query.trim().toLowerCase();
        const rows = candidates.map((c, idx) => ({ c, idx })).filter(({ c }) => {
          if (!needle) return true;
          const hay = [
            c.name, c.id, c.slot, c.quality, c.equip_id ? `#${c.equip_id}` : "", c.piece || "",
            ...(c.affixes || []).map(a => `${a.name} ${a.description || ""}`),
          ].filter(Boolean).join(" ").toLowerCase();
          return hay.includes(needle);
        });
        if (!rows.length) {
          grid.replaceChildren(node("p", {
            className: "inv-empty",
            text: candidates.length ? "没有匹配的物品，换个关键词试试。" : "你当前没有符合同品质同分类要求的可用物品。",
          }));
        } else {
          grid.replaceChildren(...rows.map(({ c, idx }) => {
            const isSel = idx === pickedIdx;
            const isEq = listing.asset_type === "equipment";
            const btn = button("", () => { pickedIdx = idx; renderCandidates(); },
              "inv-card exchange-pick-card" + (isSel ? " is-selected" : ""),
              { "data-grade": c.rarity });
            btn.append(
              node("span", { className: "inv-card-head" },
                node("span", { className: "inv-card-name", text: isEq ? `${c.name} #${c.equip_id}` : c.name }),
                node("span", { className: "inv-grade", text: isSel ? "✓ 已选" : (c.slot || c.rarity_label) }),
              ),
              isEq ? node("span", { className: "exchange-pick-sub", text: formatGearSummary(c) }) :
                (c.quality ? node("span", { className: "exchange-pick-sub", text: `品相：${c.quality}` }) : null),
              node("span", { className: "inv-card-meta" },
                node("span", { text: isEq ? "闲置装备" : `可用 ×${format(c.quantity || 1)}` }),
                node("span", { className: "inv-card-value", text: `消耗 ×${format(listing.quantity)}` }),
              ),
            );
            return btn;
          }));
        }
        confirmBtn.disabled = pickedIdx < 0 || !candidates[pickedIdx] || inRun;
      }

      const onSearch = ev => {
        if (ev.isComposing) return;
        query = searchInput.value;
        renderCandidates();
      };
      searchInput.addEventListener("input", onSearch);
      searchInput.addEventListener("compositionend", onSearch);

      dialog.replaceChildren(
        node("div", { className: "inv-detail-header", "data-grade": listing.rarity },
          node("div", {},
            node("p", { className: "inv-detail-meta", text: `${listing.rarity_label}${listing.category_label} · 挂单 #${listing.listing_id}` }),
            node("h2", { id: "inventory-detail-title", text: `以物换物 · ${listing.item_name} ×${format(listing.quantity)}` }),
          ),
          button("关闭", closeDetail, "inv-detail-close", { "aria-label": "关闭" }),
        ),
        node("div", { className: "inv-detail-body exchange-picker-body" },
          node("p", { className: "inv-detail-desc", text: `对方要求：${listing.want_item_name} ×${format(listing.quantity)}（须同品质同分类；虹彩须保留至少 1 件在仓）` }),
          candidates.length > 4 ? searchInput : null,
          grid, err,
          node("div", { className: "inv-detail-actions" }, confirmBtn),
        ),
      );
      renderCandidates();
      if (!dialog.open) dialog.showModal();
    }

    // --- 3. Listings Filter, Search & Cards ---
    const filterBar = node("div", { className: "inv-tabs exchange-filters" });
    const listSearch = node("input", {
      type: "search", className: "exchange-list-search",
      value: selection.exListQuery || "", placeholder: "🔍 搜索挂单物品、期望交换物、卖家或 #单号…",
      "aria-label": "搜索交易所挂单",
    });
    const listArea = node("div", { className: "exchange-grid" });
    const filterModes = [["all", "全部"], ["public", "公开市场"], ["direct", "1对1交易"], ["mine", "我的挂单"]];

    function renderList(fMode) {
      selection.exFilter = fMode;
      filterBar.replaceChildren(...filterModes.map(([id, label]) =>
        button(label, () => renderList(id), "inv-tab", { "aria-pressed": String(id === fMode) })));
      const needle = (selection.exListQuery || "").trim().toLowerCase();
      const rows = (ex.listings || []).filter(r => {
        const matchTab = fMode === "all" ? true : fMode === "mine" ? r.is_mine : r.scope === fMode;
        if (!matchTab) return false;
        if (!needle) return true;
        const hay = [
          `#${r.listing_id}`, r.item_name, r.want_item_name, r.seller_name,
          r.rarity_label, r.category_label, ...(r.affixes || []).map(a => a.name),
        ].filter(Boolean).join(" ").toLowerCase();
        return hay.includes(needle);
      });
      if (!rows.length) {
        listArea.replaceChildren(node("p", { className: "empty", text: needle ? "没有匹配的挂单。" : "当前分类暂无挂单。" }));
        return;
      }
      listArea.replaceChildren(...rows.map(r => {
        const actions = [];
        if (r.is_mine) {
          const cancelBtn = button("撤单取回", () => ctx.execute(control("撤单", "exchange_cancel", { listing_id: r.listing_id }), { inline: true }), "quiet small");
          cancelBtn.disabled = inRun;
          actions.push(cancelBtn);
        } else {
          if (r.mode === "gold" || r.mode === "both") {
            const buyBtn = button(`💰 ${format(r.price_gold)} 金买`, () =>
              ctx.execute(control("购买挂单", "exchange_buy", { listing_id: r.listing_id, pay_mode: "gold" }), { inline: true }), "primary small");
            buyBtn.disabled = inRun || state.player.gold < r.price_gold;
            actions.push(buyBtn);
          }
          if (r.mode === "barter" || r.mode === "both") {
            const barterBtn = button("🔄 选物交换", () => openBarterDialog(r), r.mode === "both" ? "quiet small" : "primary small");
            barterBtn.disabled = inRun;
            actions.push(barterBtn);
          }
          if (r.is_for_me) {
            const rejBtn = button("拒绝", () => ctx.execute(control("拒单", "exchange_reject", { listing_id: r.listing_id }), { inline: true }), "quiet small");
            rejBtn.disabled = inRun;
            actions.push(rejBtn);
          }
        }
        const metaStats = r.stats ? Object.entries(r.stats).map(([k, v]) => `${ctx.statNames[k] || k}${v >= 0 ? "+" : ""}${v}`).join(" · ") : "";
        const affixLine = r.affixes?.length ? "词条：" + r.affixes.map(a => a.name).join("、") : "";
        const termsLines = [];
        if (r.mode === "gold" || r.mode === "both") termsLines.push(`💰 售价：${format(r.price_gold)} 金`);
        if (r.mode === "barter" || r.mode === "both") termsLines.push(`🔄 ${r.mode === "both" ? "或换取" : "换取"}：${r.want_item_name} ×${format(r.quantity)}`);
        return node("article", { className: "panel exchange-card", "data-grade": r.rarity },
          node("div", { className: "exchange-card-head" },
            node("span", { className: "inv-grade-pill", "data-grade": r.rarity, text: `${r.rarity_label}${r.category_label}` }),
            node("span", { className: "tag" + (r.is_for_me ? " hot" : ""), text: r.scope === "direct" ? (r.is_for_me ? "🤝 发给你的 1对1" : `🤝 1对1 → ${r.target_user_id}`) : "📢 公开" }),
            r.mode === "both" ? node("span", { className: "tag", text: "金/换皆可" }) : null,
            node("small", { text: `#${r.listing_id}` }),
          ),
          node("strong", { className: "exchange-item-title", text: `${r.item_name} ×${format(r.quantity)}` }),
          metaStats ? node("p", { className: "item-facts", text: metaStats }) : null,
          affixLine ? node("p", { className: "item-facts", text: affixLine }) : null,
          node("div", { className: "exchange-terms-list" }, ...termsLines.map(t => node("p", { className: "exchange-terms", text: t }))),
          node("div", { className: "exchange-card-foot" },
            node("small", { text: `卖家：${r.is_mine ? "我" : r.seller_name}` }),
            node("div", { className: "exchange-card-actions" }, ...actions),
          ),
        );
      }));
    }

    const onListSearch = ev => {
      if (ev.isComposing) return;
      selection.exListQuery = listSearch.value;
      renderList(selection.exFilter || "all");
    };
    listSearch.addEventListener("input", onListSearch);
    listSearch.addEventListener("compositionend", onListSearch);

    renderList(selection.exFilter || "all");
    wrap.append(node("section", { className: "inv-list" },
      node("div", { className: "inv-list-head" }, node("h2", { text: "玩家交易所" }),
        node("span", { className: "inv-list-count", text: `${format((ex.listings || []).length)} 笔挂单` })),
      filterBar, listSearch, listArea));
    return wrap;
  }

  function showMarket(ctx) {
    const { node, selection } = ctx;
    const main = node("div", { className: "inv-content" });
    function render(mode) {
      selection.market = mode;
      if (mode === "exchange") {
        main.replaceChildren(exchangePanel(ctx));
        return;
      }
      const shop = mode === "shop";
      main.replaceChildren(section(ctx, mode, shop ? "补给站" : "本周兑换",
        shop ? ctx.state.shop : ctx.state.market, { filter: true, sort: shop ? rows => [...rows].sort((a,b) => a.price-b.price) : sorted }));
    }
    render(selection.market || "shop");
    return node("div", { className: "inventory" },
      tabs(ctx, [["shop", "商店"], ["market", "黑市"], ["exchange", "交易所"]], selection.market, render), main);
  }

  function showAtlas(ctx) {
    const { node, format } = ctx;
    const atlas = ctx.state.atlas || {};
    const stages = (atlas.stages || []).map(stage => {
      const needed = stage.required ?? Math.ceil(Number(stage.percent) * (atlas.total ?? 40) / 100);
      return { name: `${stage.percent}% · ${format(needed)} 件`, stats: stage.stats, unlocked: Boolean(stage.unlocked) };
    });
    const rainbowSkills = (atlas.rainbow || []).map(skill => ({ ...skill, rarity: "rainbow", unlocked: Boolean(skill.unlocked) }));
    const rainbow = [
      ...rainbowSkills,
      ...(atlas.resonance ? [{ ...atlas.resonance, rarity: "rainbow", unlocked: Boolean(atlas.resonance.unlocked) }] : []),
    ];
    const collections = (atlas.items || [])
      .filter(item => grade(item) !== "rainbow")
      .map(item => ({ ...item, unlocked: Boolean(item.unlocked ?? (Number(item.quantity) > 0)) }));
    return node("div", { className: "inventory inv-content" },
      node("section", { className: "inv-list inv-atlas-progress" },
        node("div", { className: "inv-list-head" }, node("h2", { text: "图鉴点亮进度" }),
          node("span", { className: "inv-list-count", text: `已点亮 ${format(atlas.known)} / ${format(atlas.total)}` })),
        node("progress", { className: "inv-atlas-meter", max: Math.max(1, atlas.total || 0), value: atlas.known || 0,
          "aria-label": "图鉴收集进度" }),
      ),
      stages.length ? section(ctx, "stages", "阶段增益", stages, {
        sort: list => list.slice(), paginate: false,
        countText: list => `已解锁 ${format(list.filter(s => s.unlocked).length)} / ${format(list.length)}`,
      }) : null,
      section(ctx, "atlas", "藏品图鉴", collections, {
        filter: true, sort: sortAtlas, paginate: false,
        countText: list => `已点亮 ${format(list.filter(i => i.unlocked).length)} / ${format(list.length)}`,
      }),
      heirloomCabinet(ctx,true),
      rainbow.length ? section(ctx, "rainbow", "虹彩套装 · 背包技能", rainbow, {
        sort: sortAtlas, paginate: false,
        countText: () => `已点亮 ${format(rainbowSkills.filter(s => s.unlocked).length)} / ${format(rainbowSkills.length)}`,
      }) : null,
    );
  }

  return { showStorage, showGear, showMarket, showAtlas, showItem: showDetail, closeDetail, fieldSlots, activeFieldItems, carriedBag, bagGroups };
})();
