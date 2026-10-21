"""Preparation controls only; no product catalogue or stock recognition."""
from ._black_moon_local_shop_vision import BlackMoonLocalShopVision, _image, _resources


CONTROL_NAMES = (
    "shop_entry", "shop_page", "local_tab_selected", "local_tab_unselected", "batch_on", "batch_off",
)


class BlackMoonLocalPrepareVision(BlackMoonLocalShopVision):
    @property
    def _data(self):
        return _resources(self.template_root, CONTROL_NAMES, self.plan_root)

    def observe(self, frame):
        frame = _image(frame)
        names = CONTROL_NAMES[1:]
        matches = {name: self.match(frame, name) for name in names}
        result = {"page_state": "unknown", "local_selected": None,
                  "batch_enabled": None, "ready": False, "matches": matches}
        if not matches["shop_page"]["found"]:
            return result

        def state(on, off, group):
            margin = self._data[0].get("state_comparisons", {}).get(group, {}).get("minimum_margin", .035)
            positive, negative = matches[on], matches[off]
            if positive["found"] and positive["score"] - negative["score"] >= margin:
                return True
            if negative["found"] and negative["score"] - positive["score"] >= margin:
                return False
            return None

        result["local_selected"] = state("local_tab_selected", "local_tab_unselected", "local_tab")
        result["batch_enabled"] = state("batch_on", "batch_off", "batch")
        result["page_state"] = "local_shop" if result["local_selected"] is True else "shop_page"
        result["ready"] = result["local_selected"] is True and result["batch_enabled"] is True
        return result
