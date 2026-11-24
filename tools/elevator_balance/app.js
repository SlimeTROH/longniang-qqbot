"use strict";
const el=id=>document.getElementById(id);
const copy=x=>JSON.parse(JSON.stringify(x));
const esc=x=>String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const fmt=(x,n=1)=>x==null?"—":Number(x).toLocaleString("zh-CN",{maximumFractionDigits:n});
const pct=x=>(100*x).toFixed(1)+"%";
const pp=x=>(x>=0?"+":"")+(100*x).toFixed(2)+" 个百分点";
const statName={hp:"生命",atk:"攻击",def:"防御",agi:"敏捷",luk:"幸运"};
const colors=["#17665e","#ad603f","#3e78a5","#795886"];
const state={tab:"battle",config:{},revision:0,busy:false,battle:null,trip:null};
let defaults,hero,policy;

function table(head,rows){
    return '<div class="scroll"><table><thead><tr>'+head.map(c=>"<th>"+esc(c)+"</th>").join("")+
        "</tr></thead><tbody>"+rows.map(row=>"<tr>"+row.map(c=>"<td>"+esc(c)+"</td>").join("")+"</tr>").join("")+"</tbody></table></div>";
}
function metric(label,value,note){
    return '<div class="kpi"><small>'+esc(label)+"</small><strong>"+esc(value)+"</strong><em>"+esc(note)+"</em></div>";
}
function markDirty(custom=false){
    state.revision++;
    state.fit=null;el("gearfitapply").hidden=true;el("gearfitresult").textContent="";
    el("orderresult").textContent="";
    if(custom) el("plan").value="custom";
    statusText("参数已修改，点击重新计算。");
}
function statusText(msg,err=false){el("status").textContent=msg;el("status").classList.toggle("error",err);}
function num(parent,label,value,change,step="any",bounds={}){
    const lab=document.createElement("label"),input=document.createElement("input");
    lab.append(document.createTextNode(label));input.type="number";input.step=step;input.value=value;
    Object.assign(input,bounds);lab.append(input);parent.append(lab);
    input.addEventListener("change",()=>{if(input.value===""){input.value=value;return;}change(Number(input.value));});
    return input;
}
function select(parent,label,choices,value,change){
    const lab=document.createElement("label"),s=document.createElement("select");
    lab.append(document.createTextNode(label));for(const [k,v] of choices) s.add(new Option(v,k));
    s.value=value;lab.append(s);parent.append(lab);s.onchange=()=>change(s.value);return s;
}
function tick(parent,label,checked,change){
    const l=document.createElement("label"),c=document.createElement("input");l.className="tick";c.type="checkbox";c.checked=checked;
    l.append(c,document.createTextNode(label));parent.append(l);c.onchange=()=>change(c.checked);
}
function block(parent,title){if(title){const h=document.createElement("h3");h.textContent=title;parent.append(h);}
    const b=document.createElement("div");b.className="fields";parent.append(b);return b;}
