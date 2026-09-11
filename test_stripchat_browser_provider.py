"""Browser evidence provider: fake-driver lifecycle and extraction rules."""

import unittest

from stripchat_core import ChallengeDeadlineError, StripchatBrowserProvider


class FakeElement:
    def __init__(self, text="", displayed=True):
        # A zero-box element still carries textContent while Selenium's
        # rendered ``.text`` comes back empty, which is exactly the case
        # the shutter reading has to survive.
        self.text = text if displayed else ""
        self.text_content = text
        self._displayed = displayed

    def is_displayed(self):
        return self._displayed

    def get_attribute(self, name):
        return self.text_content if name == "textContent" else None


class FakePage:
    def __init__(
        self,
        current_url,
        title="Room",
        source="<html>page</html>",
        ready="complete",
        elements=None,
    ):
        self.current_url = current_url
        self.title = title
        self.source = source
        self.ready = ready
        self.elements = dict(elements or {})


class FakeDriver:
    def __init__(self, pages, window_handles=("w1",)):
        self.pages = dict(pages)
        self.page = None
        self.gets = []
        self.quit_calls = 0
        self.page_load_timeout = None
        self._window_handles = list(window_handles)

    @property
    def window_handles(self):
        return list(self._window_handles)

    def set_page_load_timeout(self, value):
        self.page_load_timeout = value

    def set_script_timeout(self, value):
        pass

    def get(self, url):
        self.gets.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        self.page = page

    @property
    def current_url(self):
        return self.page.current_url

    @property
    def title(self):
        return self.page.title

    @property
    def page_source(self):
        return self.page.source

    def execute_script(self, script):
        return self.page.ready

    def find_elements(self, by, selector):
        return list(self.page.elements.get(selector, []))

    def quit(self):
        self.quit_calls += 1


class FakeWindowManager:
    def __init__(self):
        self.calls = []

    def reveal(self):
        self.calls.append("reveal")

    def hide(self):
        self.calls.append("hide")


URL = "https://stripchat.com/Model-A/"


def provider_for(driver, **overrides):
    values = dict(
        driver_factory=lambda: driver,
        clock=None,
        sleep=lambda _seconds: None,
        navigation_timeout=5.0,
        challenge_deadline=10.0,
    )
    values.update(overrides)
    if values["clock"] is None:
        counter = {"now": 0.0}

        def clock():
            counter["now"] += 0.05
            return counter["now"]

        values["clock"] = clock
    return StripchatBrowserProvider(**values)


