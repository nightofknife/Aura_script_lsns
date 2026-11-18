"""Display names for templates used by trade and passenger observations."""

from collections.abc import Mapping
from typing import Any

from packages.aura_core.utils.task_errors import recognition_failure


_TEMPLATE_LABELS = {
    "navigation/global/train_maintenance.png": ("确认主界面", "列车主界面"),
    "navigation/global/back.png": ("返回上一页", "返回按钮"),
    "navigation/global/city_main.png": ("返回列车主界面", "列车主界面按钮"),
    "commerce/trade/shop/menu_ready.png": ("进入交易所", "交易所菜单"),
    "commerce/books/buy_page_ready.png": ("提交买入", "买入按钮"),
    "commerce/trade/buy/preview_title.png": ("确认买入", "买入确认窗口"),
    "commerce/trade/sell/all_button.png": ("卖出货物", "全部卖出按钮"),
    "passenger/management/entry.png": ("打开客运管理", "客运管理入口"),
    "passenger/management/score_marker.png": ("确认客运管理页面", "客运评分标志"),
    "passenger/recruitment/entry.png": ("打开揽客页面", "揽客入口"),
    "passenger/recruitment/flyer_mode.png": ("选择传单揽客", "传单揽客按钮"),
    "passenger/destination/marker.png": ("确认目的地页面", "目的地选择界面"),
    "passenger/destination/next.png": ("确认目的地", "下一步按钮"),
    "passenger/dispatch/marker.png": ("确认发放页面", "传单发放界面"),
    "passenger/dispatch/location_marker.png": ("选择发放地点", "发放地点标志"),
    "passenger/dispatch/confirm.png": ("确认发放地点", "发放地点确认按钮"),
    "passenger/amount/maximum.png": ("设置传单数量", "最大数量按钮"),
    "passenger/amount/confirm.png": ("提交传单", "数量确认按钮"),
    "passenger/recruitment/success.png": ("确认揽客结果", "招募成功提示"),
    "passenger/settlement/revenue.png": ("确认客运结算", "客运收益结算画面"),
}


def with_template_failure(detail: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(detail or {})
    if "recognition" in data:
        return data
    match = data.get("match", data)
    if not isinstance(match, Mapping):
        return data
    template = str(match.get("template") or data.get("template") or "")
    label = _TEMPLATE_LABELS.get(template.removeprefix("templates/"))
    if label is None:
        return data
    if match.get("reason") == "observation_timeout":
        outcome = "timeout"
    elif match.get("valid") is True and match.get("found") is False:
        outcome = "not_found"
    elif match.get("valid") is False:
        outcome = "unavailable"
    else:
        return data
    data.update(recognition_failure(*label, "template", outcome, timeout_sec=match.get("timeout_sec")))
    return data
