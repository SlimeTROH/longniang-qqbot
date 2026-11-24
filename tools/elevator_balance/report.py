"""Build a standalone, offline Chinese report from the study CSVs and checked rules."""
import base64
import copy
import csv
import html
import io
import json
import os
from pathlib import Path
from urllib.parse import quote

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import PercentFormatter

import economy
import model as m

R=m.ROOT/"results"
COLORS=["#15675c","#ba603b","#3c75a1","#825f91"]
for font in (Path(os.environ.get("WINDIR",r"C:\Windows"))/"Fonts/msyh.ttc",
             Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")):
    if font.is_file():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"]=font_manager.FontProperties(fname=str(font)).get_name()
        break
plt.rcParams.update({"axes.unicode_minus":False,"axes.spines.top":False,"axes.spines.right":False,
                     "font.size":10,"figure.facecolor":"white","axes.facecolor":"#fbfcfa","axes.edgecolor":"#becbc4"})


def load(name):
    with (R/(name+".csv")).open(encoding="utf-8-sig",newline="") as f: return list(csv.DictReader(f))


def pct(x,d=1):
    v=float(x)*100
    if 0<v<10**(-d): return "<"+f"{10**(-d):.{d}f}%"
    if 100-10**(-d)<v<100: return ">"+f"{100-10**(-d):.{d}f}%"
    return f"{v:.{d}f}%"


def gold(x):return f"{float(x):,.0f}"
def esc(x):return html.escape(str(x))
def table(head,rows):
    return '<div class="scroll"><table><thead><tr>'+''.join("<th>"+esc(x)+"</th>" for x in head)+\
           "</tr></thead><tbody>"+''.join("<tr>"+''.join("<td>"+esc(x)+"</td>" for x in row)+"</tr>" for row in rows)+"</tbody></table></div>"
def section(id,title):return '<section id="'+id+'"><h2>'+esc(title)+'</h2>'
def paragraph(x):return "<p>"+x+"</p>"
def eq(x):return '<p class="eq">'+x+'</p>'
def picture(fig,caption):
    out=io.BytesIO();fig.savefig(out,format="png",dpi=140,bbox_inches="tight");plt.close(fig)
    filename={"图 1":"四图单场胜率.png","图 2":"四图远征撤离率.png","图 3":"四图净收益.png"}[caption[:3]]
    (R/filename).write_bytes(out.getvalue())
    return '<figure><img alt="'+esc(caption)+'" src="data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()+'"><figcaption>'+esc(caption)+'</figcaption></figure>'


def build():
    duel=load("单场曲线");travel=load("远征收益");loot=load("掉落期望");gears=load("装备测算");marg=load("属性边际")
    population=load("玩家面板等权汇总") if (R/"玩家面板等权汇总.csv").exists() else []
    ensemble=load("随机装备分布")
    contrast=load("拆分调参与配对差")
    long_runs=load("毕业装百层压力测试")
    qa=json.loads((R/"验证.json").read_text(encoding="utf-8"))
    a,b=m.scenario(),m.scenario(m.calibrated_proposal())
    maps=m.MAP_ORDER
    names={k:a["maps"][k]["name"] for k in maps}
    bench=m.MAP_BENCHMARK
    d={(v["plan"],v["map"],v["build"],int(v["floor"]),v["class"]):v for v in duel}
    t={(v["plan"],v["map"],v["build"],v["policy"],int(v["floor"])):v for v in travel}
    l={(v["plan"],v["map"],int(v["floor"])):v for v in loot}
    g={(v["plan"],v["item"]):v for v in gears}
    def fight(p,k,f,c="normal",build=None):return d[p,k,build or bench[k],f,c]
    def trip(p,k,f,pol="kit",build=None):return t[p,k,build or bench[k],pol,f]
    def best(p,k,risk):
        rows=[v for v in travel if v["plan"]==p and v["map"]==k and v["build"]==bench[k] and v["policy"]=="kit" and float(v["ci_low"])>=risk]
        return max(rows,key=lambda v:float(v["net"])) if rows else None
    def chart(title,ylabel,make,prob=True):
        fig,axes=plt.subplots(2,2,figsize=(11,6.7),layout="constrained")
        fig.suptitle(title,fontsize=15,fontweight="bold",color="#213c36")
        for ax,k in zip(axes.flat,maps):
            for label,ys,style,color in make(k):
                ax.plot(range(1,len(ys)+1),ys,style,color=color,linewidth=2,label=label)
            ax.set_title(names[k]+" · "+m.PRESETS[bench[k]][0],loc="left",fontsize=11)
            ax.set_xlabel("楼层");ax.set_ylabel(ylabel)
            ax.grid(axis="y",color="#d9e3dc",linewidth=.65)
            if prob: ax.set_ylim(-.02,1.02);ax.yaxis.set_major_formatter(PercentFormatter(1))
            ax.legend(fontsize=8,frameon=False,ncol=2)
        return fig
    out=[]
    out.append('<header><span>2026 年 11 月 23 日 · 线上规则快照</span><h1>无尽电梯的难度、战力与每票收益</h1><p>现行规则 A 与候选方案 B 的数学模型、数值表和验证结果</p></header>')
    out.append('<nav>'+''.join('<a href="#'+id+'">'+name+'</a>' for id,name in [
        ("read","结论"),("fight","战斗"),("curve","难度曲线"),("run","撤离与收益"),("gear","玩家与装备"),("tune","优化方案"),("method","验证与复算")])+'</nav><main>')
    out.append(section("read","结论与使用口径"))
    out.append(paragraph(f'线上商场的满血无装玩家，普通怪击杀率从 F5 的 <b>{pct(fight("A","mall",5)["win"])}</b> 降到 F8 的 <b>{pct(fight("A","mall",8)["win"])}</b>。即使 F5 打赢的机会极高，一场仍平均掉 {float(fight("A","mall",5)["hp_loss"]):.1f} 点生命，基础上限为 110。连续作战很快就会把单场优势耗尽。'))
    out.append(paragraph('在下一表的参考配装和标准策略下，要求目标层撤离率的 95% 置信下限至少达到 70%。表内选的是所有已测目标层中净变现均值最高的一层。均值接近的相邻层可能随抽样互换。'))
    summary=[]
    for k in maps:
        aa,bb=best("A",k,.70),best("B",k,.70)
        summary.append([names[k],m.PRESETS[bench[k]][0],f'F{aa["floor"]} · {pct(aa["target_exit"])}',gold(aa["net"]),
                        f'F{bb["floor"]} · {pct(bb["target_exit"])}',gold(bb["net"])])
    out.append(table(["地图","入图参考玩家","A 撤离层 / 成功率","A 净期望（金）","B 撤离层 / 成功率","B 净期望（金）"],summary))
    out.append(paragraph('标准策略为两瓶咖啡、一颗烟雾弹、一个安全箱格，无新手保险。途中清层硬打，血量 ≤60% 时战前喝咖啡，门口遇伏优先用烟。奇遇选免费、确定成功且有收益的选项。每个场景有 6,000 次独立开局；每次开局派生各目标层的撤离分支。'))
    obs=m.MANIFEST["observed"]
    out.append(paragraph(f'线上快照时间为 {esc(m.MANIFEST["captured"])}，五个游戏规则和配置文件在北京时间 21:03 复核一致。账本中有 {obs["players_with_runs"]} 位开过正式局的玩家，累计 {obs["runs"]} 局、{obs["extracts"]} 次撤离，占 {pct(obs["extracts"]/obs["runs"])}。这个历史比率混合了不同版本、楼层、装备、操作和应急拉回。下文单场概率来自现行战斗规则，线上人数仅用于等权面板对照。'))
    out.append("</section>")

    out.append(section("fight","一场战斗怎样结算"))
    out.append(table(["符号","含义"],[["f、M","楼层和地图怪物系数"],["H、A、D、S、L","生命、攻击、防御、敏捷和幸运"],
        ["K、J","打死对方、自己倒下所需的有效行动轮次"],["W、Lₒ、T","击杀、阵亡、打满 15 回合双方存活的概率，三者之和为 1"]]))
    out.append(eq('H<sub>怪</sub> = ⌊38 M (1 + 0.18 f + 0.008 f²)⌋　　A<sub>怪</sub> = ⌊11.5 M (1 + 0.13 f)⌋<br>D<sub>怪</sub> = ⌊3 M (1 + 0.10 f)⌋　　S<sub>怪</sub> = min(42, 7 + f)'))
    out.append(paragraph('精英在普通怪的整数生命、攻击上再乘 1.75、1.30 并取整。敏捷相同玩家先手。怪物先手时，即使双方需要同样的行动数也会改变胜负。裸玩家敏捷 10，在商场 F3 仍先动，F4 开始怪物先动。'))
    out.append(eq('单次命中基础伤害 X = max(1, ⌊A × 100 / (100 + 2.5D) × U⌋)，U 均匀分布于 [0.9, 1.1]'))
    out.append(paragraph('闪避概率为 min(45%, 3% + 0.4% × 敏捷)。玩家暴击率为 min(60%, 5% + 0.5% × 幸运)，普通暴击在已取整的伤害上乘 1.6 后再取整，豪赌之眼改为 2.0。破甲按比例降低有效防御。怪方按自身攻击结算，玩家暴击率仅用于玩家出手。护盾先吸收伤害。'))
    out.append(paragraph('计算使用完整伤害分布。设未取整的基础伤害为 b，整数桶 j 的概率为区间 [0.9b, 1.1b] 与 [j,j+1) 的交长除以 0.2b。将结果分配给闪避、普通命中和暴击，再按当前回合卷积累计伤害。击杀阈值上的首达概率直接由越界概率质量得到。'))
    out.append(eq('玩家先手时，W = Σ<sub>t=1…15</sub> P(K=t) P(J≥t)。怪物先手时，将 P(J≥t) 换成 P(J&gt;t)。<br>T = P(K&gt;15) P(J&gt;15)，E[回合数] = Σ<sub>t=0…14</sub> P(K&gt;t) P(J&gt;t)。'))
    out.append(paragraph('首回合重击与逐回合狂暴使用逐轮伤害分布。不屈额外维护“已经触发、尚余 1 点生命”的概率质量。孤注一掷依赖自身当前生命，反伤把敌人生命与所受伤害关联；这两种配置直接调用快照中的 combat.fight 逐场抽样。界面标出 DP 或 MC。单场掉血量在击杀回血结算前测量，远征计入吸血、护盾消耗、医药和跨层残血。'))
    out.append(paragraph('普通怪或精英打平会散去，本场没有击杀掉落。伏击守门怪打平继续堵门，双方剩余生命保留。硬打时的单场存活概率为 W+T，跨过守门战使用单独的整局分支。'))
    out.append("</section>")

    out.append(section("curve","四图的胜率断点"))
    out.append(table(["地图","附加门槛 / 金币","怪物系数","估值系数","基础金红率","撤离伏击"],[
        [names[k],f'累计撤离 {a["maps"][k]["extracts"]} 次 / {a["maps"][k]["entry"]:,} 金',
         a["maps"][k]["multiplier"],a["maps"][k]["value_multiplier"],pct(a["maps"][k]["rare_rate"]),
         f'F{a["maps"][k]["ambush_floor"]} 起 {pct(a["maps"][k]["ambush_rate"])}' if a["maps"][k]["ambush_rate"] else '0%'] for k in maps]))
    out.append(paragraph('每张图进场另用 1 张通行证。基础生命的逐层增量为 38M(0.188+0.016f)，越往下该增量越大。防御又压低玩家命中的实际伤害，进入 15 回合上限后，无法在限定回合内击杀也会成为失败条件。'))
    out.append(table(["楼层","宝箱","普怪","精英","奇遇","遗物"],[[str(v["from"])+" 起",*[v["weights"][k] for k in ("loot","normal","elite","choice","relic")]] for v in a["event_weights"]]))
    out.append(paragraph('这些是基础事件权重。F5、F10 的精英权重跳升，叠加在单只怪的属性曲线上。无可用遗物盒时其权重并入宝箱；潜行还可把部分普怪权重转给宝箱。实验室首次发生掉落时的紫装保底，在普通击杀、精英击杀和宝箱中共用同一个局内标记。'))
    rows=[]
    for k in maps:
        for c,cl in (("normal","普通"),("elite","精英")):
            thresholds=[next((f for f in range(1,41) if float(fight("A",k,f,c)["win"])<p),">40") for p in (.9,.5,.1)]
            rows.append([names[k],cl,*thresholds,pct(fight("A",k,5,c)["win"]),pct(fight("A",k,8,c)["win"]),pct(fight("A",k,10,c)["win"])])
    out.append(table(["地图 / 参考配装同首页","对手","首个低于 90% 的层","低于 50%","低于 10%","F5 胜率","F8 胜率","F10 胜率"],rows))
    out.append(picture(chart("同图参考配装下的满血击杀率","单场击杀概率",lambda k:[
        (p+(" 普通" if c=="normal" else " 精英"),[float(fight(p,k,f,c)["win"]) for f in range(1,31)],
         "-" if c=="normal" else "--",COLORS[i]) for i,(p,c) in enumerate((("A","normal"),("B","normal"),("A","elite"),("B","elite")))]),
        "图 1　A 为现行规则，B 为候选胜率校准。面板使用同一套底材名称，各自应用对应品质倍率。"))
    out.append(paragraph('同一套 10 撤蓝装横向比较可以单独看地图差距。下表均为线上 A、F8、满血，携带同样的破甲、重击、摸金词条。'))
    out.append(table(["地图","普通击杀","普通平均回合","普通平均掉血","精英击杀","精英平局"],[
        [names[k],pct(fight("A",k,8,build="blue")["win"]),f'{float(fight("A",k,8,build="blue")["rounds"]):.2f}',
         f'{float(fight("A",k,8,build="blue")["hp_loss"]):.1f}',pct(fight("A",k,8,"elite","blue")["win"]),
         pct(fight("A",k,8,"elite","blue")["draw"])] for k in maps]))
    if population:
        pop={(r["plan"],r["map"],int(r["floor"]),r["class"]):r for r in population}
        out.append(paragraph('将快照中每个实际开过局的玩家各计一票，使用其已穿装备的已保存属性、词条、撤离成长和已集齐套装。假设满血开战，各地图开放以便对照。包含反伤或孤注一掷的面板，每个条件由源函数采样估计；B 中老装备保留原滚值。'))
        out.append(table(["地图","A 实装普通 F8","A 实装精英 F8","A 实装普通 F15","B 实装普通 F8","B 实装精英 F8"],
            [[names[k],*[pct(pop[p,k,f,c]["win"]) for p,f,c in (("A",8,"normal"),("A",8,"elite"),("A",15,"normal"),("B",8,"normal"),("B",8,"elite"))]] for k in maps]))
    out.append("</section>")

    out.append(section("run","整趟远征的胜算、期望与尾部风险"))
    out.append(paragraph('状态包含楼层、剩余生命、护盾、不屈是否消耗、战斗或奇遇、背包、安全箱、补给与套装效果。按策略 π 选动作后，用真实 Turn 的进图、交战、逃跑、道具和结算转移到下一状态。仓库入账按实际结算物品计，装备按品质固定回收价，其余物品按入库估值可售。'))
    out.append(eq('V<sub>π</sub>(s) = Σ<sub>s′</sub> P(s′ | s, π(s)) [r(s,π(s),s′) + V<sub>π</sub>(s′)]<br>单趟净变现 X = 可售入库价值 + 钱包直接变化 − 初始补给购置费 − 通行证机会成本'))
    out.append(paragraph('金币门票已扣在钱包直接变化里。默认通行证机会成本为 0，可在工具中填入。每趟标准补给成本为 1,050 金。未用完的物资按可售价值入账。藏品、装备日后穿戴和黑市兑换的主观价值可另算。不同目标层的收益差，用同一条路径的两个结算分支相减。'))
    out.append(paragraph('到层后先计入自动获得的宝箱。清层策略处理目标层的怪物或奇遇，再叫电梯；到层即撤策略可以直接在当前遭遇请求撤离。已击杀本层怪物即免本层伏击。普通逃跑成功率为 clamp(0.45+0.015×(敏捷−1.5f),0.15,0.90)，滑溜再加 25 个百分点，封顶 90%。逃跑失败先挨一次确定伤害，再进入完整战斗。拒绝组队支援是本研究的单人策略假设。'))
    out.append(paragraph('选取安全箱里可保护且估值最高的物资，自动用药策略把咖啡和烟留在背包。翻车时按真实保险与安全箱规则结算，替身娃娃自动消耗并带包撤离。无群友遗物可领时，游戏会把遗物权重转给宝箱。日限悬赏奖金单独于重复远征经济，标准组关闭群金雨。'))
    def travel_series(k,field):
        gain=.0001 if field=="net" else 1
        return [(p,[float(trip(p,k,f)[field])*gain for f in range(1,31)],"-",COLORS[i]) for i,p in enumerate(("A","B"))]
    out.append(picture(chart("目标层成功撤离概率 · 标准策略","每次开局的成功概率",lambda k:travel_series(k,"target_exit")),
                       "图 2　每条曲线都包括此前楼层的消耗。较早被娃娃拉回仍计作任意层撤离，未达目标时单列。"))
    out.append(picture(chart("每票净变现的样本均值 · 标准策略","万金 / 次开局",lambda k:travel_series(k,"net"),False),
                       "图 3　深层撤离率接近零时，安全箱仍可能保住高价物资，因此均值可以维持在正数。"))
    rows=[]
    for k in maps:
        for plan in ("A","B"):
            r=trip(plan,k,8)
            rows.append([names[k],plan,pct(r["target_exit"]),pct(r["ci_low"])+"～"+pct(r["ci_high"]),
                         gold(r["net"])+" ± "+gold(1.96*float(r["net_se"])),gold(r["p10"]),gold(r["median"]),gold(r["p90"]),pct(r["negative"])])
    out.append(table(["目标 F8","规则","成功撤离","95% 区间","每票净期望 ± 约95%半宽","P10","中位数","P90","净亏概率"],rows))
    out.append(paragraph(f'现行拍卖行标准策略在 F1 结算时，平均净变现为 {gold(trip("A","auction",1)["net"])} 金，中位数为 {gold(trip("A","auction",1)["median"])} 金，{pct(trip("A","auction",1)["negative"])} 的样本净亏。稀有收藏品抬高均值，首次入图的体验更接近中位数。'))
    rows=[]
    for k in maps:
        r=[trip("A",k,8,p) for p in ("hard","kit","elite_flee","evade","arrival","wait")]
        rows.append([names[k],*[pct(v["target_exit"])+" / "+gold(v["net"]) for v in r]])
    out.append(table(["A · F8","空手硬打","标准清层","精英逃","逢怪先逃","到层即撤","到层等 30 分钟"],rows))
    out.append(paragraph('上表每格为目标层成功撤离率 / 每票净变现。等待策略沿用应急拉回的安全结算，另耗 30 分钟；收益列按票计算，若要比较每小时收益，应给等待时间单独定价。精英掉落奖励高，逃跑减少翻车，也放弃了部分高阶掉落。'))
    rows=[]
    for k in maps:
        u=best("A",k,0);v=best("B",k,0)
        rows.append([names[k],u["floor"],gold(u["net"]),pct(u["target_exit"]),v["floor"],gold(v["net"]),pct(v["target_exit"])])
    out.append(table(["只追求样本平均净收益","A 目标层","A 金/票","A 撤离率","B 目标层","B 金/票","B 撤离率"],rows))
    out.append(paragraph('四个例子能分清安全网和真实强度。拍卖行 F12 标准策略下，安全箱 0／1／4 格对应的 A 净期望是 '+
                         '／'.join(gold(trip("A","auction",12,p)["net"]) for p in ("box0","kit","box4"))+
                         ' 金。商场 F8 空手时，第 4 局以后／保险第 2～3 局／首局的净期望是 '+
                         '／'.join(gold(trip("A","mall",8,p)["net"]) for p in ("hard","insured","first"))+
                         ' 金。首局还会强制开局遇到宝箱。'))
    out.append(paragraph('在同一张图、固定面板和清层策略下，继续的条件到层概率是 S(f+1)/S(f)。如果每层始终存在正的死亡风险，整趟存活会累乘下降。给满血单场胜率设下界，并不能单独给连续几十层的撤离率设同样的保底。'))
    out.append(eq('简化的再走一层条件：p × E[新增可变现价值 | 过关] &gt; (1−p) × 未保住的现有价值 + 额外补给成本。'))
    out.append(paragraph('这个手算式假定现在可以安全撤离、安全箱内容固定。遇到伏击层、可以换箱或身带替身娃娃时，使用工作台的撤离分支与配对增量，更能反映当时的实际选择。'))
    out.append("</section>")

    out.append(section("gear","玩家强度与 21 件装备的填值"))
    out.append(eq('人物面板 = 裸属性 + 撤离成长 + 成套永久加成 + Σ 已穿装备滚值。<br>第 j 个底材属性成品值 = round(底材值 × 品质倍率 × U[0.9,1.1])，负属性的品质倍率取 1。'))
    out.append(paragraph('撤离成长最多 25 次，每次 +4 生命、+1 攻击，每两次 +1 防御。武器、护甲、挂件各穿一件。绿／蓝／紫／金的词条数为 0／1／2／2，重复词条按最大值生效。固定基准使用 1.00 的属性滚值，随机三件套另行统计。'))
    out.append(eq('战力指数 = 100 × 玩家 m₅₀ / 无装新人 m₅₀。m₅₀ 是对固定商场 F5 普通怪，逐步放大怪物生命、攻击、防御后，击杀率降至 50% 的临界倍率；怪物敏捷固定。'))
    rows=[]
    for build,(name,spec) in m.PRESETS.items():
        ha,hb=m.panel(a,spec),m.panel(b,spec);pa,pb=m.power(ha),m.power(hb)
        rows.append([name,"/".join(str(ha["stats"][x]) for x in m.STATS),", ".join(a["affixes"][x]["name"] for x in ha["perks"]) or "无",
                     f'{pa["m50"]:.3f}',f'{pa["index"]:.1f}',f'{pb["index"]:.1f}'])
    out.append(table(["参考玩家","A 生命/攻/防/敏/幸","有效词条","A 临界倍率","A 战力","B 战力"],rows))
    out.append(paragraph('参考成套装备固定为物理说服撬棍、保安大爷反光背心、打工人续命腰包。指数可用于同一版本、同一标尺下排序。幸运的掉落收益、吸血的续航、潜行的避战和安全箱的保底，应同时读远征表。同一个指数的两位玩家，可以有不同的每票收益。'))
    out.append(paragraph('线上「一键配装」逐部位挑最高分，权重是生命 0.45、攻击 3.4、防御 2.0、敏捷 1.0、幸运 0.9，再加每条词条 6 分。它会把价值迥异的词条按同一分数相加。工作台给出的 50% 临界倍率将这些效果放进实际战斗结算，可以检验一键配装给出的选择。'))
    out.append(paragraph('便于手算的耐久近似为 (当前生命+盾)×(1+0.025×防御)/(1−闪避率)，有贪婪诅咒再除以 1.25。命中期望近似为 命中率×基础伤害×[1+暴击率×(暴击倍率−1)]。先手、15 回合和取整会造成台阶，调参器用完整分布判断达标。'))
    out.append(paragraph('用等效耐久给护甲定初稿，在生命 H、防御 D 的基准上，+1 防约等价于 +2.5H/(100+2.5D) 生命。以 175 生命、23 防御的绿装参考玩家为例，约等于 +2.78 生命。速度跨过敌方敏捷会多拿先手，幸运同时影响暴击和掉落，这两项应分别用胜率增量与每票收益验价。'))
    rows=[]
    for k in maps:
        row=[names[k]]
        for stat in m.STATS:
            e=next(v for v in marg if v["plan"]=="A" and v["map"]==k and v["stat"]==stat)
            row.append(f'{100*float(e["win_delta"]):+.2f}')
        rows.append(row)
    out.append(table(["线上 F7 普怪 · 胜率变化（百分点）","+10 生命","+1 攻击","+1 防御","+1 敏捷","+1 幸运"],rows))
    fit=qa["gear_fit"]
    out.append(paragraph(f'反推示例采用现行地铁 F8、3 撤绿装、普通怪和 90% 击杀目标。单独调整撬棍的攻击底材，填 {fit["original"]} 时胜率 {pct(fit["win_before"],2)}，填 {fit["suggested"]-1} 时为 {pct(fit["win_previous"],2)}，填 <b>{fit["suggested"]}</b> 首次达标，胜率 {pct(fit["win_after"],2)}。这是单属性反算示例；全局建议还兼顾同槽选择和经济。'))
    out.append(paragraph('建议品质倍率从 1.25／1.85／2.75／4.00 调到 <b>1.25／1.75／2.45／3.35</b>。下表填写底材原值，列内顺序均为生命、攻击、防御、敏捷、幸运。战力列采用同一 10 撤蓝装角色，仅替换该部位，关闭词条，其他部位仍用上述标准套。'))
    rows=[]
    for key,v in a["equipment"].items():
        aa,bb=g["A",key],g["B",key]
        row=lambda e:" / ".join(e[s] for s in m.STATS)
        rows.append([v["slot"],v["name"],row(aa),row(bb),
                     f'{float(aa["strength"]):.1f} → {float(bb["strength"]):.1f}',gold(bb["abyss_f8_chest_sale"])])
    out.append(table(["部位","底材","A 生/攻/防/敏/幸","建议 B 生/攻/防/敏/幸","标准战力 A → B","B 实验室 F8 单箱变现"],rows))
    def dominated(data):
        out=[]
        for x,a1 in data["equipment"].items():
            for y,b1 in data["equipment"].items():
                if x==y or a1["slot"]!=b1["slot"]:continue
                diff=[b1["stats"].get(s,0)-a1["stats"].get(s,0) for s in m.STATS]
                if min(diff)>=0 and max(diff)>0:out.append(a1["name"]+"〈"+b1["name"])
        return out
    da,db=dominated(a),dominated(b)
    out.append(paragraph('同槽、同品质、同滚值比较，若一件物品的五维逐项都不低且至少一项更高，便存在底材支配。A 中检出 '+str(len(da))+' 对（左边被右边压住）：'+esc('、'.join(da) or '无')+'。B 检出 '+str(len(db))+' 对。高攻击配负敏，防御与厚血分开，招财挂件保留单箱收益用途。'))
    rows=[]
    for plan in ("A","B"):
        for grade in ("green","blue","purple","gold"):
            x=next(v for v in ensemble if v["plan"]==plan and v["grade"]==grade and v["floor"]=="8" and v["class"]=="normal")
            rows.append([plan,m.GRADE_LABELS[grade],names[x["map"]],pct(x["mean"]),pct(x["p10"]),pct(x["median"]),pct(x["p90"])])
    out.append(table(["随机配装 F8 普通","品质","地图","平均击杀率","配装 P10","配装中位","配装 P90"],rows))
    out.append(paragraph('每种品质抽 160 套，按实装滚动直到三个部位各取得第一件，保留随机属性与词条。参考成长依次为 3／10／18／25 撤。含特殊联动词条的每套每种场景抽 300 场；这里的分位数反映配装间差异，也含这部分抽样噪声。'))
    out.append("</section>")

    out.append(section("tune","怎样给每张地图设置可验证的边界"))
    out.append(paragraph('把设计目标写成条件概率。以指定入图玩家、血量、护盾和词条为基准，从 F1 胜率 W₁ 向深层边界 W∞ 收敛，h 层后走完两者距离的一半。若要让难度本身向下限下降，可把终点胜率设得比起点高。'))
    out.append(eq('W(f) = W∞ + (W₁−W∞)·2<sup>−(f−1)/h</sup><br>|W(f+1)−W(f)| = |W₁−W∞|·(1−2<sup>−1/h</sup>)·2<sup>−(f−1)/h</sup>'))
    out.append(paragraph('工具有两种实现。连续属性软上限分别插值生命、攻击、防御和敏捷，终点由“极限等效线上楼层”给定。取整后的属性会跳档，先手和回合上限也会造成胜率跳档。需要按胜率验收时，在「胜率校准」中使用概率混合。'))
    out.append(eq('已知普通档、强档的实战击杀率 p₀、p₁，令强档概率 q(f) = [p₀−W(f)] / [p₀−p₁]。<br>全概率公式给出 (1−q(f))p₀ + q(f)p₁ = W(f)。'))
    out.append(paragraph('强档内部使用两个相邻整数怪物面板混合，精确命中指定端点。候选精英的强档另有 12% 概率抽到可辨识的深层精英。模型先扣除这部分概率质量，再反推普通强档的胜率，所以指定参考玩家的目标曲线仍成立。地图按事先导出的模板出怪，所有玩家面对同一分布。单只怪仍有强弱波动，平滑的是平均交战难度。后台校准使用能精确递推的参考配装，反伤和孤注一掷可用源引擎独立复核。'))
    rows=[]
    for key in maps:
        x=m.TARGETS[key];md=b["maps"][key];f=8
        rows.append([names[key],m.PRESETS[bench[key]][0],pct(x[0][0],2)+" → "+pct(x[0][1]),
                     pct(x[1][0],2)+" → "+pct(x[1][1]),"12",pct(fight("B",key,f)["win"]),
                     pct(fight("B",key,f,"elite")["win"]),f'{md["value_multiplier"]} / {pct(md["rare_rate"])}'])
    out.append(table(["候选 B","标定玩家","普通 首层 → 边界","精英 首层 → 边界","半程 h","F8 普通","F8 精英","估值倍率 / 基础稀有率"],rows))
    out.append(paragraph('四图共用 h=12。对同一个固定玩家，如果相邻两图在首层、极限两处都满足难图胜率 ≤ 易图胜率，则任意楼层也满足，因为曲线是这两个端点用相同权重的插值。本次对六套标准玩家分别检查普通和精英，跨图逆序计数为 '+str(len(qa["cross_map_inversions"]))+'。换了怪物模板或玩家词条后重新验收。'))
    out.append(paragraph('例如 B 商场普通怪的参考胜率从 99.95% 向 72% 收敛，第 13 层为 85.975%，第 25 层为 78.9875%。从 F1 到 F2 下降约 1.57 个百分点，从 F13 到 F14 只降约 0.78 个百分点。如果还要求四张地图之间的倍率增量逐张缩小，可在地图参数里设置首图 1、极限 3.5、相邻增量比 0.7，得到 1、1.75、2.275、2.6425，再用统一玩家核验跨图胜率。'))
    out.append(paragraph('建议给普通怪和精英分别留出挑战区间，让同一参考玩家中期仍有清层选择，再由残血、消耗品和撤离伏击制造整局压力。随深度减小的单场难度增量、同面板的地图排序和可负担的入场成本，是三个独立的验收条件。'))
    out.append('<h3>毕业装的百层压力测试</h3>')
    rr=[]
    for k in ("mall","auction"):
        for p,title in (("A","线上"),("B0","只有软化胜率"),("B","加入深层精英")):
            vals={int(x["floor"]):x for x in long_runs if x["plan"]==p and x["map"]==k}
            if vals:
                rr.append([names[k],title,*[pct(vals[f]["target_exit"]) for f in (20,40,60,100)],
                           pct(vals[100]["ci_low"])+"～"+pct(vals[100]["ci_high"]),gold(vals[100]["net"])])
    out.append(table(["25 撤金装","方案","F20 撤离","F40 撤离","F60 撤离","F100 撤离","F100 的 95% 区间","F100 每票净期望"],rr))
    out.append(paragraph('每格来自每场景 1,200 趟远征。B0 与 B 给入图参考玩家相同的普通和精英目标曲线，但是 B0 的毕业装可能靠击杀回血持续吃掉低图怪。B 把精英强档的 12% 留给深层精英，以毕业参考装的单场阵亡率至少 45%／50%／55%／60%（四图顺序）确定其面板。深层精英会标名，玩家能选择撤离或改变战术。在基础精英权重 25% 的深层，强档概率趋于 1 时，这类敌人约占全部楼层的 3%。'))
    out.append(paragraph('风险能降低持续练级中的幸运长跑，严格的每票货币发行预算还需要单独的经济约束。若运营目标要求数学上的总奖励上界，可给所有可变现奖励增加逐层领取系数 ρ<sup>f−1</sup>。例如 ρ=0.97 且每层所有渠道的条件奖励均值 ≤G 时，累计期望至多 G/(1−0.97)。这是一项额外的经济设计，当前 B 的净收益数据按实装逐件结算。'))
    rows=[]
    for key in maps:
        x=l["A",key,8];y=l["B",key,8]
        rows.append([names[key],pct(x["rare_roll"]),pct(x["red"]),gold(x["sale"]),gold(y["sale"]),gold(x["normal_kill_sale"]),gold(x["elite_kill_sale"])])
    out.append(table(["F8 参考装","A 先判金红","A 最终单箱红率","A 箱均变现","B 箱均变现","A 普怪击杀变现","A 精英击杀变现"],rows))
    out.append(paragraph('普通单抽先判金红档，概率为地图基础率 × [1+0.12(f−1)] × (1+0.012×幸运) × 套装与宝箱词条、群金雨等乘区，封顶 75%。基础金红档内部按 3.3:0.7 分金、红。模型再展开品质保底、升档、金箱特殊物、装备、材料品相与地图掉落池，对每种入库价值和回收价求和。精英击杀通常两抽共用一次随机升 1 或 2 档；实验室每局首次有掉落时附送的紫装在整趟结算单独计入。'))
    boxes_a,boxes_b=economy.box_values(a),economy.box_values(b)
    out.append(table(["装备箱","价格","A 期望回收","A 净现金","B 期望回收","B 净现金"],[
        [boxes_a[k]["name"],gold(boxes_a[k]["cost"]),gold(boxes_a[k]["sale"]),gold(boxes_a[k]["net"]),gold(boxes_b[k]["sale"]),gold(boxes_b[k]["net"])] for k in boxes_a]))
    out.append(paragraph('现行新手箱与进阶箱的期望回收价高于购买价，长期重复买入再出售会产生正的期望现金流，绕开探索风险。B 保留 450／1,500／4,800 金箱价，将绿／蓝／紫／金装回收价从 260／750／2,200／6,500 调为 <b>160／400／1,500／5,700</b>。新手初始 500 金仍买得起第一箱。装备本身的使用价值另计。'))
    out.append(paragraph('调难度时可先对普通怪和精英选定各自首层胜率、深层边界与半程，再看整趟 P10／中位／均值、掉血和门票。B 同时下调后图估值与基础稀有率以约束安全箱的高价保底收益。要归因单个改动，可在工作台复制线上 A，只调整一项，使用同一组种子重算。'))
    out.append('<h3>把战斗、装备和经济改动拆开验证</h3>')
    rows=[]
    for k in maps:
        vals={r["variant"]:r for r in contrast if r["map"]==k and r["floor"]=="8"}
        rows.append([names[k],*[pct(vals[v]["target_exit"])+" / "+gold(vals[v]["net"]) for v in ("A","仅装备","仅怪物","仅经济","B")],
                     gold(vals["B"]["paired_net"])+" ± "+gold(1.96*float(vals["B"]["paired_net_se"]))])
    out.append(table(["参考装 F8","现行 A","仅改装备","仅改怪物","仅改经济","综合 B","B−A 配对净收益差"],rows))
    out.append(paragraph('每格为目标层撤离率 / 每票平均净变现。四图的单项干预分别与原基线共享种子。单项影响之间会交互，综合方案必须联合重算。拍卖行仅平滑敌人会放大每票产出；同时收窄估值与基础稀有率，能提高 F8 撤离率并压低现金产出。'))
    out.append("</section>")

    out.append(section("method","误差、验证与复算"))
    maxp=max(x for c in qa["combat_native_checks"] for x in c["prob_abs_error"].values())
    out.append(paragraph(f'实装敌方面板逐项核对 {qa["native_enemy_panels"]} 个条件。另选 6 个有代表性的场景，每场景用原生 combat.fight 重打 25,000 次，与精确递推对照。胜、负、平三项概率最大绝对差为 {maxp*100:.3f} 个百分点。掉落验证四组各 50,000 抽，最大标准化均值差为 {max(abs(x["z"]) for x in qa["loot_native_checks"]):.2f} 个标准误。参考胜率曲线校验到 F1,000，最大绝对概率误差 {qa["target_max_error"]:.2g}。'))
    out.append(paragraph('远征主研究使用种子 261123，每场景 6,000 条独立主路径，62 个场景共 372,000 次开局。各目标层继承同一路径至分支点，分支使用单独的确定性随机流。撤离率给 Wilson 95% 区间，平均净变现给约 ±1.96 s/√n，跨层增量由配对差估计。P10／中位数／P90 是样本分位数。有限样本中零次成功时仍应看置信上限。'))
    out.append(paragraph('单人模型关闭群友救援与外部遗物，忽略指令输入耗时和每日悬赏收入。物资可全部出售，箱子按原样回收；收藏解锁、留作材料或开盲盒的未来价值由玩家另行估计。角色处于能够支付门票的条件下参与比较，跨地图力学对照也允许测试尚未解锁的地图。高于 30 层的推荐依赖少量幸存者，有效精度应结合撤离区间判断。'))
    out.append(paragraph('装备改值作用于新滚出的物品，已经落库的装备保留原属性。曲线和回收价的候选代码随工作台导出，应以包内源码哈希对照拟应用的版本。线上动态数据只用于匿名汇总，六套公开基准及随机装备研究均可使用随附源码与种子重算。'))
    links=[]
    for name in ("单场曲线","远征收益","拆分调参与配对差","毕业装百层压力测试","掉落期望","属性边际","装备测算","随机装备分布","玩家面板等权汇总"):
        if (R/(name+".csv")).exists():links.append('<a href="results/'+quote(name+".csv")+'">'+esc(name)+" CSV</a>")
    out.append('<div class="links">'+"".join(links)+"</div></section></main>")
    css="""*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f3f5f1;color:#203331;font:16px/1.76 "Microsoft YaHei","Noto Sans CJK SC",sans-serif}header{background:#18332f;color:white;padding:38px max(5vw,20px)}header span{color:#cee1b9}header h1{font-size:clamp(27px,4vw,44px);line-height:1.2;margin:10px 0}header p{margin:6px 0}main,nav{max-width:1240px;margin:auto}nav{display:flex;flex-wrap:wrap;gap:14px;padding:14px 20px}a{color:#09665d}section{background:white;padding:22px clamp(15px,3vw,40px);margin:0 12px 18px;border:1px solid #d3ddd4;border-radius:6px}h2{font-size:25px;line-height:1.4;margin:4px 0 16px}p{margin:10px 0 15px}.eq{background:#edf3e9;padding:12px 15px;border-left:4px solid #477968;font-family:Georgia,"Microsoft YaHei",serif}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}td,th{border-bottom:1px solid #dbe3dc;padding:7px 9px;text-align:left}th{background:#edf3ec;white-space:nowrap}figure{margin:20px 0}img{display:block;max-width:100%;height:auto;margin:auto}figcaption{font-size:13px;color:#567067}.links{display:flex;flex-wrap:wrap;gap:12px}@media print{body{background:white}nav{display:none}section{border:0;margin:0;padding:15px 0}figure,tr{break-inside:avoid}}"""
    target=m.ROOT/"难度分析.html"
    target.write_text('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>无尽电梯难度分析</title><style>'+css+'</style></head><body>'+"".join(out)+"</body></html>",encoding="utf-8")
    print("REPORT_OK",target.stat().st_size,flush=True)


if __name__=="__main__":build()
