import pytest

from plans.resonance_pc.src.services.city_shop_data_pc_service import (
    CityShopDataError,
    ResonancePcCityShopDataService,
)


def test_black_moon_amusement_park_city_and_exchange_are_resolvable() -> None:
    service = ResonancePcCityShopDataService()

    city = service.resolve_city("黑月游乐城")
    administration = service.resolve_shop_point("黑月游乐城", "管理中心")
    clothing = service.resolve_shop_point("黑月游乐城", "瞳仁服饰总店")
    exchange = service.resolve_shop_point("黑月游乐城", "交易所")

    assert city == {
        "city_key": "black_moon_amusement_park",
        "city_name": "黑月游乐城",
    }
    assert (administration["shop_key"], administration["x"], administration["y"]) == (
        "administration",
        500,
        80,
    )
    assert (clothing["shop_key"], clothing["x"], clothing["y"]) == (
        "clothing",
        680,
        460,
    )
    assert exchange == {
        "city_key": "black_moon_amusement_park",
        "city_name": "黑月游乐城",
        "shop_key": "exchange",
        "shop_name": "交易所",
        "x": 1100,
        "y": 500,
    }


def test_qiyu_station_city_facilities_are_resolvable() -> None:
    service = ResonancePcCityShopDataService()

    city = service.resolve_city("栖羽站")
    maintenance = service.resolve_shop_point("栖羽站", "列车整备库")
    exchange = service.resolve_shop_point("栖羽站", "交易所")
    administration = service.resolve_shop_point("栖羽站", "管理中心")

    assert city == {
        "city_key": "qiyu_station",
        "city_name": "栖羽站",
    }
    assert (maintenance["shop_key"], maintenance["x"], maintenance["y"]) == (
        "train_maintenance",
        310,
        190,
    )
    assert (exchange["shop_key"], exchange["x"], exchange["y"]) == (
        "exchange",
        640,
        350,
    )
    assert (administration["shop_key"], administration["x"], administration["y"]) == (
        "administration",
        780,
        100,
    )


def test_lanxin_city_facilities_are_resolvable() -> None:
    service = ResonancePcCityShopDataService()

    city = service.resolve_city("岚心城")
    expected = {
        "铁安局": ("battle", 290, 150),
        "市政厅": ("city_hall", 590, 80),
        "休息区": ("rest", 670, 240),
        "商会": ("commerce", 460, 350),
        "交易所": ("exchange", 900, 350),
        "寿司店": ("sushi_restaurant", 1030, 130),
    }

    assert city == {
        "city_key": "lanxin_city",
        "city_name": "岚心城",
    }
    for shop_name, expected_point in expected.items():
        shop = service.resolve_shop_point("岚心城", shop_name)
        assert (shop["shop_key"], shop["x"], shop["y"]) == expected_point


def test_vitilin_forest_facilities_are_resolvable() -> None:
    service = ResonancePcCityShopDataService()

    city = service.resolve_city("维蒂林场")
    expected = {
        "萝赛": ("luosai", 210, 460),
        "交易所": ("exchange", 760, 540),
        "管理中心": ("administration", 970, 200),
    }

    assert city == {
        "city_key": "vitilin_forest",
        "city_name": "维蒂林场",
    }
    for shop_name, expected_point in expected.items():
        shop = service.resolve_shop_point("维蒂林场", shop_name)
        assert (shop["shop_key"], shop["x"], shop["y"]) == expected_point


def test_yunxiuqiao_base_facilities_are_resolvable_without_confluence_alias() -> None:
    service = ResonancePcCityShopDataService()

    city = service.resolve_city("云岫桥基地")
    expected = {
        "黑月服装订单站": ("black_moon_clothing_orders", 360, 180),
        "桥梁管理中心": ("bridge_administration", 540, 140),
        "云岫桥军械库": ("armory", 700, 270),
        "整顿中心": ("reforming_center", 940, 190),
        "交易所": ("exchange", 440, 490),
    }

    assert city == {
        "city_key": "yunxiuqiao_base",
        "city_name": "云岫桥基地",
    }
    for shop_name, expected_point in expected.items():
        shop = service.resolve_shop_point("云岫桥基地", shop_name)
        assert (shop["shop_key"], shop["x"], shop["y"]) == expected_point


def test_cape_city_facilities_exclude_birch_biology() -> None:
    service = ResonancePcCityShopDataService()

    city = service.resolve_city("海角城")
    expected = {
        "市政厅": ("city_hall", 110, 110),
        "水族馆": ("aquarium", 250, 240),
        "铁安局": ("battle", 540, 220),
        "商会": ("commerce", 740, 130),
        "交易所": ("exchange", 990, 270),
        "休息区": ("rest", 620, 400),
        "拉面店": ("ramen_restaurant", 1090, 420),
    }

    assert city == {
        "city_key": "cape_city",
        "city_name": "海角城",
    }
    for shop_name, expected_point in expected.items():
        shop = service.resolve_shop_point("海角城", shop_name)
        assert (shop["shop_key"], shop["x"], shop["y"]) == expected_point

    with pytest.raises(CityShopDataError) as raised:
        service.resolve_shop_point("海角城", "桦树生物")
    assert raised.value.code == "shop_not_resolved"


def test_confluence_tower_facilities_are_resolvable() -> None:
    service = ResonancePcCityShopDataService()

    city = service.resolve_city("汇流塔")
    expected = {
        "异想构建室": ("ideation_construction_room", 240, 200),
        "汇流之所": ("confluence_hub", 600, 180),
        "交易所": ("exchange", 920, 320),
    }

    assert city == {
        "city_key": "confluence_tower",
        "city_name": "汇流塔",
    }
    for shop_name, expected_point in expected.items():
        shop = service.resolve_shop_point("汇流塔", shop_name)
        assert (shop["shop_key"], shop["x"], shop["y"]) == expected_point
