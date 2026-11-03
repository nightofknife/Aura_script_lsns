"""Render opaque passenger destination templates from native card backgrounds.

Offline preparation only. Runtime uses the generated RGB rectangles, without
masks or access to the game resources / extraction dependencies.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'plans/resonance_pc'
DEFAULT_GAME_DATA = Path('D:/game/Resonance/雷索纳斯_Data')
DEFAULT_DEVTOOLS = ROOT.parent / 'Aura_script_lsns_devtools/src'
REGION = [540, 75, 710, 500]
CARD_SIZE = (333, 147)
CROP = (110, 0, 300, 112)
SIZE = [190, 112]
THRESHOLD = .80
VISIBLE_CITIES = {
    'brcl_outpost', 'wilderness_station', 'mander_mine', 'onederland',
    'clarity_data_center_administration_bureau', 'anita_weapon_research_institute',
}


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def build(plan: Path = PLAN, *, game_data: Path = DEFAULT_GAME_DATA,
          devtools_src: Path = DEFAULT_DEVTOOLS) -> dict:
    # Extraction dependencies are deliberately absent from the runtime module.
    sys.path.insert(0, str(devtools_src.resolve()))
    from aura_resonance_devtools.full_config_export import decode_binary_config
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image
    from _trade_investment_native import load_bundle

    metadata = game_data / 'il2cpp_data/Metadata/global-metadata.dat'
    paths = {
        'backgrounds': game_data / 'Patch/Asset/ui/flier/citymap.asset',
        'logos': game_data / 'Patch/Asset/ui/common.asset',
        'overlay': game_data / 'Patch/Asset/ui/flier/soucer_r.asset',
        'prefab': game_data / 'Patch/Asset/ui/flier/group_sendstation.asset',
        'stations': game_data / 'Patch/BinaryConfig/HomeStationFactory.bin',
    }
    reviewed_path = plan / 'data/meta/city_identity_templates.json'
    reviewed = json.loads(reviewed_path.read_text(encoding='utf-8'))['cities']
    alias_path = plan / 'data/meta/trade_goods_investment_cities.json'
    alias_groups = list(json.loads(alias_path.read_text(encoding='utf-8'))['cities'].values())
    records = decode_binary_config(paths['stations'])['records']
    stations = [{f['name']: f.get('value') for f in row['fields']} for row in records]

    libraries = {}
    for kind in ('backgrounds', 'logos', 'overlay'):
        env = load_bundle(paths[kind], metadata)
        libraries[kind] = {obj.read().m_Name: obj for obj in env.objects if obj.type.name == 'Sprite'}

    def sprite(kind, name):
        obj = libraries[kind][name]
        image = _logical_sprite_image(obj.read(), obj.version).convert('RGBA')
        return image, {'bundle': paths[kind].relative_to(game_data).as_posix(),
                       'name': name, 'path_id': obj.path_id, 'size': list(image.size)}

    overlay, overlay_source = sprite('overlay', 'unchoose')
    if overlay.size != (500, 220):
        raise ValueError('Native unselected overlay dimensions changed')
    folder = plan / 'templates/passenger/destination/cities'
    folder.mkdir(parents=True, exist_ok=True)
    cities, assets = [], {}
    for row in reviewed:
        station = next(r for r in stations if r['name'] == row['city_name'])
        background, background_source = sprite('backgrounds', station['leafletMap'].split('/')[-1])
        logo, logo_source = sprite('logos', station['cityMapIconPath'].split('/')[-1])
        if background.size != (500, 220):
            raise ValueError(f"Native background dimensions changed: {row['city_key']}")
        canvas = background.copy()
        canvas.alpha_composite(overlay)
        canvas.alpha_composite(logo.resize((166, 166), Image.Resampling.BILINEAR), (167, 2))
        rectangle = canvas.resize(CARD_SIZE, Image.Resampling.BILINEAR).convert('RGB').crop(CROP)
        target = folder / f"{row['city_key']}.png"
        rectangle.save(target)
        relative = target.relative_to(plan).as_posix()
        assets[relative] = {'sha256': digest(target), 'size': SIZE, 'mode': 'RGB'}
        names = next(([group['name'], *group.get('aliases', [])] for group in alias_groups
                      if row['city_key'] in group.get('aliases', [])), [])
        if row['city_key'] == 'lanxin_city':
            names.append('岚心域')  # Preserve the independent passenger action's accepted legacy alias.
        cities.append({'city_key': row['city_key'], 'city_name': row['city_name'],
                       'aliases': list(dict.fromkeys([row['city_key'], row['city_name'], *names])),
                       'template': relative, 'native_station_id': station['id'],
                       'background': background_source, 'logo': logo_source})
    # Retire only mask files produced by the previous version of this tool.
    for row in reviewed:
        retired = folder / f"{row['city_key']}_mask.png"
        if retired.is_file():
            retired.unlink()
    catalog = {
        'schema_version': 2,
        'source': {'kind': 'native_passenger_card_background_overlay_and_city_logo',
                   'files': {path.relative_to(game_data).as_posix(): digest(path) for path in paths.values()},
                   'city_catalog': reviewed_path.relative_to(plan).as_posix(),
                   'city_catalog_sha256': digest(reviewed_path),
                   'alias_catalog': alias_path.relative_to(plan).as_posix(),
                   'alias_catalog_sha256': digest(alias_path)},
        'render': {'native_card_size': [500, 220], 'client_card_size': list(CARD_SIZE),
                   'native_logo_size': [166, 166], 'native_logo_position': [167, 2],
                   'client_crop': list(CROP), 'overlay': overlay_source,
                   'resample': 'BILINEAR',
                   'selection': 'Use ordinary-state template; selected-state brightness difference is tolerated by normalized correlation.',
                   'exclusions': 'Rectangle excludes left news tag, outer border and lower city title.'},
        'match': {'roi': REGION, 'size': SIZE, 'threshold': THRESHOLD,
                  'method': 'TM_CCOEFF_NORMED', 'use_grayscale': True, 'preprocess': 'none'},
        'cities': cities, 'assets': assets,
        'calibration_note': 'Offline replay covers ten distinct complete city cards across three actual frames; other cities require live coverage.',
    }
    write_json(plan / 'data/meta/passenger_destination_templates.json', catalog)
    return catalog


def preview(catalog: dict, output: Path, plan: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    width, height = 330, 140
    sheet = Image.new('RGB', (width * 3, height * 7), '#303740')
    draw = ImageDraw.Draw(sheet)
    for i, row in enumerate(catalog['cities']):
        x, y = i % 3 * width, i // 3 * height
        with Image.open(plan / row['template']) as rectangle:
            sheet.paste(rectangle, (x + 2, y + 2))
        draw.text((x + 2, y + 116), row['city_key'][:45], fill='white')
    path = output / 'city-background-logos-preview.png'
    sheet.save(path)
    return path


def calibrate(catalog: dict, source: Path, fixtures: Path, output: Path, plan: Path) -> dict:
    with Image.open(source) as image:
        if image.size != (1332, 802):
            raise ValueError(f'Expected actual 1332x802 user screenshot, got {image.size}')
        client = image.convert('RGB').crop((25, 56, 1305, 776))
    x, y, w, h = REGION
    roi = client.crop((x, y, x + w, y + h))
    fixtures.mkdir(parents=True, exist_ok=True)
    fixture = fixtures / 'user_destination_list.png'
    roi.save(fixture)
    write_json(fixtures / 'sources.json', {
        'kind': 'actual_user_screenshot_roi_not_synthetic', 'attachment': source.name,
        'source_sha256': digest(source), 'client_crop': [25, 56, 1305, 776],
        'region': REGION, 'fixture': fixture.name, 'fixture_sha256': digest(fixture),
        'fully_visible_city_keys': sorted(VISIBLE_CITIES),
    })
    source_gray = cv2.cvtColor(np.asarray(roi), cv2.COLOR_RGB2GRAY)
    annotated = roi.copy()
    draw = ImageDraw.Draw(annotated)
    results = []
    for row in catalog['cities']:
        with Image.open(plan / row['template']) as image:
            template = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
        response = cv2.matchTemplate(source_gray, template, cv2.TM_CCOEFF_NORMED)
        response[~np.isfinite(response)] = -1
        _, score, _, position = cv2.minMaxLoc(response)
        found = score >= THRESHOLD
        results.append({'city_key': row['city_key'], 'fully_visible': row['city_key'] in VISIBLE_CITIES,
                        'confidence': round(score, 6), 'found': found, 'rect_roi': [*position, *SIZE]})
        if found:
            px, py = position
            draw.rectangle((px, py, px + SIZE[0], py + SIZE[1]), outline='#00ff60', width=2)
            draw.text((px, py + SIZE[1] - 12), f"{row['city_key'][:25]} {score:.3f}", fill='#00ff60')
    annotated.save(output / 'destination-background-matches.png')
    positives = [r['confidence'] for r in results if r['fully_visible']]
    negatives = [r['confidence'] for r in results if not r['fully_visible']]
    report = {'kind': 'offline_actual_user_screenshot_replay', 'threshold': THRESHOLD,
              'visible_minimum': min(positives), 'absent_maximum': max(negatives),
              'correct': all(r['found'] == r['fully_visible'] for r in results), 'results': results,
              'limitations': 'One actual initial page with six complete city cards. This is not live operation acceptance.'}
    write_json(output / 'background-calibration.json', report)
    if not report['correct']:
        raise ValueError('Screenshot calibration did not separate present and absent cities')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, default=PLAN)
    parser.add_argument('--game-data', type=Path, default=DEFAULT_GAME_DATA)
    parser.add_argument('--devtools-src', type=Path, default=DEFAULT_DEVTOOLS)
    parser.add_argument('--screenshot', type=Path)
    parser.add_argument('--fixtures', type=Path, default=ROOT / 'tests/fixtures/passenger_destination')
    parser.add_argument('--output', type=Path, default=ROOT / '.pytest_tmp/passenger-destination-backgrounds')
    args = parser.parse_args()
    catalog = build(args.plan, game_data=args.game_data, devtools_src=args.devtools_src)
    path = preview(catalog, args.output, args.plan)
    report = calibrate(catalog, args.screenshot, args.fixtures, args.output, args.plan) if args.screenshot else None
    print(json.dumps({'cities': len(catalog['cities']), 'preview': str(path),
                      'visible_minimum': report['visible_minimum'] if report else None,
                      'absent_maximum': report['absent_maximum'] if report else None,
                      'calibration_correct': report['correct'] if report else None}))


if __name__ == '__main__':
    main()