function mapOverride(){state.config.maps??={};return state.config.maps[el("map").value]??={};}
function mapValue(){return {...defaults.maps[el("map").value],...state.config.maps?.[el("map").value]};}
function tuned(){const m=mapOverride();m.difficulty??=copy(defaults.tuning);return m.difficulty;}
function clearFit(){const m=mapOverride();if(m.difficulty)delete m.difficulty.mixtures;}
function currentReq(operation){
    const floor=Number(el("floor").value), horizon=Math.max(floor,Number(el("horizon").value));
    el("horizon").value=horizon;
    return {operation,config:state.config,hero,map:el("map").value,floor,horizon,
        samples:Number(el("samples").value),seed:Number(el("seed").value),policy,risk:state.risk??.7};
}
function download(name,blob){
    const ref=URL.createObjectURL(blob),a=document.createElement("a");a.href=ref;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(ref),2000);
}
async function request(url,body){
    const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(!r.ok){const e=await r.json();throw Error(e.error||"计算失败");}
    return r;
}
async function compute(operation){
    if(state.busy)return;
    const revision=state.revision;
    state.busy=true;["run","triprun","fit"].forEach(k=>el(k).disabled=true);
    statusText(operation==="expedition"?"正在按同一随机种子模拟两套远征……":"正在计算胜率和面板……");
    try{
        const req=currentReq(operation);
        if(operation==="calibrate")req.targets={normal:[+el("n1").value/100,+el("ne").value/100],
            elite:[+el("e1").value/100,+el("ee").value/100],half:+el("halfcal").value,tail_weight:+el("tailcal").value/100};
        const resp=await request("/api/calculate",req),result=await resp.json();
        if(revision!==state.revision){statusText("参数在计算中发生变化，请重新计算。");return;}
        if(operation==="calibrate"){
            tuned().mixtures=result.mixtures;markDirty(true);renderRules();renderCalibration();
            el("cal-result").innerHTML="<h3>已写入当前地图的 B 方案</h3>"+table(["敌人","弱模板胜率","强模板胜率","首层强模板占比","极限强模板占比"],
                Object.entries(result.mixtures).map(([k,v])=>[k==="elite"?"精英":"普通",pct(v.weak_win),pct(v.strong_win),pct(v.start_weight),pct(v.end_weight)]));
            statusText("校准完成，抽查至第 1,000 层，最大概率误差 "+result.max_error.toExponential(2)+"。");
        }else if(operation==="expedition"){
            state.trip={req:copy(req),result};renderTrip();statusText("远征计算完成，每套方案 "+fmt(req.samples,0)+" 条独立路径。");
        }else{
            state.battle={req:copy(req),result};renderBattle();statusText("单场计算完成。");
        }
    }catch(e){statusText(e.message,true);}
    finally{state.busy=false;["run","triprun","fit"].forEach(k=>el(k).disabled=false);}
}
function changeTab(name){
    state.tab=name;
    document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("on",b.dataset.tab===name));
    document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.id===name));
    requestAnimationFrame(()=>{if(name==="battle"&&state.battle)battleChart();if(name==="trip"&&state.trip)tripChart();});
}