class ExtractionTests(unittest.TestCase):
    def test_only_displayed_scoped_notice_text_is_extracted(self):
        page = FakePage(
            URL,
            elements={
                StripchatBrowserProvider.NOTICE_SELECTOR: [
                    FakeElement("Account Hidden", displayed=True),
                    FakeElement("Account Disabled", displayed=False),
                ],
                StripchatBrowserProvider.DISABLED_CLASS_SELECTOR: [
                    FakeElement("Account Hidden", displayed=True),
                ],
            },
        )
        driver = FakeDriver({URL: page})
        observation = provider_for(driver).observe(URL)
        self.assertEqual(observation.account_notice_texts, ("Account Hidden",))
        self.assertTrue(observation.has_hidden_text)
        self.assertFalse(
            observation.has_disabled_text,
            "hidden/undisplayed disabled text must not count",
        )
        self.assertEqual(observation.visible_disabled_selector_count, 1)

    def test_navigation_is_sequential_and_url_bound(self):
        page = FakePage(URL)
        driver = FakeDriver({URL: page})
        provider_for(driver).observe(URL)
        self.assertEqual(driver.gets, [URL])

    def test_video_counts_do_not_require_visibility(self):
        page = FakePage(
            URL,
            elements={"video": [FakeElement(displayed=False)]},
        )

        class TagAwareDriver(FakeDriver):
            def find_elements(self, by, selector):
                return list(self.page.elements.get(selector, []))

        driver = TagAwareDriver({URL: page})
        observation = provider_for(driver).observe(URL)
        self.assertEqual(observation.video_element_count, 1)

    def test_timeout_becomes_a_structured_unknown(self):
        driver = FakeDriver({URL: TimeoutError("timed out after 45s")})
        observation = provider_for(driver).observe(URL)
        self.assertEqual(observation.navigation_error, "navigation_timeout")

    def test_a_windowless_driver_is_lost_whatever_it_says(self):
        """The message list cannot be the only way to spot a dead browser.

        A live run lost its Chrome window two thirds of the way through
        step 4. The failure did not match any known signature, so all 108
        remaining candidates were filed as per-page selector faults and
        burned their whole retry budget in four seconds - the exact
        failure the restart path exists to prevent. A driver that has no
        window cannot answer for any page, so ask it rather than parsing
        its wording.
        """
        from stripchat_core import BrowserLostError

        for message in (
            "no such window: target window already closed",
            "web view not found",
            "something nobody has seen before",
        ):
            with self.subTest(message=message[:30]):
                driver = FakeDriver(
                    {URL: RuntimeError(message)}, window_handles=()
                )
                provider = provider_for(
                    None, driver_factory=lambda: driver, max_restarts=0
                )
                with self.assertRaises(BrowserLostError):
                    provider.observe(URL)

    def test_a_healthy_driver_keeps_its_precise_failure(self):
        """A page that times out on a working browser is not a lost one."""
        driver = FakeDriver({URL: TimeoutError("timed out after 45s")})
        observation = provider_for(driver).observe(URL)
        self.assertEqual(observation.navigation_error, "navigation_timeout")

    def test_a_window_lost_mid_extraction_is_not_a_selector_fault(self):
        from stripchat_core import BrowserLostError

        class BreakingDriver(FakeDriver):
            def find_elements(self, by, selector):
                raise RuntimeError("web view not found")

        driver = BreakingDriver({URL: FakePage(URL)}, window_handles=())
        provider = provider_for(
            None, driver_factory=lambda: driver, max_restarts=0
        )
        with self.assertRaises(BrowserLostError):
            provider.observe(URL)

    def test_a_browser_that_stops_answering_costs_a_few_candidates(self):
        """A renderer can die while the browser still has its window.

        The second live failure did exactly that: `window_handles` kept
        answering, `current_url` did not, and 262 candidates were filed as
        per-page selector faults in twelve seconds. No message list and no
        single probe covers every way a browser stops answering, so count
        instead - a driver that cannot report even the current URL, for
        candidate after candidate, has a problem of its own.
        """
        from stripchat_core import BrowserLostError

        class MuteDriver(FakeDriver):
            @property
            def current_url(self):
                raise RuntimeError("unable to receive message from renderer")

        opened = []

        def factory():
            opened.append(True)
            return MuteDriver({URL: FakePage(URL)})

        provider = provider_for(None, driver_factory=factory, max_restarts=2)
        with self.assertRaises(BrowserLostError):
            provider.observe(URL)
        self.assertEqual(
            len(opened), 3, "restart at once, then stop; do not spend pages"
        )

    def test_a_hung_browser_is_given_a_few_pages_before_being_blamed(self):
        """A timeout is a page's own verdict until it keeps happening."""
        from stripchat_core import BrowserLostError

        provider = provider_for(
            None,
            driver_factory=lambda: FakeDriver(
                {URL: TimeoutError("timed out after 45s")}
            ),
            max_restarts=0,
        )
        timeouts = 0
        while True:
            try:
                observation = provider.observe(URL)
            except BrowserLostError:
                break
            self.assertEqual(
                observation.navigation_error, "navigation_timeout"
            )
            timeouts += 1
            self.assertLess(timeouts, 8, "a hung browser must still be caught")
        self.assertGreaterEqual(
            timeouts, 2, "a slow page is not immediately a broken browser"
        )

    def test_a_readable_page_failure_never_trips_the_breaker(self):
        """A page that answers is diagnosing itself, not the browser."""
        page = FakePage(URL)

        class SelectorBreaks(FakeDriver):
            def find_elements(self, by, selector):
                raise RuntimeError("stale element reference")

        provider = provider_for(
            None,
            driver_factory=lambda: SelectorBreaks({URL: page}),
            max_restarts=0,
        )
        for _ in range(6):
            observation = provider.observe(URL)
            self.assertEqual(
                observation.navigation_error, "selector_error"
            )

    def test_a_lost_window_costs_one_candidate_not_all_of_them(self):
        """The whole point: the step stops, it does not burn the list.

        The live failure filed 108 candidates in four seconds against a
        browser that was already gone, spending each one's retry budget on
        an error that had nothing to do with its page.
        """
        from stripchat_core import BrowserLostError

        seen = []

        class DeadAfterFirst(FakeDriver):
            def get(self, url):
                seen.append(url)
                self._window_handles = []
                raise RuntimeError("web view not found")

        provider = provider_for(
            None,
            driver_factory=lambda: DeadAfterFirst({URL: FakePage(URL)}),
            max_restarts=1,
        )
        with self.assertRaises(BrowserLostError):
            provider.observe(URL)
        self.assertEqual(
            len(seen), 2, "one attempt plus one bounded restart, then stop"
        )

    def test_dead_driver_signatures_are_recognized_as_browser_loss(self):
        from stripchat_core import StripchatBrowserProvider as P

        for message in (
            "invalid session id",
            "session deleted because of page crash",
            "no such session",
            "disconnected: not connected to DevTools",
            "chrome not reachable",
            "Max retries exceeded with url: /session/abc/url",
            "Failed to establish a new connection: [WinError 10061] "
            "No connection could be made because the target machine "
            "actively refused it",
            "Remote end closed connection without response",
            # Observed live: the renderer dies while the browser process
            # and its window keep answering.
            "Message: tab crashed (Session info: chrome=150.0.7871.115)",
            "unable to receive message from renderer",
        ):
            with self.subTest(message=message[:40]):
                self.assertEqual(
                    P._navigation_reason(RuntimeError(message)),
                    "browser_lost",
                )

    def test_offline_and_not_found_do_not_require_visibility(self):
        """Stripchat renders the offline shutter with a zero-size box.

        The live probe found ``.vc-status-offline`` three times on every
        offline room and never once with a layout box, so requiring
        ``is_displayed`` files every offline model as an unclassifiable
        transient unknown - which is what kept Step 4 from finishing.
        """
        for selector, field in (
            (
                StripchatBrowserProvider.OFFLINE_SELECTOR,
                "offline_notice_detected",
            ),
            (
                StripchatBrowserProvider.NOT_FOUND_SELECTOR,
                "not_found_notice_detected",
            ),
        ):
            with self.subTest(field=field):
                page = FakePage(
                    URL, elements={selector: [FakeElement(displayed=False)]}
                )
                observation = provider_for(
                    FakeDriver({URL: page})
                ).observe(URL)
                self.assertTrue(getattr(observation, field))

    def test_modal_gates_still_require_visibility(self):
        """A login or age modal sits in the DOM of every room."""
        for selector, field in (
            (
                StripchatBrowserProvider.LOGIN_GATE_SELECTOR,
                "login_gate_detected",
            ),
            (
                StripchatBrowserProvider.CONSENT_SELECTOR,
                "consent_gate_detected",
            ),
        ):
            with self.subTest(field=field):
                page = FakePage(
                    URL, elements={selector: [FakeElement(displayed=False)]}
                )
                observation = provider_for(
                    FakeDriver({URL: page})
                ).observe(URL)
                self.assertFalse(getattr(observation, field))

    def test_the_shutter_wording_is_read_but_never_kept(self):
        """The shutter has a zero-size box, so read it, do not look at it."""
        page = FakePage(
            URL,
            elements={
                StripchatBrowserProvider.SHUTTER_STATUS_SELECTOR: [
                    FakeElement("", displayed=False),
                    FakeElement(
                        "Model is having fun in Private show",
                        displayed=False,
                    ),
                ]
            },
        )
        observation = provider_for(FakeDriver({URL: page})).observe(URL)
        self.assertTrue(observation.has_shutter_status)
        self.assertNotIn("Private", observation.diagnostic())

    def test_selectors_match_the_classes_the_live_pages_use(self):
        """Pin the class names read off live rooms.

        The previous set (``.status-off``, ``.offline-banner``,
        ``.not-found-page``, ``.video-container``) matched nothing on any
        of 362 verified pages; these were read from the DOM directly.
        """
        expected = {
            "OFFLINE_SELECTOR": (
                ".vc-status-offline",
                ".offline-status",
                ".view-cam-buttons-wrapper--offline",
            ),
            "NOT_FOUND_SELECTOR": (".not-found-wrapper", ".not-found-error"),
            "PLAYER_CONTAINER_SELECTOR": (".player-wrapper", ".video-element"),
        }
        for name, classes in expected.items():
            selector = getattr(StripchatBrowserProvider, name)
            for css_class in classes:
                with self.subTest(name=name, css_class=css_class):
                    self.assertIn(css_class, selector)

    def test_waiting_stops_at_the_first_decisive_marker(self):
        """A room says what it is ~1s in; its load event lands ~20s later."""
        sleeps = []
        page = FakePage(
            URL, ready="interactive", elements={"video": [FakeElement()]}
        )
        provider = provider_for(
            FakeDriver({URL: page}), sleep=sleeps.append
        )
        observation = provider.observe(URL)
        self.assertEqual(observation.video_element_count, 1)
        self.assertEqual(
            sleeps, [], "a decided page must not be polled again"
        )

    def test_waiting_continues_while_the_page_shows_nothing(self):
        sleeps = []
        page = FakePage(URL, ready="interactive")
        provider = provider_for(
            FakeDriver({URL: page}), sleep=sleeps.append
        )
        provider.observe(URL)
        self.assertTrue(sleeps, "an empty page still waits for the load")

    def test_no_raw_html_leaves_the_provider(self):
        page = FakePage(URL, source="<html>secret body text</html>")
        driver = FakeDriver({URL: page})
        observation = provider_for(driver).observe(URL)
        self.assertNotIn("secret", observation.diagnostic())
        self.assertNotIn("secret", str(observation.account_notice_texts))


