"""Batch-pricing request forwarding, metadata, and cache tests; no game calls."""
from copy import deepcopy
import inspect
import json
from pathlib import Path

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment
from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions._freight_contract import normalize_planning_inputs
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import trade_planner_pc_actions as action
from plans.resonance_pc.src.services.resonance_pc_trade_planner_service import (
    ResonancePcTradePlannerError, ResonancePcTradePlannerService,
)
from tests.unit.test_resonance_pc_freight_planner_service import service, plan

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('taxes', [{'19': 700, '14': 750}, {'1': 0}, {'1': 10000}])
@pytest.mark.parametrize('task_name,callback', [('preview_trade_plan_pc', trade.resonance_pc_preview_trade_plan_flow),
    ('auto_cycle_trade_pc', trade.resonance_pc_auto_cycle_trade_flow)])
def test_schema_and_action_keep_exact_tax_rates(task_name, callback, taxes):
    spec = yaml.safe_load((ROOT / 'plans/resonance_pc/tasks' / (task_name+'.yaml')).read_text('utf-8'))[task_name]
    payload = {'city_tax_rates_bps': taxes}
    if task_name.startswith('preview'):
        payload['start_city_id'] = '19'
    ok, values = InputValidator(None).validate_inputs_against_meta(spec['meta']['inputs'], payload)
    assert ok, values
    rendered = {key: NativeEnvironment().from_string(value).render(inputs=values)
                for key, value in spec['steps']['run']['params'].items()}
    inspect.signature(callback).bind(**rendered)
    assert rendered['city_tax_rates_bps'] == taxes
    assert normalize_planning_inputs(rendered)['city_tax_rates_bps'] == taxes


@pytest.mark.parametrize('value', [True, False, -1, 10001, 7.5, '750', float('nan')])
def test_invalid_tax_rates_rejected_before_runtime(value):
    with pytest.raises(ValueError, match='city_tax_rates_bps'):
        normalize_planning_inputs({'city_tax_rates_bps': {'19': value}})


def test_action_forwards_independent_city_rates_without_mutating_input():
    calls = []
    class Planner:
        def plan_optimal_route(self, **kw):
            calls.append(deepcopy(kw))
            return {}
    rates = {'19': 700, '14': 750}
    action.resonance_pc_trade_plan_optimal_route(city_tax_rates_bps=rates, resonance_pc_trade_planner=Planner())
    assert calls[0]['city_tax_rates_bps'] == rates
    calls[0]['city_tax_rates_bps']['19'] = 0
    assert rates['19'] == 700


def test_tax_rates_participate_in_route_cache(service):
    api, calls = service
    first = plan(api, city_tax_rates_bps={'A': 700, 'B': 750})
    assert first == plan(api, city_tax_rates_bps={'B': 750, 'A': 700})
    assert len(calls) == 1
    plan(api, city_tax_rates_bps={'A': 701, 'B': 750})
    assert len(calls) == 2


def test_unknown_tax_city_is_rejected_by_service(service):
    api, _ = service
    with pytest.raises(ResonancePcTradePlannerError, match='unknown city'):
        plan(api, city_tax_rates_bps={'missing': 500})


def test_default_excludes_foreign_currency_but_explicit_selection_fails(service, monkeypatch):
    api, calls = service
    rules = deepcopy(api._load_trade_rules_payload())
    rules['tax'] = {'city_rules': {'A': {'prestige_city_id': 'A', 'delta_bps': 0},
        'B': {'prestige_city_id': 'B', 'delta_bps': 0},
        'C': {'supported_currency': False, 'currency_id': 11401048}}}
    monkeypatch.setattr(api, '_load_trade_rules_payload', lambda: rules)
    result = plan(api)
    assert result['pricing_excluded_city_ids'] == ['C']
    assert 'C' not in result['city_path_ids']
    for kwargs in ({'available_city_ids': ['A', 'C']}, {'current_city_id': 'C'},
                   {'trade_mode': 'fixed', 'fixed_route_city_ids': ['A', 'C']}):
        with pytest.raises(ResonancePcTradePlannerError) as error:
            plan(api, **kwargs)
        assert error.value.code == 'unsupported_trade_currency'


def test_metadata_records_native_special_cases_and_user_receipt():
    rules = json.loads((ROOT / 'plans/resonance_pc/data/meta/trade_rules.json').read_text('utf-8'))
    cities = rules['tax']['city_rules']
    assert cities['17']['fixed_tax_bps'] == 1000
    assert cities['21']['fixed_tax_bps'] == 1500 and not cities['21']['supported_currency']
    assert cities['14']['prestige_city_id'] == '19'
    assert cities['6']['prestige_city_id'] == '3'
    assert rules['source']['sample']['buy_total'] == 1472
    assert rules['source']['sample']['sell_total'] == 1568
    assert rules['source']['sample']['net_profit'] == 96


@pytest.fixture
def receipt_service(monkeypatch):
    class Market:
        def get_latest(self):
            return {'snapshot_id': 'user-crystal-receipt', 'products': {'crystal': {
                'name': '水晶工艺品', 'market': {'buy': {'19': {'price': 688}},
                                              'sell': {'14': {'price': 792}}}}}}
        def get_all_travel_fatigue(self):
            return {'cities': {'19': '贡露城', '14': '远星大桥'},
                    'costs': {'19': {'14': 1}, '14': {'19': 1}}}
    api = ResonancePcTradePlannerService(Market())
    monkeypatch.setattr(api, '_load_trade_constraints_payload', lambda: {
        'default_available_city_ids': ['19', '14'], 'key_to_city_id': {}})
    monkeypatch.setattr(api, '_load_buy_lot_payload', lambda: {'city_product_buy_lot': {'19': {'crystal': 1}}})
    monkeypatch.setattr(api, '_load_product_unlocks_payload', lambda: {'product_ids': []})
    return api


@pytest.mark.parametrize('mode', ['free', 'fixed'])
def test_user_receipt_through_public_action_and_real_service(receipt_service, mode):
    result = action.resonance_pc_trade_plan_optimal_route(
        fatigue_budget=1, cargo_capacity=2, current_city_id='19',
        trade_mode=mode, fixed_route_city_ids=['19', '14'], negotiation_policy='disabled',
        available_city_ids=['19', '14'], city_tax_rates_bps={'19': 700, '14': 750},
        resonance_pc_trade_planner=receipt_service)
    assert result['status'] == 'ok'
    assert result['expected_profit_exact'] == '96'
    amounts = result['route'][0]['settlement']
    assert amounts['buy_goods_amount'] == 1376
    assert amounts['buy_tax'] == 96 and amounts['buy_total'] == 1472
    assert amounts['sell_goods_amount'] == 1584 and amounts['taxable_profit'] == 208
    assert amounts['sell_tax'] == 16 and amounts['sell_total'] == 1568
    assert amounts['net_profit'] == 96


def test_profit_target_uses_net_not_gross_profit(receipt_service):
    result = action.resonance_pc_trade_plan_optimal_route(
        fatigue_budget=1, cargo_capacity=2, current_city_id='19', target_profit=97,
        negotiation_policy='disabled', available_city_ids=['19', '14'],
        city_tax_rates_bps={'19': 700, '14': 750}, resonance_pc_trade_planner=receipt_service)
    assert result['status'] == 'target_unreachable'
    assert result['route'] == []