function chart(canvas,legend,hover,series,unit,floor){
    const rect=canvas.getBoundingClientRect();if(rect.width<20)return;
    const w=rect.width,h=rect.height,ratio=window.devicePixelRatio||1;
    canvas.width=Math.round(w*ratio);canvas.height=Math.round(h*ratio);
    const c=canvas.getContext("2d");c.scale(ratio,ratio);
    const left=unit==="prob"?43:58,right=w-14,top=14,bottom=h-29;
    const count=series[0].v.length,all=series.flatMap(s=>s.v);
    const low=unit==="prob"?0:Math.min(0,...all),high=unit==="prob"?1:Math.max(1,...all);
    const x=i=>left+(right-left)*i/Math.max(1,count-1),y=v=>bottom-(bottom-top)*(v-low)/(high-low);
    c.font="12px 'Microsoft YaHei',sans-serif";c.lineWidth=1;
    for(let j=0;j<=4;j++){const val=low+j*(high-low)/4,yy=y(val);
        c.strokeStyle="#dce5df";c.beginPath();c.moveTo(left,yy);c.lineTo(right,yy);c.stroke();
        c.fillStyle="#566962";c.textAlign="right";c.fillText(unit==="prob"?Math.round(100*val)+"%":fmt(val,0),left-5,yy+4);}
    c.textAlign="center";const ticks=new Set([0,count-1,...[1,2,3,4].map(i=>Math.round(i*(count-1)/5))]);
    for(const i of ticks)c.fillText("F"+(i+1),x(i),bottom+19);
    c.strokeStyle="#94a79d";c.setLineDash([2,4]);c.beginPath();c.moveTo(x(floor-1),top);c.lineTo(x(floor-1),bottom);c.stroke();
    series.forEach((s,j)=>{c.strokeStyle=colors[j];c.lineWidth=2.7;c.setLineDash(j>=2?[6,4]:[]);
        c.beginPath();s.v.forEach((v,i)=>{if(i)c.lineTo(x(i),y(v));else c.moveTo(x(i),y(v));});c.stroke();
        c.setLineDash([]);c.fillStyle=colors[j];c.beginPath();c.arc(x(floor-1),y(s.v[floor-1]),3.5,0,2*Math.PI);c.fill();});
    legend.innerHTML=series.map((s,i)=>'<span><b style="background:'+colors[i]+'"></b>'+esc(s.n)+"</span>").join("");
    function label(i){hover.textContent="F"+(i+1)+"　"+series.map(s=>s.n+" "+(unit==="prob"?pct(s.v[i]):fmt(s.v[i]))).join("　");}
    canvas.onpointermove=e=>label(Math.max(0,Math.min(count-1,Math.round((e.clientX-rect.left-left)/(right-left)*(count-1)))));
    canvas.onpointerleave=()=>label(floor-1);label(floor-1);
}
function battleChart(){
    const {result:r,req}=state.battle;
    const series=[["A","normal","线上普通"],["B","normal","方案普通"],["A","elite","线上精英"],["B","elite","方案精英"]]
        .map(([p,k,n])=>({n,v:r[p].curve.map(x=>x[k].win)}));
    chart(el("battleplot"),el("battle-legend"),el("battle-hover"),series,"prob",req.floor);
}
function renderBattle(){
    const {result:r,req}=state.battle, a=r.A.curve[req.floor-1],b=r.B.curve[req.floor-1];
    el("battle-caption").textContent=defaults.maps[req.map].name+" · F"+req.floor+" · "+
        (b.normal.method==="DP"?"有限回合精确递推":"实装战斗抽样，每点 "+fmt(req.samples,0)+" 场");
    el("battle-kpis").innerHTML=metric("方案普通怪击杀率",pct(b.normal.win),"线上 "+pct(a.normal.win))+
        metric("方案精英击杀率",pct(b.elite.win),"线上 "+pct(a.elite.win))+
        metric("方案普通怪平均掉血",fmt(b.normal.hp_loss),"线上 "+fmt(a.normal.hp_loss))+
        metric("方案玩家战力",fmt(r.B.strength.index),"线上 "+fmt(r.A.strength.index)+" · 裸装基准 100");
    let html=table(["F"+req.floor,"击杀","阵亡","平局","平均回合","平均净掉血","胜率误差"],
        [["线上普通",a.normal],["方案普通",b.normal],["线上精英",a.elite],["方案精英",b.elite]]
        .map(([n,x])=>[n,pct(x.win),pct(x.loss),pct(x.draw),fmt(x.rounds,2),fmt(x.hp_loss,2),x.method==="DP"?"精确递推":mcInterval(x)]));
    html+="<h3>当前楼层的宝箱与逃跑</h3>"+table(["","单抽估值","可变现期望","红色概率","收藏品概率","逃跑判定成功率"],
        ["A","B"].map(p=>[p,fmt(r[p].chest.gross),fmt(r[p].chest.sale),pct(r[p].chest.red),pct(r[p].chest.collection),pct(r[p].flee)]));
    html+=table(["普通怪","硬打存活率","选择逃跑后的总存活率","逃跑失败后反杀（每次逃跑计）","点逃跑的平均掉血"],
        ["A","B"].map(p=>[p,pct(1-r[p].curve[req.floor-1].normal.loss),pct(r[p].flee_choice.survive),
            pct(r[p].flee_choice.kill),fmt(r[p].flee_choice.hp_loss,1)]));
    html+="<h3>敌方面板</h3>"+table(["方案","类型","生命 / 攻击 / 防御 / 敏捷","强模板概率"],
        ["A","B"].flatMap(p=>["normal_enemy","elite_enemy"].map(k=>{
            const v=r[p][k],form=s=>[s.hp,s.atk,s.def,s.agi].join(" / ");
            let hard=v.hard_near?form(v.hard_near)+" 或 "+form(v.strong):v.strong?form(v.strong):"";
            if(v.tail)hard+="，深层精英 "+form(v.tail)+"（本层占精英 "+pct(v.tail_probability)+"）";
            return [p,k==="normal_enemy"?"普通":"精英",v.strong?form(v.weak)+" → "+hard:form(v.weak),v.strong?pct(v.weight):"单一模板"];
        })));
    el("battle-detail").innerHTML=html;
    el("battle-rows").innerHTML=table(["层","A 普通击杀","B 普通击杀","B 普通阵亡","B 普通平局","B 普通回合","A 精英击杀","B 精英击杀"],
        r.B.curve.map((v,i)=>[v.floor,pct(r.A.curve[i].normal.win),pct(v.normal.win),pct(v.normal.loss),pct(v.normal.draw),fmt(v.normal.rounds),pct(r.A.curve[i].elite.win),pct(v.elite.win)]));
    el("marginal").innerHTML=table(["添加属性","线上普通胜率变化","方案普通胜率变化","方案平均掉血变化"],
        r.B.sensitivity.map((d,i)=>[statName[d.stat]+" +"+d.delta,pp(r.A.sensitivity[i].win),pp(d.win),fmt(d.loss_hp,2)]))+
        "<p>观察楼层 F"+req.floor+"。面板 A："+esc(Object.entries(r.A.hero.stats).map(([k,v])=>statName[k]+v).join("　"))+
        "。面板 B："+esc(Object.entries(r.B.hero.stats).map(([k,v])=>statName[k]+v).join("　"))+"。</p>";
    showBoxes();if(state.tab==="battle")battleChart();
}
function mcInterval(x){const n=x.n,p=x.win,z=1.96,den=1+z*z/n,c=(p+z*z/(2*n))/den,
    d=z*Math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den;
    return x.mixed?"混合估计 SE "+pp(x.se):pct(Math.max(0,c-d))+"～"+pct(Math.min(1,c+d));}
