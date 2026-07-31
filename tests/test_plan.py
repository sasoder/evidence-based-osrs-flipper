from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from flipper import intents, plan, signals

class TriageOutlierBidTests(unittest.TestCase):
    """A stale sell must not clear against a single anomalous live tick (the antifire
    15000-vs-20086 incident, 2026-06-24)."""

    def _sig(self, current_high: int) -> dict:
        return {
            "name": "Extended super antifire(4)",
            "regime": {"level": "low", "reason": "stable_recent_distribution"},
            "entry_price": 19000, "buy_band": 20086, "exit_price": 21391,
            "current_low": 19000, "current_high": current_high,
            "price_fresh": True, "ready_to_buy": False, "ready_to_sell": False,
        }

    def _stale_sell(self) -> dict:
        return {"id": 22209, "side": "sell", "qty": 204, "price": 20006,
                "age_hours": 13.8, "filled_qty": 216, "state": "ACTIVE"}

    def test_outlier_bid_is_dropped(self) -> None:
        self.assertIsNone(plan._sane_bid(self._sig(15000)))   # 25% below the recent low band
        self.assertEqual(plan._sane_bid(self._sig(18500)), 18500)  # plausible dip, trusted

    def test_stale_sell_holds_instead_of_dumping_on_a_bad_tick(self) -> None:
        sig = self._sig(15000)
        res = plan._decide_triage(self._stale_sell(), sig, sig)
        self.assertEqual(res["verdict"], "hold")
        self.assertNotIn("new_price", res)

    def test_stale_sell_still_clears_against_a_real_bid(self) -> None:
        sig = self._sig(18500)
        res = plan._decide_triage(self._stale_sell(), sig, sig)
        self.assertEqual(res["verdict"], "reprice")
        self.assertEqual(res["new_price"], 18500)



class CostBasisTests(unittest.TestCase):
    def test_weights_open_lots_and_ignores_sold_inventory(self) -> None:
        flips = [
            # 100 held @ 200 and 300 held @ 240 -> weighted avg over 400 held = 230
            {"id": 1, "bought": 200, "bought_qty": 100, "sold_qty": 0},
            {"id": 1, "bought": 240, "bought_qty": 400, "sold_qty": 100},
            # fully sold lot contributes nothing
            {"id": 2, "bought": 999, "bought_qty": 50, "sold_qty": 50},
            # a pure sell record (no buy price) is skipped
            {"id": 3, "bought": 0, "bought_qty": 0, "sold_qty": 10},
        ]
        with patch("flipper.plan.runelite.read_flips", return_value=flips):
            self.assertEqual(plan._cost_basis(), {1: 230})


class OpenOfferContractTests(unittest.TestCase):
    """Open offers must come from the enriched current-slot export."""

    def test_plan_rejects_open_offer_without_limit_price(self) -> None:
        # Patch every data source: this must fail on the offer contract, not on
        # whatever FU exports / price cache happen to exist on this machine.
        with (
            patch("flipper.plan.signals.item_signal", return_value=None),
            patch("flipper.plan.signals.live_quote", return_value=None),
            patch("flipper.plan._cost_basis", return_value={}),
            patch("flipper.plan._personal_execution_stats", return_value={}),
        ):
            with self.assertRaisesRegex(ValueError, "no limit price"):
                plan.plan(cash=1_000_000, offers=[{
                    "slot": 0,
                    "id": 13237,
                    "side": "buy",
                    "qty": 1,
                    "filled_qty": 0,
                    "price": 0,
                    "age_hours": 0.1,
                    "state": "ACTIVE",
                }])

    def test_plan_cli_reports_stale_export_without_traceback(self) -> None:
        err = io.StringIO()
        with (
            patch("flipper.runelite.read_open_offers",
                  side_effect=RuntimeError("current GE slot export is stale")),
            redirect_stderr(err),
        ):
            code = plan._main(["--cash", "1000000"])

        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "error: current GE slot export is stale\n")



class UnknownAgeTriageTests(unittest.TestCase):
    """When age cannot be anchored at all, triage must fail stale, not hold."""

    def _offer(self, side: str) -> dict:
        return {"id": 565, "side": side, "qty": 100, "filled_qty": 0,
                "price": 5000, "age_hours": None, "last_fill_age_hours": None,
                "state": "ACTIVE"}

    def test_unknown_age_sell_clears_instead_of_holding(self) -> None:
        quote = {"name": "Fire rune", "current_high": 4800}
        res = plan._decide_triage(self._offer("sell"), None, quote)
        self.assertEqual(res["verdict"], "reprice")
        self.assertEqual(res["new_price"], 4800)
        self.assertIn("age unknown", res["note"])

    def test_unknown_age_high_value_buy_not_held_as_fresh(self) -> None:
        # Unattributed high-value buy with unknown age must not get the "fresh,
        # preserve through the 30m window" hold; it falls through to band logic.
        offer = {"id": 565, "side": "buy", "qty": 1, "price": 2_000_000,
                 "filled_qty": 0, "age_hours": None, "last_fill_age_hours": None,
                 "state": "ACTIVE"}
        res = plan._decide_triage(offer, None, {"current_high": 1_900_000})
        # no-signal buy -> free the slot, rather than a phantom-fresh hold
        self.assertEqual(res["verdict"], "cancel")
        self.assertNotIn("fresh", res["note"])



