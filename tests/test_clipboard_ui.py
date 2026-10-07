"""Offscreen clipboard/controller contracts; no network or session worker."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import unittest

from PySide6.QtCore import QMimeData, QObject, Signal
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from radmin_viewer.clipboard_ui import ClipboardController


class ClipboardUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.clipboard = self.app.clipboard()
        self.clipboard.setText("local")
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        self.app.processEvents()
        self.clipboard.clear()

    def controller(self):
        events, statuses = [], []
        controller = ClipboardController(self.clipboard, events.append, statuses.append)
        self.controllers.append(controller)
        controller.set_available(True)
        return controller, events, statuses

    def request(self, controller, events):
        controller.receive_remote()
        return events[-1]["request_id"]

    def test_bidirectional_auto_coalesces_and_suppresses_remote_echo(self):
        controller, events, _ = self.controller()
        controller.set_automatic(True)
        self.assertEqual(events, [{"type": "clipboard_send", "text": "local"}])
        self.clipboard.setText("intermediate")
        self.clipboard.setText("latest")
        self.app.processEvents()
        self.assertEqual(events[-1], {"type": "clipboard_send", "text": "latest"})
        self.assertEqual(len(events), 2)
        request_id = self.request(controller, events)
        controller.receive_remote()
        self.assertEqual(len(events), 3)
        controller.received(request_id, "remote")
        self.app.processEvents()
        self.assertEqual(self.clipboard.text(), "remote")
        self.assertEqual(len(events), 3)

    def test_local_revision_invalidates_inflight_even_if_text_returns_to_original(self):
        controller, events, statuses = self.controller()
        controller.set_automatic(True)
        request_id = self.request(controller, events)
        self.clipboard.setText("new")
        self.clipboard.setText("local")
        controller.received(request_id, "stale remote")
        self.app.processEvents()
        self.assertEqual(self.clipboard.text(), "local")
        self.assertTrue(any("Stale" in status for status in statuses))
        self.assertEqual(events[-1], {"type": "clipboard_send", "text": "local"})

    def test_timeout_does_not_rearm_and_late_reply_resumes_polling(self):
        controller, events, statuses = self.controller()
        self.assertEqual(controller._deadline.interval(), 3000)
        self.assertEqual(controller._poll.interval(), 750)
        controller._deadline.setInterval(15)
        controller._poll.setInterval(5)
        controller.set_automatic(True)
        request_id = self.request(controller, events)
        QTest.qWait(80)
        self.assertEqual(len([e for e in events if e["type"] == "clipboard_receive"]), 1)
        self.assertEqual(sum("timed out" in s for s in statuses), 1)
        controller.receive_remote()
        controller.set_automatic(False)
        controller.set_automatic(True)
        self.assertFalse(controller._poll.isActive())
        QTest.qWait(40)
        self.assertEqual(len([e for e in events if e["type"] == "clipboard_receive"]), 1)
        self.clipboard.setText("new local")
        controller.received(request_id, "late remote")
        self.assertEqual(self.clipboard.text(), "new local")
        QTest.qWait(15)
        receives = [e for e in events if e["type"] == "clipboard_receive"]
        self.assertEqual(len(receives), 2)
        self.assertGreater(receives[-1]["request_id"], request_id)

    def test_empty_text_is_real_but_none_and_nontext_leave_local_intact(self):
        controller, events, statuses = self.controller()
        controller.received(self.request(controller, events), None)
        self.assertEqual(self.clipboard.text(), "local")
        controller.received(self.request(controller, events), "")
        self.assertEqual(self.clipboard.text(), "")
        controller.send_local()
        self.assertEqual(events[-1], {"type": "clipboard_send", "text": ""})
        controller.received(self.request(controller, events), b"unsupported")
        self.assertEqual(self.clipboard.text(), "")
        self.assertTrue(any("unsupported" in s for s in statuses))

    def test_nontext_change_cancels_queued_send_and_invalidates_reply(self):
        controller, events, _ = self.controller()
        controller.set_automatic(True)
        request_id = self.request(controller, events)
        self.clipboard.setText("must not send")
        mime = QMimeData()
        image = QImage(2, 2, QImage.Format.Format_RGB32)
        image.fill(0)
        mime.setImageData(image)
        self.clipboard.setMimeData(mime)
        self.app.processEvents()
        controller.send_local()
        controller.received(request_id, "stale")
        self.assertTrue(self.clipboard.mimeData().hasImage())
        self.assertEqual(len(events), 2)

    def test_multiwindow_ownership_relinquishes_and_invalidates_old_reply(self):
        first, first_events, _ = self.controller()
        second, second_events, _ = self.controller()
        changes = []
        first.automatic_changed.connect(changes.append)
        first.set_automatic(True)
        old_id = self.request(first, first_events)
        second.set_automatic(True)
        self.assertEqual(changes, [True, False])
        self.assertFalse(first.automatic)
        first.received(old_id, "wrong session")
        self.assertEqual(self.clipboard.text(), "local")
        second.received(self.request(second, second_events), "second remote")
        self.app.processEvents()
        self.assertEqual(len(first_events), 2)
        self.assertEqual(len(second_events), 2)
        self.clipboard.setText("user copy")
        self.app.processEvents()
        self.assertEqual(len(first_events), 2)
        self.assertEqual(second_events[-1]["text"], "user copy")

    def test_manual_remote_write_does_not_leak_to_automatic_other_session(self):
        first, first_events, _ = self.controller()
        second, second_events, _ = self.controller()
        second.set_automatic(True)
        second_id = self.request(second, second_events)
        first.received(self.request(first, first_events), "manual remote")
        self.app.processEvents()
        self.assertEqual(len(second_events), 2)
        second.received(second_id, "older remote")
        self.assertEqual(self.clipboard.text(), "manual remote")

    def test_polling_without_focused_window_and_reconnect_ids(self):
        controller, events, _ = self.controller()
        controller._poll.setInterval(5)
        controller.set_automatic(True)
        QTest.qWait(20)
        old_id = events[-1]["request_id"]
        controller.set_available(False)
        count = len(events)
        QTest.qWait(20)
        self.assertEqual(len(events), count)
        controller.set_available(True)
        self.assertEqual(events[-1]["type"], "clipboard_send")
        new_id = self.request(controller, events)
        self.assertGreater(new_id, old_id)
        controller.received(old_id, "old connection")
        self.assertEqual(self.clipboard.text(), "local")
        controller.received(new_id, "fresh connection")
        self.assertEqual(self.clipboard.text(), "fresh connection")

    def test_failed_is_content_free_and_preserves_wire_pending(self):
        controller, events, statuses = self.controller()
        request_id = self.request(controller, events)
        controller.failed(request_id + 1, "ignore")
        self.assertEqual(statuses, [])
        controller.failed(request_id, "SECRET contents")
        controller.receive_remote()
        self.assertEqual(len(events), 1)
        self.assertNotIn("SECRET", " ".join(statuses))
        controller.received(request_id, "eventual reply")
        self.assertEqual(self.clipboard.text(), "eventual reply")

    def test_validation_bounds_utf16_and_never_displays_contents(self):
        controller, events, statuses = self.controller()
        for text in ("SECRET\x00text", "\ud800", "\U0001f600" * (1024 * 1024 // 4)):
            # Call the reply path to avoid Qt normalizing invalid Unicode first.
            controller.received(self.request(controller, events), text)
            self.assertEqual(self.clipboard.text(), "local")
        self.clipboard.setText("x" * (1024 * 1024 // 2))
        count = len(events)
        controller.send_local()
        self.assertEqual(len(events), count)
        self.assertNotIn("SECRET", " ".join(statuses))
        self.clipboard.setText("x" * ((1024 * 1024 - 2) // 2))
        controller.send_local()
        self.assertEqual(events[-1]["type"], "clipboard_send")

    def test_close_stops_timers_disconnects_and_ignores_callbacks(self):
        controller, events, statuses = self.controller()
        controller._poll.setInterval(5)
        controller._deadline.setInterval(10)
        controller.set_automatic(True)
        request_id = self.request(controller, events)
        self.clipboard.setText("queued")
        controller.close()
        controller.close()
        snapshot = (len(events), len(statuses))
        controller.received(request_id, "after close")
        controller.failed(request_id, "after close")
        controller.set_available(True)
        controller.set_automatic(True)
        controller.send_local()
        controller.receive_remote()
        self.clipboard.setText("after teardown")
        QTest.qWait(30)
        self.assertEqual((len(events), len(statuses)), snapshot)
        self.assertEqual(self.clipboard.text(), "after teardown")
        self.assertFalse(controller.automatic)

    def test_worker_signal_is_queued_and_direct_offthread_access_is_rejected(self):
        class Worker(QObject):
            reply = Signal(int, object)

        controller, events, _ = self.controller()
        request_id = self.request(controller, events)
        worker = Worker()
        worker.reply.connect(controller.received)
        errors = []

        def run():
            try:
                controller.send_local()
            except RuntimeError as error:
                errors.append(error)
            worker.reply.emit(request_id, "worker reply")

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.clipboard.text(), "local")
        self.app.processEvents()
        self.assertEqual(self.clipboard.text(), "worker reply")


if __name__ == "__main__":
    unittest.main()
