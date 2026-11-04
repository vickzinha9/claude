"""Tests of the real execution with a FAKE platform: no browser, no account, no money.

FakePlatform stands for the Pocket Option page: the order panel that TradingSession reads and
fills, and the socket.io frames that the page sends and receives, in the platform's own format
(checked on its demo account on 2026-11-04). The frames go through the real SocketMonitor and
the orders through the real PocketBroker, OrderExecutor, Coordinator and Operator. Nothing
here reaches the Pocket Option: passing says that the rules hold, not that the platform
accepted an order.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pocketautonomo import analysis, arming, operation
from pocketautonomo.broker import ORDER_ACCEPTED, ORDER_REJECTED, OrderRequest
from pocketautonomo.coordinator import Coordinator
from pocketautonomo.cycle import ABANDONED, CALL, LOSS, PUT, REJECTED, UNKNOWN, WIN
from pocketautonomo.operation import OperationLog, Operator, SessionLimits
from pocketautonomo.pocket_broker import (
    DEMO,
    REAL,
    PocketBroker,
    PocketBrokerError,
    SubmissionUncertain,
    account_of,
    money_number,
    read_orders,
    stake_text,
)
from pocketautonomo.pocket_socket import SocketMonitor, asset_code, order_result, same_asset
from pocketautonomo.storage import SqliteStore

START = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
ASSET = "EUR/USD OTC"


class Clock:
    def __init__(self):
        self.t = START
        self.m = 5000.0

    def now(self):
        return self.t

    def monotonic(self):
        return self.m

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)
        self.m += seconds


def binary_event(monitor, name, data):
    monitor.received(f'451-["{name}",{{"_placeholder":true,"num":0}}]')
    monitor.received(json.dumps(data).encode("utf-8"))


class FakePlatform:
    """The order panel and the socket of the platform's page, as TradingSession sees them."""

    def __init__(self, clock, monitor, account="QT Real", balance=1000.0):
        self.clock = clock
        self.monitor = monitor
        self.is_demo = 1 if "Demo" in account else 0
        self.balance = balance
        self.state = {
            "asset": ASSET, "account": account, "balance": f"R${balance:,.2f}", "expiration": "00:01:00",
            "amount": "10", "payout": "+92%", "call": 1, "put": 1, "modal": 0,
        }
        self.mode = "accept"  # accept, refuse, silent, nothing, dead, wrong_amount, balance_off
        self.expiration_works = True
        self.clicks = []  # (direction, amount shown) of every click on Buy or Sell
        self.typed = []
        self.request_id = 9000
        self.open = {}  # order id -> deal
        self.payout = 92

    # TradingSession's methods.

    def panel(self):
        self.stream()
        return dict(self.state)

    def set_expiration(self):
        if self.expiration_works:
            self.state["expiration"] = "00:05:00"

    def set_amount(self, text):
        self.typed.append(text)
        self.state["amount"] = text

    def pump(self, milliseconds):
        self.clock.m += milliseconds / 1000
        if self.mode != "dead":
            self.stream()

    def click_direction(self, direction):
        amount = money_number(self.state["amount"])
        self.clicks.append((direction, amount))
        if self.mode == "nothing":
            return
        self.request_id += 1
        sent_amount = amount * 2 if self.mode == "wrong_amount" else amount
        request = {
            "asset": asset_code(self.state["asset"]), "amount": sent_amount, "action": direction.lower(),
            "isDemo": self.is_demo, "requestId": self.request_id, "optionType": 100, "time": 300,
        }
        self.monitor.sent("42" + json.dumps(["openOrder", request]))
        if self.mode in ("silent", "dead"):
            return
        if self.mode == "refuse":
            binary_event(self.monitor, "failopenOrder", {"asset": request["asset"], "amount": sent_amount, "error": "not_money"})
            return
        self.balance -= sent_amount + (1 if self.mode == "balance_off" else 0)
        binary_event(self.monitor, "successupdateBalance", {"isDemo": self.is_demo, "balance": self.balance})
        order_id = f"PO{self.request_id}"
        deal = {
            "id": order_id, "openTimestamp": 1793772475, "closeTimestamp": 1793772775, "isDemo": self.is_demo,
            "amount": sent_amount, "profit": sent_amount * self.payout / 100, "percentProfit": self.payout,
            "openPrice": 1.11451, "closePrice": 0, "command": 0 if direction == CALL else 1,
            "asset": request["asset"], "requestId": self.request_id, "currency": "BRL",
        }
        self.open[order_id] = deal
        binary_event(self.monitor, "successopenOrder", deal)

    # The platform's side.

    def stream(self):
        self.monitor.received('451-["updateStream",{"_placeholder":true,"num":0}]')
        self.monitor.received(b'[["EURUSD_otc",1793772466.242,1.11442]]')

    def close(self, order_id, result, prices=True):
        deal = dict(self.open.pop(order_id))
        up = (result == WIN) == (deal["command"] == 0)
        if result == "DRAW":
            deal["closePrice"] = deal["openPrice"]
            profit = 0
        else:
            deal["closePrice"] = round(deal["openPrice"] + (0.0003 if up == prices else -0.0003), 5)
            profit = deal["amount"] * self.payout / 100 if result == WIN else -deal["amount"]
        deal["profit"] = profit
        if result != LOSS:
            self.balance += deal["amount"] + profit
        binary_event(self.monitor, "successupdateBalance", {"isDemo": self.is_demo, "balance": self.balance})
        binary_event(self.monitor, "successcloseOrder", {"profit": profit, "deals": [deal]})


class RealTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.clock = Clock()
        self.monitor = SocketMonitor(self.clock.monotonic)
        self.platform = FakePlatform(self.clock, self.monitor)
        self.switch = arming.ArmSwitch(self.directory, "real")
        self.switch.arm("teste")
        self.broker = PocketBroker(
            self.directory / "operacao-real-ordens.db", account=REAL, armed=self.switch, session=self.platform,
            monitor=self.monitor, now=self.clock.now, monotonic=self.clock.monotonic,
        )
        self.addCleanup(self.broker.close)
        self.count = 0

    def request(self, stake=500, direction=CALL, deadline=5.0, asset=ASSET):
        self.count += 1
        until = None if deadline is None else self.clock.now() + timedelta(seconds=deadline)
        return OrderRequest(f"r{self.count}", asset, direction, stake, until)


class SocketTest(unittest.TestCase):
    def test_the_platform_messages_are_read_with_their_attachments(self):
        clock = Clock()
        monitor = SocketMonitor(clock.monotonic)
        mark = monitor.mark()
        monitor.sent('42["openOrder",{"asset":"EURUSD_otc","amount":5,"action":"put","isDemo":1,"requestId":7,"time":300}]')
        binary_event(monitor, "updateStream", [["EURUSD_otc", 1, 1.1]])  # dropped
        binary_event(monitor, "successupdateBalance", {"isDemo": 1, "balance": 49990})
        binary_event(monitor, "successopenOrder", {"id": "abc", "requestId": 7, "amount": 5, "command": 1})
        self.assertEqual(monitor.open_request(mark).data["requestId"], 7)
        self.assertEqual(monitor.opened(7, mark).data["id"], "abc")
        self.assertIsNone(monitor.opened(8, mark))
        self.assertEqual(monitor.balance(1), 49990.0)
        self.assertEqual(monitor.balance_after(1, mark), 49990.0)
        self.assertEqual([event.name for event in monitor.events()], ["openOrder", "successupdateBalance", "successopenOrder"])
        later = monitor.mark()
        self.assertIsNone(monitor.opened(7, later))  # an old answer never answers a new order
        binary_event(monitor, "successcloseOrder", {"profit": -5, "deals": [{"id": "abc", "profit": -5}]})
        self.assertEqual(monitor.close_of("abc")["profit"], -5)

    def test_the_asset_names_and_the_results(self):
        self.assertEqual(asset_code("EUR/USD OTC"), "EURUSD_otc")
        self.assertTrue(same_asset("EUR/USD OTC", "EURUSD_otc"))
        self.assertFalse(same_asset("EUR/USD", "EURUSD_otc"))
        self.assertEqual(order_result({"profit": -5, "command": 0, "openPrice": 1.11423, "closePrice": 1.11391}), ("LOSS", None))
        self.assertEqual(order_result({"profit": 4.6, "command": 1, "openPrice": 1.2, "closePrice": 1.1})[0], "WIN")
        self.assertEqual(order_result({"profit": 0, "command": 1, "openPrice": 1.2, "closePrice": 1.2})[0], "DRAW")
        result, problem = order_result({"profit": 4.6, "command": 0, "openPrice": 1.2, "closePrice": 1.1})
        self.assertIsNone(result)  # the prices contradict the profit: no result is taken
        self.assertIn("dizem LOSS", problem)

    def test_money_texts_and_accounts(self):
        self.assertEqual(money_number("$49,990"), 49990.0)
        self.assertEqual(money_number("R$ 1.234,56"), 1234.56)
        self.assertEqual(money_number("R$1,234.56"), 1234.56)
        self.assertEqual(money_number("20,000"), 20000.0)
        self.assertEqual(money_number("5"), 5.0)
        self.assertEqual(money_number("7,5"), 7.5)
        self.assertIsNone(money_number("abc"))
        self.assertEqual((account_of("QT Real"), account_of("QT Demo"), account_of("")), (REAL, DEMO, None))
        self.assertEqual([stake_text(stake) for stake in (500, 1000, 2000, 4000)], ["5", "10", "20", "40"])


