"""Test desktop integration without opening Tk or starting a bot process."""
import os
import queue
import socket
import threading
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(os.name == "nt", "Windows desktop frontend")
class DesktopTests(unittest.TestCase):
    def test_socket_wakeup_is_acknowledged_and_queued_to_ui_thread(self):
        from bot_app.presentation.desktop.tray import TrayApp, SHOW_ACK, SHOW_REQUEST
        app = object.__new__(TrayApp)
        app.shutting_down = False
        app.ui_actions = queue.Queue()
        app.show_window = Mock()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.1)
        app.server_socket = listener
        threads = []
        thread_type = threading.Thread
        def make_thread(*args, **kwargs):
            thread = thread_type(*args, **kwargs)
            threads.append(thread)
            return thread
        try:
            with patch("bot_app.presentation.desktop.tray.threading.Thread", side_effect=make_thread):
                app.start_command_server()
            with socket.create_connection(listener.getsockname(), timeout=2) as connection:
                connection.sendall(SHOW_REQUEST)
                connection.shutdown(socket.SHUT_WR)
                self.assertEqual(connection.recv(64), SHOW_ACK)
            app.show_window.assert_not_called()
            callback = app.ui_actions.get(timeout=2)
            callback()
            app.show_window.assert_called_once()
        finally:
            app.shutting_down = True
            listener.close()
            for thread in threads:
                thread.join(timeout=2)

    def test_desktop_reads_shared_management_interface(self):
        from bot_app.presentation.desktop.tray import TrayApp
        app = object.__new__(TrayApp)
        with patch("bot_app.presentation.desktop.tray.management") as management:
            app.read_status()
            app.read_voice_moderation_config()
            app.read_recent_log_lines()
            management.get_status.assert_called_once()
            management.get_settings.assert_called_once_with("voice_moderation")
            management.get_logs.assert_called_once()

    def test_bot_lifecycle_delegates_to_os_controller(self):
        from bot_app.presentation.desktop.tray import TrayApp
        app = object.__new__(TrayApp)
        app.controller = Mock()
        app.shutting_down = True
        app._start_bot()
        app._stop_bot()
        app.controller._start_bot.assert_called_once()
        app.controller._stop_bot.assert_called_once()
        self.assertTrue(app.controller.shutting_down)
