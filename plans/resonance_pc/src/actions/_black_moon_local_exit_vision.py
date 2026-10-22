"""Small-template evidence for settled shopping and either exit destination."""
from pathlib import Path

import cv2

from ._black_moon_local_select_vision import BlackMoonLocalSelectVision, _pixels
from ._black_moon_local_shop_vision import _crop, _image, _response
from . import _depart_button_vision as departure


NAVIGATION_ROOT = Path(__file__).resolve().parents[2] / "templates/navigation/global"


class BlackMoonLocalExitVision:
    def __init__(self):
        self.shop = BlackMoonLocalSelectVision()
        self.VIEWPORT = self.shop.VIEWPORT

    def observe(self, frame):
        frame = _image(frame)
        controls = self.shop.prepare
        shop = controls.match(frame, "shop_page")["found"]
        rest = controls.match(frame, "shop_entry")["found"]
        depart_profile = departure.load_depart_profile()
        response = _response(_crop(frame, depart_profile.region), departure._load_template(),
                             method="1-TM_SQDIFF_NORMED")
        main = bool(response.size and float(response.max()) >= depart_profile.threshold)
        result = {"page_state": "unknown", "shop_ready": False, "total_zero": False}
        if sum((shop, rest, main)) != 1:
            return result
        if rest:
            result["page_state"] = "rest_menu"
        elif main:
            result["page_state"] = "main"
        else:
            result.update(page_state="local_shop", shop_ready=self.shop.ready(frame),
                          total_zero=self.shop.total_zero(frame))
        return result

    def button(self, frame, target):
        if target == "rest_menu":
            filename, roi = "back.png", (0, 0, 170, 80)
        elif target == "main":
            filename, roi = "city_main.png", (140, 0, 130, 80)
        else:
            raise ValueError("Unknown Black Moon exit destination")
        # Existing shared navigation artwork; paths remain inside the template root.
        template = _pixels(NAVIGATION_ROOT.resolve(), filename)
        response = _response(_crop(_image(frame), roi), template, method="1-TM_SQDIFF_NORMED")
        if not response.size:
            return {"found": False, "score": 0., "center": None}
        _, score, _, point = cv2.minMaxLoc(response)
        return {"found": score >= .9, "score": float(score),
                "center": (roi[0] + point[0] + template.shape[1] // 2,
                           roi[1] + point[1] + template.shape[0] // 2)}
