"""Simplified <-> Traditional Chinese character variants for keyword matching (no dependency).

The keyword terms of a zh idea are Simplified (the local model writes them so, CNINFO filings are Simplified), while
MOPS / TWSE filings are Traditional (認證, 機器人, 權限). `screen._term_pattern` matches every Han character of a term
against all its variants from this table, and `calib._fold` folds text to Simplified, so 身份认证 finds 身份認證 and
the background df counts both scripts.

The table is small on purpose: the characters of business, technology, finance, pharma, metals and industry
vocabulary (about 1,200 pairs), not a general converter. A character that is the same in both scripts needs no
entry. A Traditional form that is itself a different Simplified word is never listed; a few one-to-many characters
are grouped (制 製, 台 臺, 志 誌, 干 幹, 余 馀 餘, 硅 矽, 复 複 復, 系 係 繫, 历 歷 曆); every group folds to one
Simplified form. Japanese shinjitai (証, 権, 変) are not Chinese variants and are not listed. Only characters are
bridged, not vocabulary: 软件 / 軟體, 信息 / 資訊, 身份 / 身分 are different words and do not match each other.
"""
from __future__ import annotations

import hashlib

# "SimplifiedTraditional" pairs, separated by spaces
_PAIRS = """
认認 证證 权權 访訪 问問 验驗 码碼 钥鑰 数數 据據 库庫 网網 络絡 云雲 计計 务務 软軟 设設 备備 统統 应應 开開 发發
电電 脑腦 机機 车車 动動 气氣 录錄 号號 帐帳 账賬 户戶 签簽 书書 击擊 恶惡 隐隱 执執 纸紙 志誌 审審 许許 专專 类類
规規 则則 标標 准準 质質 检檢 测測 试試 仪儀 传傳 导導 体體 圆圓 显顯 学學 术術 创創 驱驅 减減 齿齒 轮輪 轴軸 关關
节節 谐諧 组組 装裝 构構 链鏈 条條 线線 输輸 运運 维維 护護 监監 视視 频頻 声聲 语語 识識 别別 图圖 处處 实實 现現
时時 间間 单單 双雙 对對 为為 这這 个個 们們 来來 从從 进進 选選 择擇 项項 页頁 题題 顾顧 员員 费費 价價 额額 总總
润潤 亏虧 损損 负負 责責 资資 产產 业業 营營 销銷 贸貿 银銀 币幣 钱錢 结結 汇匯 转轉 财財 险險 债債 东東 亚亞 国國
际際 区區 华華 韩韓 马馬 并並 购購 买買 卖賣 贷貸 储儲 门門 终終 边邊 缘緣 块塊 矿礦 铁鐵 钢鋼 铜銅 铝鋁 锂鋰 镍鎳
钴鈷 风風 热熱 阳陽 农農 渔漁 养養 岛島 细細 临臨 订訂 诊診 断斷 药藥 医醫 疗療 剂劑 厂廠 场場 游遊 园園 饮飲 厅廳
连連 锁鎖 仓倉 递遞 邮郵 轨軌 桥橋 筑築 楼樓 变變 压壓 纤纖 纺紡 织織 戏戲 娱娛 乐樂 广廣 报報 杂雜 训訓 课課 预預
决決 优優 调調 货貨 还還 义義 严嚴 历歷 经經 济濟 势勢 竞競 争爭 战戰 划劃 过過 复複 简簡 众眾 侧側 层層 级級 头頭
编編 写寫 读讀 获獲 档檔 馈饋 响響 请請 协協 议議 虚虛 拟擬 强強 习習 练練 讯訊 联聯 话話 会會 办辦 岗崗 税稅 兑兌
换換 台臺 后後 里裡 余餘 范範 丰豐 制製 点點 扫掃 尔爾 鲁魯 宾賓 达達 桩樁 与與 亿億 万萬 长長 闭閉 队隊 阶階 陆陸
随隨 离離 难難 须須 顺順 领領 飞飛 馆館 驾駕 鱼魚 鸟鳥 黄黃 齐齊 龙龍 丝絲 两兩 丽麗 举舉 乌烏 乱亂 亲親 仅僅 伙夥
伟偉 伤傷 伦倫 侦偵 侨僑 俭儉 倾傾 偿償 儿兒 兴興 兰蘭 养養 兽獸 册冊 军軍 冲衝 况況 冻凍 净淨 凉涼 凤鳳 凭憑 刚剛
删刪 剧劇 励勵 劳勞 勋勳 卢盧 卫衛 却卻 厌厭 县縣 参參 叙敘 叶葉 叹嘆 吗嗎 启啟 团團 围圍 圣聖 坏壞 坚堅 坛壇 垒壘
夹夾 夺奪 奋奮 奖獎 妇婦 婴嬰 孙孫 宁寧 宝寶 宪憲 宫宮 宽寬 寻尋 寿壽 将將 尘塵 尝嘗 属屬 岁歲 岭嶺 师師 带帶 帮幫
干幹 庆慶 废廢 异異 弃棄 张張 弯彎 弹彈 归歸 当當 彻徹 径徑 忆憶 态態 恋戀 恳懇 惊驚 惧懼 惯慣 愤憤 忧憂 怀懷 扑撲
扩擴 扬揚 扰擾 抚撫 抛拋 担擔 拥擁 挂掛 挡擋 挤擠 挥揮 掷擲 摄攝 摆擺 携攜 敌敵 斋齋 无無 旧舊 晋晉 晒曬 晓曉 暂暫
杀殺 杨楊 枪槍 柜櫃 栈棧 栋棟 树樹 样樣 梦夢 横橫 欢歡 欧歐 残殘 毁毀 毕畢 汉漢 汤湯 沟溝 没沒 泽澤 洁潔 浅淺 浓濃
涂塗 涨漲 渐漸 湾灣 湿濕 满滿 滚滾 滞滯 灭滅 灯燈 灵靈 灾災 炉爐 炼煉 烟煙 烧燒 爱愛 牵牽 犹猶 状狀 独獨 环環 玛瑪
画畫 畅暢 盐鹽 盖蓋 盘盤 础礎 硕碩 确確 碍礙 礼禮 种種 积積 称稱 稳穩 穷窮 窃竊 笔筆 粮糧 紧緊 纪紀 约約 纳納 纵縱
纷紛 绍紹 绕繞 绘繪 给給 绝絕 继繼 绩績 续續 综綜 绿綠 缓緩 缩縮 缴繳 罗羅 罚罰 职職 聪聰 肃肅 胁脅 脱脫 脏臟 舰艦
舱艙 艺藝 芦蘆 苏蘇 荐薦 萝蘿 蓝藍 虑慮 虽雖 蚀蝕 补補 衬襯 袭襲 见見 观觀 览覽 觉覺 誉譽 讨討 让讓 记記 讲講 论論
评評 诉訴 词詞 译譯 诚誠 询詢 该該 详詳 误誤 说說 诸諸 谈談 谋謀 谓謂 谢謝 谱譜 贝貝 贡貢 贤賢 败敗 贩販 贪貪 贯貫
贵貴 贺賀 赋賦 赔賠 赖賴 赚賺 赛賽 赞贊 赠贈 赵趙 赶趕 趋趨 跃躍 践踐 踪蹤 轻輕 载載 较較 辅輔 辆輛 辈輩 辑輯 辖轄
迁遷 远遠 违違 迟遲 适適 逻邏 遗遺 邻鄰 郑鄭 释釋 鉴鑒 针針 钉釘 钟鐘 铃鈴 铅鉛 铺鋪 锅鍋 错錯 锚錨 锡錫 锦錦 键鍵
锻鍛 镇鎮 镜鏡 闪閃 闲閒 闸閘 阀閥 阅閱 阴陰 阵陣 陈陳 雾霧 顶頂 顿頓 饭飯 驶駛 驻駐 骤驟 鲜鮮 鸡雞 麦麥 氢氫 锌鋅
钛鈦 钨鎢 钼鉬 硅矽 纯純 纲綱 纬緯 缝縫 缆纜 网網 罐罐 肠腸 肤膚 肿腫 胶膠 脉脈 脂脂 腾騰 舍捨 艰艱 药藥 荧熒 莱萊
获獲 蚁蟻 蛮蠻 衔銜 补補 视視 触觸 计計 讼訟 诈詐 诱誘 贴貼 贮貯 赁賃 跟跟 践踐 轰轟 辉輝 辞辭 迈邁 这這 邀邀 郁鬱
酝醞 酿釀 钓釣 钞鈔 钠鈉 钩鉤 钮鈕 钻鑽 铭銘 铸鑄 铬鉻 销銷 锈鏽 锋鋒 锐銳 镀鍍 镁鎂 门門 闯闖 闻聞 阁閣 阔闊 阻阻
陨隕 隶隸 韧韌 韵韻 颁頒 颇頗 频頻 颖穎 颗顆 颜顏 飘飄 饰飾 饱飽 饲飼 馏餾 驰馳 驳駁 骄驕 骗騙 髅髏 鹅鵝 鹤鶴 麻麻
黾黽 鼓鼓 齿齒 猪豬 猫貓 狮獅 献獻 疮瘡 疯瘋 痒癢 瘫癱 癫癲 皱皺 盏盞 睁睜 瞒瞞 矫矯 砖磚 砚硯 硷鹼 碱鹼 祸禍 禅禪
秃禿 秆稈 税稅 稣穌 窑窯 窜竄 竖豎 笼籠 筛篩 筹籌 签簽 篮籃 粪糞 糁糝 纠糾 红紅 纤纖 级級 纹紋 纺紡 线線 组組 绅紳
绒絨 绑綁 绒絨 统統 绢絹 绣繡 绥綏 绳繩 维維 绵綿 绸綢 缀綴 缅緬 缆纜 缔締 缕縷 缚縛 缝縫 缠纏 缸缸 网網 罢罷 罩罩
羡羨 翘翹 耸聳 耻恥 聂聶 聋聾 肾腎 胀脹 胆膽 胜勝 胧朧 脍膾 脸臉 腊臘 腻膩 膑臏 舆輿 舣艤 艳豔 芜蕪 苇葦 苍蒼 茎莖
荚莢 荡蕩 荣榮 荤葷 荫蔭 药藥 莲蓮 莹瑩 莺鶯 萤螢 营營 萧蕭 蔼藹 蕴蘊 虏虜 虫蟲 虾蝦 蚂螞 蚕蠶 蛎蠣 蜡蠟 蝇蠅 衅釁
补補 衮袞 袄襖 裤褲 览覽 觅覓 誊謄 讥譏 讪訕 讫訖 讳諱 讶訝 讹訛 讽諷 设設 诀訣 证證 诂詁 诃訶 诅詛 识識 诈詐 诉訴
诊診 诏詔 诡詭 询詢 诣詣 试試 诗詩 诘詰 诙詼 诚誠 诛誅 话話 诞誕 诟詬 诠詮 诡詭 诣詣 该該 详詳 诧詫 诫誡 诬誣 语語
误誤 诱誘 诲誨 说說 诵誦 请請 诸諸 诺諾 读讀 诽誹 课課 谁誰 调調 谅諒 谆諄 谈談 谊誼 谋謀 谍諜 谎謊 谐諧 谒謁 谓謂
谕諭 谚諺 谜謎 谤謗 谦謙 谨謹 谬謬 谭譚 谱譜 谴譴 谷穀 豚豚 贞貞 负負 贡貢 财財 责責 贤賢 败敗 账賬 货貨 质質 贩販
贬貶 购購 贮貯 贯貫 贱賤 贴貼 贵貴 贷貸 贸貿 费費 贺賀 贼賊 贾賈 贿賄 赁賃 资資 赂賂 赃贓 赌賭 赏賞 赐賜 赔賠 赖賴
赘贅 赚賺 赛賽 赠贈 赡贍 赢贏 赣贛 趋趨 跃躍 踌躊 踊踴 踪蹤 蹑躡 躯軀 轧軋 轨軌 轩軒 转轉 轮輪 软軟 轰轟 轴軸 轶軼
轻輕 载載 轿轎 较較 辅輔 辆輛 辉輝 辐輻 输輸 辕轅 辖轄 辗輾 辙轍 辩辯 辫辮 边邊 辽遼 达達 迁遷 过過 迈邁 运運 还還
这這 进進 远遠 违違 连連 迟遲 迹跡 适適 选選 逊遜 递遞 逻邏 遗遺 遥遙 邓鄧 邝鄺 邬鄔 邮郵 邹鄒 邻鄰 郁鬱 郄郤 郏郟
郐鄶 郑鄭 郓鄆 郦酈 郧鄖 郸鄲 酝醞 酱醬 酿釀 释釋 鉴鑒 銮鑾 錾鏨 钇釔 钉釘 钊釗 钋釙 钌釕 钍釷 钎釺 钏釧 钐釤 钒釩
钓釣 钔鍆 钕釹 钗釵 钙鈣 钝鈍 钞鈔 钟鐘 钠鈉 钡鋇 钢鋼 钣鈑 钤鈐 钥鑰 钦欽 钧鈞 钩鉤 钪鈧 钫鈁 钬鈥 钭鈄 钮鈕 钯鈀
钰鈺 钱錢 钲鉦 钳鉗 钴鈷 钵缽 钹鈸 钺鉞 钻鑽 钼鉬 钾鉀 钿鈿 铀鈾 铁鐵 铂鉑 铃鈴 铄鑠 铅鉛 铆鉚 铈鈰 铉鉉 铊鉈 铋鉍
铌鈮 铍鈹 铎鐸 铐銬 铑銠 铒鉺 铕銪 铖鋮 铗鋏 铙鐃 铛鐺 铜銅 铝鋁 铞銱 铟銦 铠鎧 铡鍘 铢銖 铣銑 铤鋌 铥銩 铧鏵 铨銓
铩鎩 铪鉿 铫銚 铬鉻 铭銘 铮錚 铯銫 铰鉸 铱銥 铲鏟 铳銃 铴鐋 铵銨 银銀 铷銣 铸鑄 铹鐒 铺鋪 铼錸 铽鋱 链鏈 铿鏗 销銷
锁鎖 锂鋰 锃鋥 锄鋤 锅鍋 锆鋯 锇鋨 锈鏽 锉銼 锊鋝 锋鋒 锌鋅 锍鋶 锎鐦 锏鐧 锐銳 锑銻 锒鋃 锓鋟 锔鋦 锕錒 锖錆 锗鍺
错錯 锚錨 锛錛 锞錁 锟錕 锡錫 锢錮 锣鑼 锤錘 锥錐 锦錦 锨鍁 锩錈 锪鍃 锫錇 锬錟 锭錠 键鍵 锯鋸 锰錳 锱錙 锲鍥 锴鍇
锵鏘 锶鍶 锷鍔 锸鍤 锹鍬 锺鍾 锻鍛 锼鎪 锾鍰 锿鎄 镀鍍 镁鎂 镂鏤 镄鐨 镅鎇 镆鏌 镇鎮 镉鎘 镊鑷 镌鐫 镍鎳 镎鎿 镏鎦
镐鎬 镑鎊 镒鎰 镓鎵 镔鑌 镖鏢 镗鏜 镘鏝 镙鏍 镛鏞 镜鏡 镝鏑 镞鏃 镟鏇 镡鐔 镢鐝 镣鐐 镤鏷 镧鑭 镨鐠 镩鑹 镪鏹 镫鐙
镬鑊 镭鐳 镯鐲 镰鐮 镱鐿 镲鑔 镳鑣 镶鑲 长長 门門 闩閂 闪閃 闫閆 闭閉 问問 闯闖 闰閏 闱闈 闲閒 闳閎 间間 闵閔 闶閌
闷悶 闸閘 闹鬧 闺閨 闻聞 闼闥 闽閩 闾閭 阀閥 阁閣 阂閡 阃閫 阄鬮 阅閱 阈閾 阉閹 阊閶 阋鬩 阌閿 阍閽 阎閻 阏閼 阐闡
阑闌 阒闃 阔闊 阕闋 阖闔 阗闐 阙闕 阚闞 队隊 阳陽 阴陰 阵陣 阶階 际際 陆陸 陇隴 陈陳 陉陘 陕陝 陧隉 陨隕 险險 随隨
隐隱 隶隸 隽雋 难難 雏雛 雠讎 雳靂 雾霧 霁霽 霭靄 靓靚 静靜 靥靨 鞑韃 鞒鞽 鞯韉 韦韋 韧韌 韩韓 韪韙 韫韞 韬韜 韵韻
页頁 顶頂 顷頃 项項 顺順 须須 顼頊 顽頑 顾顧 顿頓 颀頎 颁頒 颂頌 颃頏 预預 颅顱 领領 颇頗 颈頸 颉頡 颊頰 颌頜 颍潁
颏頦 颐頤 频頻 颓頹 颔頷 颖穎 颗顆 题題 颚顎 颛顓 颜顏 额額 颞顳 颟顢 颠顛 颡顙 颢顥 颤顫 颦顰 颧顴 风風 飏颺 飐颭
飑颮 飒颯 飓颶 飕颼 飘飄 飙飆 飞飛 飨饗 餍饜 饥飢 饧餳 饨飩 饪飪 饫飫 饬飭 饭飯 饮飲 饯餞 饰飾 饱飽 饲飼 饴飴 饵餌
饶饒 饷餉 饺餃 饼餅 饽餑 饿餓 馀餘 馁餒 馄餛 馅餡 馆館 馈饋 馊餿 馋饞 馍饃 馏餾 馐饈 馑饉 馒饅 馓饊 馔饌 馕饢 马馬
驭馭 驮馱 驯馴 驰馳 驱驅 驳駁 驴驢 驵駔 驶駛 驷駟 驸駙 驹駒 驺騶 驻駐 驼駝 驽駑 驾駕 驿驛 骀駘 骁驍 骂罵 骄驕 骅驊
骆駱 骇駭 骈駢 骊驪 骋騁 验驗 骏駿 骐騏 骑騎 骒騍 骓騅 骖驂 骗騙 骘騭 骚騷 骛騖 骜驁 骝騮 骞騫 骟騸 骠驃 骡騾 骢驄
骣驏 骤驟 骥驥 骧驤 髅髏 髋髖 髌髕 鬓鬢 魇魘 魉魎 鱼魚 鸟鳥 鸡雞 鸣鳴 鸭鴨 鸽鴿 鹅鵝 鹤鶴 鹰鷹 麦麥 黄黃 齐齊 齿齒
龙龍 龟龜
复復 系係 系繫 于於 历曆 获穫 钟鍾 表錶 发髮 面麵
"""