class OverpricedNoBandAskTests(unittest.TestCase):
    """A no-band sell far above the live bid must not hide behind the 6h stale clock."""

    def _offer(self, price: int) -> dict:
        return {"id": 11228, "side": "sell", "qty": 125, "filled_qty": 0,
                "price": price, "age_hours": 0.14, "last_fill_age_hours": None,
                "state": "ACTIVE"}

    def test_far_above_bid_reprices_to_bid_before_stale(self) -> None:
        quote = {"name": "Dragon arrow(p+)", "current_high": 2234}
        res = plan._decide_triage(self._offer(3598), None, quote)
        self.assertEqual(res["verdict"], "reprice")
        self.assertEqual(res["new_price"], 2234)
        self.assertIn("above live bid", res["note"])

    def test_modest_premium_keeps_stale_grace(self) -> None:
        quote = {"name": "Dragon arrow(p+)", "current_high": 2234}
        res = plan._decide_triage(self._offer(2300), None, quote)  # ~3% above bid
        self.assertEqual(res["verdict"], "hold")
        self.assertIn("not stale yet", res["note"])

    def test_cost_guard_clamps_overpriced_reprice_to_break_even(self) -> None:
        quote = {"name": "Dragon arrow(p+)", "current_high": 2234}
        offer = self._offer(3598)
        res = plan._apply_cost_guard(
            plan._decide_triage(offer, None, quote), offer, quote, cost=2500)
        self.assertEqual(res["verdict"], "reprice")
        self.assertEqual(res["new_price"], plan._break_even(2500))
        self.assertTrue(res["cost_floor"])



def _sig(iid, score, *, regime="low", entry_price=100, exit_price=200, fillable=50, ge_limit=1000,
         exit_capacity=0):
    return {"id": iid, "name": f"item{iid}", "entry_price": entry_price, "exit_price": exit_price,
            "buy_band": entry_price,
            "regime": {"level": regime, "reason": "x"}, "fillable_qty": fillable,
            "exit_capacity_qty": exit_capacity,
            "fill_window_hours": 4.0, "ge_limit": ge_limit, "score": score,
            "current_low": entry_price, "current_high": exit_price, "price_fresh": True,
            "ready_to_buy": True, "patient_probe_ready": False,
            "distance_to_buy_pct": 0.0}


def _active_scan():
    return {"candidates": [{
        "id": 7, "name": "Test gear", "entry_price": 150_000, "exit_price": 165_000,
        "current_low": 149_999, "current_high": 165_001,
        "net_margin": 11_700, "roi_pct": 7.8, "max_qty": 8,
        "fillable_qty": 8, "expected_value_per_unit": 10_000,
        "high_age_minutes": 1.0, "low_age_minutes": 2.0,
        "high_vol_1h": 5, "low_vol_1h": 5, "short_drift_pct": 0.0,
    }], "rejected": []}


def _bt(ok=True):
    return {
        "total_profit_per_unit": 500,
        "avg_profit_per_unit": 100,
        "trades": 5,
        "median_hold_points": 4,
        "median_hold_hours": 4,
        "max_adverse_pct": -5.0,
    } if ok else None


