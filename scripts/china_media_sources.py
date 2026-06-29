"""Domestic finance media source definitions and access notes."""

from __future__ import annotations


CHINA_MEDIA_FEEDS = {
    "yicai_brief": "https://www.yicai.com/api/ajax/getbrieflist?type=0&page=1&pagesize=20",
    "yicai_brief_rsshub": "https://rsshub.rssforever.com/yicai/brief",
    "cls_telegraph_page": "https://www.cls.cn/telegraph",
    "jin10_rsshub_important": "https://rsshub.rssforever.com/jin10/important",
}


CHINA_MEDIA_LABELS = {
    "yicai_brief": "第一财经 / 早晚快讯",
    "yicai_brief_rsshub": "第一财经 / RSSHub 备选",
    "cls_telegraph_page": "财联社 / 电报",
    "jin10_rsshub_important": "金十资讯 / 重要事件",
}


CHINA_MEDIA_ACCESS_NOTES = {
    "yicai_brief": "公开 JSON 接口：当前优先使用，不绕过登录或付费墙。",
    "yicai_brief_rsshub": "RSSHub 备选：仅作为公开路由补充，不作为唯一主路径。",
    "cls_telegraph_page": "公开电报页：优先解析页面公开内容；如你提供授权 API，可另行接入。",
    "jin10_rsshub_important": "RSSHub 备选：仅作为公开路由补充，不绕过登录、付费或 WAF。",
}


def is_china_media_source(source: str) -> bool:
    return source in CHINA_MEDIA_FEEDS


def china_media_module(source: str) -> str:
    return CHINA_MEDIA_LABELS.get(source, source)


def china_media_access_note(source: str, body_source: str) -> str:
    return CHINA_MEDIA_ACCESS_NOTES.get(
        source,
        f"免费/付费状态未知：正文来源为 {body_source}；以原页面访问权限为准。",
    )
