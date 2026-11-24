"""Rebuild the published balance study with independently seeded real encounters."""
import json
from concurrent.futures import ProcessPoolExecutor
from itertools import product

import season_model as model

PROFILES = (("mall","new"),("mall","green"),("subway","blue"),("abyss","purple"),
            ("auction","gold"),("auction","rainbow"),("tide","gold"),("zenith","gold"),("zenith","red"))
FLOORS = [4,8,18,30,50,70]
SEED = 261123


def study(job):
    map_id, preset, prepared = job
    cat,key,hero = model.build({"map":map_id,"preset":preset})
    fields = model.PREPARED if prepared else []
    fights = [model.battle(cat,key,hero,f,"boss",fields,2000,SEED+f*10009) for f in FLOORS[1:]]
    normal = [model.battle(cat,key,hero,f,kind,[],2000,SEED+f*10009)
              for f in (1,7,17,29) for kind in ("normal","elite")] if not prepared else []
    trips = model.trips(cat,key,hero,FLOORS,1000,SEED,prepared)
    return {"map":key,"preset":preset,"prepared":prepared,"panel":hero["stats"],
            "fights":fights,"normal":normal,"trips":trips}


def report(rows):
    economy = model.economy(model.game.Catalog())
    text = ["# 无尽电梯 · 首领赛季数值实测", "",
            "每格单挑模拟 2,000 次。每种远征策略另跑 1,000 条独立路线，所有战斗、掉落和结算调用本赛季游戏代码。"
            "随机种子 261123。表内 0% 与 100% 是样本频率；2,000 次中未见胜利时，Wilson 95% 上界约 0.19%。", "",
            "## 首领单挑", "", "满血开战，开启装备自带的开局盾。每格是「直接开战 / 使用红线驱动针、虹吸滴管、连动棘轮」。"
            "三个都是通用限层道具，此处控制起手条件，实局需要自己拾到。首领平手时原地再战，无队友、咖啡和手雷。", "",
            "| 地图与配装 | F8 | F18 | F30 | F50 | F70 |","|---|---:|---:|---:|---:|---:|"]
    cat=model.game.Catalog()
    for a,b in zip(rows[::2],rows[1::2]):
        label=cat.maps[a["map"]]["name"]+" · "+model.PRESETS[a["preset"]][0]
        text.append("| "+label+" | "+" | ".join(f"{x['win']:.1%} / {y['win']:.1%}" for x,y in zip(a["fights"],b["fights"]))+" |")
    text.extend(["","## 从大厅出发到达、清关并撤离", "",
                 "每局扣地图门票与 2 杯咖啡、1 枚烟雾弹的购入价；免费通行证机会成本取零。按同一策略保护最高估值的一件货，"
                 "走稳妥奇遇、不捡其他玩家遗物、无新手保险和每日悬赏。两组各自只使用实际掉落的物资。"
                 "开启临装策略后会回血、按拾取顺序穿戴临装，在精英和首领前最多叠三种限层增益，保留保命血量。"
                 "装备与收藏初始固定，入场权限视为已解锁。下表每格为关闭 / 开启临装策略的目标清关撤离率。",
                 "","| 地图与配装 | F4 | F8 | F18 | F30 | F50 | F70 |","|---|---:|---:|---:|---:|---:|---:|"])
    for a,b in zip(rows[::2],rows[1::2]):
        label=cat.maps[a["map"]]["name"]+" · "+model.PRESETS[a["preset"]][0]
        text.append("| "+label+" | "+" | ".join(f"{x['target_exit']:.1%} / {y['target_exit']:.1%}" for x,y in zip(a["trips"],b["trips"]))+" |")
    text.extend(["","## 金币期望", "", "假设回城货物全部以真实回收价出售。以每次开局为分母，失败只计实际保住的物品。"
                 "区间是均值 ±1.96 个标准误，单局结果可能偏离很远。全部原始汇总另存 JSON，其中包括中位数、最差一成均值、负收益比例和撤离率置信区间。",
                 "","| 使用临装的参考配装 | F4 净金 | F8 净金 | F18 净金 | F30 净金 |","|---|---:|---:|---:|---:|"])
    for b in rows[1::2]:
        text.append("| "+cat.maps[b["map"]]["name"]+" · "+model.PRESETS[b["preset"]][0]+" | "+
                    " | ".join(f"{r['net']:,.0f} ± {1.96*r['net_se']:,.0f}" for r in b["trips"][:4])+" |")
    text.extend(["","一次首领胜利独立抽两份常规奖励。第一位首领至少一份金或红的概率为 93.75%，"
                 "到第六位起为 96%。普通物资使用地图估值倍率，金红藏品地图倍率封顶 1.60。正式首领奖励另有一次独立红装判定。"
                 "首领藏品再按当地同款完好藏品的 48% 至 75% 估值。",
                 "", "| 装备箱 | 售价 | 立即卖装的期望回收 | 净金期望 |","|---|---:|---:|---:|"])
    for key,row in economy["boxes"].items():
        text.append(f"| {cat.gear_boxes[key][0]} | {row['price']:,} | {row['sale_ev']:,.1f} | {row['net']:,.1f} |")
    text.extend(["","## 调整数值的方法", "",
                 "对楼层 f 令 x=f−1、z=6 ln(1+x/6)。每种敌人的每项属性先取 a+b·x+c·z，生命、攻击、防御再乘地图倍率。"
                 "边际增长为 b+c/(1+x/6)，正值逐渐趋向 b，保证深层继续增长。敏捷最高 55。"
                 "第 k 个首领的生命另乘 1+0.055 log₂k，攻击乘 1+0.033 log₂k。每第四回合重击，四回合交锋后可以整备。", "",
                 "| 敌人 | 生命 (a,b,c) | 攻击 | 防御 | 敏捷 |","|---|---|---|---|---|"])
    for kind,columns in model.expedition.DIFFICULTY.items():
        text.append("| "+{"normal":"普通","elite":"精英","boss":"首领"}[kind]+" | "+" | ".join(", ".join(map(str,columns[k])) for k in ("hp","atk","def","agi"))+" |")
    text.extend(["", "装备面板由五维基础值、撤离熟练度、四件装备、普通套装、图鉴阶段及虹彩组成。"
                 "同词条取最大值，同彩级重复持有只算一次。工作台战力是输出与有效生存的乘积，供配装排序；具体胜率用逐回合实测。"
                 "工作台可以单独调地图强度、装备底材与浮动、图鉴进度、虹彩技能、开战血量和临装配置。", "",
                 f"局内道具池共 {len(cat.field_items)} 件，各地图只从通用及本地专属道具中抽取。首层、首领前一层及首领层保证拾取，"
                 "其余每层独立 52%。局内物品不参与仓库、售卖、安全箱、遗物或普通掉落判定。", "",
                 "每个正式首领的虹彩总概率固定 p=0.005，命中时优先补缺。首彩期望 200 胜，四件期望 800 胜。"
                 "集齐概率 P(至少四次命中)=1−Σᵢ₌₀³ C(n,i)pⁱ(1−p)ⁿ⁻ⁱ。达到 50%、90%、95% 的累计胜场分别为 735、1,335、1,549。"])
    text.extend(["","## 毕业配装的深层边界","","拍卖行，满图鉴金装、满血单挑首领，每格 2,000 次。"
                 "普通层的属性边际增幅有下界；固定配装的胜率仍会随深度最终趋近零。","","| 首领层 | 单靠装备与背包技能 | 再加三种通用临装 |","|---|---:|---:|"])
    cat,key,hero=model.build({"map":"auction","preset":"rainbow"})
    for floor in (90,130,170,210,250,290):
        values=[model.battle(cat,key,hero,floor,"boss",kit,2000,SEED+floor*10009)["win"] for kit in ([],model.PREPARED)]
        text.append(f"| F{floor} | {values[0]:.1%} | {values[1]:.1%} |")
    return "\n".join(text)+"\n"


def main():
    jobs = [(m,h,p) for (m,h),p in product(PROFILES,(False,True))]
    with ProcessPoolExecutor(max_workers=4) as pool:
        rows=list(pool.map(study,jobs))
    (model.ROOT/"results"/"首领赛季实测.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8")
    (model.ROOT/"首领赛季数值报告.md").write_text(report(rows),encoding="utf-8")
    print(f"{len(rows)} scenarios; 12000 campaigns; battle and economic tables saved")


if __name__=="__main__":
    main()
