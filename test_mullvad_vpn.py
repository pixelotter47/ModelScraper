import unittest
from unittest import mock

from mullvad_vpn import MullvadError, MullvadVpnController
from windows_subprocess import hidden_subprocess_kwargs


class _FakeCommandRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append((list(args), timeout))
        if not self.outputs:
            return ""
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


class MullvadVpnControllerTests(unittest.TestCase):
    def test_subprocess_runner_hides_windows_console(self):
        vpn = MullvadVpnController(command_path=r"C:\Mullvad\mullvad.exe")
        completed = mock.Mock(returncode=0, stdout="Connected", stderr="")

        with mock.patch("mullvad_vpn.subprocess.run", return_value=completed) as run:
            result = vpn._run_subprocess(["status"], 30)

        self.assertEqual(result, "Connected")
        expected_hidden = hidden_subprocess_kwargs()
        run.assert_called_once()
        call_kwargs = run.call_args.kwargs
        self.assertEqual(
            call_kwargs["creationflags"],
            expected_hidden["creationflags"],
        )
        self.assertEqual(
            call_kwargs["startupinfo"].wShowWindow,
            expected_hidden["startupinfo"].wShowWindow,
        )

    def test_connect_ireland_sets_location_and_connects_when_disconnected(self):
        runner = _FakeCommandRunner(
            [
                "",
                "Disconnected",
                "Connecting...",
                "Connected\nVisible location: Ireland, Dublin. IPv4: 1.2.3.4",
            ]
        )
        vpn = MullvadVpnController(command_runner=runner, sleep=lambda _: None)

        vpn.connect_ireland()

        self.assertEqual(
            runner.calls,
            [
                (["relay", "set", "location", "ie"], 30),
                (["status"], 30),
                (["connect", "--wait"], 90),
                (["status"], 30),
            ],
        )

    def test_connect_ireland_reconnects_when_connected_outside_ireland(self):
        runner = _FakeCommandRunner(
            [
                "",
                "Connected\nVisible location: Germany, Frankfurt. IPv4: 1.2.3.4",
                "Reconnecting...",
                "Connected\nRelay: ie-dub-wg-102\nVisible location: Ireland, Dublin.",
            ]
        )
        vpn = MullvadVpnController(command_runner=runner, sleep=lambda _: None)

        vpn.connect_ireland()

        self.assertEqual(
            runner.calls,
            [
                (["relay", "set", "location", "ie"], 30),
                (["status"], 30),
                (["reconnect", "--wait"], 90),
                (["status"], 30),
            ],
        )

    def test_disconnect_skips_command_when_already_disconnected(self):
        runner = _FakeCommandRunner(["Disconnected"])
        vpn = MullvadVpnController(command_runner=runner, sleep=lambda _: None)

        vpn.disconnect()

        self.assertEqual(runner.calls, [(["status"], 30)])

    def test_connect_ireland_raises_clear_error_when_status_never_matches(self):
        runner = _FakeCommandRunner(
            [
                "",
                "Disconnected",
                "Connecting...",
                "Connected\nVisible location: Germany, Frankfurt.",
            ]
        )
        vpn = MullvadVpnController(command_runner=runner, sleep=lambda _: None)

        with self.assertRaises(MullvadError) as ctx:
            vpn.connect_ireland()

        self.assertIn("Ireland", str(ctx.exception))

    def test_connect_ireland_waits_through_transitional_connecting_status(self):
        runner = _FakeCommandRunner(
            [
                "",
                "Disconnected",
                "Connecting...",
                "Connecting\nRelay: ie-dub-wg-102\nVisible location: Ireland, Dublin.",
                "Connecting\nRelay: ie-dub-wg-102\nVisible location: Ireland, Dublin.",
                "Connecting\nRelay: ie-dub-wg-102\nVisible location: Ireland, Dublin.",
                "Connected\nRelay: ie-dub-wg-102\nVisible location: Ireland, Dublin.",
            ]
        )
        vpn = MullvadVpnController(command_runner=runner, sleep=lambda _: None)

        vpn.connect_ireland()

        self.assertEqual(runner.calls[-1][0], ["status"])

    def test_remove_chrome_from_split_tunnel_removes_only_when_listed(self):
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        runner = _FakeCommandRunner(
            [
                "Split tunneling state: on\n"
                "Excluded applications:\n"
                "C:\\Program Files\\qBittorrent\\qbittorrent.exe\n"
                "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\n",
                "",
                "Split tunneling state: on\nExcluded applications:\n",
            ]
        )
        vpn = MullvadVpnController(
            chrome_path=chrome_path,
            command_runner=runner,
            sleep=lambda _: None,
        )

        vpn.remove_chrome_from_split_tunnel()

        self.assertEqual(
            runner.calls,
            [
                (["split-tunnel", "get"], 30),
                (["split-tunnel", "app", "remove", chrome_path], 30),
                (["split-tunnel", "get"], 30),
            ],
        )

    def test_remove_chrome_from_split_tunnel_skips_when_not_listed(self):
        runner = _FakeCommandRunner(
            [
                "Split tunneling state: on\n"
                "Excluded applications:\n"
                "C:\\Program Files\\qBittorrent\\qbittorrent.exe\n",
            ]
        )
        vpn = MullvadVpnController(
            chrome_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            command_runner=runner,
            sleep=lambda _: None,
        )

        vpn.remove_chrome_from_split_tunnel()

        self.assertEqual(
            runner.calls,
            [
                (["split-tunnel", "get"], 30),
                (["split-tunnel", "get"], 30),
            ],
        )

    def test_add_chrome_to_split_tunnel_adds_configured_chrome_path(self):
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        runner = _FakeCommandRunner(
            [
                "Split tunneling state: on\nExcluded applications:\n",
                "",
                "Split tunneling state: on\n"
                "Excluded applications:\n"
                "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\n",
            ]
        )
        vpn = MullvadVpnController(
            chrome_path=chrome_path,
            command_runner=runner,
            sleep=lambda _: None,
        )

        vpn.add_chrome_to_split_tunnel()

        self.assertEqual(
            runner.calls,
            [
                (["split-tunnel", "get"], 30),
                (["split-tunnel", "app", "add", chrome_path], 30),
                (["split-tunnel", "get"], 30),
            ],
        )


if __name__ == "__main__":
    unittest.main()
