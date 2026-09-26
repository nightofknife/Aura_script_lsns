"""Build review-only event option templates from local game config/font/UI assets.
Run from repository root. Does not control the game or register runtime actions.
Extracted fonts are local inputs, never copied into the output.
"""
from pathlib import Path
import struct, json, hashlib, argparse, re, html
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

def decode_factory(root, name):
    b = (root / (name + '.bin')).read_bytes()
    u32 = lambda buf, pos: struct.unpack_from('<I', buf, pos)[0]
    p = 70
    pool_end = p + u32(b, 66)
    pool = {}
    while p < pool_end:
        offset = p - 70
        n, shift = 0, 0
        while True:
            q = b[p]
            p += 1
            n |= (q & 127) << shift
            if q < 128:
                break
            shift += 7
        pool[offset] = b[p:p+n].decode('utf-8')
        p += n
    assert p == pool_end

    def row(buf, pos):
        size = u32(buf, pos)
        pos += 4
        end = pos + size
        count = u32(buf, pos)
        pos += 4
        result = {}
        for _ in range(count):
            length = u32(buf, pos)
            pos += 4
            field = buf[pos:pos+length]
            pos += length
            key, typ, payload = pool[u32(field, 0)], field[4], field[5:]
            if typ in (0, 6):
                assert len(payload) == 4
                value = struct.unpack('<i', payload)[0]
            elif typ == 1:
                assert len(payload) == 8
                value = struct.unpack('<d', payload)[0]
            elif typ == 3:
                assert len(payload) == 1
                value = bool(payload[0])
            elif typ in (4, 5, 9, 12, 13):
                assert len(payload) == 4
                value = pool[u32(payload, 0)]
            elif typ == 7:
                length = u32(payload, 0)
                assert len(payload) == length + 4
                assert payload[4] == 99
                child_count = u32(payload, 5)
                child_pos, value = 9, []
                for _ in range(child_count):
                    item, child_pos = row(payload, child_pos)
                    value.append(item)
                assert child_pos == len(payload)
            else:
                raise ValueError((key, typ))
            assert key not in result
            result[key] = value
        assert pos == end
        return result, pos

    p += 8 + u32(b, p+4)  # factory metadata block
    p += 4 + u32(b, p)  # id/name/index map
    length, count = struct.unpack_from('<II', b, p)
    end = p + 4 + length
    p += 8
    rows = []
    for _ in range(count):
        item, p = row(b, p)
        rows.append(item)
    assert p == end == len(b)
    return rows, hashlib.sha256(b).hexdigest()


def wrap(text, font, width=283):
    lines = []
    for paragraph in text.split('\n'):
        current = ''
        for char in paragraph:
            if current and font.getlength(current + char) > width:
                lines.append(current)
                current = ''
            current += char
        lines.append(current)
    return lines