class SubmissionTest(RealTestCase):
    def test_each_level_sends_its_stake_at_300_seconds_and_waits_for_the_account(self):
        for stake in (500, 1000, 2000, 4000):
            response = self.broker.submit_order(self.request(stake))
            self.assertEqual(response.status, ORDER_ACCEPTED, response)
            order = self.broker.order(response.broker_order_id)
            self.assertEqual((order.state, order.stake_cents, order.duration_s), ("OPEN", stake, 300))
            self.assertEqual(order.balance_before_cents - stake, order.balance_after_open_cents)
            sent = json.loads(order.sent_json)
            self.assertEqual((sent["amount"] * 100, sent["time"], sent["action"]), (stake, 300, "call"))
        self.assertEqual([amount for _, amount in self.platform.clicks], [5, 10, 20, 40])
        self.assertEqual(self.platform.state["expiration"], "00:05:00")
        self.assertIsNone(self.broker.fault)

    def test_nothing_is_clicked_when_a_check_fails(self):
        cases = {
            "desarmada": lambda: self.switch.disarm(),
            "conta demo": lambda: self.platform.state.update(account="QT Demo"),
            "outro ativo": lambda: self.platform.state.update(asset="GBP/USD OTC"),
            "tempo": lambda: setattr(self.platform, "expiration_works", False),
            "socket mudo": lambda: setattr(self.platform, "mode", "dead"),
        }
        for name, breaking in cases.items():
            with self.subTest(name):
                self.setUp()
                breaking()
                if name == "socket mudo":
                    self.clock.advance(10)
                    self.platform.panel = lambda: dict(self.platform.state)  # no stream at all
                response = self.broker.submit_order(self.request())
                self.assertEqual(response.status, ORDER_REJECTED, name)
                self.assertEqual(self.platform.clicks, [], name)
        self.setUp()
        late = self.request(deadline=-0.1)
        response = self.broker.submit_order(late)
        self.assertEqual(response.status, ORDER_REJECTED)
        self.assertIn("prazo da entrada vencido", response.reason)
        response = self.broker.submit_order(self.request(stake=800))
        self.assertIn("fora da tabela", response.reason)
        self.platform.balance = 3.0
        self.platform.state["balance"] = "R$3.00"
        self.assertIn("não cobre", self.broker.submit_order(self.request()).reason)
        self.assertEqual(self.platform.clicks, [])

    def test_a_refusal_and_a_click_that_sent_nothing_are_rejections(self):
        self.platform.mode = "refuse"
        response = self.broker.submit_order(self.request())
        self.assertEqual(response.status, ORDER_REJECTED)
        self.assertIn("not_money", response.reason)
        self.platform.mode = "nothing"
        response = self.broker.submit_order(self.request())
        self.assertEqual(response.status, ORDER_REJECTED)
        self.assertIn("não enviou", response.reason)

    def test_no_answer_is_uncertain_and_never_confirmed_absent(self):
        self.platform.mode = "silent"
        request = self.request()
        with self.assertRaises(SubmissionUncertain):
            self.broker.submit_order(request)
        self.assertEqual(self.broker.orders()[0].state, UNKNOWN)
        self.assertIsNone(self.broker.find_order_for(request.reservation_id))
        self.assertFalse(self.broker.confirm_absent(request))
        with self.assertRaises(PocketBrokerError):
            self.broker.submit_order(request)  # never twice for one reservation
        self.assertEqual(len(self.platform.clicks), 1)

    def test_a_late_acceptance_is_found_by_its_request_id(self):
        self.platform.mode = "silent"
        request = self.request()
        with self.assertRaises(SubmissionUncertain):
            self.broker.submit_order(request)
        sent = self.monitor.events("openOrder")[-1].data
        binary_event(self.monitor, "successopenOrder", {
            "id": "LATE1", "requestId": sent["requestId"], "amount": 5, "command": 0, "asset": "EURUSD_otc",
            "isDemo": 0, "openTimestamp": 0, "closeTimestamp": 300, "openPrice": 1.2, "percentProfit": 92,
        })
        found = self.broker.find_order_for(request.reservation_id)
        self.assertEqual((found.order_id, found.state), ("LATE1", "OPEN"))

    def test_an_order_unlike_the_request_or_a_wrong_balance_stops_new_orders(self):
        self.platform.mode = "wrong_amount"
        response = self.broker.submit_order(self.request())
        self.assertEqual(response.status, ORDER_ACCEPTED)  # it is on the account: it is followed
        self.assertIn("diferente", self.broker.fault)
        self.platform.mode = "accept"
        self.assertIn("execução parada", self.broker.submit_order(self.request()).reason)
        self.setUp()
        self.platform.mode = "balance_off"
        self.broker.submit_order(self.request())
        self.assertIn("saldo", self.broker.fault)

    def test_the_result_comes_from_the_account_and_must_agree_with_the_prices(self):
        response = self.broker.submit_order(self.request(direction=PUT))
        order_id = response.broker_order_id
        self.assertEqual(self.broker.get_order(order_id).state, "OPEN")
        self.clock.advance(300.2)
        self.broker.observe(ASSET, self.clock.now(), 1.1149, "1.11490")
        self.platform.close(order_id, WIN)
        self.assertEqual(self.broker.get_order(order_id).state, WIN)
        order = self.broker.order(order_id)
        self.assertEqual(order.profit_cents, 460)
        self.assertEqual(order.screen_exit_quote, "1.11490")
        self.assertIn("divergência", order.note)  # the screen said up, a PUT would lose: written, not used
        response = self.broker.submit_order(self.request())
        self.platform.close(response.broker_order_id, LOSS, prices=False)
        with self.assertRaises(PocketBrokerError):
            self.broker.get_order(response.broker_order_id)
        response = self.broker.submit_order(self.request())
        self.clock.advance(300 + 91)
        with self.assertRaises(PocketBrokerError):
            self.broker.get_order(response.broker_order_id)  # no result in time: nothing is made up
        self.assertEqual(len(read_orders(self.directory / "operacao-real-ordens.db")), 3)