class PlanTests(unittest.TestCase):
    def _plan(self, scan_sigs, *, cash=1_000_000, active=None, time_scan=None,
              bt=lambda i: _bt(True), item=lambda i: None,
              quote=lambda i: None, cost_map=None, open_strategies=None, personal=None, **kw):
        with (
            patch("flipper.plan.signals.scan", return_value=scan_sigs),
            patch("flipper.plan.signals.active_margin_scan",
                  return_value=active or {"candidates": [], "rejected": []}),
            patch("flipper.plan.signals.time_of_day_scan",
                  return_value=time_scan or {"candidates": [], "rejected": []}),
            patch("flipper.plan.signals.backtest_signal", side_effect=lambda iid, **k: bt(iid)),
            patch("flipper.plan.signals.item_signal", side_effect=lambda iid, **k: item(iid)),
            patch("flipper.plan.signals.live_quote", side_effect=lambda iid: quote(iid)),
            patch("flipper.plan._cost_basis", return_value=cost_map or {}),
            patch("flipper.plan._personal_execution_stats", return_value=personal or {}),
            patch("flipper.plan._open_strategy_by_item", return_value=open_strategies or {}),
        ):
            return plan.plan(cash=cash, **kw)

    def test_time_of_day_strategy_uses_available_capital_not_a_percentage_cap(self) -> None:
        time_scan = {"candidates": [{
            "id": 7,
            "name": "Timed item",
            "entry_price": 100,
            "exit_price": 120,
            "current_low": 99,
            "current_high": 111,
            "ge_limit": 5_000,
            "fillable_qty": 5_000,
            "expected_profit_per_unit": 18,
            "score": 7_500,
            "hold_hours": 12,
            "entry_window_utc": "00:00-06:00",
            "exit_window_utc": "12:00-18:00",
            "train": {"trades": 20, "win_rate": 0.7, "median_profit_per_unit": 15},
            "test": {"trades": 10, "win_rate": 0.6, "median_profit_per_unit": 12},
        }], "rejected": []}

        p = self._plan([], time_scan=time_scan)

        self.assertEqual(p["time_buys"][0]["qty"], 5_000)
        self.assertEqual(p["time_buys"][0]["strategy"], "time-of-day")
        self.assertEqual(p["time_buys"][0]["live_low"], 99)
        self.assertEqual(p["time_buys"][0]["live_high"], 111)
        self.assertIn("| 99/111 |", plan._render_md(p))
        self.assertEqual(p["budget_left_gp"], 500_000)

    def test_time_of_day_only_can_use_requested_slots(self) -> None:
        time_scan = {"candidates": [
            {
                "id": i,
                "name": f"Timed item {i}",
                "entry_price": 100,
                "exit_price": 120,
                "ge_limit": 5_000,
                "fillable_qty": 1_000,
                "expected_profit_per_unit": 18,
                "score": 7_500 - i,
                "hold_hours": 12,
                "entry_window_utc": "00:00-06:00",
                "exit_window_utc": "12:00-18:00",
                "train": {"trades": 20, "win_rate": 0.7, "median_profit_per_unit": 15},
                "test": {"trades": 10, "win_rate": 0.6, "median_profit_per_unit": 12},
            }
            for i in range(1, 4)
        ], "rejected": []}

        p = self._plan([], time_scan=time_scan, strategies="time", max_new_slots=3)

        self.assertEqual(len(p["time_buys"]), 3)
        self.assertEqual(p["slots"]["time_buys"], 3)

    def test_time_of_day_competes_with_patient_on_gp_per_hour(self) -> None:
        time_scan = {"candidates": [{
            "id": 7,
            "name": "Slower timed item",
            "entry_price": 100,
            "exit_price": 120,
            "ge_limit": 5_000,
            "fillable_qty": 5_000,
            "expected_profit_per_unit": 18,
            "score": 100,
            "hold_hours": 12,
            "entry_window_utc": "00:00-06:00",
            "exit_window_utc": "12:00-18:00",
            "train": {"trades": 20, "win_rate": 0.7, "median_profit_per_unit": 15},
            "test": {"trades": 10, "win_rate": 0.6, "median_profit_per_unit": 12},
        }], "rejected": []}

        p = self._plan(
            [_sig(1, 100, entry_price=100_000, fillable=9, ge_limit=9)],
            time_scan=time_scan,
            # enough per-unit edge to clear the capital-return floor on 900k locked
            bt=lambda i: {**_bt(), "avg_profit_per_unit": 1_000},
        )

        self.assertEqual(p["buys"][0]["qty"], 9)
        self.assertEqual(p["time_buys"][0]["qty"], 1_000)
        self.assertEqual(p["budget_left_gp"], 0)

    def test_patient_order_size_scales_to_exit_capacity_with_ev_on_expected_fills(self) -> None:
        # Cheap item: post what the sell side can absorb, but expected profit and the
        # capital floor stay anchored to the conservative expected-fill estimate.
        p = self._plan([_sig(1, 100, fillable=10, exit_capacity=500)])

        self.assertEqual(p["buys"][0]["qty"], 500)
        self.assertEqual(p["buys"][0]["expected_profit"], 1_000)  # 100/u * 10 expected fills

    def test_time_lane_posts_exit_capacity_but_keeps_ev_on_expected_fills(self) -> None:
        time_scan = {"candidates": [{
            "id": 7,
            "name": "Cheap tabs",
            "entry_price": 100,
            "exit_price": 130,
            "ge_limit": 10_000,
            "fillable_qty": 60,
            "exit_capacity_qty": 2_000,
            "expected_profit_per_unit": 20,
            "score": 100,
            "hold_hours": 12,
            "entry_window_utc": "00:00-06:00",
            "exit_window_utc": "12:00-18:00",
            "train": {"trades": 20, "win_rate": 0.7, "median_profit_per_unit": 15},
            "test": {"trades": 10, "win_rate": 0.6, "median_profit_per_unit": 12},
        }], "rejected": []}

        p = self._plan([], time_scan=time_scan)

        self.assertEqual(p["time_buys"][0]["qty"], 2_000)
        self.assertEqual(p["time_buys"][0]["expected_profit"], 1_200)  # 20/u * 60 expected fills
        self.assertEqual(p["budget_left_gp"], 800_000)

    def test_capital_floor_rejects_high_capital_low_ev_slot(self) -> None:
        # Tome-of-fire shape: one expensive unit whose EV is fine against the flat floor
        # (200gp at 1m liquid) but poor for the capital it commits for up to 24h.
        time_scan = {"candidates": [{
            "id": 8,
            "name": "Expensive tome",
            "entry_price": 100_000,
            "exit_price": 101_000,
            "ge_limit": 8,
            "fillable_qty": 1,
            "exit_capacity_qty": 1,
            "expected_profit_per_unit": 550,
            "score": 50,
            "hold_hours": 6,
            "entry_window_utc": "06:00-12:00",
            "exit_window_utc": "12:00-18:00",
            "train": {"trades": 20, "win_rate": 0.7, "median_profit_per_unit": 500},
            "test": {"trades": 10, "win_rate": 0.6, "median_profit_per_unit": 450},
        }], "rejected": []}

        p = self._plan([], time_scan=time_scan)

        self.assertEqual(p["time_buys"], [])
        # 100,000gp * 24h * 0.0005 = 1,200gp capital floor beats the 550gp EV
        self.assertIn(
            "expected profit 550gp < floor 1,200gp (100,000gp committed ≤24h)",
            p["time_skipped"][0]["reason"],
        )

    def test_survival_gate_and_avoid_drop(self) -> None:
        sigs = [_sig(1, 100), _sig(2, 50)]
        # item 2 fails the survival gate; item 1 is research-avoided -> no buys.
        p = self._plan(sigs, bt=lambda i: _bt(i == 1), overlay={"avoid": [{"id": 1}]})
        self.assertEqual(p["buys"], [])
        reasons = {s["id"]: s["reason"] for s in p["skipped"]}
        self.assertEqual(reasons[1], "research avoid")
        self.assertEqual(reasons[2], "fails survival gate")

    def test_boost_reranks_survivors(self) -> None:
        sigs = [_sig(1, 100), _sig(2, 50)]  # 1 outranks 2 by score
        p = self._plan(sigs, overlay={"boost": [{"id": 2}]})
        self.assertEqual([b["id"] for b in p["buys"]], [2, 1])  # boost puts 2 first

    def test_survivors_rank_by_expected_realized_gp_per_hour(self) -> None:
        sigs = [_sig(1, 100, fillable=10), _sig(2, 50, fillable=100)]
        p = self._plan(sigs)
        self.assertEqual([b["id"] for b in p["buys"]], [2, 1])

    def test_personal_best_flip_is_checked_outside_margin_seed_scan(self) -> None:
        personal = {
            99: {
                "name": "Known winner",
                "round_trip": {
                    "staple": True,
                    "profitable_trips": 7,
                    "net_profit": 100_000,
                    "median_hours": 3,
                    "median_gp_per_capital_hour": 20_000,
                },
            }
        }
        p = self._plan([], item=lambda i: _sig(i, 100, fillable=20), personal=personal)

        self.assertEqual(p["buys"][0]["id"], 99)
        self.assertEqual(p["buys"][0]["confidence"], 0.65)
        self.assertIn("personal staple", p["buys"][0]["reason"])

    def test_regime_high_skipped_without_boost(self) -> None:
        p = self._plan([_sig(1, 100, regime="high")])
        self.assertEqual(p["buys"], [])
        self.assertEqual(p["skipped"][0]["reason"], "regime high — needs research thesis")

    def test_candidate_with_unreachable_bid_is_skipped(self) -> None:
        sig = {**_sig(1, 100), "ready_to_buy": False,
               "patient_probe_ready": False, "distance_to_buy_pct": 8.5}
        p = self._plan([sig])
        self.assertEqual(p["buys"], [])
        self.assertIn("outside the 3% probe window", p["skipped"][0]["reason"])

    def test_near_band_candidate_uses_five_percent_probe_cap(self) -> None:
        sig = {
            **_sig(1, 100, entry_price=100, fillable=1000),
            "ready_to_buy": False,
            "patient_probe_ready": True,
            "distance_to_buy_pct": 2.0,
            "current_low": 102,
        }
        p = self._plan([sig])

        self.assertEqual(p["buys"], [])
        self.assertEqual(len(p["patient_probes"]), 1)
        self.assertEqual(p["patient_probes"][0]["qty"], 500)  # 5% of 1M / 100gp
        self.assertEqual(p["patient_probes"][0]["bucket"], "flip-patient-probe")
        self.assertEqual(p["slots"]["patient_probes"], 1)

    def test_probe_only_can_use_requested_slots_within_total_cap(self) -> None:
        sigs = [
            {
                **_sig(i, 100 - i, entry_price=100, fillable=250),
                "ready_to_buy": False,
                "patient_probe_ready": True,
                "distance_to_buy_pct": 2.0,
                "current_low": 102,
            }
            for i in range(1, 3)
        ]

        p = self._plan(sigs, strategies="probe", max_new_slots=2)

        self.assertEqual(len(p["patient_probes"]), 2)
        self.assertEqual([b["qty"] for b in p["patient_probes"]], [250, 250])
        self.assertEqual(p["slots"]["patient_probes"], 2)

    def test_active_strategy_sizes_to_ge_limit_and_available_gp(self) -> None:
        active = _active_scan()
        p = self._plan([], active=active)

        self.assertEqual(p["buys"], [])
        self.assertEqual(len(p["active_buys"]), 1)
        self.assertEqual(p["active_buys"][0]["qty"], 6)
        self.assertEqual(p["active_buys"][0]["bucket"], "flip-active")
        self.assertEqual(p["active_buys"][0]["live_low"], 149_999)
        self.assertEqual(p["active_buys"][0]["live_high"], 165_001)
        self.assertIn("| 149,999/165,001 |", plan._render_md(p))
        self.assertEqual(p["slots"]["active_buys"], 1)

    def test_strategies_can_select_patient_only(self) -> None:
        active = _active_scan()
        time_scan = {"candidates": [{
            "id": 8, "name": "Timed item", "entry_price": 100, "exit_price": 120,
            "ge_limit": 5_000, "fillable_qty": 5_000,
            "expected_profit_per_unit": 18, "score": 7_500, "hold_hours": 12,
            "entry_window_utc": "00:00-06:00", "exit_window_utc": "12:00-18:00",
            "train": {"trades": 20}, "test": {"trades": 10},
        }], "rejected": []}

        p = self._plan(
            [_sig(1, 100, entry_price=100, fillable=50)],
            active=active,
            time_scan=time_scan,
            strategies="patient",
        )

        self.assertEqual([b["id"] for b in p["buys"]], [1])
        self.assertEqual(p["active_buys"], [])
        self.assertEqual(p["time_buys"], [])
        self.assertEqual(p["inputs"]["strategies"], ["patient"])

    def test_strategies_can_select_active_only(self) -> None:
        active = _active_scan()

        personal = {
            99: {
                "name": "Known patient winner",
                "round_trip": {
                    "staple": True,
                    "profitable_trips": 7,
                    "net_profit": 100_000,
                    "median_hours": 3,
                    "median_gp_per_capital_hour": 20_000,
                },
            }
        }

        p = self._plan(
            [_sig(1, 100, entry_price=100, fillable=50)],
            active=active,
            personal=personal,
            strategies="active",
        )

        self.assertEqual(p["buys"], [])
        self.assertEqual(len(p["active_buys"]), 1)
        self.assertEqual(p["inputs"]["strategies"], ["active"])

    def test_conservative_strategies_exclude_patient_probes(self) -> None:
        sig = {
            **_sig(1, 100, entry_price=100, fillable=1000),
            "ready_to_buy": False,
            "patient_probe_ready": True,
            "distance_to_buy_pct": 2.0,
            "current_low": 102,
        }

        p = self._plan([sig], strategies="conservative")

        self.assertEqual(p["patient_probes"], [])
        self.assertEqual(p["inputs"]["strategies"], ["active", "patient", "time"])
        self.assertEqual(p["skipped"][0]["reason"], "patient-probe strategy disabled")

    def test_overnight_horizon_excludes_active_strategy_before_intents(self) -> None:
        active = _active_scan()
        p = self._plan([], active=active, horizon="overnight")

        self.assertEqual(p["inputs"]["horizon"], "overnight")
        self.assertEqual(p["active_buys"], [])
        self.assertEqual(intents.intents_from_plan(p), [])
        self.assertTrue(any("active strategy disabled" in reason
                            for reason in p["active_filter_summary"]))

    def test_away_hours_excludes_active_strategy_before_intents(self) -> None:
        active = _active_scan()
        p = self._plan([], active=active, away_hours=3)

        self.assertEqual(p["inputs"]["horizon"], "intraday")
        self.assertEqual(p["inputs"]["away_hours"], 3)
        self.assertEqual(p["active_buys"], [])
        self.assertEqual(intents.intents_from_plan(p), [])
        self.assertTrue(any("active strategy disabled" in reason
                            for reason in p["active_filter_summary"]))

    def test_brief_absence_keeps_active_strategy(self) -> None:
        active = _active_scan()
        p = self._plan([], active=active, away_hours=0.25)

        self.assertEqual(len(p["active_buys"]), 1)

    def test_long_absence_implies_overnight_horizon(self) -> None:
        p = self._plan([], away_hours=plan.OVERNIGHT_AWAY_HOURS)

        self.assertEqual(p["inputs"]["horizon"], "overnight")

    def test_overnight_horizon_implies_absence(self) -> None:
        p = self._plan([], horizon="overnight")

        self.assertEqual(p["inputs"]["away_hours"], plan.OVERNIGHT_FILL_WINDOW_HOURS)

    def test_overnight_horizon_keeps_active_excluded_despite_short_away_hours(self) -> None:
        p = self._plan([], active=_active_scan(), horizon="overnight", away_hours=0.25)

        self.assertEqual(p["active_buys"], [])
        self.assertEqual(p["inputs"]["away_hours"], plan.OVERNIGHT_FILL_WINDOW_HOURS)

    def test_active_only_while_away_is_a_contradiction(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._plan([], strategies="active", away_hours=3)
        self.assertIn("contradiction", str(ctx.exception))

    def test_overnight_horizon_sizes_patient_scan_to_twelve_hour_window(self) -> None:
        captured = {}

        def scan(**kwargs):
            captured.update(kwargs)
            return [_sig(1, 100, fillable=50)]

        with (
            patch("flipper.plan.signals.scan", side_effect=scan),
            patch("flipper.plan.signals.active_margin_scan",
                  return_value={"candidates": [], "rejected": []}),
            patch("flipper.plan.signals.time_of_day_scan",
                  return_value={"candidates": [], "rejected": []}),
            patch("flipper.plan.signals.backtest_signal", return_value=_bt(True)),
            patch("flipper.plan.signals.item_signal", return_value=None),
            patch("flipper.plan._cost_basis", return_value={}),
            patch("flipper.plan._personal_execution_stats", return_value={}) as personal_stats,
            patch("flipper.plan._open_strategy_by_item", return_value={}),
        ):
            plan.plan(cash=1_000_000, horizon="overnight")

        self.assertEqual(captured["fill_window_hours"], signals.MAX_HOLD_HOURS)
        personal_stats.assert_called_once_with(signals.MAX_HOLD_HOURS)

    def test_active_buy_cancels_after_thirty_minutes(self) -> None:
        offer = {"id": 7, "side": "buy", "qty": 1, "filled_qty": 0,
                 "price": 100, "age_hours": 0.5, "state": "ACTIVE"}
        row = plan._decide_triage(
            offer, None, {"id": 7, "name": "gear"}, strategy="active-margin"
        )
        self.assertEqual(row["verdict"], "cancel")
        self.assertIn("30m", row["note"])

    def test_unattributed_fresh_high_value_buy_is_not_cancelled_by_patient_band(self) -> None:
        offer = {
            "id": 7,
            "side": "buy",
            "qty": 1,
            "filled_qty": 0,
            "price": 10_000_000,
            "age_hours": 0.1,
            "state": "ACTIVE",
        }
        sig = {
            **_sig(7, 100, entry_price=9_000_000, exit_price=9_500_000, fillable=1),
            "ready_to_buy": False,
        }

        row = plan._decide_triage(
            offer,
            sig,
            {"id": 7, "name": "gear"},
            strategy=None,
        )

        self.assertEqual(row["verdict"], "hold")
        self.assertIn("no FU strategy tag", row["note"])

    def test_partially_filled_active_buy_cancels_only_the_remainder(self) -> None:
        offer = {"id": 7, "side": "buy", "qty": 2, "filled_qty": 1,
                 "price": 100, "age_hours": 0.5, "state": "ACTIVE"}
        row = plan._decide_triage(
            offer, None, {"id": 7, "name": "gear"}, strategy="active-margin"
        )
        self.assertEqual(row["verdict"], "cancel")
        self.assertIn("cancel unfilled remainder", row["note"])
        self.assertIn("collect 1 filled", row["note"])

    def test_active_sell_hard_exits_after_ninety_minutes(self) -> None:
        offer = {"id": 7, "side": "sell", "qty": 1, "filled_qty": 0,
                 "price": 200, "age_hours": 1.5, "state": "ACTIVE"}
        quote = {"id": 7, "name": "gear", "current_high": 180}
        row = plan._decide_triage(offer, None, quote, strategy="active-margin")
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)
        self.assertIn("90m", row["note"])

    def test_active_sell_uses_original_call_deadline_not_sell_offer_age(self) -> None:
        offer = {"id": 7, "side": "sell", "qty": 1, "filled_qty": 0,
                 "price": 200, "age_hours": 0.1, "state": "ACTIVE"}
        quote = {"id": 7, "name": "gear", "current_high": 180}
        row = plan._decide_triage(
            offer,
            None,
            quote,
            strategy="active-margin",
            hard_exit_at="2020-01-01T00:00:00+00:00",
        )
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)

    def test_open_strategy_comes_from_fu_slot_tag(self) -> None:
        offer = {
            "slot": 3,
            "id": 7,
            "side": "buy",
            "qty": 10,
            "price": 80,
            "strategy": "patient-band",
            "hard_exit_at": "2026-06-25T22:00:00Z",
        }

        strategies = plan._open_strategy_by_item([offer])

        self.assertEqual(strategies[3]["strategy"], "patient-band")
        self.assertEqual(strategies[3]["hard_exit_at"], "2026-06-25T22:00:00Z")

    def test_time_of_day_buy_cancels_after_its_entry_window(self) -> None:
        offer = {"id": 7, "side": "buy", "qty": 100, "filled_qty": 0,
                 "price": 100, "age_hours": 6, "state": "ACTIVE"}

        row = plan._decide_triage(
            offer, None, {"id": 7, "name": "timed item"}, strategy="time-of-day"
        )

        self.assertEqual(row["verdict"], "cancel")
        self.assertIn("UTC entry window expired", row["note"])

    def test_active_deadline_allows_the_documented_stop_loss(self) -> None:
        offer = {"id": 7, "side": "sell", "qty": 1, "filled_qty": 0,
                 "price": 210, "age_hours": 0.1, "state": "ACTIVE"}
        quote = {"id": 7, "name": "gear", "current_high": 180}
        row = plan._apply_cost_guard(
            plan._decide_triage(
                offer,
                None,
                quote,
                strategy="active-margin",
                hard_exit_at="2020-01-01T00:00:00+00:00",
            ),
            offer,
            quote,
            cost=200,
            strategy="active-margin",
            hard_exit_due=True,
        )
        self.assertEqual(row["verdict"], "reprice")
        self.assertIn("90m active stop-loss", row["note"])

    def test_tiny_flip_is_skipped_below_manual_liquid_profit_floor(self) -> None:
        # liquid 50M -> floor 0.02% = 10,000. A flip realizing avg 100/u * 50 = 5,000 is noise.
        p = self._plan([_sig(1, 100, fillable=50)], cash=50_000_000)
        self.assertEqual(p["buys"], [])
        self.assertEqual(p["inputs"]["profit_floor_gp"], 10_000)
        self.assertIn("< floor", p["skipped"][0]["reason"])

    def test_same_flip_clears_floor_at_a_smaller_bankroll(self) -> None:
        # liquid 1M -> floor 200; the 5,000-gp flip is worth a slot.
        p = self._plan([_sig(1, 100, fillable=50)])
        self.assertEqual(p["buys"][0]["qty"], 50)
        self.assertEqual(p["inputs"]["profit_floor_gp"], 200)

    def test_personal_execution_history_can_only_reduce_sizing(self) -> None:
        personal = {
            1: {"buy": {"eligible": True, "orders": 4, "window_fill_factor": 0.4}}
        }
        p = self._plan([_sig(1, 100, fillable=50)], personal=personal)

        self.assertEqual(p["buys"][0]["qty"], 20)
        self.assertEqual(p["buys"][0]["execution_stats"]["market_fillable_qty"], 50)
        self.assertIn("personal FU cap 20", p["buys"][0]["reason"])

    def test_offer_triage_reprice_and_cancel(self) -> None:
        item = lambda i: _sig(i, 100, regime=("high" if i == 9 else "low"), entry_price=100, exit_price=200)
        offers = [{"id": 1, "side": "buy", "qty": 10, "price": 150},   # far from band 100 -> reprice
                  {"id": 2, "side": "buy", "qty": 10, "price": 100},   # at band -> hold
                  {"id": 9, "side": "buy", "qty": 10, "price": 100}]   # regime high -> cancel
        p = self._plan([], item=item, offers=offers)
        v = {o["id"]: o["verdict"] for o in p["offer_triage"]}
        self.assertEqual(v, {1: "reprice", 2: "hold", 9: "cancel"})
        self.assertEqual(next(o["new_price"] for o in p["offer_triage"] if o["id"] == 1), 100)

    def test_stale_zero_fill_buy_is_cancelled(self) -> None:
        offers = [{"id": 1, "side": "buy", "qty": 10, "filled_qty": 0,
                   "price": 100, "age_hours": 4}]
        row = self._plan([], item=lambda i: _sig(i, 100), offers=offers)["offer_triage"][0]
        self.assertEqual(row["verdict"], "cancel")
        self.assertIn("entry window expired", row["note"])

    def test_cancelled_buy_frees_slot_and_budget_for_new_buy(self) -> None:
        offers = [
            {"id": 99, "side": "buy", "qty": 10, "filled_qty": 0,
             "price": 100, "age_hours": 4},
        ] + [
            {"id": i, "side": "sell", "qty": 1, "filled_qty": 0,
             "price": 200, "age_hours": 1}
            for i in range(100, 107)
        ]

        def item(i):
            if i == 99:
                return _sig(i, 100, entry_price=100, exit_price=200)
            return {**_sig(i, 100, entry_price=100, exit_price=200), "current_high": 200}

        p = self._plan([_sig(1, 100, entry_price=100, fillable=20_000, ge_limit=20_000)],
                       item=item, offers=offers)

        self.assertEqual(p["offer_triage"][0]["verdict"], "cancel")
        self.assertEqual(p["projection"]["released_buy_gp"], 1_000)
        self.assertEqual(p["buys"][0]["qty"], 10_010)
        self.assertEqual(p["slots"]["new_buys"], 1)

    def test_partially_filled_patient_buy_emits_sell_fill_instruction(self) -> None:
        sig = {**_sig(1, 100, entry_price=100, exit_price=200), "ready_to_buy": False}
        offers = [{"id": 1, "side": "buy", "qty": 10, "filled_qty": 4,
                   "price": 100, "age_hours": 1}]
        p = self._plan([], item=lambda i: sig, offers=offers)

        self.assertEqual(p["offer_triage"][0]["verdict"], "cancel")
        self.assertEqual(p["sell_fills"][0]["qty"], 4)
        self.assertEqual(p["sell_fills"][0]["price"], 200)
        self.assertEqual(p["sell_fills"][0]["action"], "sell")

    def test_patient_probe_is_not_cancelled_for_waiting_below_live_low(self) -> None:
        offers = [{"id": 1, "side": "buy", "qty": 10, "filled_qty": 0,
                   "price": 100, "age_hours": 1}]
        sig = {
            **_sig(1, 100),
            "ready_to_buy": False,
            "patient_probe_ready": True,
            "distance_to_buy_pct": 2.0,
            "current_low": 102,
        }
        row = self._plan(
            [], item=lambda i: sig, offers=offers,
            open_strategies={1: {"strategy": "patient-probe"}},
        )["offer_triage"][0]
        self.assertEqual(row["verdict"], "hold")
        self.assertIn("never reprice upward", row["note"])

    def test_stale_sell_without_a_band_uses_the_live_bid(self) -> None:
        offers = [{"id": 1, "side": "sell", "qty": 10, "filled_qty": 0,
                   "price": 220, "age_hours": 6}]
        quote = lambda i: {"id": i, "name": "item1", "current_high": 180}
        row = self._plan([], quote=quote, offers=offers)["offer_triage"][0]
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)

    def test_buy_is_never_repriced_upward(self) -> None:
        offers = [{"id": 1, "side": "buy", "qty": 10, "filled_qty": 0,
                   "price": 90, "age_hours": 1}]
        row = self._plan([], item=lambda i: _sig(i, 100), offers=offers)["offer_triage"][0]
        self.assertEqual(row["verdict"], "hold")
        self.assertNotIn("new_price", row)
        self.assertIn("never reprice upward", row["note"])

    def test_filled_and_cancelled_slots_are_collect_actions(self) -> None:
        item = lambda i: _sig(i, 100, entry_price=100, exit_price=200)
        offers = [
            {"id": 1, "side": "buy", "qty": 1984, "filled_qty": 1984,
             "price": 8508, "state": "FILLED"},
            {"id": 2, "side": "sell", "qty": 100, "filled_qty": 20,
             "price": 200, "state": "CANCELLED"},
        ]
        p = self._plan([], item=item, offers=offers)

        self.assertEqual([o["verdict"] for o in p["offer_triage"]], ["collect", "collect"])
        self.assertIn("filled but uncollected", p["offer_triage"][0]["note"])
        self.assertIn("cancelled but uncollected", p["offer_triage"][1]["note"])
        self.assertEqual(p["sell_fills"][0]["qty"], 1984)
        self.assertEqual(p["sell_fills"][0]["price"], 200)

    def test_markdown_uses_one_stable_action_table(self) -> None:
        p = self._plan([_sig(1, 100, entry_price=100, exit_price=200, fillable=10)])
        md = plan._render_md(p)

        self.assertIn("## Actions", md)
        self.assertIn(
            "| action | item | qty | price | capital | exp. profit | live lo/hi | sell target | deadline | reason |",
            md,
        )
        self.assertIn("| **buy** | item1 | 10 | 100 | 1,000 | 1,000 | 100/200 | 200 |", md)
        self.assertNotIn("## Buy", md)

    def test_stale_sell_reprices_down_to_market(self) -> None:
        item = lambda i: {**_sig(i, 100, entry_price=100, exit_price=250), "current_high": 180}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 220,
                   "age_hours": 6, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers)
        row = p["offer_triage"][0]
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)
        self.assertIn("no fills for 6h", row["note"])

    def test_recent_partial_fill_keeps_sell_open(self) -> None:
        item = lambda i: {**_sig(i, 100, entry_price=100, exit_price=250), "current_high": 180}
        offers = [{
            "id": 1,
            "side": "sell",
            "qty": 10,
            "price": 220,
            "age_hours": 8,
            "filled_qty": 5,
            "last_fill_age_hours": 0.25,
        }]
        row = self._plan([], item=item, offers=offers)["offer_triage"][0]

        self.assertEqual(row["verdict"], "hold")

    def test_stale_sell_never_reprices_upward(self) -> None:
        item = lambda i: {**_sig(i, 100, entry_price=100, exit_price=250), "current_high": 240}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 220,
                   "age_hours": 18, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers)
        row = p["offer_triage"][0]
        self.assertEqual(row["verdict"], "hold")
        self.assertNotIn("new_price", row)
        self.assertIn("do not raise", row["note"])

    def test_fresh_sell_never_reprices_above_live_bid(self) -> None:
        # Regression: the band-top sell (250) is unreachable when the live bid
        # (current_high) sits below it. A fresh or partially-filled sell must clear
        # at the bid, not chase the band — the keel/vial/antifire failure.
        item = lambda i: {**_sig(i, 100, entry_price=100, exit_price=250), "current_high": 180}
        offers = [
            {"id": 1, "side": "sell", "qty": 10, "price": 230,        # above bid -> clear down
             "age_hours": 0.7, "filled_qty": 0},
            {"id": 2, "side": "sell", "qty": 10, "price": 181,        # already at bid -> hold
             "age_hours": 0.7, "filled_qty": 0},
            {"id": 3, "side": "sell", "qty": 10, "price": 230,        # partial fill, still stuck high
             "age_hours": 0.7, "filled_qty": 5},
        ]
        p = self._plan([], item=item, offers=offers)
        rows = {o["id"]: o for o in p["offer_triage"]}
        self.assertEqual(rows[1]["verdict"], "reprice")
        self.assertEqual(rows[1]["new_price"], 180)
        self.assertEqual(rows[2]["verdict"], "hold")
        self.assertEqual(rows[3]["new_price"], 180)
        # No reprice anywhere posts an ask above the live bid.
        for o in p["offer_triage"]:
            if o.get("verdict") == "reprice" and o.get("side") == "sell":
                self.assertLessEqual(o["new_price"], 180)

    def test_enforce_fillable_rejects_ask_above_bid(self) -> None:
        # The invariant is a hard guard: a sell reprice above the live bid is a
        # logic error and must raise, not ship into the GE.
        sig = {"id": 1, "current_high": 180}
        bad = {"id": 1, "side": "sell", "verdict": "reprice", "new_price": 250}
        with self.assertRaises(AssertionError):
            plan._enforce_fillable(bad, sig)
        ok = {"id": 1, "side": "sell", "verdict": "reprice", "new_price": 180}
        self.assertEqual(plan._enforce_fillable(ok, sig), ok)

    def test_enforce_fillable_allows_cost_floor_ask_above_bid(self) -> None:
        # A cost-floored break-even ask sits above the bid on purpose; the guard
        # must not treat it as the unfillable-ask logic error.
        sig = {"id": 1, "current_high": 180}
        floored = {"id": 1, "side": "sell", "verdict": "reprice",
                   "new_price": 205, "cost_floor": True}
        self.assertEqual(plan._enforce_fillable(floored, sig), floored)

    def test_triage_rows_carry_live_low_and_high_as_combined_markdown(self) -> None:
        item = lambda i: {**_sig(i, 100, entry_price=100, exit_price=250),
                          "current_low": 178, "current_high": 180}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 220,
                   "age_hours": 6, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers)
        row = p["offer_triage"][0]
        self.assertEqual(row["live_low"], 178)
        self.assertEqual(row["live_high"], 180)
        md = plan._render_md(p)
        self.assertIn("| 178/180 |", md)

    def test_floor_age_is_disclosed_in_the_triage_note(self) -> None:
        item = lambda i: {**_sig(i, 100, entry_price=100, exit_price=250), "current_high": 180}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 220,
                   "age_hours": 0.3, "filled_qty": 0, "age_is_floor": True}]
        p = self._plan([], item=item, offers=offers)
        row = p["offer_triage"][0]
        self.assertTrue(row["age_is_floor"])
        self.assertIn("age ≥0.3h", row["note"])
        self.assertIn("placement time unknown", row["note"])

    def test_below_cost_sell_reprices_down_to_break_even_not_the_bid(self) -> None:
        # Live bid (180) is below our cost (200): clearing down to it books a loss.
        # But holding a 210 ask when 205 recovers cost is a fantasy ask — lower it
        # to break-even (the cost floor), and quantify the clear-now alternative.
        item = lambda i: {**_sig(i, 100, entry_price=100, exit_price=250),
                          "current_high": 180, "trend": {"direction": "flat"}}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 210,
                   "age_hours": 1, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers, cost_map={1: 200})
        row = p["offer_triage"][0]
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 205)  # ceil(200 / 0.98)
        self.assertTrue(row["cost_floor"])
        self.assertIn("break-even 205", row["note"])
        self.assertIn("clear now at bid 180", row["note"])

    def test_below_cost_sell_already_at_break_even_holds(self) -> None:
        # Ask (205) already sits at the cost floor; there is nothing better to post,
        # so hold — the clear-now alternative stays quantified in the note.
        item = lambda i: {**_sig(i, 100, entry_price=100, exit_price=250),
                          "current_high": 180, "trend": {"direction": "flat"}}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 205,
                   "age_hours": 1, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers, cost_map={1: 200})
        row = p["offer_triage"][0]
        self.assertEqual(row["verdict"], "hold")
        self.assertNotIn("new_price", row)
        self.assertIn("break-even 205", row["note"])
        self.assertIn("clear now at bid 180", row["note"])

    def test_below_cost_sell_clears_at_the_hard_12h_stop(self) -> None:
        item = lambda i: {**_sig(i, 100, entry_price=100, exit_price=250),
                          "current_high": 180, "trend": {"direction": "flat"}}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 210,
                   "age_hours": 12, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers, cost_map={1: 200})
        row = p["offer_triage"][0]
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)
        self.assertIn("12h stop-loss", row["note"])

    def test_above_cost_sell_still_clears_to_market(self) -> None:
        # Cost (150) below the bid (180): clearing is profitable, so the guard stays out
        # of the way and the clamp-to-bid reprice proceeds.
        item = lambda i: {**_sig(i, 100, entry_price=100, exit_price=250),
                          "current_high": 180, "trend": {"direction": "flat"}}
        offers = [{"id": 1, "side": "sell", "qty": 10, "price": 230,
                   "age_hours": 1, "filled_qty": 0}]
        p = self._plan([], item=item, offers=offers, cost_map={1: 150})
        row = p["offer_triage"][0]
        self.assertEqual(row["verdict"], "reprice")
        self.assertEqual(row["new_price"], 180)

    def test_members_slots_allow_eight_offers(self) -> None:
        sigs = [_sig(i, 100 - i, entry_price=100, fillable=10) for i in range(1, 9)]
        p = self._plan(sigs)
        self.assertEqual(len(p["buys"]), 8)
        self.assertEqual(p["slots"]["max"], 8)

    def test_max_new_slots_caps_new_recommendations(self) -> None:
        sigs = [_sig(i, 100 - i, entry_price=100, fillable=10) for i in range(1, 9)]
        p = self._plan(sigs, max_new_slots=2)
        self.assertEqual(len(p["buys"]), 2)
        self.assertEqual(p["slots"]["new_slot_cap"], 2)

    def test_deployment_uses_full_liquid(self) -> None:
        p = self._plan([
            _sig(1, 100, entry_price=100, fillable=20_000, ge_limit=20_000),
        ])

        self.assertEqual(p["inputs"]["liquid_gp"], 1_000_000)
        self.assertEqual(p["buys"][0]["qty"], 10_000)
        self.assertEqual(p["deployment"]["planned_gp"], 1_000_000)
        self.assertEqual(p["deployment"]["utilization_pct"], 100.0)
        self.assertEqual(p["deployment"]["unspent_gp"], 0)
        self.assertIsNone(p["deployment"]["constraint"])

    def test_deployment_shortfall_reports_constraint_without_weak_trade(self) -> None:
        p = self._plan([_sig(1, 100, entry_price=100, fillable=50)])

        self.assertEqual(p["deployment"]["planned_gp"], 5_000)
        self.assertGreater(p["deployment"]["unspent_gp"], 0)
        self.assertIsNotNone(p["deployment"]["constraint"])

    def test_held_buy_escrow_is_reported_not_counted_as_deployment(self) -> None:
        # A fresh high-value untracked buy triages to hold; its 2m escrow was spent by a
        # previous run and must not push utilization of today's 1m liquid past 100%.
        held_buy = {"id": 9, "side": "buy", "qty": 1, "filled_qty": 0,
                    "price": 2_000_000, "age_hours": 0.1, "state": "ACTIVE"}
        p = self._plan(
            [_sig(1, 100, entry_price=100, fillable=20_000, ge_limit=20_000)],
            offers=[held_buy],
        )

        self.assertEqual(p["deployment"]["planned_gp"], 1_000_000)
        self.assertEqual(p["deployment"]["utilization_pct"], 100.0)
        self.assertEqual(p["deployment"]["held_buy_gp"], 2_000_000)
        self.assertIn("2,000,000gp already escrowed in held buys", plan._render_md(p))

    def test_collected_sell_proceeds_are_spendable_this_run(self) -> None:
        # A filled-but-uncollected sell frees its after-tax proceeds when the plan's own
        # collect instruction runs, so the budget and utilization base include them.
        filled_sell = {"id": 5, "side": "sell", "qty": 10, "filled_qty": 10,
                       "price": 1_000, "state": "FILLED"}
        p = self._plan(
            [_sig(1, 100, entry_price=100, fillable=20_000, ge_limit=20_000)],
            offers=[filled_sell],
        )

        self.assertEqual(p["projection"]["released_sell_gp"], 9_800)  # 10 * (1000 - 2% tax)
        self.assertEqual(p["deployment"]["available_gp"], 1_009_800)
        self.assertEqual(p["buys"][0]["qty"], 10_098)
        self.assertEqual(p["deployment"]["utilization_pct"], 100.0)

    def test_cash_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "cash is required"):
            plan.plan(cash=None, time_candidate_limit=0)



if __name__ == "__main__":
    unittest.main()