def _build() -> tuple[dict[str, str], dict[str, str]]:
    """(variants, to_simplified). Every member of a group (余 馀 餘, 碱 硷 鹼, 钟 锺 鐘 鍾) folds to ONE Simplified
    form, the one of the group's first pair in the table, so a folded term is a substring of the folded text
    whenever the pattern matches (calib._hits relies on it)."""
    groups: dict[str, set[str]] = {}
    canon: dict[str, str] = {}                 # any member -> the canonical Simplified form of its group
    for pair in _PAIRS.split():
        if len(pair) != 2 or pair[0] == pair[1]:
            continue
        s, t = pair
        c = canon.get(s) or canon.get(t) or s
        g = groups.get(s, {s}) | groups.get(t, {t})
        for ch in g:
            groups[ch] = g
            canon[ch] = c
    variants = {ch: "".join(sorted(g)) for ch, g in groups.items() if len(g) > 1}
    to_simp = {ch: c for ch, c in canon.items() if ch != c}
    return variants, to_simp


VARIANTS, TO_SIMPLIFIED = _build()
TABLE_VERSION = "zhv-" + hashlib.sha256(_PAIRS.encode("utf-8")).hexdigest()[:12]   # keys caches of matched counts
_TABLE = str.maketrans(TO_SIMPLIFIED)


def variants(ch: str) -> str:
    """Every form of a character in the table ('认' -> '認认'), or the character itself."""
    return VARIANTS.get(ch, ch)


def to_simplified(text: str) -> str:
    """Traditional characters of the table replaced by their Simplified form (others unchanged)."""
    return text.translate(_TABLE) if text else text