class RealOperationTest(RealTestCase):
    """The Operator with the real broker: the progression, the gates and the session's limit."""

    def setUp(self):
        super().setUp()
        self.coordinator = Coordinator(SqliteStore(self.directory / "operacao-real.db"))
        self.addCleanup(self.coordinator.close)
        self.lines = []
        self.log = OperationLog(self.directory / "operacao-real-20261104-teste.csv", operation.REAL, say=self.lines.append)
        self.addCleanup(self.log.close)
        self.operator = Operator(
            self.coordinator, self.broker, self.log, now=self.clock.now, monotonic=self.clock.monotonic,
            gate=self.switch.gate, limits=SessionLimits(),
        )
        self.operator.recover()
        self.price = 1.1

    def decision(self, action=CALL, age=0.5, payout=92.0):
        self.clock.advance(30)
        self.price = round(self.price + 0.001, 5)
        at = self.clock.now() - timedelta(seconds=age)
        sample = analysis.Sample(at, self.price, f"{self.price:.5f}", payout)
        validation = analysis.Validation(True, "regra de teste (artificial)", analysis.Rule(analysis.TREND, 1))
        if action == analysis.WAIT:
            return analysis.Decision(ASSET, action, "sem regra", validation, sample)
        return analysis.Decision(ASSET, action, "regra", validation, sample, at + timedelta(seconds=5))

    def settle(self, result):
        order_id = self.coordinator.status().broker_order_id
        self.clock.advance(300.5)
        self.platform.close(order_id, result)
        self.operator.observe(ASSET, self.clock.now(), self.price, f"{self.price:.5f}", 92.0, 50.0)

    def read(self):
        self.clock.advance(0.5)
        self.operator.observe(ASSET, self.clock.now(), self.price, f"{self.price:.5f}", 92.0, 50.0)

    def test_aguardar_bad_data_and_disarmed_never_send(self):
        self.assertFalse(self.operator.consider(self.decision(analysis.WAIT)).started)
        self.assertFalse(self.operator.consider(self.decision(age=6.0)).started)  # expired quote
        self.assertFalse(self.operator.consider(self.decision(payout=80.0)).started)
        self.switch.disarm()
        refused = self.operator.consider(self.decision())
        self.assertFalse(refused.started)
        self.assertIn("desarmada", refused.reason)
        arming.set_pause(self.directory, True)
        self.switch.arm("teste")
        self.assertIn("pausa", self.operator.consider(self.decision()).reason)
        self.assertEqual(self.platform.clicks, [])

    def test_four_confirmed_losses_send_5_10_20_40_once_each_and_end_the_session(self):
        started = self.operator.consider(self.decision(PUT))
        self.assertTrue(started.started, started)
        for attempt in range(4):
            self.settle(LOSS)
            if attempt < 3:
                self.read()
        self.assertEqual(self.platform.clicks, [("PUT", 5), ("PUT", 10), ("PUT", 20), ("PUT", 40)])
        status = self.coordinator.status()
        self.assertEqual((status.is_active, status.outcome), (False, LOSS))
        self.assertEqual(self.operator.session["net_cents"], -7500)
        refused = self.operator.consider(self.decision())
        self.assertFalse(refused.started)
        self.assertIn("limite da sessão", refused.reason)
        self.assertEqual(len(self.platform.clicks), 4)  # never a fifth order
        results = [row for row in self.log.rows if row["event"] == "resultado da conta"]
        self.assertEqual([row["profit_cents"] for row in results], [-500, -1000, -2000, -4000])
        self.assertEqual([row["session_net_cents"] for row in results], [-500, -1500, -3500, -7500])
        self.assertTrue(all(row["execution"] == operation.REAL and row["real_order_id"] for row in results))

    def test_a_win_ends_the_cycle_and_the_next_one_starts_at_5(self):
        self.operator.consider(self.decision())
        self.settle(LOSS)
        self.read()
        self.settle(WIN)
        self.assertEqual(self.coordinator.status().outcome, WIN)
        self.assertEqual(self.operator.session["net_cents"], -500 + 920)
        self.assertTrue(self.operator.consider(self.decision()).started)
        self.assertEqual([amount for _, amount in self.platform.clicks], [5, 10, 5])

    def test_no_gale_without_a_confirmed_result_and_a_refused_gale_ends_the_cycle(self):
        self.operator.consider(self.decision())
        self.clock.advance(300.5)
        self.read()  # the expiry passed, the account has not answered: nothing advances
        self.assertEqual(len(self.platform.clicks), 1)
        self.assertEqual(self.operator.state(), operation.PENDING_ENTRY)
        order_id = self.coordinator.status().broker_order_id
        self.platform.close(order_id, LOSS)
        self.platform.mode = "refuse"
        self.read()  # LOSS confirmed: the gale's window opens, and its entry is refused
        self.assertEqual(len(self.platform.clicks), 2)
        status = self.coordinator.status()
        self.assertEqual((status.is_active, status.outcome), (False, REJECTED))  # a refused gale never skips a level
        self.platform.mode = "accept"
        self.assertTrue(self.operator.consider(self.decision()).started)
        self.assertEqual([amount for _, amount in self.platform.clicks], [5, 10, 5])


    def test_the_gale_enters_with_a_quote_read_after_the_expiry_while_the_chart_hides_the_price(self):
        # As on 2026-11-04 at 19:55: a reading 0.6 s after the expiry, the LOSS from the account a moment later,
        # and then no reading for 7 s, the closed deal covering the price on the chart.
        self.operator.consider(self.decision())
        order_id = self.coordinator.status().broker_order_id
        self.clock.advance(299.0)
        self.read()  # before the expiry: not a quote of after the order
        self.clock.advance(1.0)
        self.platform.close(order_id, LOSS)
        self.operator.tick()  # no reading after the expiry: the account's closing price is the fresh quote
        self.assertEqual(self.platform.clicks, [("CALL", 5), ("CALL", 10)])
        [opened] = [row for row in self.log.rows if row["event"] == operation.WINDOW_OPENED]
        self.assertIn("fechamento da conta", opened["reason"])
        self.read()
        self.assertEqual(self.platform.clicks, [("CALL", 5), ("CALL", 10)])  # never twice

    def test_a_quote_read_after_the_expiry_but_too_old_lets_the_gale_window_expire(self):
        self.operator.consider(self.decision())
        order_id = self.coordinator.status().broker_order_id
        self.clock.advance(300.5)
        self.read()  # after the expiry
        self.clock.advance(5.5)  # the account answers late: that quote is 6 s old
        self.platform.close(order_id, LOSS)
        self.operator.tick()
        self.assertEqual(len(self.platform.clicks), 1)
        self.clock.advance(5.1)
        self.operator.tick()
        self.assertEqual(self.coordinator.status().outcome, ABANDONED)
        self.assertEqual(len(self.platform.clicks), 1)  # never a late gale

