import contextlib
import io
import json
import os
import time
import unittest
from unittest.mock import patch

from telemetry import PREFIX, TelemetryStore, emit


def event(stage, status='complete', trace='first', source='quinn', **metrics):
    return dict(source=source, stage=stage, status=status, trace=trace,
                time=time.time(), detail='Isolated measurement', metrics=metrics)


class RunHistoryTests(unittest.TestCase):
    def setUp(self):
        self.store = TelemetryStore()

    def record(self, trace, status, stamp, source='quinn', **metrics):
        from telemetry import HISTORY_STAGE
        self.store.record({**event(HISTORY_STAGE[source], status, trace, source, **metrics), 'time': stamp})

    def runs(self, source='quinn'):
        from dashboard import recent_runs
        return recent_runs(self.store.snapshot(source), source)

    def test_history_keeps_attempts_ordered_and_prefers_measured_duration(self):
        from dashboard import stage_duration
        for source in ('quinn', 'sniper'):
            with self.subTest(source=source):
                self.record('first', 'start', 100, source)
                self.record('second', 'start', 101, source)
                self.record('second', 'complete', 104, source, duration_ms=420)
                self.record('first', 'error', 105, source)
                self.record('third', 'start', 106, source)
                self.assertEqual([trace for trace, _, _ in self.runs(source)], ['first', 'second', 'third'])
                self.assertEqual([stage_duration(stage, True, 107) for _, stage, _ in self.runs(source)],
                                 [5000, 420, 1000])

    def test_interrupted_runs_freeze_and_late_samples_do_not_reopen_them(self):
        from dashboard import stage_duration
        self.record('first', 'start', 100)
        self.record('second', 'start', 101)
        with patch('telemetry.time.time', return_value=106):
            self.store.mark_stopped('quinn')
        self.record('first', 'info', 120, output_chars=80, elapsed_ms=20000)
        self.record('second', 'complete', 121, duration_ms=20000)
        for now in (125, 500):
            self.assertEqual([(stage['status'], stage_duration(stage, False, now)) for _, stage, _ in self.runs()],
                             [('stopped', 6000), ('stopped', 5000)])

    def test_missing_timing_and_bounded_session_history(self):
        from dashboard import stage_duration
        self.record('missing', 'error', 100)
        self.assertIsNone(stage_duration(self.runs()[0][1], False, 200))
        for index in range(30):
            self.record(str(index), 'complete', 101 + index, duration_ms=index)
        for index in range(30):
            self.store.record(event('intake', 'skipped', f'skipped-{index}'))
        self.assertEqual([trace for trace, _, _ in self.runs()], [str(index) for index in range(18, 30)])
        self.store.reset('quinn')
        self.assertEqual(self.runs(), [])


