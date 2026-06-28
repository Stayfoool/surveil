"""Overseas semiconductor media source definitions."""

from __future__ import annotations


OVERSEAS_MEDIA_FEEDS = {
    "digitimes_tw_semiconductors_components": "https://www.digitimes.com.tw/tech/rss/xml/xmlrss_10_40.xml",
    "digitimes_tw_ic_design": "https://www.digitimes.com.tw/tech/rss/xml/xmlrss_30_16.xml",
    "digitimes_tw_ic_manufacturing": "https://www.digitimes.com.tw/tech/rss/xml/xmlrss_30_17.xml",
    "digitimes_tw_ai_focus": "https://www.digitimes.com.tw/tech/rss/xml/xmlrss_30_25.xml",
    "digitimes_tw_server": "https://www.digitimes.com.tw/tech/rss/xml/xmlrss_30_26.xml",
    "digitimes_en_daily": "https://www.digitimes.com/rss/daily.xml",
    "nikkei_xtech_all": "https://xtech.nikkei.com/rss/index.rdf",
    "thelec_kr_semiconductor": "https://www.thelec.kr/rss/S1N2.xml",
    "thelec_kr_all": "https://www.thelec.kr/rss/allArticle.xml",
}


OVERSEAS_MEDIA_LABELS = {
    "digitimes_tw_semiconductors_components": "DIGITIMES Taiwan / 半导体与零组件",
    "digitimes_tw_ic_design": "DIGITIMES Taiwan / IC 设计",
    "digitimes_tw_ic_manufacturing": "DIGITIMES Taiwan / IC 制造",
    "digitimes_tw_ai_focus": "DIGITIMES Taiwan / AI Focus",
    "digitimes_tw_server": "DIGITIMES Taiwan / 服务器",
    "digitimes_en_daily": "DIGITIMES English / Daily RSS",
    "nikkei_xtech_all": "日经 xTECH / 全站 RDF",
    "thelec_kr_semiconductor": "The Elec Korea / 半导体",
    "thelec_kr_all": "The Elec Korea / 全站 RSS",
}


OVERSEAS_MEDIA_ACCESS_NOTES = {
    "digitimes_tw_semiconductors_components": "免费/付费状态以 DIGITIMES 原页面为准；当前通过官方 RSS 读取公开标题/摘要，不绕过会员权限。",
    "digitimes_tw_ic_design": "免费/付费状态以 DIGITIMES 原页面为准；当前通过官方 RSS 读取公开标题/摘要，不绕过会员权限。",
    "digitimes_tw_ic_manufacturing": "免费/付费状态以 DIGITIMES 原页面为准；当前通过官方 RSS 读取公开标题/摘要，不绕过会员权限。",
    "digitimes_tw_ai_focus": "免费/付费状态以 DIGITIMES 原页面为准；当前通过官方 RSS 读取公开标题/摘要，不绕过会员权限。",
    "digitimes_tw_server": "免费/付费状态以 DIGITIMES 原页面为准；当前通过官方 RSS 读取公开标题/摘要，不绕过会员权限。",
    "digitimes_en_daily": "免费/付费状态以 DIGITIMES 原页面为准；当前通过官方 RSS 读取公开标题/摘要，不绕过会员权限。",
    "nikkei_xtech_all": "免费/付费状态以日经 xTECH 原页面为准；当前通过官方 RDF 读取公开标题/摘要，不绕过登录或会员权限。",
    "thelec_kr_semiconductor": "免费/付费状态以 The Elec 原页面为准；当前通过官方 RSS 读取公开标题/摘要，不绕过登录或付费墙。",
    "thelec_kr_all": "免费/付费状态以 The Elec 原页面为准；当前通过官方 RSS 读取公开标题/摘要，不绕过登录或付费墙。",
}


def is_overseas_media_source(source: str) -> bool:
    return source in OVERSEAS_MEDIA_FEEDS


def overseas_media_module(source: str) -> str:
    return OVERSEAS_MEDIA_LABELS.get(source, source)


def overseas_media_access_note(source: str, body_source: str) -> str:
    return OVERSEAS_MEDIA_ACCESS_NOTES.get(
        source,
        f"免费/付费状态未知：正文来源为 {body_source}；以原页面访问权限为准。",
    )
