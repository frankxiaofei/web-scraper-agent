"""站点 ID 别名：遗留 site_id / 口语 → sites.yaml canonical id。"""

from __future__ import annotations

# 精确 site_id 别名 → canonical id（sites.yaml）
SITE_ID_ALIASES: dict[str, str] = {
    "zhfdc_dlzb": "crec_bidding",
    "www_dlzb": "dlzb_power",
    "tjbid": "中国铁道建筑集团有限公司_物资采购网",
    "zgjtjs": "中国交通建设集团有限公司_供应链管理信息系统",
    "powerchina": "中国电力建设集团有限公司_公共资源交易服务平台",
    "powerchina_bid": "中国电力建设集团有限公司_公共资源交易服务平台",
    "ceec_ec": "中国能源建设集团_ec_ceec",
    "ecceec": "中国能源建设集团_ec_ceec",
    "gov_cg": "gov_cg_national",
    "gov-cg": "gov_cg_national",
}


def resolve_canonical_site_id(site_id: str) -> str:
    """将遗留或误用 site_id 解析为 sites.yaml 中的 canonical id。"""
    key = (site_id or "").strip()
    return SITE_ID_ALIASES.get(key, key)


def site_not_registered_error(site_id: str) -> str:
    """站点未在 config/sites.yaml 注册时的统一错误文案。"""
    return (
        f"站点未注册: {site_id}。"
        "请在 config/sites.yaml 的 sites 列表中添加该站点（或于 src/core/site_aliases.py 添加别名）；"
        "无需 MongoDB sites 集合。"
    )