function tripChart(){
    const {result:r,req}=state.trip,k=el("tripmetric").value;
    chart(el("tripplot"),el("trip-legend"),el("trip-hover"),
        ["A","B"].map((p,i)=>({n:i?"方案 B":"线上 A",v:r[p].rows.map(x=>x[k])})),
        ["target_exit","death"].includes(k)?"prob":"number",req.floor);
}
function renderTrip(){
    const {result:r,req}=state.trip,a=r.A.rows[req.floor-1],b=r.B.rows[req.floor-1],d=r.paired[req.floor-1];
    el("trip-kpis").innerHTML=metric("目标层撤离率 B",pct(b.target_exit),"线上 "+pct(a.target_exit))+
        metric("每票净变现期望 B",fmt(b.net,0)+" 金","线上 "+fmt(a.net,0))+
        metric("净变现中位数 B",fmt(b.median,0)+" 金","P10 "+fmt(b.p10,0)+" · P90 "+fmt(b.p90,0))+
        metric("满足风险线的最高均值",r.B.best?"F"+r.B.best:"暂无","线上 "+(r.A.best?"F"+r.A.best:"暂无"));
    el("trip-detail").innerHTML=table(["F"+req.floor,"到层概率","目标层撤离 95% 区间","任意层撤离","失败","均值约 95% 半宽","亏损概率"],
        [["A",a],["B",b]].map(([s,x])=>[s,pct(x.reach),pct(x.ci_low)+"～"+pct(x.ci_high),pct(x.any_exit),pct(x.death),
            "±"+fmt(1.96*x.net_se,0)+" 金",pct(x.negative)]))+
        "<p>同种子配对，B − A 的每票净变现差 "+fmt(d.mean,0)+" ± "+fmt(1.96*d.se,0)+" 金（约 95%）。风险线按目标层撤离率的 Wilson 下限筛选。</p>";
    el("trip-rows").innerHTML=table(["层","A 撤离率","B 撤离率","A 净期望","B 净期望","B P10","B 中位","B P90","B 再走一层收益差"],
        r.B.rows.map((v,i)=>[v.floor,pct(r.A.rows[i].target_exit),pct(v.target_exit),fmt(r.A.rows[i].net,0),fmt(v.net,0),fmt(v.p10,0),fmt(v.median,0),fmt(v.p90,0),
            v.delta_net==null?"—":fmt(v.delta_net,0)+" ± "+fmt(1.96*v.delta_se,0)]));
    if(state.tab==="trip")tripChart();
}