class ChallengeLifecycleTests(unittest.TestCase):
    def _captcha_page(self):
        return FakePage(URL, title="Just a moment...", source="cf-challenge")

    def test_waiting_is_persisted_before_the_wait_and_cleared_after(self):
        events = []
        captcha = self._captcha_page()
        clean = FakePage(URL)

        class HealingDriver(FakeDriver):
            def __init__(self):
                super().__init__({URL: captcha})
                self.reads = 0

            @property
            def title(self):
                self.reads += 1
                if self.reads > 2:
                    self.page = clean
                return self.page.title

        driver = HealingDriver()
        window = FakeWindowManager()
        provider = provider_for(
            driver,
            window_manager=window,
            on_waiting=lambda reason, deadline: events.append(
                ("waiting", reason)
            ),
            on_waiting_cleared=lambda: events.append(("cleared",)),
        )
        observation = provider.observe(URL)
        self.assertFalse(observation.captcha_detected)
        self.assertEqual(events[0], ("waiting", "captcha_required"))
        self.assertIn(("cleared",), events)
        self.assertIn("reveal", window.calls)
        self.assertIn("hide", window.calls)

    def test_challenge_deadline_raises_typed_error(self):
        driver = FakeDriver({URL: self._captcha_page()})
        provider = provider_for(driver, challenge_deadline=1.0)
        with self.assertRaises(ChallengeDeadlineError) as caught:
            provider.observe(URL)
        self.assertEqual(caught.exception.reason, "captcha_timeout")

    def test_waits_are_cancellable_in_small_chunks(self):
        sleeps = []
        driver = FakeDriver({URL: self._captcha_page()})
        provider = provider_for(
            driver, challenge_deadline=1.0, sleep=sleeps.append
        )
        with self.assertRaises(ChallengeDeadlineError):
            provider.observe(URL)
        self.assertTrue(sleeps)
        self.assertTrue(all(chunk <= 0.25 for chunk in sleeps))

    def test_stop_during_challenge_returns_without_clearing(self):
        events = []

        class StopSoon:
            def __init__(self):
                self.calls = 0

            def is_cancelled(self):
                self.calls += 1
                return self.calls > 2

        driver = FakeDriver({URL: self._captcha_page()})
        provider = provider_for(
            driver,
            stop_token=StopSoon(),
            on_waiting=lambda reason, deadline: events.append("waiting"),
            on_waiting_cleared=lambda: events.append("cleared"),
        )
        observation = provider.observe(URL)
        self.assertTrue(
            observation.captcha_detected or observation.navigation_error
        )
        self.assertNotIn("cleared", events)


