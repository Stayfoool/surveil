"""Small curated company map for market-analysis cards."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Company:
    symbol: str
    name: str
    listing: str
    cn_name: str = ""


COMPANIES: dict[str, Company] = {
    "$NVDA": Company("$NVDA", "NVIDIA Corporation", "NASDAQ", "英伟达"),
    "$AMD": Company("$AMD", "Advanced Micro Devices, Inc.", "NASDAQ", "超威半导体"),
    "$AVGO": Company("$AVGO", "Broadcom Inc.", "NASDAQ", "博通"),
    "$MU": Company("$MU", "Micron Technology, Inc.", "NASDAQ", "美光科技"),
    "$AMZN": Company("$AMZN", "Amazon.com, Inc.", "NASDAQ", "亚马逊"),
    "$MSFT": Company("$MSFT", "Microsoft Corporation", "NASDAQ", "微软"),
    "$GOOGL": Company("$GOOGL", "Alphabet Inc.", "NASDAQ", "谷歌母公司 Alphabet"),
    "$META": Company("$META", "Meta Platforms, Inc.", "NASDAQ", "Meta"),
    "$ORCL": Company("$ORCL", "Oracle Corporation", "NYSE", "甲骨文"),
    "$CRWV": Company("$CRWV", "CoreWeave, Inc.", "NASDAQ", "CoreWeave"),
    "$AAOI": Company("$AAOI", "Applied Optoelectronics, Inc.", "NASDAQ", "应用光电"),
    "$LITE": Company("$LITE", "Lumentum Holdings Inc.", "NASDAQ", "Lumentum"),
    "$COHR": Company("$COHR", "Coherent Corp.", "NYSE", "Coherent"),
    "$SIVE": Company("$SIVE", "Sivers Semiconductors AB", "Nasdaq Stockholm", "Sivers Semiconductors"),
    "$AXTI": Company("$AXTI", "AXT, Inc.", "NASDAQ", "AXT"),
    "$SPCX": Company("$SPCX", "SPCX", "unknown", "SPCX"),
    "$EWY": Company("$EWY", "iShares MSCI South Korea ETF", "NYSE Arca", "韩国 ETF"),
    "$SNDK": Company("$SNDK", "SanDisk Corporation", "NASDAQ", "闪迪"),
    "$IREN": Company("$IREN", "IREN Limited", "NASDAQ", "IREN"),
    "$SOI": Company("$SOI", "Soitec S.A.", "Euronext Paris", "Soitec"),
    "$IQE": Company("$IQE", "IQE plc", "London Stock Exchange AIM", "IQE"),
    "$XFAB": Company("$XFAB", "X-FAB Silicon Foundries SE", "Euronext Paris", "X-FAB"),
    "$LPK": Company("$LPK", "LPKF Laser & Electronics SE", "Xetra", "LPKF Laser"),
    "$ALRIB": Company("$ALRIB", "Riber S.A.", "Euronext Growth Paris", "Riber"),
    "$RPI": Company("$RPI", "Raspberry Pi Holdings plc", "London Stock Exchange", "Raspberry Pi"),
    "$INTC": Company("$INTC", "Intel Corporation", "NASDAQ", "英特尔"),
}


A_SHARE_THEMES = {
    "MLCC/被动元件": {
        "positive": [
            ("风华高科（000636.SZ，深交所主板）", "国内 MLCC 龙头之一，若高端 MLCC 规格集中和结构性缺货加剧，具备国产替代和价格弹性观察价值。"),
            ("三环集团（300408.SZ，深交所创业板）", "电子陶瓷和被动元件平台型公司，高容高可靠 MLCC/陶瓷材料趋势相关度较高。"),
            ("洁美科技（002859.SZ，深交所主板）", "MLCC 离型膜/纸质载带等上游材料相关，受益取决于 MLCC 产能利用率和扩产节奏。"),
            ("火炬电子（603678.SH，上交所主板）", "军工和高可靠电容相关，主题相关但与 AI 高端 MLCC 的直接性需看产品结构。"),
        ],
        "negative": [
            "若海外 MLCC 厂商扩产快于预期或 AI ASIC 设计切换降低用量，高端 MLCC 缺货和涨价预期可能降温。"
        ],
        "us_positive": [
            ("Murata Manufacturing（6981.T，东京证券交易所）", "全球 MLCC 龙头，高端规格集中和结构性短缺最直接受益。"),
            ("Taiyo Yuden（6976.T，东京证券交易所）", "高端 MLCC 主要供应商之一，受益于 AI 加速器高规格被动元件需求。"),
            ("TDK（6762.T，东京证券交易所）", "被动元件和电子材料大厂，高可靠 MLCC 需求改善相关。"),
        ],
        "us_negative": [
            "ASIC/AI 加速器厂商若受 MLCC 短缺约束，交付节奏和 BOM 成本可能承压，例如 $AMD、$NVDA 自研/定制 ASIC 供应链。"
        ],
    },
    "光互连/CPO/激光器": {
        "positive": [
            ("中际旭创（300308.SZ，深交所创业板）", "全球高速光模块龙头，最直接受益于 AI 数据中心光互连需求和 800G/1.6T 升级。"),
            ("新易盛（300502.SZ，深交所创业板）", "高速光模块核心供应商，CPO/高速光引擎预期升温时弹性通常较高。"),
            ("天孚通信（300394.SZ，深交所创业板）", "光器件平台型公司，受益于高速光模块和上游精密光器件需求增长。"),
            ("源杰科技（688498.SH，上交所科创板）", "激光芯片标的，若推文核心在激光器/DFB/CW laser，相关度更直接。"),
            ("光迅科技（002281.SZ，深交所主板）", "光器件和模块老牌厂商，受益方向明确但弹性需看产品结构和客户进展。"),
        ],
        "negative": [
            "若海外激光器/光引擎供应商锁定核心产能，国内后进入者或低端同质化光模块厂商议价能力承压。"
        ],
    },
    "存储/HBM/DRAM": {
        "positive": [
            ("澜起科技（688008.SH，上交所科创板）", "内存接口芯片龙头，HBM/服务器内存升级与 AI 服务器需求相关度高。"),
            ("香农芯创（300475.SZ，深交所创业板）", "存储分销和产业链弹性标的，存储涨价周期中市场关注度较高。"),
            ("兆易创新（603986.SH，上交所主板）", "存储芯片设计龙头之一，存储周期改善时具备方向相关性。"),
            ("北京君正（300223.SZ，深交所创业板）", "车规/工规存储相关标的，受益更多取决于细分需求恢复。"),
            ("深科技（000021.SZ，深交所主板）", "存储封测相关，周期弹性和订单兑现需持续验证。"),
        ],
        "negative": [
            "若海外 DRAM/HBM 大厂扩产超预期或价格回落，存储周期弹性标的估值可能承压。"
        ],
    },
    "半导体材料/衬底/外延": {
        "positive": [
            ("源杰科技（688498.SH，上交所科创板）", "若主题聚焦 InP/DFB/激光芯片，相关度高于泛材料标的。"),
            ("天岳先进（688234.SH，上交所科创板）", "SiC 衬底标的，适用于推文涉及 SiC/功率半导体材料时。"),
            ("沪硅产业（688126.SH，上交所科创板）", "硅片平台公司，适用于半导体衬底和国产替代主题。"),
            ("立昂微（605358.SH，上交所主板）", "硅片和功率器件相关，受益需看周期修复与产品结构。"),
            ("有研新材（600206.SH，上交所主板）", "半导体材料平台，主题相关但直接性需结合具体材料环节。"),
        ],
        "negative": [
            "若海外关键材料供应链缓解，国产替代的紧迫性和涨价预期可能降温。"
        ],
    },
    "AI 算力/资本开支": {
        "positive": [
            ("工业富联（601138.SH，上交所主板）", "AI 服务器制造链核心标的，直接受益于海外云厂商资本开支扩张。"),
            ("沪电股份（002463.SZ，深交所主板）", "AI 服务器 PCB 高相关标的，受益于高速互连和服务器平台升级。"),
            ("胜宏科技（300476.SZ，深交所创业板）", "AI PCB 弹性标的，受益逻辑与服务器/加速卡需求相关。"),
            ("浪潮信息（000977.SZ，深交所主板）", "服务器整机标的，更多受国内 AI 服务器需求和国产生态影响。"),
            ("中科曙光（603019.SH，上交所主板）", "国产算力基础设施标的，偏国内算力建设方向。"),
        ],
        "negative": [
            "若海外云厂商资本开支放缓，AI 服务器、PCB、散热和光模块链条估值可能回撤。"
        ],
    },
    "先进封装/玻璃基板/PCB": {
        "positive": [
            ("沪电股份（002463.SZ，深交所主板）", "AI 服务器和高速互连 PCB 高相关，若 CoPoS/FOPLP/玻璃基板推动封装与板级互连升级，直接受益方向较清晰。"),
            ("胜宏科技（300476.SZ，深交所创业板）", "AI 服务器 PCB 弹性标的，受益于高速板、加速卡和先进封装配套需求。"),
            ("深南电路（002916.SZ，深交所主板）", "PCB 与封装基板平台公司，若先进封装/玻璃基板产业链扩张，具备产业链配套观察价值。"),
        ],
        "negative": [
            "若玻璃基板/FOPLP 量产良率或客户验证不及预期，相关 PCB、封装材料和设备主题估值可能回落。"
        ],
        "us_positive": [
            ("TSMC（2330.TW / TSM，台湾证券交易所 / NYSE ADR）", "先进封装产能和 CoWoS/CoPoS 路线核心受益者。"),
            ("Amkor Technology（AMKR，NASDAQ）", "先进封装外包龙头之一，受益于 AI 芯片封装需求外溢。"),
            ("Ibiden（4062.T，东京证券交易所）", "高端封装基板供应商，受益于 AI/HPC 封装基板需求。"),
        ],
        "us_negative": [
            "先进封装瓶颈若继续存在，部分 AI 加速器客户交付节奏可能受约束。"
        ],
    },
    "半导体设备/材料": {
        "positive": [
            ("北方华创（002371.SZ，深交所主板）", "国产半导体设备平台龙头，若晶圆厂扩产或先进制程设备需求上修，相关度高。"),
            ("中微公司（688012.SH，上交所科创板）", "刻蚀/MOCVD 等设备核心标的，受益于先进制程和国产替代设备需求。"),
            ("盛美上海（688082.SH，上交所科创板）", "清洗、电镀等设备供应商，受益于晶圆厂资本开支和先进封装设备需求。"),
        ],
        "negative": [
            "若全球晶圆厂 capex 延后、出口管制加剧或国产验证不及预期，设备材料链条可能承压。"
        ],
    },
    "AI 应用/大模型": {
        "positive": [
            ("科大讯飞（002230.SZ，深交所主板）", "国内 AI 应用和大模型产业化代表，受益取决于商业化订单和端侧/行业应用落地。"),
            ("金山办公（688111.SH，上交所科创板）", "办公软件 AI 应用高相关标的，受益逻辑来自订阅提价、AI 功能渗透和企业付费。"),
            ("拓尔思（300229.SZ，深交所创业板）", "NLP/数据智能相关，AI 应用主题弹性较高但需验证订单质量。"),
        ],
        "negative": [
            "若大模型应用商业化低于预期或算力成本难以下降，AI 应用主题估值可能承压。"
        ],
    },
}
