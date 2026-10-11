"""Focused offline fraction parsing; no room navigation or game inputs."""
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _raw_workshop_digits as digits

FIXTURES = Path(__file__).resolve().parents[1]/'fixtures/raw_workshop_digits'
RECORDS = json.loads((FIXTURES/'manifest.json').read_text(encoding='utf-8'))['records']


@pytest.mark.parametrize('record', RECORDS, ids=lambda item:item['name'])
def test_dynamic_fraction_native_font_and_saved_client_crops(record):
    binary=(FIXTURES/record['image']).read_bytes()
    assert hashlib.sha256(binary).hexdigest()==record['sha256']
    image=cv2.imdecode(np.frombuffer(binary,np.uint8),cv2.IMREAD_COLOR)
    result=digits.read_fraction(image,style=record['style'])
    if record['expected'] is None:
        assert result is None
    else:
        assert result is not None and result['text']==record['expected']
        current,capacity=map(int,record['expected'].split('/'))
        assert result['current']==current and result['capacity']==capacity
        assert result['full']==(current==capacity)


@pytest.mark.parametrize('color',[0,128,255])
def test_blank_never_becomes_zero_or_full(color):
    assert digits.read_fraction(np.full((30,60,3),color,np.uint8)) is None


def test_localization_region_and_input_validation():
    patch=cv2.imread(str(FIXTURES/'work_full.png'))
    frame=np.zeros((720,1280,3),np.uint8)
    h,w=patch.shape[:2]
    frame[280:280+h,730:730+w]=patch
    assert digits.read_fraction(frame,(730,280,w,h))['full']
    assert digits.read_fraction(frame,(-1,280,w,h)) is None
    assert digits.read_fraction(frame,(1250,280,100,h)) is None
    assert digits.read_fraction(frame,(0,0,60,30)) is None
    assert digits.read_fraction(None) is None
    assert digits.read_fraction(np.zeros((20,40,2),np.uint8)) is None


def test_clipped_glyph_does_not_return_a_partial_number():
    image=cv2.imread(str(FIXTURES/'work_full.png'))
    ink=digits.foreground(image)
    x,y,w,h=digits.segment(ink)[0]
    assert digits.read_fraction(image[:,x:]) is None
    assert digits.read_fraction(image[y:]) is None


def test_font_families_and_single_character_alphabet():
    for style in ('list','selection'):
        assert {letter for letter,_ in digits.glyphs(style)}==set('0123456789/')
