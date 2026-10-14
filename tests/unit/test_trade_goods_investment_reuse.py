"""Single-account cached levels and fixed positions drive the real visit loop."""
from types import SimpleNamespace

import numpy as np
import pytest

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.resonance_pc.src.actions import trade_goods_investment_pc_actions as investment


@pytest.fixture
def visit(monkeypatch, tmp_path):
    state = SimpleNamespace(now=0., count=4, levels=[10] * 12, selected=1, page=0,
                            phase="shop", scans=[], state_reads=[], level_reads=[],
                            clicks=[], drags=0, commits=[], commit_to={}, no_effect=False,
                            origin=True, return_error=False, unstable=False)
    persistence = PersistentDataService(tmp_path)
    persistence.set("user-info.json", None, {"status": {"fatigue": 500}, "custom": "keep"})
    state.persistence = persistence

    def cards():
        start = 1 if state.page == 0 else 7
        end = min(state.count, 9 if state.page == 0 else 15)
        return [{"rect": (62 + 127 * ((i - start) % 3), 143 + 132 * ((i - start) // 3), 115, 129),
                 "partial": False, "occluded": False, "locked": None, "selected": False,
                 "maximum": False, "test_index": i} for i in range(start, end + 1)]

    class Reader:
        city_id = "9"
        VIEWPORT = (49, 143, 394, 446)
        metadata = {"cards": {"viewport": VIEWPORT, "size": (115, 129)}}

        @property
        def city_product_count(self):
            return state.count

        @property
        def city_catalog_signature(self):
            return f"fixture-city9-count{state.count}"

        def read_entry_state(self, frame):
            return {"availability": "available", "center": (840, 490),
                    "white_score": .95, "gray_score": .7}

        def match(self, frame, name):
            return {"found": name == "entry" or (name == "page_anchor" and state.phase == "investment"),
                    "center": (840, 490)}

        def locate_cards(self, frame, *, success_visible):
            state.scans.append(state.page)
            return cards()

        def initial_order_confirmed(self, frame, rows):
            return state.origin

        def read_card_state(self, frame, card):
            index = card["test_index"]
            state.state_reads.append(index)
            if state.unstable:
                raise investment.InvestmentRecognitionError("card_position_uncertain", "unknown")
            return {**card, "locked": False, "selected": state.selected == index}

        def read_card_selection(self, frame, card):
            return {**card, "selected": state.selected == card["test_index"]}

        def read_levels(self, frame):
            state.level_reads.append(state.selected)
            current = state.levels[state.selected - 1]
            return {"current": current, "preview": current + 1, "max_level": False}

        def fingerprint(self, frame, card):
            return card["test_index"]

        def same_fingerprint(self, first, second):
            return first == second

    def click(**point):
        state.clicks.append(point)
        if point == {"x": 840, "y": 490}:
            state.phase, state.selected, state.page = "investment", 1, 0
        else:
            state.selected = next(row["test_index"] for row in cards()
                                  if point == {"x": row["rect"][0] + 57, "y": row["rect"][1] + 64})

    def drag(**kwargs):
        state.drags += 1
        state.page = 1

    def commit(app, reader, operation):
        state.commits.append(state.selected)
        if state.no_effect:
            return None
        state.levels[state.selected - 1] = operation["upgrade_level"]
        return {"confirmed": True, "current_level": operation["upgrade_level"]}

    def return_shop(*args):
        if state.return_error:
            raise investment.TradeGoodsInvestmentError("investment_return_shop_timeout", "timeout")
        state.phase = "shop"

    def prepare(app, reader, levels, target, update):
        actual = state.commit_to.get(state.selected, target)
        return {"current_level": levels["current"], "upgrade_level": actual,
                "stop_after_commit": actual < target}

    monkeypatch.setattr(investment, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(investment, "_pause", lambda seconds: setattr(state, "now", state.now + seconds))
    monkeypatch.setattr(investment, "_cancel_check", lambda: None)
    monkeypatch.setattr(investment, "_capture", lambda app: np.full((720, 1280, 3), state.page * 20, np.uint8))
    monkeypatch.setattr(investment, "InvestmentVision", lambda **kwargs: Reader())
    monkeypatch.setattr(investment, "_prepare_upgrade", prepare)
    monkeypatch.setattr(investment, "_commit", commit)
    monkeypatch.setattr(investment, "_return_shop", return_shop)
    app = SimpleNamespace(click=click, drag=drag)
    state.run = lambda target=10: investment.execute_trade_goods_investment_from_shop(
        mode=target, city_name="淘金乐园", app=app, vision=object(), persistent_data=persistence)
    state.saved = lambda: persistence.read("user-info.json")
    return state


def test_positions_are_reused_for_every_product_without_scroll(visit):
    visit.run()
    assert visit.scans == [0, 0]  # Only the initial two stable layout observations.
    assert visit.state_reads == [1, 1, 2, 2, 3, 3, 4, 4]
    assert visit.saved()["trade_goods_investment_levels"]["9"] == [10, 10, 10, 10]
    assert visit.saved()["custom"] == "keep"
    assert {"x": 119, "y": 207} not in visit.clicks  # Native entry already selected item 1.


def test_next_visit_skips_saved_levels_without_card_or_level_reads(visit):
    visit.run()
    visit.scans.clear(); visit.state_reads.clear(); visit.level_reads.clear(); visit.clicks.clear()
    visit.run()
    assert visit.scans == [0, 0]
    assert not visit.state_reads and not visit.level_reads and not visit.commits
    assert visit.clicks == [{"x": 840, "y": 490}]


def test_cached_prefix_skips_to_first_unfinished_and_stops_below_ten(visit):
    visit.persistence.set("user-info.json", ["trade_goods_investment_levels"], {"9": [10, 13, 5, None]})
    visit.persistence.set("user-info.json", ["trade_goods_investment_catalog_signatures"], {"9": "fixture-city9-count4"})
    visit.levels[:4] = [10, 13, 5, 0]
    visit.commit_to[3] = 8
    result = visit.run()
    assert result["reason"] == "no_further_upgrade"
    assert visit.state_reads == [3, 3]
    assert visit.level_reads == [3, 3]
    assert visit.commits == [3]
    assert visit.saved()["trade_goods_investment_levels"]["9"] == [10, 13, 8, None]


def test_reaching_ten_allows_current_following_item(visit):
    visit.count = 2
    visit.levels[:2] = [5, 0]
    visit.run()
    assert visit.commits == [1, 2]
    assert visit.saved()["trade_goods_investment_levels"]["9"] == [10, 10]


@pytest.mark.parametrize("cached", [False, True])
def test_low_target_does_not_bypass_ten_level_gate(visit, cached):
    visit.count = 2
    visit.levels[:2] = [5, 0]
    if cached:
        visit.persistence.set("user-info.json", ["trade_goods_investment_levels"], {"9": [5, None]})
        visit.persistence.set("user-info.json", ["trade_goods_investment_catalog_signatures"], {"9": "fixture-city9-count2"})
    visit.run(5)
    assert 2 not in visit.state_reads and 2 not in visit.level_reads
    assert not visit.commits and visit.drags == 0


def test_higher_target_rechecks_previous_confirmed_level(visit):
    visit.count = 1
    visit.run()
    visit.run(15)
    assert visit.commits == [1]
    assert visit.saved()["trade_goods_investment_levels"]["9"] == [15]


def test_return_failure_keeps_each_confirmed_level(visit):
    visit.count = 1
    visit.levels[0] = 5
    visit.return_error = True
    with pytest.raises(investment.TradeGoodsInvestmentError):
        visit.run()
    assert visit.saved()["trade_goods_investment_levels"]["9"] == [10]
    assert visit.commits == [1]


def test_no_effect_does_not_save_preview_target_or_read_next(visit):
    visit.levels[0] = 0
    visit.no_effect = True
    visit.run()
    assert visit.saved()["trade_goods_investment_levels"]["9"] == [0, None, None, None]
    assert set(visit.level_reads) == {1} and visit.commits == [1]


def test_origin_not_confirmed_cannot_number_or_save_items(visit):
    visit.origin = False
    with pytest.raises(investment.TradeGoodsInvestmentError) as exc:
        visit.run()
    assert exc.value.code == "investment_initial_order_unconfirmed"
    assert "trade_goods_investment_levels" not in visit.saved()
    assert not visit.state_reads and not visit.level_reads


def test_unknown_current_slot_never_relocates_or_inspects_later_items(visit):
    visit.unstable = True
    with pytest.raises(investment.TradeGoodsInvestmentError):
        visit.run()
    assert set(visit.state_reads) == {1}
    assert visit.scans == [0, 0] and not visit.level_reads
    assert "trade_goods_investment_levels" not in visit.saved()


def test_scrolling_relocates_once_and_keeps_absolute_sequence(visit):
    visit.count = 12
    result = visit.run()
    assert visit.scans == [0, 0, 1, 1]
    assert visit.drags == result["scroll_count"] == 1
    assert [index for index in visit.state_reads[::2]] == list(range(1, 13))
    assert visit.saved()["trade_goods_investment_levels"]["9"] == [10] * 12


def test_below_ten_in_last_visible_item_does_not_scroll_to_more(visit):
    visit.count = 12
    visit.levels[8] = 5
    visit.commit_to[9] = 8
    visit.run()
    assert visit.drags == 0 and visit.scans == [0, 0]
    assert max(visit.state_reads) == max(visit.level_reads) == 9


def test_scrolled_partial_row_keeps_absolute_indices():
    previous = [{"partial": False, "investment_index": i} for i in range(1, 10)]
    following = [{"partial": i <= 3} for i in range(1, 11)]
    assigned = investment._scrolled_card_indices(previous, following, overlap_count=6)
    assert [row["investment_index"] for row in assigned] == list(range(1, 11))
    assert [row["investment_index"] for row in assigned if not row["partial"]] == list(range(4, 11))


def test_invalid_scroll_origin_does_not_guess_indices():
    previous = [{"partial": False, "investment_index": 1}]
    following = [{"partial": True}, {"partial": False}]
    with pytest.raises(investment.TradeGoodsInvestmentError) as exc:
        investment._scrolled_card_indices(previous, following, overlap_count=1)
    assert exc.value.code == "investment_scroll_overlap_unconfirmed"


def test_known_remaining_products_cannot_finish_on_stalled_scroll(visit, monkeypatch):
    visit.count = 12
    monkeypatch.setattr(investment, "_scroll", lambda *args: None)
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        visit.run()
    assert error.value.code == "investment_scroll_stalled"
    assert visit.saved()["trade_goods_investment_levels"]["9"] == [10] * 9 + [None] * 3


@pytest.mark.parametrize("signature", [None, "different-same-length-order"])
def test_unsigned_or_changed_order_cache_rechecks_actual_levels(visit, signature):
    visit.persistence.set("user-info.json", ["trade_goods_investment_levels"], {"9": [20] * 4})
    if signature is not None:
        visit.persistence.set("user-info.json", ["trade_goods_investment_catalog_signatures"], {"9": signature})
    visit.run()
    assert visit.state_reads == [1, 1, 2, 2, 3, 3, 4, 4]
    assert visit.saved()["trade_goods_investment_levels"]["9"] == [10] * 4
    assert visit.saved()["trade_goods_investment_catalog_signatures"]["9"] == "fixture-city9-count4"