class TelemetryTests(unittest.TestCase):
    def test_emission_is_opt_in_and_pipe_failures_are_nonfatal(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            emit('quinn', 'inference', output_chars=20)
        self.assertEqual(output.getvalue(), '')
        with patch.dict(os.environ, QUINN_TELEMETRY='1'), contextlib.redirect_stdout(output):
            emit('quinn', 'inference', 'complete', trace='one', output_chars=20)
        store = TelemetryStore()
        self.assertTrue(store.ingest(output.getvalue()))
        self.assertEqual(store.snapshot('quinn')['metrics']['output_chars'], 20)
        with patch.dict(os.environ, QUINN_TELEMETRY='1'), patch('sys.stdout.write', side_effect=BrokenPipeError):
            emit('quinn', 'inference', output_chars=20)

    def test_malformed_events_cannot_break_log_collection(self):
        store = TelemetryStore()
        self.assertFalse(store.ingest('ordinary output'))
        for payload in ('bad json', '[]', '{"source":"other"}', json.dumps(event('inference', status=[])),
                        json.dumps({**event('inference'), 'time': float('nan')})):
            self.assertTrue(store.ingest(PREFIX + payload))
        self.assertEqual(store.snapshot('quinn')['events'], [])

    def test_interleaved_intake_does_not_replace_active_inference(self):
        store = TelemetryStore()
        store.record(event('context', 'start'))
        store.record(event('inference', 'info', output_chars=80))
        store.record(event('intake', trace='second', input_chars=17))
        snapshot = store.snapshot('quinn')
        self.assertEqual(snapshot['trace'], 'first')
        self.assertEqual(snapshot['metrics']['output_chars'], 80)
        store.record(event('context', 'start', trace='second'))
        self.assertNotIn('output_chars', store.snapshot('quinn')['metrics'])
        store.record(event('delivery', trace='first', duration_ms=10))
        self.assertEqual(store.snapshot('quinn')['trace'], 'second')
        self.assertNotIn('delivery', store.snapshot('quinn')['stages'])

    def test_stop_freezes_stages_without_rewriting_evidence(self):
        store = TelemetryStore()
        start = event('inference', 'start')
        store.record(start)
        store.record(event('inference', 'info', output_chars=100))
        store.mark_stopped('quinn', 'Backend exited')
        snapshot = store.snapshot('quinn')
        self.assertEqual(snapshot['stages']['inference']['status'], 'stopped')
        self.assertEqual(snapshot['events'][-1]['status'], 'info')
        self.assertEqual(snapshot['stages']['inference']['started'], start['time'])

    def test_buffers_are_bounded_and_snapshots_are_detached(self):
        store = TelemetryStore()
        for index in range(500):
            store.record(event('intake', trace=str(index)))
        snapshot = store.snapshot('quinn')
        self.assertEqual(len(snapshot['events']), 240)
        self.assertLessEqual(len(snapshot['traces']), 24)
        snapshot['metrics']['output_chars'] = 10
        self.assertNotIn('output_chars', store.snapshot('quinn')['metrics'])
        store.reset('quinn')
        self.assertEqual(store.snapshot('quinn')['events'], [])

    def test_cooldowns_survive_a_new_drop_without_stale_ocr(self):
        store = TelemetryStore()
        store.record(event('cooldown', trace='', source='sniper', grab_seconds=300, collected=2))
        store.record(event('intake', 'start', source='sniper'))
        store.record(event('ocr', source='sniper', duration_ms=1200))
        store.record(event('intake', 'start', trace='second', source='sniper'))
        snapshot = store.snapshot('sniper')
        self.assertEqual(snapshot['metrics']['collected'], 2)
        self.assertNotIn('duration_ms', snapshot['metrics'])

    def test_waiting_drop_does_not_hide_an_active_scan(self):
        store = TelemetryStore()
        store.record(event('intake', source='sniper'))
        store.record(event('download', 'start', source='sniper'))
        store.record(event('ocr', 'start', source='sniper'))
        store.record(event('intake', trace='second', source='sniper'))
        self.assertEqual(store.snapshot('sniper')['trace'], 'first')
        store.record(event('download', 'start', trace='second', source='sniper'))
        self.assertEqual(store.snapshot('sniper')['trace'], 'second')
        self.assertNotIn('ocr', store.snapshot('sniper')['stages'])

    def test_current_chart_survives_log_rollover_and_counters_never_rewind(self):
        store = TelemetryStore()
        store.record(event('intake', source='sniper'))
        store.record(event('ocr', source='sniper', pass_ms=25))
        store.record(event('claim', source='sniper', collected=3, grab_seconds=60))
        for seconds in range(300):
            store.record(event('cooldown', trace='', source='sniper', grab_seconds=0))
        store.record(event('wishlist', source='sniper'))
        snapshot = store.snapshot('sniper')
        self.assertEqual(len(snapshot['samples']), 1)
        self.assertEqual(snapshot['metrics']['grab_seconds'], 0)
        self.assertEqual(snapshot['metrics']['collected'], 3)


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        from dashboard import Dashboard
        from launcher import Launcher
        try:
            cls.root = tk.Tk()
        except tk.TclError as error:
            raise unittest.SkipTest(str(error))
        cls.launcher = Launcher(gui=True)
        cls.app = Dashboard(cls.root, cls.launcher)
        cls.root.update_idletasks()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.after_cancel(cls.app.refresh_timer)
        cls.root.destroy()

    def test_idle_view_has_no_generated_samples(self):
        self.launcher.telemetry.reset()
        for source, card in self.app.cards.items():
            card.update_data(self.launcher.telemetry.snapshot(source), False)
            self.assertFalse(card.animated)
            self.assertTrue(all(value.cget('text') == '—' for value in card.values))

    def test_real_measurements_and_failure_are_rendered(self):
        self.launcher.telemetry.reset()
        self.launcher.telemetry.record(event('context', 'start'))
        self.launcher.telemetry.record(event('inference', 'start', output_chars=0))
        self.launcher.telemetry.record(event('context', 'complete', context_messages=12))
        self.launcher.telemetry.record(event('inference', 'info', output_chars=42, elapsed_ms=300, first_output_ms=220))
        card = self.app.cards['quinn']
        card.update_data(self.launcher.telemetry.snapshot('quinn'), True)
        self.assertEqual(card.values[0].cget('text'), '42')
        self.assertEqual(card.values[2].cget('text'), '220 ms')
        self.assertTrue(card.animated)
        self.launcher.telemetry.record(event('inference', 'error', duration_ms=500))
        card.update_data(self.launcher.telemetry.snapshot('quinn'), True)
        self.assertFalse(card.animated)
        self.assertEqual(card.badge.cget('text'), 'ATTENTION')
        self.app.refresh_events(self.launcher.telemetry.snapshot())
        item = self.app.tree.get_children()[0]
        self.app.tree.selection_set(item)
        self.app.select_event(None)
        self.assertIn('Duration ms  500 ms', self.app.inspector.get('1.0', 'end'))

    def test_navigation_and_log_switch_preserve_events(self):
        before = self.launcher.telemetry.snapshot()
        for view in ('quinn', 'sniper', 'activity', 'overview'):
            self.app.navigate(view)
            self.root.update_idletasks()
            self.assertEqual(self.app.filter, view)
        self.app.toggle_log()
        self.assertTrue(self.app.show_logs)
        self.app.toggle_log()
        self.assertFalse(self.app.show_logs)
        self.assertEqual(self.launcher.telemetry.snapshot(), before)

    def test_layout_remains_visible_at_minimum_size(self):
        self.root.geometry('1050x790')
        self.root.deiconify()
        self.root.after(100, self.root.quit)
        self.root.mainloop()
        self.root.withdraw()
        bottom = self.app.status.winfo_rooty() - self.root.winfo_rooty() + self.app.status.winfo_height()
        self.assertLessEqual(bottom, self.root.winfo_height())
        for card in self.app.cards.values():
            bounds = card.canvas.bbox('all')
            self.assertGreater(card.canvas.winfo_height(), 140)
            self.assertLessEqual(bounds[3], card.canvas.winfo_height())
            self.assertLessEqual(bounds[2], card.canvas.winfo_width())

    def test_controls_logs_and_inspector_interactions(self):
        from dashboard import RED
        self.app.navigate('activity')
        self.root.deiconify()
        self.root.update()
        if not self.app.show_logs:
            self.app.toggle_log()
        self.root.update()
        lines = tuple(f'Service output {index}' for index in range(100))
        self.app.refresh_logs(lines)
        self.root.update_idletasks()
        self.assertAlmostEqual(self.app.logs.yview()[1], 1)
        self.app.logs.yview('20.0')
        self.app.refresh_logs((*lines[3:], 'New output'))
        self.root.update_idletasks()
        self.assertEqual(self.app.logs.get('@0,0 linestart', '@0,0 lineend'), 'Service output 19')
        self.app.logs.see('end')
        self.app.refresh_logs((*lines[3:], 'New output', 'Next output'))
        self.root.update_idletasks()
        self.assertAlmostEqual(self.app.logs.yview()[1], 1)
        self.assertGreaterEqual(self.app.log_panel.winfo_height(), self.app.tree.winfo_reqheight())

        self.launcher.telemetry.reset()
        self.launcher.telemetry.record(event('ocr', source='sniper', status='info', card=1, field='name', pass_ms=120))
        self.launcher.telemetry.record(event('ocr', source='sniper', status='error', duration_ms=300))
        from dashboard import recent_runs
        _, stage, run = recent_runs(self.launcher.telemetry.snapshot('sniper'), 'sniper')[0]
        self.app.inspect((stage, run, 300, 'error'))
        self.assertFalse(self.app.show_logs)
        self.assertIn('Card 1 · name  120 ms', self.app.inspector.get('1.0', 'end'))
        self.assertEqual(str(self.app.tree.tag_configure('error', 'foreground')), RED)

        start = self.app.start_buttons[0]
        with patch.object(self.launcher, 'handle') as handle:
            start.set_enabled(False)
            start.set_hover(True)
            start.invoke()
            self.assertIsNone(start.timer)
            handle.assert_not_called()
            start.set_hover(False)
            start.set_enabled(True)
            start.invoke()
            handle.assert_called_once_with('3')
        self.app.navigate('overview')
        self.root.withdraw()

    def test_history_bar_click_opens_run_details(self):
        self.app.navigate('quinn')
        self.root.deiconify()
        self.root.update()
        self.launcher.telemetry.reset()
        self.launcher.telemetry.record(event('inference', duration_ms=1500, output_chars=96, first_output_ms=380))
        card = self.app.cards['quinn']
        card.update_data(self.launcher.telemetry.snapshot('quinn'), False)
        card.draw()
        _, (left, top, right, bottom), _ = next(area for area in card.hit_areas if area[0][0] == 'run')
        position = dict(x=int((left + right) / 2), y=int((top + bottom) / 2))
        card.canvas.event_generate('<Motion>', **position)
        card.canvas.event_generate('<ButtonPress-1>', **position)
        card.canvas.event_generate('<ButtonRelease-1>', **position)
        self.root.update()
        details = self.app.inspector.get('1.0', 'end')
        self.assertIn('Output characters  96', details)
        self.assertIn('First output  380 ms', details)
        self.app.navigate('sniper')
        self.assertIn('Select an event', self.app.inspector.get('1.0', 'end'))
        self.app.navigate('overview')
        self.root.withdraw()


if __name__ == '__main__':
    unittest.main()