class OwnershipTests(unittest.TestCase):
    def test_close_quits_only_the_owned_driver_once(self):
        driver = FakeDriver({URL: FakePage(URL)})
        provider = provider_for(driver)
        provider.observe(URL)
        provider.close()
        provider.close()
        self.assertEqual(driver.quit_calls, 1)

    def test_driver_is_opened_lazily(self):
        created = []

        def factory():
            created.append(True)
            return FakeDriver({URL: FakePage(URL)})

        provider = provider_for(None, driver_factory=factory)
        self.assertEqual(created, [])
        provider.observe(URL)
        self.assertEqual(created, [True])


if __name__ == "__main__":
    unittest.main()


class BrowserRestartTests(unittest.TestCase):
    """A dead browser must not consume every candidate's retry budget."""

    def test_a_lost_browser_is_restarted_and_the_page_still_judged(self):
        from stripchat_core import BrowserLostError

        dead = FakeDriver({URL: RuntimeError("invalid session id")})
        healthy = FakeDriver({URL: FakePage(URL, elements={"video": [FakeElement()]})})
        drivers = [dead, healthy]
        opened = []

        def factory():
            driver = drivers[min(len(opened), len(drivers) - 1)]
            opened.append(driver)
            return driver

        provider = provider_for(None, driver_factory=factory)
        observation = provider.observe(URL)

        self.assertIsNone(observation.navigation_error)
        self.assertEqual(observation.video_element_count, 1)
        self.assertEqual(len(opened), 2, "the dead driver must be replaced")
        self.assertEqual(dead.quit_calls, 1, "the dead driver is closed")

    def test_a_browser_that_stays_dead_escalates_instead_of_looping(self):
        from stripchat_core import BrowserLostError

        opened = []

        def factory():
            driver = FakeDriver({URL: RuntimeError("invalid session id")})
            opened.append(driver)
            return driver

        provider = provider_for(None, driver_factory=factory, max_restarts=2)
        with self.assertRaises(BrowserLostError):
            provider.observe(URL)
        self.assertEqual(
            len(opened), 3, "one initial open plus max_restarts retries"
        )

    def test_restarts_are_bounded_across_the_whole_step(self):
        from stripchat_core import BrowserLostError

        opened = []

        def factory():
            driver = FakeDriver({URL: RuntimeError("chrome not reachable")})
            opened.append(driver)
            return driver

        provider = provider_for(None, driver_factory=factory, max_restarts=1)
        with self.assertRaises(BrowserLostError):
            provider.observe(URL)
        before = len(opened)
        # A second candidate must not get a fresh restart allowance.
        with self.assertRaises(BrowserLostError):
            provider.observe(URL)
        self.assertEqual(
            len(opened),
            before,
            "the restart budget is per step, not per candidate",
        )

    def test_stop_during_restart_returns_cancelled(self):
        class StopNow:
            def __init__(self):
                self.calls = 0

            def is_cancelled(self):
                self.calls += 1
                return self.calls > 1

        def factory():
            return FakeDriver({URL: RuntimeError("invalid session id")})

        provider = provider_for(
            None, driver_factory=factory, stop_token=StopNow()
        )
        observation = provider.observe(URL)
        self.assertEqual(observation.navigation_error, "cancelled")
