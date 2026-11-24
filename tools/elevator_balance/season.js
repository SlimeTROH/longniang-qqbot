"use strict";
const $ = id => document.getElementById(id);
let defs, last;
let selectedFields = [];
const percentage = n => (n * 100).toFixed(1) + "%";
const number = n => Math.round(n).toLocaleString("zh-CN");
function options(select, pairs) {
  select.replaceChildren(...pairs.map(([value,label]) => new Option(label,value)));
}
function check(container,value,label,change) {
  const row = document.createElement("label"), input = document.createElement("input");
  row.className="tick"; input.type="checkbox"; input.value=value;
  input.addEventListener("change",() => change?.(input.checked));
  row.append(input,label); container.append(row); return input;
}
function mapChanged() {
  selectedFields=[]; $("fields").replaceChildren(); $("fielddesc").textContent="";
  for (const [id,item] of Object.entries(defs.field)) {
    if (!["all",$("map").value].includes(item.map)) continue;
    const elem=check($("fields"),id,item.name,active=>{
      selectedFields=selectedFields.filter(k=>k!==id);
      if(active) selectedFields.push(id);
      $("fielddesc").textContent=item.name+" · "+item.effect;
    });
    elem.parentElement.title=item.effect;
  }
}
function presetChanged() {
  const key=$("preset").value, [,grade,veteran,known]=defs.presets[key];
  $("grade").value=grade||"green"; $("extracts").value=veteran; $("known").value=known;
  chooseGear(grade||"green", !grade);
  $("rainbow").querySelectorAll("input").forEach(el=>el.checked=key==="rainbow");
}
function chooseGear(grade, empty = false) {
  const red = grade === "red";
  const defaults = red ? defs.red_standard : defs.standard;
  Array.from($("slots").querySelectorAll("select")).forEach((select,index) => {
    const pool = Object.entries(defs.gear[select.dataset.slot]).filter(([id]) => defs.red_bases.includes(id) === red);
    options(select, [["","空槽"], ...pool]);
    select.value = empty ? "" : defaults[index];
  });
}
function table(id,headers,rows) {
  const table=document.createElement("table"), head=table.createTHead().insertRow(), body=table.createTBody();
  for(const h of headers) { const cell=document.createElement("th"); cell.textContent=h; head.append(cell); }
  for(const row of rows) { const tr=body.insertRow(); for(const value of row) tr.insertCell().textContent=value; }
  $(id).replaceChildren(table);
}
async function run(mode) {
  $("run").disabled=$("trip").disabled=true; $("status").textContent="正在逐回合模拟…";
  const req={operation:"season",mode,map:$("map").value,preset:$("preset").value,map_scale:+$("scale").value,
    health:+$("health").value,grade:$("grade").value,extracts:+$("extracts").value,known:+$("known").value,
    rainbow:[...$("rainbow").querySelectorAll("input:checked")].map(x=>x.value),
    gear:[...$("slots").querySelectorAll("select")].map(x=>x.value),roll:+$("roll").value,
    field:selectedFields,medicines:[...$("boss-medicine").querySelectorAll("input:checked")].map(x=>x.value),
    floors:$("floors").value.trim().split(/[,，\s]+/).map(Number),samples:+$("samples").value,
    seed:+$("seed").value,use_field:$("usefield").checked,coffee:+$("coffee").value,smoke:+$("smoke").value};
  try {
    req.overrides=JSON.parse($("overrides").value.trim()||"{}");
    const response=await fetch("/api/calculate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(req)});
    const r=await response.json(); if(!response.ok) throw new Error(r.error||"计算失败");
    last={request:req,result:r}; $("download").disabled=false;
    const labels={hp:"生命",atk:"攻击",def:"防御",agi:"敏捷",luk:"幸运",power:"配装评分"};
    $("panel").replaceChildren(...Object.entries({...r.stats,power:r.power}).map(([k,v])=>{
      const d=document.createElement("div"), b=document.createElement("strong"), s=document.createElement("span");
      b.textContent=number(v);s.textContent=labels[k];d.append(b,s);return d;
    }));
    if(r.fights) table("fights",["楼层 / 敌人","胜率","95% 区间","平手","平均回合","平均剩余 HP"],r.fights.map(x=>[
      "F"+x.floor+" · "+({normal:"普通",elite:"精英",boss:"首领"})[x.kind],percentage(x.win),x.ci.map(percentage).join(" – "),
      percentage(x.draw),x.rounds.toFixed(1),number(x.hp_after_combat)]));
    if(r.trips) table("trips",["目标层","清关撤离率","95% 区间","平均净金","中位净金","亏损概率"],r.trips.map(x=>[
      "F"+x.floor,percentage(x.target_exit),[x.ci_low,x.ci_high].map(percentage).join(" – "),number(x.net),number(x.median),percentage(x.negative)]));
    if (r.trips) $("fights").replaceChildren(); else $("trips").replaceChildren();
    table("boxes",["装备箱","售价","期望回收","净金期望"],Object.entries(r.economy.boxes).map(([k,x])=>[
      ({gear_box:"新手",gear_box_mid:"进阶",gear_box_high:"尊享"})[k],number(x.price),x.sale_ev.toLocaleString("zh-CN"),x.net.toLocaleString("zh-CN")]));
    $("status").textContent="已完成，每格 "+number((r.fights||r.trips)[0].n)+" 个样本";
  } catch(error) { $("status").textContent=error.message; }
  finally { $("run").disabled=$("trip").disabled=false; }
}
async function init() {
  defs=await fetch("/api/season-defaults").then(r=>r.json());
  options($("map"),Object.entries(defs.maps)); options($("preset"),Object.entries(defs.presets).map(([k,v])=>[k,v[0]]));
  for(const [slot,gear] of Object.entries(defs.gear)) {
    const label=document.createElement("label"),select=document.createElement("select");
    select.dataset.slot=slot; label.append(slot,select); $("slots").append(label);
  }
  Object.entries(defs.rainbow).forEach(([k,n])=>check($("rainbow"),k,n));
  Object.entries(defs.medicines).forEach(([k,n])=>check($("boss-medicine"),k,n));
  $("preset").value="green"; presetChanged(); mapChanged();
  $("preset").onchange=presetChanged; $("map").onchange=mapChanged;
  $("grade").onchange=()=>chooseGear($("grade").value);
  $("run").onclick=()=>run("battle"); $("trip").onclick=()=>run("trip");
  $("example").onclick=()=>{
    $("overrides").value=JSON.stringify({
      curve:{boss:{hp:[175,2.3,6.5],atk:[28,.18,.55]}},equipment:{crowbar:{atk:12}},
      map:{rare_rate:.018,value_multiplier:1},box_prices:{gear_box_mid:1600},gear_sell:{purple:1450}
    },null,2);
  };
  $("download").onclick=()=>{
    const url=URL.createObjectURL(new Blob([JSON.stringify(last,null,2)],{type:"application/json"}));
    const a=document.createElement("a");a.href=url;a.download="首领赛季测算.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  await run("battle");
}
init().catch(error=>{$("status").textContent=error.message;});