function renderPolicy(){
    const p=el("policy");p.replaceChildren();
    select(p,"遇敌策略",[["fight","全打"],["elite_flee","普怪打、精英逃"],["flee","非伏击一律逃"]],policy.combat,v=>{policy.combat=v;markDirty();});
    select(p,"目标层撤离时机",[["clear","清完本层再撤"],["arrival","到层即按撤离"],["wait","到层等待 30 分钟拉回"]],policy.exit,v=>{policy.exit=v;markDirty();});
    select(p,"奇遇处理",[["safe","选免费且确定成功的选项"],["skip","一律离开"]],policy.event,v=>{policy.event=v;markDirty();});
    select(p,"新手期",[["mature","第 4 局及以后"],["insured","第 2～3 局保险"],["first","首局保险和首层宝箱"]],policy.insurance,v=>{policy.insurance=v;markDirty();});
    for(const [k,label,max] of [["coffee","出门咖啡（瓶）",8],["smoke","出门烟雾弹（颗）",4],["box","安全箱格数",4],["ticket_value","每票机会成本（金）",10000]])
        num(p,label,policy[k],v=>{policy[k]=v;markDirty();},1,{min:0,max});
    num(p,"最低目标层撤离率（%）",100*(state.risk??.7),v=>{state.risk=v/100;markDirty();},1,{min:0,max:100});
    tick(p,"血量 ≤60% 喝咖啡，门口优先用烟",policy.auto_use,v=>{policy.auto_use=v;markDirty();});
    tick(p,"已有群金雨",policy.blessing,v=>{policy.blessing=v;markDirty();});
}
function renderHero(){
    const d=el("hero-edit");d.replaceChildren();const b=block(d,"参考玩家");
    const changed=()=>{el("preset").value="custom";el("follow").checked=false;markDirty();};
    num(b,"累计成功撤离",hero.extracts,v=>{hero.extracts=v;changed();},1,{min:0});
    num(b,"开战生命（%）",100*hero.hp_fraction,v=>{hero.hp_fraction=v/100;changed();},1,{min:1,max:100});
    num(b,"额外开局护盾",hero.shield,v=>{hero.shield=v;changed();},1,{min:0,max:1000});
    num(b,"预设攻击增幅（%）",100*hero.atk_boost,v=>{hero.atk_boost=v/100;changed();},1,{min:0,max:60});
    for(const slot of ["武器","护甲","挂件"]){
        const row=block(d),items=Object.entries(defaults.gear).filter(([,g])=>g.slot===slot);
        const piece=hero.gear.find(p=>defaults.gear[p.id].slot===slot);
        select(row,slot,[["","空"],...items.map(([k,v])=>[k,v.name])],piece?.id??"",v=>{
            hero.gear=hero.gear.filter(p=>defaults.gear[p.id].slot!==slot);
            if(v)hero.gear.push({id:v,grade:piece?.grade??"blue",roll:piece?.roll??1});changed();renderHero();
        });
        const s=select(row,"品质",Object.entries(defaults.grade_labels),piece?.grade??"blue",v=>{if(piece){piece.grade=v;changed();}});
        const n=num(row,"成品浮动",piece?.roll??1,v=>{if(piece){piece.roll=v;changed();}},.01,{min:.9,max:1.1});
        s.disabled=n.disabled=!piece;
    }
    const adjust=block(d,"手动面板加减值");for(const k of Object.keys(statName))
        num(adjust,statName[k],hero.bonus[k]??0,v=>{hero.bonus[k]=v;changed();},1);
    for(const [key,defs,title] of [["perks",defaults.perks,"生效词条"],["sets",defaults.sets,"已集齐套装"]]){
        const part=block(d,title);part.className="checks";
        for(const [id,value] of Object.entries(defs)){
            tick(part,value.name??id,hero[key].includes(id),yes=>{hero[key]=hero[key].filter(k=>k!==id);if(yes)hero[key].push(id);changed();});
            part.lastChild.title=value.description;
        }
    }
    const g=el("gear-edit");g.replaceChildren();const rates=block(g,"正属性品质倍率");
    for(const [key,base] of Object.entries(defaults.grades))
        num(rates,defaults.grade_labels[key]+"（A "+base+"）",state.config.gear_factors?.[key]??base,v=>{
            state.config.gear_factors??={};state.config.gear_factors[key]=v;markDirty(true);
        },.05,{min:.1,max:10});
    const wrap=document.createElement("div");wrap.className="scroll";const t=document.createElement("table");
    t.innerHTML="<thead><tr>"+["底材","部位",...Object.values(statName)].map(n=>"<th>"+esc(n)+"</th>").join("")+"</tr></thead>";
    const tbody=t.createTBody();
    for(const [id,v] of Object.entries(defaults.gear)){
        const tr=tbody.insertRow();tr.insertCell().textContent=v.name;tr.insertCell().textContent=v.slot;
        for(const k of Object.keys(statName)){
            const c=tr.insertCell(),input=document.createElement("input");input.type="number";input.step=1;
            input.value=(state.config.equipment?.[id]??v.stats)[k]??0;input.title="线上 A："+(v.stats[k]??0);
            input.setAttribute("aria-label",v.name+" "+statName[k]);c.append(input);
            input.onchange=()=>{state.config.equipment??={};state.config.equipment[id]??=copy(v.stats);
                state.config.equipment[id][k]=Number(input.value);markDirty(true);};
        }
    }
    wrap.append(t);g.append(wrap);
    const fit=el("gearfit");fit.replaceChildren();
    const invalidate=()=>{state.fit=null;el("gearfitapply").hidden=true;el("gearfitresult").textContent="";};
    select(fit,"穿戴底材",hero.gear.map(p=>[p.id,defaults.gear[p.id].name]),hero.gear[0]?.id??"",invalidate).id="fititem";
    select(fit,"待调整属性",Object.entries(statName),"atk",invalidate).id="fitstat";
    select(fit,"对手",[["normal","普通"],["elite","精英"]],"normal",invalidate).id="fitenemy";
    num(fit,"目标胜率（%）",80,invalidate,.1,{min:.1,max:99.99}).id="fitpercent";
    el("gearfitgo").disabled=!hero.gear.length;
}
function renderCalibration(){
    const p=el("cal-edit");p.replaceChildren();const k=el("map").value,targets=defaults.targets[k],mix=mapValue().difficulty?.mixtures;
    const row=block(p);
    for(const [id,label,value] of [
        ["n1","普通首层胜率（%）",100*(mix?.normal?.target_first??targets[0][0])],
        ["ne","普通极限胜率（%）",100*(mix?.normal?.target_end??targets[0][1])],
        ["e1","精英首层胜率（%）",100*(mix?.elite?.target_first??targets[1][0])],
        ["ee","精英极限胜率（%）",100*(mix?.elite?.target_end??targets[1][1])],
        ["halfcal","半程层数 h",mix?.normal?.half??12],
        ["tailcal","强档中的深层精英（%）",100*(mix?.elite?.tail_weight??0)]]){
        const n=num(row,label,Number(value.toFixed(3)),()=>{},.01);n.id=id;
    }
    const reference=mix?.normal?.reference;
    el("cal-result").textContent=reference?"普通怪原校准面板："+Object.entries(reference.stats).map(([s,v])=>statName[s]+v).join("　")+
        "。更换玩家后可在单场战斗页重算。":mix?"本图已配置概率混合模板。":"本图目前按怪物属性公式出怪。";
}
function renderRules(){
    const d=el("map-edit");d.replaceChildren();
    const key=el("map").value,current=mapValue(),t={...defaults.tuning,...current.difficulty};
    let row=block(d,defaults.maps[key].name);
    select(row,"属性增长方式",[["live","线上多项式"],["bounded","有上限的属性增长"]],t.mode,v=>{
        clearFit();tuned().mode=v;markDirty(true);renderRules();renderCalibration();
    });
    for(const [k,name,step] of [["multiplier","怪物倍率",.05],["value_multiplier","估值倍率",.1],["rare_rate","基础稀有率（0～1）",.005],
        ["entry","金币门票",50],["ambush_floor","伏击起始层",1],["ambush_rate","伏击率（0～1）",.01]]){
        num(row,name,current[k],v=>{mapOverride()[k]=v;if(k==="multiplier")clearFit();markDirty(true);renderCalibration();},step);
    }
    row=block(d,"怪物属性曲线");
    for(const [k,name,step] of [["cap_floor","极限等效线上楼层",.1],["half","属性半程层数",.25],["hp_scale","生命系数",.05],
        ["atk_scale","攻击系数",.05],["elite_hp","精英生命倍数",.05],["elite_atk","精英攻击倍数",.05]])
        num(row,name,t[k],v=>{clearFit();tuned()[k]=v;markDirty(true);renderCalibration();},step);
    const note=document.createElement("p");note.className="note";note.textContent=t.mixtures?
        "胜率校准模板正在决定本图的怪物属性。修改属性曲线或怪物倍率会切回属性公式。":"属性软上限用线上第 1 层面板做起点、极限等效层做终点。HP、攻击、防御、敏捷分别插值后取整。";
    d.append(note);
    const reset=document.createElement("button");reset.textContent="本图改用属性公式";reset.onclick=()=>{
        clearFit();markDirty(true);renderRules();renderCalibration();
    };d.append(reset);
    row=block(d,"玩家全局基础面板 B");
    for(const [k,value] of Object.entries(defaults.stats))num(row,statName[k],state.config.stats?.[k]??value,v=>{
        state.config.stats??={};state.config.stats[k]=v;markDirty(true);
    },1);
    const groups=state.config.event_weights??defaults.weights;
    for(const [i,r] of groups.entries()){
        row=block(d,"F"+r.from+" 起的事件权重");
        for(const [k,label] of [["loot","宝箱"],["normal","普通"],["elite","精英"],["choice","奇遇"],["relic","遗物"]]){
            num(row,label,r.weights[k],v=>{state.config.event_weights??=copy(defaults.weights);
                state.config.event_weights[i].weights[k]=v;markDirty(true);},1,{min:0});
        }
    }
    row=block(d,"装备回收价（金）");
    for(const [k,value] of Object.entries(defaults.sell))
        num(row,defaults.grade_labels[k]+"装",state.config.gear_sell?.[k]??value,v=>{state.config.gear_sell??={};state.config.gear_sell[k]=v;markDirty(true);},10);
    row=block(d,"装备箱价格（金）");
    for(const [k,v] of Object.entries(defaults.boxes))
        num(row,v.name,state.config.shop?.[k]??v.cost,value=>{state.config.shop??={};state.config.shop[k]=value;markDirty(true);},10);
    const output=document.createElement("div");output.id="box-values";d.append(output);
    showBoxes();
    row=block(d,"按地图顺序生成递减增量的怪物倍率");
    const v1=num(row,"首图倍率",1,()=>{},.05),end=num(row,"极限倍率",3.5,()=>{},.05),decay=num(row,"相邻增量比",.7,()=>{},.01);
    const apply=document.createElement("button");apply.textContent="写入四图倍率并改用属性曲线";d.append(apply);
    apply.onclick=()=>{
        const x=+v1.value,y=+end.value,r=+decay.value;
        if(!(x>=.1&&x<=12&&y>=.1&&y<=12&&r>0&&r<1)){statusText("倍率应为 0.1～12，相邻增量比应在 0 和 1 之间。",true);return;}
        state.config.maps??={};
        Object.keys(defaults.maps).forEach((k,i)=>{const map=state.config.maps[k]??={};
            map.multiplier=+(y+(x-y)*r**i).toFixed(6);if(map.difficulty)delete map.difficulty.mixtures;});
        markDirty(true);renderRules();renderCalibration();
    };
}
function showBoxes(){
    if(!el("box-values")||!state.battle)return;
    const {A,B}=state.battle.result;
    el("box-values").innerHTML="<h3>开箱后按原样回收的现金期望</h3>"+table(["装备箱","A 净收益","B 净收益","B 期望回收率"],
        Object.keys(A.boxes).map(k=>[A.boxes[k].name,fmt(A.boxes[k].net),fmt(B.boxes[k].net),pct(B.boxes[k].return_ratio)]));
}
function csv(name,rows){
    if(!rows.length)return;const columns=[...new Set(rows.flatMap(row=>Object.keys(row)))];
    const quote=v=>'"'+String(v??"").replaceAll('"','""')+'"';
    download(name,new Blob(["\uFEFF"+[columns,...rows.map(r=>columns.map(c=>r[c]))].map(a=>a.map(quote).join(",")).join("\r\n")],{type:"text/csv;charset=utf-8"}));
}
function selectPreset(id){hero=copy(defaults.presets[id][1]);el("preset").value=id;renderHero();}
function selectPlan(id){
    state.config=copy(id==="proposal"?defaults.proposal:id==="soft"?defaults.soft:{});
    renderHero();renderRules();renderCalibration();markDirty();
}
async function init(){
    defaults=await (await fetch("/api/defaults")).json();
    for(const [k,v] of Object.entries(defaults.maps))el("map").add(new Option(v.name,k));
    for(const [k,v] of Object.entries(defaults.presets))el("preset").add(new Option(v[0],k));
    const custom=new Option("自定义配装","custom");custom.disabled=true;el("preset").add(custom);
    hero=copy(defaults.presets.new[1]);policy={...defaults.policy,auto_use:true,coffee:2,smoke:1};
    state.risk=.7;selectPlan("proposal");renderPolicy();
    el("snapshot").textContent="线上基线 "+defaults.version.replace("T"," ").slice(0,19)+"（北京时间）";
    el("map").onchange=()=>{if(el("follow").checked)selectPreset(defaults.benchmark[el("map").value]);
        renderRules();renderCalibration();markDirty();};
    el("preset").onchange=()=>{selectPreset(el("preset").value);el("follow").checked=false;markDirty();};
    el("follow").onchange=()=>{if(el("follow").checked){selectPreset(defaults.benchmark[el("map").value]);markDirty();}};
    el("plan").onchange=()=>selectPlan(el("plan").value);
    ["floor","horizon","samples","seed"].forEach(k=>el(k).onchange=()=>markDirty());
    document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>changeTab(b.dataset.tab));
    el("run").onclick=()=>compute(state.tab==="trip"?"expedition":"battle");
    el("triprun").onclick=()=>compute("expedition");el("fit").onclick=()=>compute("calibrate");
    el("tripmetric").onchange=()=>{if(state.trip)tripChart();};
    el("ordercheck").onclick=async()=>{
        try{
            el("ordercheck").disabled=true;const rev=state.revision;
            const result=await (await request("/api/calculate",currentReq("order"))).json();
            if(rev===state.revision)el("orderresult").innerHTML=table(["对手","从易图到难图","最小胜率差","发生楼层","核验范围"],
                result.rows.map(v=>[v.kind,v.pair,pp(Math.abs(v.min_gap)<1e-12?0:v.min_gap),v.floor,v.scope]));
        }catch(e){statusText(e.message,true);}finally{el("ordercheck").disabled=false;}
    };
    el("gearfitgo").onclick=async()=>{
        try{
            const rev=state.revision,req={...currentReq("gearfit"),item:el("fititem").value,stat:el("fitstat").value,
                target:+el("fitpercent").value/100,elite:el("fitenemy").value==="elite"};
            const result=await (await request("/api/calculate",req)).json();
            if(rev!==state.revision)return;state.fit=result;
            el("gearfitresult").textContent=defaults.gear[result.item].name+" · "+statName[result.stat]+" "+
                result.original+" → "+result.suggested+"，击杀率 "+pct(result.win_before)+" → "+pct(result.win_after)+
                (result.win_previous==null?"（原值已达标）":"；少填 1 点时为 "+pct(result.win_previous))+"。";
            el("gearfitapply").hidden=result.suggested===result.original;
        }catch(e){el("gearfitresult").textContent=e.message;el("gearfitapply").hidden=true;}
    };
    el("gearfitapply").onclick=()=>{const r=state.fit;if(!r)return;
        state.config.equipment??={};state.config.equipment[r.item]??=copy(defaults.gear[r.item].stats);
        state.config.equipment[r.item][r.stat]=r.suggested;markDirty(true);renderHero();};
    el("save").onclick=()=>download("电梯实验.json",new Blob([JSON.stringify(currentReq(state.tab==="trip"?"expedition":"battle"),null,2)],{type:"application/json"}));
    el("load").onclick=()=>el("file").click();
    el("file").onchange=async()=>{try{
        const input=JSON.parse(await el("file").files[0].text());
        if(!input || Array.isArray(input) || typeof input!=="object")throw Error("需要方案 JSON 对象");
        const cfg=input.config??input;await request("/api/export",{config:cfg});
        if(input.config){
            if(!defaults.maps[input.map]||!Array.isArray(input.hero?.gear)||!Array.isArray(input.hero?.perks)||!Array.isArray(input.hero?.sets))throw Error("实验条件缺少地图或玩家配装");
            // Validate the saved loadout with the same model that the workbench will run.
            await request("/api/calculate",{...input,operation:"battle",floor:1,horizon:1,samples:50});
            hero=copy(input.hero);policy={...defaults.policy,...input.policy};state.risk=input.risk??.7;
            el("map").value=input.map;el("preset").value="custom";el("follow").checked=false;
            ["floor","horizon","samples","seed"].forEach(k=>{if(k in input)el(k).value=input[k];});
            renderPolicy();
        }
        state.config=copy(cfg);markDirty(true);renderRules();renderHero();renderCalibration();statusText("方案已载入，点击重新计算。");
    }catch(e){statusText(e.message,true);}el("file").value="";};
    el("patch").onclick=async()=>{try{const r=await request("/api/export",{config:state.config});
        download("电梯候选规则.zip",await r.blob());statusText("已导出候选文件与基线校验值。");
    }catch(e){statusText(e.message,true);}};
    el("battlecsv").onclick=()=>{if(!state.battle)return;const {result,req}=state.battle;
        csv("单场对照.csv",["A","B"].flatMap(plan=>result[plan].curve.flatMap(x=>["normal","elite"].map(kind=>({plan,map:req.map,floor:x.floor,kind,...x[kind]})))));};
    el("tripcsv").onclick=()=>{if(state.trip){const {result,req}=state.trip;
        csv("远征对照.csv",["A","B"].flatMap(plan=>result[plan].rows.map((x,i)=>({plan,map:req.map,seed:req.seed,...x,paired_delta:result.paired[i].mean,paired_se:result.paired[i].se}))));}};
    window.addEventListener("resize",()=>{if(state.tab==="battle"&&state.battle)battleChart();if(state.tab==="trip"&&state.trip)tripChart();});
    await compute("battle");
}
init().catch(e=>statusText(e.message,true));
