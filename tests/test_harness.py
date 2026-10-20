import copy
import json
import time
import unittest

from lab.config import Settings
from lab.harness import Harness


def tool(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call-" + name, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class FakeModel:
    """Ответы только для тестирования harness, не режим учебного интерфейса."""
    def __init__(self, messages):
        self.responses = iter(messages)
        self.requests = []

    def complete(self, messages, tools, timeout):
        self.requests.append(copy.deepcopy(messages))
        result = next(self.responses)
        if callable(result):
            result = result()
        return result, {"prompt_tokens": 100, "completion_tokens": 20}


DONE = {"role": "assistant", "content": "Заявка готова."}


class HarnessTests(unittest.TestCase):
    def setup_session(self, replies, scenario="normal", limits=None):
        model = FakeModel(replies)
        harness = Harness(model, Settings(input_price=1, output_price=2))
        return harness, harness.new(scenario, "Оформить возврат по заказу 42", limits or {})

    def flow(self, order="42", amount=3200):
        return [tool("get_order", order_id=order), tool("prepare_refund", order_id=order),
                tool("create_refund", order_id=order, amount=amount)]

    def approve(self, harness, session):
        pending = copy.deepcopy(session.pending)
        harness.resume(session, {"pending_id": pending["id"], "decision": "approve", "args": pending["args"]})

    def test_no_write_before_approval_and_resume_same_call_once(self):
        h, s = self.setup_session(self.flow() + [DONE])
        h.run(s)
        self.assertEqual(s.status, "awaiting_approval")
        self.assertEqual(s.store.write_calls, 0)
        self.assertEqual(s.store.refunds, {})
        old = copy.deepcopy(s.pending)
        self.approve(h, s)
        h.run(s)
        self.assertEqual(s.status, "completed")
        self.assertEqual(s.store.write_calls, 1)
        with self.assertRaises(ValueError):
            h.resume(s, {"pending_id": old["id"], "decision": "approve", "args": old["args"]})
        self.assertEqual(s.store.write_calls, 1)

    def test_rejection_stops_even_if_model_would_try_again(self):
        h, s = self.setup_session(self.flow() + [tool("create_refund", order_id="42", amount=3200)])
        h.run(s)
        h.resume(s, {"pending_id": s.pending["id"], "decision": "reject"})
        h.run(s)
        self.assertEqual(s.status, "cancelled")
        self.assertEqual(s.store.write_calls, 0)
        self.assertEqual(s.turns, 3)

    def test_changed_amount_rejected_without_consuming_valid_confirmation(self):
        h, s = self.setup_session(self.flow())
        h.run(s)
        pending = copy.deepcopy(s.pending)
        with self.assertRaises(ValueError):
            h.resume(s, {"pending_id": pending["id"], "decision": "approve", "args": {"order_id": "42", "amount": 3300}})
        self.assertEqual(s.pending["id"], pending["id"])
        self.assertEqual(s.store.write_calls, 0)

    def test_expired_confirmation_and_changed_order_are_blocked(self):
        for change in ("expiry", "version"):
            with self.subTest(change=change):
                h, s = self.setup_session(self.flow())
                h.run(s)
                if change == "expiry":
                    s.pending["expires_at"] = time.time() - 1
                else:
                    s.store.orders["42"]["version"] += 1
                self.approve(h, s)
                self.assertEqual(s.status, "blocked")
                self.assertEqual(s.store.write_calls, 0)

    def test_business_limit_cannot_be_overridden_by_model(self):
        h, s = self.setup_session(self.flow("43", 6400), scenario="over_limit")
        h.run(s)
        self.assertEqual(s.status, "handoff")
        self.assertIsNone(s.pending)
        self.assertEqual(s.store.write_calls, 0)

    def test_false_done_is_not_success(self):
        h, s = self.setup_session(self.flow() + [DONE], scenario="validation_error")
        h.run(s)
        self.approve(h, s)
        h.run(s)
        self.assertEqual(s.status, "unverified")
        self.assertEqual(s.store.refunds["42"]["status"], "CHECKERROR")

    def test_unknown_outcome_blocks_second_write_until_status_read(self):
        h, s = self.setup_session(self.flow() + [tool("create_refund", order_id="42", amount=3200),
                  tool("get_refund_status", order_id="42"), DONE], scenario="unknown_result")
        h.run(s)
        self.approve(h, s)
        h.run(s)
        self.assertEqual(s.status, "completed")
        self.assertEqual(s.store.write_calls, 1)
        self.assertEqual(len(s.store.refunds), 1)

    def test_retry_limit_has_initial_attempt_plus_retries(self):
        h, s = self.setup_session([tool("get_order", order_id="42")] * 5, scenario="tool_failure", limits={"max_retries": 2})
        h.run(s)
        self.assertEqual(s.status, "handoff")
        self.assertEqual(s.tool_calls, 3)

    def test_budget_survives_human_pause(self):
        h, s = self.setup_session([tool("ask_user", question="Что сделать?")], limits={"max_turns": 1})
        h.run(s)
        h.resume(s, {"pending_id": s.pending["id"], "answer": "Вернуть деньги"})
        h.run(s)
        self.assertEqual(s.status, "budget_exceeded")
        self.assertEqual(s.turns, 1)

    def test_human_wait_not_counted_as_active_time(self):
        h, s = self.setup_session(self.flow())
        h.run(s)
        before = s.elapsed()
        time.sleep(0.03)
        self.assertEqual(s.elapsed(), before)

    def test_cost_cap_stops_before_tool_execution(self):
        h, s = self.setup_session([tool("get_order", order_id="42")], limits={"max_usd": 0.00001})
        h.run(s)
        self.assertEqual(s.status, "budget_exceeded")
        self.assertGreater(s.cost_usd, s.limits.max_usd)
        self.assertEqual(s.tool_calls, 0)

    def test_deadline_discards_late_model_proposal(self):
        h, s = self.setup_session([lambda: (setattr(s, "active_seconds", 2) or tool("get_order", order_id="42"))], limits={"active_seconds": 1})
        h.run(s)
        self.assertEqual(s.status, "budget_exceeded")
        self.assertEqual(s.tool_calls, 0)

    def test_untrusted_note_cannot_bypass_approval(self):
        h, s = self.setup_session(self.flow(), scenario="untrusted_note")
        h.run(s)
        self.assertEqual(s.status, "awaiting_approval")
        self.assertEqual(s.store.write_calls, 0)

    def test_response_after_cancel_is_accounted_but_not_executed(self):
        def response():
            h.cancel(s)
            return tool("get_order", order_id="42")
        h, s = self.setup_session([response])
        h.run(s)
        self.assertEqual(s.status, "cancelled")
        self.assertEqual(s.tool_calls, 0)
        self.assertGreater(s.cost_usd, 0)
        self.assertEqual(s.tokens, 120)

    def test_invalid_tool_is_not_executed(self):
        h, s = self.setup_session([tool("send_money", amount=100)])
        h.run(s)
        self.assertEqual(s.status, "failed")
        self.assertEqual(s.tool_calls, 0)

    def test_cancel_before_worker_starts_clears_busy(self):
        h, s = self.setup_session([])
        s.worker_running = True
        h.cancel(s)
        h.run(s)
        self.assertEqual(s.status, "cancelled")
        self.assertFalse(s.worker_running)


if __name__ == "__main__":
    unittest.main()
