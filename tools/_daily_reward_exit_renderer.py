"""Rebuild a complete native exit-hint candidate without screenshot pixels."""
from __future__ import annotations

import io
from PIL import Image, ImageDraw, ImageFont, ImageFilter


def render_exit(builder, recipe):
    """Return RGBA native warning sprite plus the full native hint string.

    `builder` is the daily native builder module. Assets must be initialized by
    the caller with ui/common.asset, ui/font.asset, ui/font/originpack.asset.
    """
    show = builder.Prefab('ui/common/showitem.asset')
    tree = show.text('/ShowItem/Group_Tips/Txt_Tips')
    _, _ = builder.font_for(tree)
    data = builder.FONTS[tree['m_FontData']['m_Font']['m_PathID']]['bytes']
    factor = recipe.get('client_oversample', 3)
    size = round(recipe['font_size_native'] * factor * (2 / 3))
    font = ImageFont.truetype(io.BytesIO(data), size)
    stroke = recipe['highres_stroke']
    high = Image.new('L', (round(170 * factor), round(30 * factor)))
    draw = ImageDraw.Draw(high)
    bbox = draw.textbbox((0, 0), tree['m_Text'], font=font, stroke_width=stroke)
    phase_x = recipe.get('phase_x_highres', 0)
    phase_y = recipe.get('phase_y_highres', 0)
    if recipe.get('render_family') == 'fixed_origin':
        draw.text((factor + phase_x - bbox[0], factor + phase_y - bbox[1]),
                  tree['m_Text'], font=font, fill=255,
                  stroke_width=stroke, stroke_fill=255)
        high = high.resize((170, 30), getattr(Image.Resampling, recipe['downsample']))
        alpha = high.crop(high.getbbox())
    else:
        draw.text((4 + phase_x, phase_y), tree['m_Text'], font=font, fill=255,
                  stroke_width=stroke, stroke_fill=255)
        high = high.crop(high.getbbox())
        alpha = high.resize((round(high.width / factor), round(high.height / factor)),
                            getattr(Image.Resampling, recipe['downsample']))
    gamma = recipe.get('alpha_gamma', 1.0)
    if gamma != 1:
        alpha = alpha.point([round(255 * (i / 255) ** gamma) for i in range(256)])
    icon, _ = show.image('/ShowItem/Group_Tips/Img_Tips')
    icon = icon.resize((27, 27), Image.Resampling.LANCZOS)
    gap = recipe['gap_client']
    dy = recipe['text_dy_client']
    height = max(icon.height, alpha.height + abs(dy)) + 4
    image = Image.new('RGBA', (icon.width + gap + alpha.width + 4, height))
    image.alpha_composite(icon, (2, (height - icon.height) // 2))
    letters = Image.new('RGBA', alpha.size, 'white')
    letters.putalpha(alpha)
    image.alpha_composite(letters, (2 + icon.width + gap,
                                   (height - alpha.height) // 2 + dy))
    return image


def exit_match_layers(image, halo, alpha_floor=32):
    """Return neutral-black grayscale pixels and a glyph-plus-space mask."""
    black = Image.new('RGBA', image.size, (0, 0, 0, 255))
    black.alpha_composite(image)
    mask = image.getchannel('A').point(lambda value: 255 if value >= alpha_floor else 0)
    if halo:
        mask = mask.filter(ImageFilter.MaxFilter(2 * halo + 1))
    return black.convert('L'), mask