if __name__ == "__main__":
    unittest.main()


class RealStatsTest(RealOperationTest):
    def test_the_real_sample_is_counted_apart_with_the_account_profits(self):
        from pocketautonomo import stats

        self.operator.consider(self.decision(PUT))
        self.settle(LOSS)
        self.read()
        self.settle(WIN)
        self.log.close()
        reports, _, _ = stats.build(self.directory)
        simulated, real, demo = reports
        self.assertFalse(simulated.present or demo.present)  # never mixed with the other samples
        self.assertTrue(real.present)
        self.assertEqual((real.total.orders, real.total.wins, real.total.losses), (2, 1, 1))
        self.assertEqual(real.total.net_cents, -500 + 920)
        self.assertEqual(sorted(real.by_level), [1, 2])
        self.assertEqual([(row.loss_before_cents, row.cycle_net_cents) for row in real.attempts], [(0, -500), (500, 420)])
        self.assertEqual(real.payout_mean, 92)
        self.assertEqual(real.latency["real_submit_ms"]["n"], 2)
        report = stats.text(reports, {}, [])
        self.assertIn("Amostra insuficiente", report)
        self.assertIn("IC95%", report)


class PanelFailureTest(RealTestCase):
    def test_a_panel_that_fails_or_is_covered_before_the_click_is_an_explicit_refusal(self):
        from pocketautonomo.trading_session import TradingError

        def broken():
            raise TradingError("o tempo da ordem não foi escolhido (Timeout)")

        self.platform.set_expiration = broken  # as with the platform's tutorial over the panel
        response = self.broker.submit_order(self.request())
        self.assertEqual(response.status, ORDER_REJECTED)
        self.assertIn("nada foi clicado", response.reason)
        self.assertEqual(self.broker.orders()[-1].state, REJECTED)
        self.platform.state.update(expiration="00:05:00", covering=1)
        response = self.broker.submit_order(self.request())
        self.assertIn("tutorial", response.reason)
        self.assertEqual(self.platform.clicks, [])
