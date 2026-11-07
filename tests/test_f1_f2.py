"""F1/F2 only. Fake HTTP gateway, local announcement file, controllable clock."""
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from test_gateway_sim import Sim, Clock, A, U, SP, DAY, REF, NAV
from test_notices_and_plan import PublicSource
from v12 import announcements as ann
from v12.config import CODES, digest
from v12.events import CN
from v12.execute import execute, NoticeBlocked, Planner
from v12.gate.order_loop import Stop
from v12.gate.trial import TrialAuthorization
from v12.publish import publish


class FixTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.plan = self.root / 'plan.json'
        self.notices = self.root / 'notices' / 'current.json'
        self.clock = Clock(14, 26, 5)
        self.addCleanup(mock.patch.stopall)
        mock.patch('v12.gate.trial.AUTH_ROOT', self.root / 'shared_authorizations').start()
        self.auth = TrialAuthorization('explicit-test-01', A, 'BUY')

    def prepare(self, sim):
        if not self.notices.exists():
            ann.save(self.notices, sim.notices)
        if not self.plan.exists():
            obj = sim.plan()
            state = ann.read(self.notices)
            obj['notices_digest'] = digest(state)
            obj['notice_terms'] = ann.terms_identity(state, REF, self.clock.now())
            obj.pop('plan_id')
            obj['plan_id'] = digest(obj)
            publish(self.plan, obj)

    def go(self, sim, mode='trial-send', *, name='run', auth=None):
        self.prepare(sim)
        return execute(self.plan, self.root / name, lambda: sim.client(mode != 'dry-run'),
                       mode=mode, now=self.clock.now, sleep=self.clock.sleep, notices=self.notices,
                       trial=(auth or self.auth) if mode == 'trial-send' else auth)

    def report(self, mode='send', name='run'):
        return ann.read(self.root / name / DAY / mode / 'report.json')

    def after_sleep(self, action):
        old = self.clock.sleep
        fired = False
        def sleep(seconds):
            nonlocal fired
            old(seconds)
            if not fired:
                fired = True
                action()
        self.clock.sleep = sleep

    def pending(self):
        state = ann.read(self.notices)
        state['pending'] = {'local-correction': {'reason': 'new correction needs human review'}}
        ann.save(self.notices, state)

    def public_source(self):
        src = PublicSource()
        ann.scan(self.notices, REF, '2026-11-03', src.fetch,
                 datetime(2026, 11, 4, 18, tzinfo=CN))
        return src

    def test_f1_ten_buy_targets_only_authorized_100_shares(self):
        sim = Sim(self.clock, {})
        sim.q = dict.fromkeys(CODES.values(), 0)
        sim.cash = NAV
        result = self.go(sim)
        self.assertEqual(result['status'], 'TRIAL_COMPLETE')
        self.assertEqual(result['scope'], 'ONE_DOMESTIC_ETF_ONE_ORDER')
        requests = [req for _, req in sim.submitted]
        self.assertEqual(len(requests), 1)
        self.assertEqual((requests[0]['stock_code'], requests[0]['direction'], requests[0]['volume']),
                         (A, 23, 100))
        self.assertEqual(result['reconciliation']['status'], 'RECONCILED')
        self.assertGreaterEqual(sim.cash, 100000)
        # Both same-folder and different-output-folder invocations only recover.
        self.assertEqual(self.go(sim)['status'], 'RECOVERED_REVIEW_REQUIRED')
        self.assertEqual(self.go(sim, name='elsewhere')['status'], 'RECOVERED_REVIEW_REQUIRED')
        self.assertEqual(len(sim.submitted), 1)

    def test_f1_explicit_sell_only_one_order_100(self):
        sim = Sim(self.clock, {A: 2000, CODES['gold']: -1000})
        result = self.go(sim, auth=TrialAuthorization('sell-01', A, 'SELL'))
        self.assertEqual(result['status'], 'TRIAL_COMPLETE')
        self.assertEqual([(b['stock_code'], b['direction'], b['volume']) for _, b in sim.submitted],
                         [(A, 24, 100)])

    def test_f1_untriggered_plan_not_forced(self):
        sim = Sim(self.clock, {})
        result = self.go(sim)
        self.assertEqual(result['status'], 'TRIAL_INCOMPLETE')
        self.assertEqual(result['message'], 'TRIAL_NOT_TRIGGERED_BY_PLAN')
        self.assertFalse(sim.submitted)

    def test_f1_sub_lot_residual_at_current_limit_not_rounded_up(self):
        code = CODES['csi1000']
        sim = Sim(self.clock, {code: -100})
        result = self.go(sim, auth=TrialAuthorization('sub-lot', code, 'BUY'))
        self.assertEqual(result['status'], 'TRIAL_INCOMPLETE')
        self.assertFalse(sim.submitted)
        self.assertEqual(result['legs'][code+'B']['reason'], 'TARGET_REACHED')

    def test_f1_requires_unauthorized_domestic_sale_skips(self):
        sim = Sim(self.clock, {A: -2000, CODES['gold']: 1000})
        result = self.go(sim)
        self.assertEqual(result['message'], 'TRIAL_REQUIRES_UNAUTHORIZED_DOMESTIC_SELL')
        self.assertFalse(sim.submitted)

    def test_f1_cash_floor_and_unconfirmed_qdii_sale_preserved(self):
        sim = Sim(self.clock, {A: -1000, U: 2000})
        sim.halted.add(U)
        before = sim.cash
        result = self.go(sim)
        self.assertFalse(sim.submitted)
        self.assertEqual(result['legs'][A+'B']['reason'], 'CASH_FLOOR_OR_MINIMUM_LOT')
        self.assertEqual(sim.cash, before)

    def test_f1_sellable_restriction_is_not_bypassed(self):
        sim = Sim(self.clock, {A: 1000})
        original = sim.open
        def no_sellable(req, timeout):
            response = original(req, timeout)
            if req.full_url.endswith('/positions'):
                data = json.loads(response.read())
                for row in data['positions']:
                    if row['stock_code'] == A:
                        row['can_use_volume'] = 0
                from test_gateway_sim import Response
                return Response(data)
            return response
        sim.open = no_sellable
        result = self.go(sim, auth=TrialAuthorization('sell-02', A, 'SELL'))
        self.assertFalse(sim.submitted)
        self.assertEqual(result['legs'][A+'S']['reason'], 'NO_SELLABLE_SHARES')

    def test_f1_partial_cancel_no_topup_after_restart_or_directory_change(self):
        sim = Sim(self.clock, {A: -1000})
        sim.hold.add(A)
        sim.partial[A] = 40
        result = self.go(sim)
        self.assertEqual(result['status'], 'TRIAL_INCOMPLETE')
        self.assertEqual(result['rows'][0]['filled'], 40)
        self.assertEqual(len(sim.cancels), 1)
        self.go(sim, name='new-output')
        self.assertEqual(len(sim.submitted), 1)
        self.assertEqual(sum(t['traded_volume'] for t in sim.history), 40)

    def test_f1_confirmed_refusal_does_not_get_new_attempt(self):
        sim = Sim(self.clock, {A: -1000})
        sim.behavior[A] = ['gateway busy with other orders; not sent, retry with a new request_id']
        result = self.go(sim)
        self.assertEqual(result['status'], 'TRIAL_INCOMPLETE')
        self.go(sim, name='new-output')
        self.assertEqual(len(sim.submitted), 1)
        self.assertFalse(sim.intents)

    def test_f1_lost_response_replays_identical_logical_order(self):
        sim = Sim(self.clock, {A: -1000})
        sim.behavior[A] = ['lost']
        result = self.go(sim)
        self.assertEqual(result['status'], 'TRIAL_COMPLETE')
        self.assertEqual(len(sim.submitted), 2)
        self.assertEqual(sim.submitted[0][1], sim.submitted[1][1])
        self.assertEqual(len(sim.intents), 1)
        self.assertEqual(sum(t['traded_volume'] for t in sim.history), 100)

    def test_f1_crash_before_ack_recovers_new_output_without_submit(self):
        sim = Sim(self.clock, {A: -1000})
        opener = sim.open
        def crash(req, timeout):
            answer = opener(req, timeout)
            if req.method == 'POST' and req.full_url.endswith('/orders'):
                raise KeyboardInterrupt('fixture process death after gateway accepted')
            return answer
        sim.open = crash
        with self.assertRaises(KeyboardInterrupt):
            self.go(sim)
        sim.open = opener
        result = self.go(sim, name='new-output')
        self.assertEqual(result['status'], 'RECOVERED_REVIEW_REQUIRED')
        self.assertEqual(len(sim.submitted), 1)
        self.assertEqual(result['reconciliation']['status'], 'RECONCILED')

    def test_f1_unknown_never_arrived_stops_without_new_id(self):
        sim = Sim(self.clock, {A: -1000})
        opener = sim.open
        def crash(req, timeout):
            if req.method == 'POST' and req.full_url.endswith('/orders'):
                raise KeyboardInterrupt('fixture process death before dispatch')
            return opener(req, timeout)
        sim.open = crash
        with self.assertRaises(KeyboardInterrupt):
            self.go(sim)
        sim.open = opener
        with self.assertRaisesRegex(Stop, 'Outcome unknown'):
            self.go(sim, name='new-output')
        self.assertFalse(sim.submitted)

    def test_f1_final_submit_rejects_scope_or_quantity_tampering(self):
        original = TrialAuthorization.make_request
        for mutation in ({'volume': 200}, {'stock_code': U}, {'direction': 24}):
            with self.subTest(mutation=mutation):
                sim = Sim(self.clock, {A: -1000})
                auth = TrialAuthorization('tamper-'+next(iter(mutation)), A, 'BUY')
                def tamper(obj, *args):
                    request, facts = original(obj, *args)
                    request.update(mutation)
                    return request, facts
                with mock.patch.object(TrialAuthorization, 'make_request', tamper):
                    with self.assertRaisesRegex(Stop, 'TRIAL_AUTHORIZATION_MISMATCH'):
                        self.go(sim, auth=auth, name=auth.authorization_id)
                self.assertFalse(sim.submitted)

    def test_f1_same_authorization_cannot_change_scope(self):
        sim = Sim(self.clock, {A: -1000})
        self.go(sim)
        with self.assertRaisesRegex(Stop, 'another plan/day'):
            self.go(sim, name='another', auth=TrialAuthorization(self.auth.authorization_id, A, 'SELL'))
        self.assertEqual(len(sim.submitted), 1)
        for code in (U, SP, '513300.SH'):
            with self.assertRaises(ValueError):
                TrialAuthorization('not-domestic', code, 'BUY')

    def test_f1_dry_run_does_not_consume_trial(self):
        sim = Sim(self.clock, {A: -1000})
        result = self.go(sim, 'dry-run', auth=self.auth)
        proposed = list(result['legs'].values())
        self.assertEqual(proposed[0]['proposed']['volume'], 100)
        self.assertFalse((self.auth.folder / 'execution_state.json').exists())
        self.assertTrue(all(method == 'GET' for method, _ in sim.calls))
        self.go(sim)
        self.assertEqual(len(sim.submitted), 1)

    def open_change(self, mode):
        self.clock.time = self.clock.now().replace(hour=9, minute=25, second=0)
        sim = Sim(self.clock, {A: -1000})
        self.prepare(sim)
        self.after_sleep(self.pending)
        with self.assertRaises(NoticeBlocked):
            self.go(sim, mode)
        self.assertFalse(sim.submitted)
        out = self.report(mode)
        self.assertEqual(out['status'], 'STOPPED')
        self.assertIn('NOTICE_BLOCKED', out['message'])
        self.assertFalse(out['candidates_valid'])
        self.assertEqual(out['legs'], {})

    def test_f2_wait_open_send_rechecks(self):
        self.open_change('send')

    def test_f2_wait_open_dry_run_rechecks_no_stale_candidates(self):
        self.open_change('dry-run')

    def test_f2_real_local_scanner_correction_while_waiting_reopen(self):
        sim = Sim(self.clock, {U: -2000})
        src = self.public_source()
        self.prepare(sim)
        sim.halted.add(U)
        def update():
            src.rows[A].append(src.notice('correction', '收益分配更正公告'))
            ann.scan(self.notices, REF, '2026-11-03', src.fetch, self.clock.now())
            sim.halted.discard(U)
        self.after_sleep(update)
        with self.assertRaises(NoticeBlocked):
            self.go(sim, 'send')
        self.assertEqual(len(ann.read(self.notices)['pending']), 1)
        self.assertFalse(sim.submitted)
        self.assertEqual(self.report()['status'], 'STOPPED')

    def test_f2_same_terms_non_economic_notice_allows_reopen(self):
        sim = Sim(self.clock, {U: -2000})
        src = self.public_source()
        self.prepare(sim)
        before_terms = ann.terms_identity(ann.read(self.notices), REF, self.clock.now())
        sim.halted.add(U)
        def update():
            src.rows[A].append(src.notice('ordinary', '例行报告'))
            ann.scan(self.notices, REF, '2026-11-03', src.fetch, self.clock.now())
            sim.halted.discard(U)
        self.after_sleep(update)
        result = self.go(sim, 'send')
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(ann.terms_identity(ann.read(self.notices), REF, self.clock.now()), before_terms)
        self.assertEqual(len(sim.submitted), 1)
        self.assertEqual(sim.submitted[0][1]['volume'], 1900)

    def test_f2_file_missing_incomplete_or_changed_terms_block(self):
        mutations = ['missing', 'invalid_json', 'incomplete', 'missing_code', 'changed_terms']
        for name in mutations:
            with self.subTest(name=name):
                sim = Sim(self.clock, {A: -1000})
                ann.save(self.notices, sim.notices)
                self.prepare(sim)
                original = Planner.make_request
                def mutate(planner, *args):
                    result = original(planner, *args)
                    state = ann.read(self.notices)
                    if name == 'missing':
                        self.notices.unlink()
                    elif name == 'invalid_json':
                        self.notices.write_text('{broken', encoding='utf8')
                    else:
                        if name == 'incomplete': state['status'] = 'INCOMPLETE'
                        if name == 'missing_code': state['codes'] = state['codes'][:-1]
                        if name == 'changed_terms':
                            state['decisions']['amendment'] = dict(
                                reviewed_at=self.clock.now().isoformat(),
                                event=dict(key='csi300', record_date=REF, ex_date=DAY,
                                           payment_date='2026-11-06', cash_per_actual_share=.1))
                        ann.save(self.notices, state)
                    return result
                with mock.patch.object(Planner, 'make_request', mutate):
                    with self.assertRaises(NoticeBlocked):
                        self.go(sim, 'send', name=name)
                self.assertFalse(sim.submitted)
                self.assertEqual(self.report(name=name)['status'], 'STOPPED')

    def test_f2_dry_run_rechecks_after_computing_proposal(self):
        sim = Sim(self.clock, {A: -1000})
        original = Planner.make_request
        def mutate(planner, *args):
            result = original(planner, *args)
            self.pending()
            return result
        with mock.patch.object(Planner, 'make_request', mutate):
            with self.assertRaises(NoticeBlocked):
                self.go(sim, 'dry-run')
        out = self.report('dry-run')
        self.assertEqual(out['legs'], {})
        self.assertTrue(all(m == 'GET' for m, _ in sim.calls))

    def test_f2_after_confirmed_refusal_no_new_attempt(self):
        sim = Sim(self.clock, {A: -1000})
        sim.behavior[A] = ['gateway busy with other orders; not sent, retry with a new request_id']
        self.prepare(sim)
        self.after_sleep(self.pending)
        with self.assertRaises(NoticeBlocked):
            self.go(sim, 'send')
        self.assertEqual(len(sim.submitted), 1)
        self.assertEqual(self.report()['reconciliation']['status'], 'RECONCILED')

    def test_f2_lost_response_after_actual_fill_no_replay_and_book_fill(self):
        sim = Sim(self.clock, {A: -1000})
        sim.behavior[A] = ['lost']
        self.prepare(sim)
        self.after_sleep(self.pending)
        with self.assertRaises(NoticeBlocked):
            self.go(sim)
        self.assertEqual(len(sim.submitted), 1)
        out = self.report('trial-send')
        self.assertEqual(out['status'], 'STOPPED')
        self.assertEqual(out['rows'][0]['filled'], 100)
        self.assertEqual(out['reconciliation']['status'], 'RECONCILED')
        self.assertIn('NOTICE_BLOCKED', out['message'])
        # Changing output folder while notices remain blocked is query/cleanup only.
        with self.assertRaises(NoticeBlocked):
            self.go(sim, name='another')
        self.assertEqual(len(sim.submitted), 1)

    def test_f2_transport_never_arrived_no_post_to_find_out(self):
        sim = Sim(self.clock, {A: -1000})
        opener = sim.open
        posts = []
        def absent(req, timeout):
            if req.method == 'POST' and req.full_url.endswith('/orders'):
                posts.append(json.loads(req.data))
                raise URLError('fixture transport failed before server arrival')
            return opener(req, timeout)
        sim.open = absent
        self.prepare(sim)
        self.after_sleep(self.pending)
        with self.assertRaises(NoticeBlocked):
            self.go(sim)
        self.assertEqual(len(posts), 1)
        self.assertFalse(sim.intents)
        out = self.report('trial-send')
        self.assertIn('Outcome unknown', out['cleanup_message'])
        self.assertEqual(len(out['requests']), 1)
        self.assertEqual(next(iter(out['requests'].values()))['request'], posts[0])

    def test_f2_partial_working_order_can_be_closed_without_new_submit(self):
        sim = Sim(self.clock, {U: 1000, SP: -2000})
        sim.hold.add(U)
        sim.partial[U] = 100
        sim.halted.add(SP)
        self.prepare(sim)
        self.after_sleep(self.pending)
        with self.assertRaises(NoticeBlocked):
            self.go(sim, 'send')
        self.assertEqual(len(sim.submitted), 1)
        self.assertEqual(len(sim.cancels), 1)
        out = self.report()
        self.assertEqual(out['status'], 'STOPPED')
        self.assertEqual(out['rows'][0]['filled'], 100)
        self.assertEqual(out['reconciliation']['status'], 'RECONCILED')
        self.assertTrue(all(i['state'] == 'DONE' for i in sim.intents.values()))

    def test_f2_unknown_cancel_keeps_identity_and_unsettled_report(self):
        sim = Sim(self.clock, {U: 1000, SP: -2000})
        sim.hold.add(U)
        sim.partial[U] = 100
        sim.halted.add(SP)
        sim.refuse_cancel = True
        self.prepare(sim)
        self.after_sleep(self.pending)
        with self.assertRaises(NoticeBlocked):
            self.go(sim, 'send')
        out = self.report()
        self.assertEqual(len(sim.submitted), 1)
        self.assertEqual(len(sim.cancels), 1)
        self.assertEqual(out['status'], 'STOPPED')
        self.assertEqual(out['reconciliation']['status'], 'UNSETTLED')
        entry = next(iter(out['requests'].values()))
        self.assertTrue(entry['intent_id'])
        self.assertIn('cancel_requested_at', entry)
        self.assertEqual(entry['last']['filled'], 100)


if __name__ == '__main__':
    unittest.main()