def render(option, state, assets):
    names = {'normal': 'Btn_Normal', 'disabled': 'Btn_Unavailable',
             'selected': 'Btn_Selected_Green'}
    sprite = Image.open(assets / (names[state] + '.png')).convert('RGBA')
    sprite = sprite.resize((round(sprite.width * 2/3), round(sprite.height * 2/3)), Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', sprite.size, (12, 10, 10, 255))
    canvas.alpha_composite(sprite)
    canvas = canvas.convert('RGB')
    title_font = ImageFont.truetype(str(assets / 'SourceHanSansCN-Medium.ttf'), 20)
    body_font = ImageFont.truetype(str(assets / 'SourceHanSansCN-Medium.ttf'), 16)
    title = option['title']
    # Unity BestFit may shrink long titles; preserve all words in the review output.
    size = 20
    while title_font.getlength(title) > 283 and size > 10:
        size -= 1
        title_font = ImageFont.truetype(str(assets / 'SourceHanSansCN-Medium.ttf'), size)
    lines = wrap(option['description'], body_font)
    x, y = (95, 40) if state == 'selected' else (92, 37)
    alpha = .502 if state == 'disabled' else 1.
    overlay = Image.new('RGBA', canvas.size)
    draw = ImageDraw.Draw(overlay)
    draw.text((x, y), title, font=title_font, fill=(255,255,255,round(255*alpha)), anchor='lt')
    for i, line in enumerate(lines):
        draw.text((x, y+38+i*19), line, font=body_font, fill=(166,166,166,round(255*alpha)), anchor='lt')
    canvas = Image.alpha_composite(canvas.convert('RGBA'), overlay).convert('RGB')
    width = min(286, int(np.ceil(max([title_font.getlength(title)] + [body_font.getlength(s) for s in lines]))))
    box = (x-2, y-2, x+width+2, min(canvas.height, y+38+19*(len(lines)-1)+18))
    return canvas.crop(box), {'crop_on_sprite':list(box),'description_lines':lines,
                              'title_size':size,'body_size':16,'title_band':[2,2,width+2,24],
                              'body_band':[2,40,width+2,box[3]-box[1]]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', type=Path, required=True)
    ap.add_argument('--assets', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    data, hashes = {}, {}
    for name in ('RogueEventFactory', 'ActivityListFactory', 'RogueOrderFactory'):
        rows, hashes[name] = decode_factory(args.config, name)
        data[name] = {r['id']:r for r in rows}
    events, options = [], {}
    for e in data['RogueEventFactory'].values():
        if e.get('func') != 'Options' or e.get('isInformalData'):
            continue
        ids = [r['id'] for r in e.get('optionList', [])]
        events.append({'id':e['id'], 'name':e.get('name'), 'variant':e['idCN'],
                       'node_type':e.get('typeId'), 'options':ids})
        for oid in ids:
            o = data['ActivityListFactory'][oid]
            effects = [data['RogueOrderFactory'][r['id']] for r in o.get('optionFuncList', [])]
            blocked = any(r['orderType'] in ('Action','StepNum') or
                          (r['orderType']=='SpinNum' and r.get('param',0)>0) for r in effects)
            options[oid] = {'id':oid, 'title':o.get('optionName',''),
                            'description':re.sub('<[^>]+>', '', o.get('optionDes','')),
                            'effects':effects, 'blocked':blocked}
    templates, unique = [], {}
    for o in options.values():
        key = (o['title'],o['description'])
        if key not in unique:
            unique[key] = f'text_{len(unique):03d}'
            for state in ('normal','disabled','selected'):
                image, details = render(o,state,args.assets)
                filename = f'{unique[key]}_{state}.png'
                image.save(args.output/filename)
                templates.append({'text_key':unique[key], 'state':state, 'file':filename, **details})
        o['text_key'] = unique[key]
    catalog = {'status':'review_prototype_not_runtime_approved', 'source_hashes':hashes,
               'font_sha256':hashlib.sha256((args.assets/'SourceHanSansCN-Medium.ttf').read_bytes()).hexdigest(),
               'notes':['All three states rendered for visual review; disabled may be unreachable for some options.',
                        'No confirm button or unavailable-reason recognition.',
                        'Pillow rasterization and static backgrounds approximate Unity; screenshot validation required.'],
               'events':events, 'options':list(options.values()), 'templates':templates}
    (args.output/'catalog.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf8')
    cards=[]
    for (title,desc),key in unique.items():
        cards.append('<article><h3>'+html.escape(title)+'</h3><p>'+html.escape(desc)+'</p><div>'+''.join(
            f'<figure><figcaption>{label}</figcaption><img src="{key}_{state}.png"></figure>'
            for state,label in [('normal','普通可选'),('disabled','灰色不可选'),('selected','蓝色已选中')])+'</div></article>')
    page='<!doctype html><meta charset="utf-8"><title>事件三态模板审阅</title><style>body{background:#181b20;color:#eee;font-family:Arial;margin:24px}article{border-bottom:1px solid #555;padding:12px}article>div{display:flex;gap:24px}figure{margin:0;width:310px}img{image-rendering:auto;max-width:100%}p,figcaption{color:#bbb}h3{margin:4px 0}</style><h1>游戏资源合成的三态文本模板 · 审阅版</h1><p>紧贴标题与效果说明，排除确认按钮、限制提示和外围光效。未接入运行任务。三态均展示，不代表每个选项实际都会置灰。</p>'+''.join(cards)
    (args.output/'index.html').write_text(page,encoding='utf8')
    print(json.dumps({'events':len(events),'option_records':len(options),'unique_texts':len(unique),'templates':len(templates),'output':str(args.output)},ensure_ascii=False))


if __name__ == '__main__':
    main()
