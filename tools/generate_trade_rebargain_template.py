"""Compose the exhausted-negotiation marker from native Resonance UI materials.

Requires Pillow, UnityPy and the separate aura-resonance-devtools source tree.
Run from the repository root, supplying the installed game's *_Data directory:
  python tools/generate_trade_rebargain_template.py --data-root path/to/game_Data \
    --devtools-root path/to/Aura_script_lsns_devtools

No captured pixels or game fonts are written to the repository. The output keeps
the localized label, native request-book icon and white background; the variable
cost to their right is excluded.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


_ROOT = Path(__file__).resolve().parents[1]
_OUTPUT = _ROOT / "plans/resonance_pc/templates/trade_buy_rebargain_button.png"
_PREFAB = "ui/hometrade/hometrade_splited/group_trade.asset"
_BUTTON_GO = 2913748993047873171
_LABEL_GO = -5319291662600138162
_ICON_GO = 1976339534609225916
_LABEL = "再交涉"
_CROP_NATIVE = (12, 5, 166, 55)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--devtools-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=_OUTPUT)
    args = parser.parse_args()
    sys.path.insert(0, str(args.devtools_root / "src"))
    from aura_resonance_devtools.binary_config import PooledBinaryConfig
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image, load_bundle

    metadata = args.data_root / "il2cpp_data/Metadata/global-metadata.dat"
    asset_root = args.data_root / "Patch/Asset"
    prefab = load_bundle(asset_root / _PREFAB, metadata)
    objects = {obj.path_id: obj for obj in prefab.objects}

    def components(game_object: int) -> list[dict]:
        return [
            objects[item["component"]["m_PathID"]].read_typetree()
            for item in objects[game_object].read_typetree()["m_Component"]
        ]

    button_components = components(_BUTTON_GO)
    label_components = components(_LABEL_GO)
    icon_components = components(_ICON_GO)
    button_rect = next(tree for tree in button_components if "m_SizeDelta" in tree)
    button_image = next(tree for tree in button_components if "m_Sprite" in tree)
    label_rect = next(tree for tree in label_components if "m_SizeDelta" in tree)
    label_text = next(tree for tree in label_components if "m_FontData" in tree)
    icon_rect = next(tree for tree in icon_components if "m_SizeDelta" in tree)
    icon_image = next(tree for tree in icon_components if "m_Sprite" in tree)
    text_source = PooledBinaryConfig.load(args.data_root / "Patch/BinaryConfig/TextFactory.bin")
    # Use the exact localized string from the game's own pool.
    native_label = text_source.strings[text_source.string_offset(_LABEL)]

    sprites = load_bundle(asset_root / "ui/hometrade.asset", metadata)
    sprite_object = next(obj for obj in sprites.objects if obj.path_id == button_image["m_Sprite"]["m_PathID"])
    background = _logical_sprite_image(sprite_object.read(), sprite_object.version)
    icon_object = next(obj for obj in sprites.objects if obj.path_id == icon_image["m_Sprite"]["m_PathID"])
    icon = _logical_sprite_image(icon_object.read(), icon_object.version)
    font_bundle = load_bundle(asset_root / "ui/font/originpack.asset", metadata)
    font_asset = next(obj.read() for obj in font_bundle.objects if obj.path_id == label_text["m_FontData"]["m_Font"]["m_PathID"])
    font = ImageFont.truetype(io.BytesIO(bytes(font_asset.m_FontData)), label_text["m_FontData"]["m_FontSize"])
    width = round(button_rect["m_SizeDelta"]["x"])
    height = round(button_rect["m_SizeDelta"]["y"])
    image = background.resize((width, height), Image.Resampling.BILINEAR)
    icon_width = round(icon_rect["m_SizeDelta"]["x"])
    icon_height = round(icon_rect["m_SizeDelta"]["y"])
    icon = icon.resize((icon_width, icon_height), Image.Resampling.BILINEAR)
    icon_x = round(width / 2 + icon_rect["m_AnchoredPosition"]["x"] - icon_width / 2)
    icon_y = round(height / 2 - icon_rect["m_AnchoredPosition"]["y"] - icon_height / 2)
    image.alpha_composite(icon, (icon_x, icon_y))
    x = width / 2 + label_rect["m_AnchoredPosition"]["x"] - label_rect["m_SizeDelta"]["x"] / 2
    y = height / 2 - label_rect["m_AnchoredPosition"]["y"]
    color = tuple(round(label_text["m_Color"][channel] * 255) for channel in ("r", "g", "b", "a"))
    ImageDraw.Draw(image).text((x, y), native_label, font=font, anchor="lm", fill=color)
    # Native canvas is 1920x1080; Aura's reference capture is 1280x720.
    image = image.resize((round(width * 2 / 3), round(height * 2 / 3)), Image.Resampling.BILINEAR)
    # Cost text begins at native x=167; stop immediately before its rectangle.
    crop = tuple(round(value * 2 / 3) for value in _CROP_NATIVE)
    output = image.crop(crop).convert("RGB")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.save(args.output)
    print(f"Wrote {args.output} ({output.width}x{output.height})")


if __name__ == "__main__":
    main()
