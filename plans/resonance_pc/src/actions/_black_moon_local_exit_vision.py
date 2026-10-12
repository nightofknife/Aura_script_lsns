"""Small-template evidence for settled shopping and either exit destination."""
import cv2

from ._black_moon_local_select_vision import BlackMoonLocalSelectVision, _pixels
from ._black_moon_local_shop_vision import _crop, _image, _response
from ._depart_button_vision import REGION, THRESHOLD, _load_template


class BlackMoonLocalExitVision:
    def __init__(self):
        self.shop = BlackMoonLocalSelectVision()
        self.VIEWPORT = self.shop.VIEWPORT

    def observe(self, frame):
        frame = _image(frame)
        controls = self.shop.prepare
        shop = controls.match(frame, "shop_page")["found"]
        rest = controls.match(frame, "shop_entry")["found"]
        response = _response(_crop(frame, REGION), _load_template(), method="1-TM_SQDIFF_NORMED")
        main = bool(response.size and float(response.max()) >= THRESHOLD)
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
            filename, roi = "nav_back_button.png", (0, 0, 170, 80)
        elif target == "main":
            filename, roi = "nav_city_main_button.png", (140, 0, 130, 80)
        else:
            raise ValueError("Unknown Black Moon exit destination")
        # Existing shared navigation artwork; paths remain inside the template root.
        template = _pixels(self.shop.root.parent, filename)
        response = _response(_crop(_image(frame), roi), template, method="1-TM_SQDIFF_NORMED")
        if not response.size:
            return {"found": False, "score": 0., "center": None}
        _, score, _, point = cv2.minMaxLoc(response)
        return {"found": score >= .9, "score": float(score),
                "center": (roi[0] + point[0] + template.shape[1] // 2,
                           roi[1] + point[1] + template.shape[0] // 2)}
