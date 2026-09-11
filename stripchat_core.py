"""Active Stripchat runner and the hardened workflow implementation.

The legacy implementation lives frozen in ``legacy_stripchat_family`` so
XHamsterLive keeps its behavior. The public ``StripchatRunner`` still
resolves to the legacy behavior through a thin subclass until the hardened
flow is promoted; ``HardenedStripchatRunner`` below is the new typed
implementation the promotion will switch to.
"""

from __future__ import annotations

from typing import Callable, Mapping

import dataclasses as _dataclasses
import datetime as _datetime
import time

from legacy_stripchat_family import (
    LegacyStripchatFamilyPaths,
    LegacyStripchatFamilyRunner,
)
from stripchat_api import StripchatApiClient, StripchatSnapshotResult
from stripchat_classifier import (
    StripchatPageObservation,
    canonical_model_url,
    classify_stripchat_page,
    model_lookup_key,
)
from stripchat_store import (
    STRIPCHAT_PROJECTIONS,
    STRIPCHAT_SPEC,
    StripchatRunStore,
    merge_blocked_into_master,
    stripchat_master_repository,
)
from workflow_finalizer import WorkflowFinalizer
from workflow_store import RunContext, RunStoreError
from workflow_verifier import GenericCheckpointedVerifier
from workflow_types import (
    OutcomeStatus,
    ProgressEvent,
    ProgressStatus,
    StepName,
    StepOutcome,
    utc_now_iso,
)

# Import-compatible aliases for existing callers and tests.
StripchatPaths = LegacyStripchatFamilyPaths


class StripchatRunner(LegacyStripchatFamilyRunner):
    """Public Stripchat runner (legacy behavior until hardening lands)."""


class ChallengeDeadlineError(RuntimeError):
    """A CAPTCHA/consent gate stayed unresolved past its deadline."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class BrowserLostError(RuntimeError):
    """The owned browser died and could not be brought back.

    Raised instead of returning an observation so verification stops at a
    safe checkpoint. Feeding a dead driver every remaining candidate would
    spend each one's retry budget on an error that has nothing to do with
    the page.
    """

    reason = "browser_lost"

    def __init__(self, message: str = "browser_lost"):
        super().__init__(message)


class StripchatBrowserProvider:
    """Owns the managed browser and extracts structural page evidence.

    All decisions live in the pure classifier; this class only navigates,
    waits cancellably, runs the active-worker challenge lifecycle, and
    reads tightly scoped account-state notices. It never persists raw
    HTML or screenshots.
    """

    NOTICE_SELECTOR = (
        ".account-hidden-header, .account-hidden-description, "
        ".account-hidden-page, .account-disabled-header, "
        ".account-disabled-description, .account-disabled-page, "
        ".model-deleted-page"
    )
    HIDDEN_CLASS_SELECTOR = (
        ".account-hidden-header, .account-hidden-description, "
        ".account-hidden-page"
    )
    DISABLED_CLASS_SELECTOR = (
        ".account-disabled-header, .account-disabled-description, "
        ".account-disabled-page, .model-deleted-page"
    )
    # Read off live rooms, not guessed: the previous set (.video-container,
    # .player-panel, .status-off, .offline-banner, .not-found-page,
    # .error-404) matched nothing on any of 362 verified pages, so every
    # offline model fell through to an unclassifiable transient unknown.
    PLAYER_CONTAINER_SELECTOR = (
        ".player-wrapper, .video-element, .player-controls-user"
    )
    OFFLINE_SELECTOR = (
        ".vc-status-offline, .offline-status, .vc-status-next-broadcast, "
        ".view-cam-buttons-wrapper--offline"
    )
    NOT_FOUND_SELECTOR = ".not-found-wrapper, .not-found-error"
    # The shutter exists on every room but only carries wording when it is
    # covering the stream rather than letting it play.
    SHUTTER_STATUS_SELECTOR = '[class*="ViewCamShutterWrapper"]'
    LOGIN_GATE_SELECTOR = ".login-modal, .auth-modal"
    CONSENT_SELECTOR = ".age-gate, .consent-modal"

    _CAPTCHA_MARKERS = (
        "just a moment",
        "checking your browser",
        "cf-challenge",
    )

    def __init__(
        self,
        *,
        driver_factory,
        window_manager=None,
        clock=time.monotonic,
        sleep=time.sleep,
        stop_token=None,
        on_waiting=None,
        on_waiting_cleared=None,
        logger=None,
        navigation_timeout: float = 45.0,
        challenge_deadline: float = 600.0,
        reveal_on_challenge: bool = True,
        max_restarts: int = 3,
        blind_failure_limit: int = 3,
    ):
        self.driver_factory = driver_factory
        self.window_manager = window_manager
        self.clock = clock
        self.sleep = sleep
        self.stop_token = stop_token
        self.on_waiting = on_waiting or (lambda _reason, _deadline: None)
        self.on_waiting_cleared = on_waiting_cleared or (lambda: None)
        self.logger = logger or (lambda _message: None)
        self.navigation_timeout = float(navigation_timeout)
        self.challenge_deadline = float(challenge_deadline)
        self.reveal_on_challenge = reveal_on_challenge
        self.max_restarts = max(0, int(max_restarts))
        # How many candidates in a row may fail without the driver being
        # able to report even the current URL before the browser, rather
        # than the pages, is held responsible.
        self.blind_failure_limit = max(1, int(blind_failure_limit))
        self._restarts = 0
        self._blind_failures = 0
        self._seen_signatures = set()
        self._driver = None

    # ------------------------------------------------------------------

    def _cancelled(self) -> bool:
        return bool(
            self.stop_token
            and callable(getattr(self.stop_token, "is_cancelled", None))
            and self.stop_token.is_cancelled()
        )

    def _driver_or_open(self):
        if self._driver is None:
            self._driver = self.driver_factory()
            for method, value in (
                ("set_page_load_timeout", self.navigation_timeout),
                ("set_script_timeout", self.navigation_timeout),
            ):
                try:
                    getattr(self._driver, method)(value)
                except Exception:
                    pass
        return self._driver

    def close(self) -> None:
        driver = self._driver
        self._driver = None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def _reveal(self):
        if self.window_manager is not None:
            try:
                self.window_manager.reveal()
            except Exception:
                pass

    def _hide(self):
        if self.window_manager is not None:
            try:
                self.window_manager.hide()
            except Exception:
                pass

    # ------------------------------------------------------------------

    def observe(self, canonical_url: str) -> StripchatPageObservation:
        url = canonical_model_url(canonical_url)
        if self._cancelled():
            return StripchatPageObservation(
                original_url=url,
                current_url="",
                navigation_error="cancelled",
            )
        observation = self._judge_browser(self._observe_once(url))
        if observation.navigation_error != "browser_lost":
            return observation
        # Chrome died. Retrying the same dead driver would fail instantly
        # for every remaining candidate, so restart it once (bounded) and
        # give the page a genuine attempt.
        while self._restarts < self.max_restarts:
            if self._cancelled():
                return StripchatPageObservation(
                    original_url=url,
                    current_url="",
                    navigation_error="cancelled",
                )
            self._restarts += 1
            self.logger(
                f"[WARN] The verification browser was lost; restarting it "
                f"({self._restarts}/{self.max_restarts})."
            )
            self.close()
            self._blind_failures = 0
            observation = self._judge_browser(self._observe_once(url))
            if observation.navigation_error != "browser_lost":
                return observation
        raise BrowserLostError(
            f"the verification browser could not be restarted after "
            f"{self.max_restarts} attempts"
        )

    def _judge_browser(
        self, observation: StripchatPageObservation
    ) -> StripchatPageObservation:
        """Blame the browser once the pages stop being readable at all.

        A page-level failure names one candidate's problem. A driver that
        cannot report even the current URL, candidate after candidate, has
        a problem of its own - and it will not name it: one live run lost
        its window, another lost only its renderer, and neither matched a
        known signature. Both filed every remaining candidate as a per-page
        selector fault within seconds. Counting consecutive unreadable
        pages catches all of it without guessing at wording.
        """
        error = observation.navigation_error
        if error is None or error in ("cancelled", "browser_lost"):
            if error is None:
                self._blind_failures = 0
            return observation
        if observation.current_url:
            # The driver still answers for this page, so the page is the
            # one with the problem.
            self._blind_failures = 0
            return observation
        self._blind_failures += 1
        # A driver that fails outright without naming a cause anyone knows
        # is not diagnosing a page, so restart before spending the
        # candidate it was asked about; it gets a genuine attempt on the
        # new browser. A timeout is the page's own verdict until it keeps
        # repeating, which is what a hung browser looks like.
        limit = 1 if error == "selector_error" else self.blind_failure_limit
        if self._blind_failures < limit:
            return observation
        self.logger(
            f"[WARN] The verification browser stopped answering: "
            f"{self._blind_failures} candidates in a row could not be read."
        )
        return _dataclasses.replace(
            observation, navigation_error="browser_lost"
        )

    def _observe_once(self, url: str) -> StripchatPageObservation:
        try:
            driver = self._driver_or_open()
        except Exception as exc:
            return StripchatPageObservation(
                original_url=url,
                current_url="",
                navigation_error=self._navigation_reason(exc),
            )
        try:
            driver.get(url)
        except Exception as exc:
            return StripchatPageObservation(
                original_url=url,
                current_url="",
                navigation_error=self._failure_reason(driver, exc),
            )
        self._wait_ready(driver)
        return self._observe_with_challenges(driver, url)

    def _observe_with_challenges(self, driver, url, depth=0):
        observation = self._extract(driver, url)
        if observation.navigation_error:
            return observation
        if observation.captcha_detected or observation.consent_gate_detected:
            reason = (
                "captcha_required"
                if observation.captcha_detected
                else "consent_gate_blocked"
            )
            if depth >= 3:
                return observation
            self._await_challenge(driver, reason)
            # Re-observe from scratch: no stale DOM references survive a
            # challenge transition.
            return self._observe_with_challenges(driver, url, depth + 1)
        return observation

    def _await_challenge(self, driver, reason):
        deadline_at = (
            _datetime.datetime.now(_datetime.timezone.utc)
            + _datetime.timedelta(seconds=self.challenge_deadline)
        ).isoformat(timespec="seconds")
        self.on_waiting(reason, deadline_at)
        if self.reveal_on_challenge:
            self._reveal()
        self.logger(
            "[WARN] User action required in the browser "
            f"({reason}); waiting up to {self.challenge_deadline:.0f}s."
        )
        started = self.clock()
        try:
            while True:
                if self._cancelled():
                    return
                if self.clock() - started > self.challenge_deadline:
                    raise ChallengeDeadlineError(
                        "captcha_timeout"
                        if reason == "captcha_required"
                        else "consent_gate_blocked"
                    )
                observation = self._extract(driver, None, light=True)
                if not (
                    observation.captcha_detected
                    or observation.consent_gate_detected
                ):
                    self.on_waiting_cleared()
                    self.logger("[INFO] Challenge solved. Resuming checks.")
                    return
                self.sleep(0.25)
        finally:
            self._hide()

    def _wait_ready(self, driver):
        """Wait until the page can be decided, not until it stops loading.

        A room mounts its account notice, its player or its offline shutter
        about a second into the load and never revises it; the load event
        lands roughly twenty seconds later, after media and telemetry that
        say nothing about the account. Waiting for it cost the step most of
        its runtime, so stop at the first decisive marker and keep the load
        event only as the fallback for a page that shows none.
        """
        started = self.clock()
        while self.clock() - started <= self.navigation_timeout:
            if self._cancelled():
                return
            try:
                if driver.execute_script("return document.readyState") == (
                    "complete"
                ):
                    return
            except Exception:
                return
            if self._has_decisive_marker(driver):
                return
            self.sleep(0.25)

    def _has_decisive_marker(self, driver) -> bool:
        from selenium.webdriver.common.by import By

        try:
            if driver.find_elements(By.TAG_NAME, "video"):
                return True
            for selector in (
                self.NOTICE_SELECTOR,
                self.OFFLINE_SELECTOR,
                self.NOT_FOUND_SELECTOR,
            ):
                if driver.find_elements(By.CSS_SELECTOR, selector):
                    return True
        except Exception:
            # A driver that cannot answer will not answer later either;
            # let the extraction below name the failure.
            return True
        return False

    @staticmethod
    def _navigation_reason(exc) -> str:
        message = str(exc).lower()
        # A dead driver answers every later command with a transport
        # error, not a Selenium session error, so match those too. Getting
        # this wrong reports a lost browser as a per-page selector fault.
        for marker in (
            "invalid session",
            "session deleted",
            "no such session",
            "not connected to devtools",
            "chrome not reachable",
            "disconnected",
            "connection refused",
            "actively refused",
            "max retries",
            "failed to establish",
            "target closed",
            "browser has closed",
            "connection aborted",
            "remote end closed",
            "cannot connect to chrome",
            "winerror 10061",
            # The renderer can die while the browser process and its window
            # keep answering; observed on a live 341-candidate step as
            # "Message: tab crashed (Session info: chrome=150...)".
            "tab crashed",
            "unable to receive message from renderer",
            "renderer process",
        ):
            if marker in message:
                return "browser_lost"
        if "timeout" in message or "timed out" in message:
            return "navigation_timeout"
        return "selector_error"

    @staticmethod
    def _has_window(driver) -> bool:
        """Whether the browser still has a window to answer for a page."""
        try:
            return bool(driver.window_handles)
        except Exception:
            return False

    def _failure_reason(self, driver, exc) -> str:
        """Classify a page failure, asking the driver before believing it.

        Matching the exception's wording alone is whack-a-mole: a live run
        lost its window to a signature that was not on the list, so every
        remaining candidate was filed as a per-page selector fault and
        spent its whole retry budget in seconds against a browser that was
        already gone. A driver with no window cannot answer for any page,
        whatever it says, so that question decides it.
        """
        reason = self._navigation_reason(exc)
        if reason != "browser_lost" and not self._has_window(driver):
            reason = "browser_lost"
        # The wording is the one thing the previous two failures needed and
        # did not record. Log each distinct signature once: enough to
        # diagnose, bounded enough not to flood a 700-candidate step.
        text = " ".join(str(exc).split())[:120] or type(exc).__name__
        if text not in self._seen_signatures:
            self._seen_signatures.add(text)
            self.logger(f"[WARN] Page failure read as {reason}: {text}")
        return reason

    def _extract(self, driver, url, light=False) -> StripchatPageObservation:
        from selenium.webdriver.common.by import By

        original = url or ""
        try:
            current_url = str(driver.current_url or "")
            title = str(driver.title or "")
            page_source = str(driver.page_source or "")
        except Exception as exc:
            return StripchatPageObservation(
                original_url=original or "https://stripchat.com/placeholder/",
                current_url="",
                navigation_error=self._failure_reason(driver, exc),
            )
        lowered = f"{title}\n{page_source}".lower()
        captcha = any(
            marker in lowered for marker in self._CAPTCHA_MARKERS
        )
        if light:
            return StripchatPageObservation(
                original_url=original or "https://stripchat.com/placeholder/",
                current_url=current_url,
                captcha_detected=captcha,
                consent_gate_detected=self._any_visible(
                    driver, By, self.CONSENT_SELECTOR
                ),
            )
        try:
            ready_state = str(
                driver.execute_script("return document.readyState") or ""
            )
        except Exception:
            ready_state = ""
        try:
            notice_texts = tuple(
                element.text
                for element in driver.find_elements(
                    By.CSS_SELECTOR, self.NOTICE_SELECTOR
                )
                if self._displayed(element) and (element.text or "").strip()
            )
            hidden_classes = sum(
                1
                for element in driver.find_elements(
                    By.CSS_SELECTOR, self.HIDDEN_CLASS_SELECTOR
                )
                if self._displayed(element)
            )
            disabled_classes = sum(
                1
                for element in driver.find_elements(
                    By.CSS_SELECTOR, self.DISABLED_CLASS_SELECTOR
                )
                if self._displayed(element)
            )
            # The live accessible probe rendered a video that headless
            # Chrome marked as not displayed; count elements, not
            # visibility.
            videos = len(driver.find_elements(By.TAG_NAME, "video"))
            player_containers = len(
                driver.find_elements(
                    By.CSS_SELECTOR, self.PLAYER_CONTAINER_SELECTOR
                )
            )
            # The offline shutter is laid out inside the video frame and so
            # reports a zero-size box on every offline room; the same trap
            # the video element already documents. Count them instead.
            offline = self._any_present(driver, By, self.OFFLINE_SELECTOR)
            not_found = self._any_present(
                driver, By, self.NOT_FOUND_SELECTOR
            )
            shutter_status = self._first_text(
                driver, By, self.SHUTTER_STATUS_SELECTOR
            )
            login_gate = self._any_visible(
                driver, By, self.LOGIN_GATE_SELECTOR
            )
            consent = self._any_visible(driver, By, self.CONSENT_SELECTOR)
            body_readable = bool(page_source.strip())
        except Exception as exc:
            return StripchatPageObservation(
                original_url=original or "https://stripchat.com/placeholder/",
                current_url=current_url,
                navigation_error=self._failure_reason(driver, exc),
            )
        return StripchatPageObservation(
            original_url=original or "https://stripchat.com/placeholder/",
            current_url=current_url,
            ready_state=ready_state,
            account_notice_texts=notice_texts,
            visible_hidden_selector_count=hidden_classes,
            visible_disabled_selector_count=disabled_classes,
            video_element_count=videos,
            player_container_count=player_containers,
            captcha_detected=captcha,
            login_gate_detected=login_gate,
            consent_gate_detected=consent,
            offline_notice_detected=offline,
            not_found_notice_detected=not_found,
            shutter_status_text=shutter_status,
            body_readable=body_readable,
        )

    @staticmethod
    def _displayed(element) -> bool:
        try:
            return bool(element.is_displayed())
        except Exception:
            return False

    def _any_visible(self, driver, By, selector) -> bool:
        """For modals, which sit in the DOM of every room until opened."""
        try:
            return any(
                self._displayed(element)
                for element in driver.find_elements(
                    By.CSS_SELECTOR, selector
                )
            )
        except Exception:
            return False

    def _any_present(self, driver, By, selector) -> bool:
        """For markup a room only mounts when it is in that state."""
        try:
            return bool(driver.find_elements(By.CSS_SELECTOR, selector))
        except Exception:
            return False

    def _first_text(self, driver, By, selector) -> str:
        """First non-empty wording under a selector, bounded in length.

        Reads ``textContent`` rather than the rendered text: the shutter is
        laid out inside the video frame and reports a zero-size box, for
        which Selenium returns an empty string however much the element
        says.
        """
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                text = str(
                    element.get_attribute("textContent") or ""
                ).strip()
                if text:
                    return text[:240]
        except Exception:
            return ""
        return ""


class HardenedStripchatRunner:
    """Typed Stripchat Steps 1-3 bound to one immutable generation.

    Step 4 (browser verification and publication) is layered on in later
    phases; every step here returns exactly one StepOutcome and writes
    only run-owned artifacts, never root compatibility files.
    """

    spec = STRIPCHAT_SPEC

    def __init__(
        self,
        store: StripchatRunStore,
        *,
        api_client_factory: Callable[[str], StripchatApiClient] | None = None,
        policy_observer: Callable[[], Mapping[str, object]] | None = None,
        target_relay_prefix: str | None = None,
        logger=None,
        stop_token=None,
        progress: Callable[[ProgressEvent], None] | None = None,
    ):
        self.store = store
        self._api_client_factory = api_client_factory
        self.policy_observer = policy_observer
        self.target_relay_prefix = (
            target_relay_prefix if target_relay_prefix is not None
            else store.config["relevant_values"].get("target_relay_prefix", "ie")
        )
        self.logger = logger or (lambda _message: None)
        self.stop_token = stop_token
        self.progress = progress or (lambda _event: None)

    # ------------------------------------------------------------------

    def _api_client(self, route: str) -> StripchatApiClient:
        if self._api_client_factory is not None:
            return self._api_client_factory(route)
        values = self.store.config["relevant_values"]
        return StripchatApiClient(
            logger=self.logger,
            stop_token=self.stop_token,
            page_size=values["page_size"],
            max_pages=values["max_pages"],
            max_models=values["max_models"],
            page_retries=values["page_retries"],
            minimum_request_interval=values["minimum_request_interval"],
            max_retry_delay=values["max_retry_delay"],
            min_successful_passes=values["min_successful_passes"],
            max_passes=values["max_passes"],
            required_stable_pairs=values["required_stable_pairs"],
            jaccard_threshold=values["jaccard_threshold"],
            count_delta_threshold=values["count_delta_threshold"],
            maximum_quarantine_ratio=values["maximum_quarantine_ratio"],
            duplicate_ratio_threshold=values["duplicate_ratio_threshold"],
            generation_poll_interval=values["generation_poll_interval"],
            max_generation_wait=values["max_generation_wait"],
        )

    def _observe_policy(self) -> Mapping[str, object]:
        if self.policy_observer is None:
            return {"vpn_state": "unknown", "relay_code": None}
        try:
            return dict(self.policy_observer())
        except Exception:
            return {"vpn_state": "unknown", "relay_code": None}

    def _require_policy(self, context, step, *, connected: bool):
        values = self.store.config["relevant_values"]
        if values.get("vpn_provider", "mullvad") == "manual":
            # Manual Step 3 is a pure comparison of the sealed snapshots.
            if step is StepName.COMPARE and not values.get("local_recheck"):
                return None
            observed = self._observe_policy()
            route = "vpn" if connected else "local"
            expected = {
                "source": "operator_declared", "operator_confirmed": True,
                "vpn_state": "unknown", "declared_route": route,
                "step": step.value, "run_id": context.run_id,
                "generation_id": context.generation_id, "session_id": context.session_id,
            }
            valid = all(observed.get(key) == value for key, value in expected.items())
            valid = valid and observed.get("operator_confirmed") is True
            if not valid:
                return self._outcome(
                    context, step, OutcomeStatus.FAILED, utc_now_iso(),
                    error_code="manual_network_confirmation_required",
                    error_message=f"Explicit confirmation of the {route} route is required for this step.",
                )
            attestation = {
                "source": "operator_declared", "declared_route": route,
                "operator_confirmed": True, "observed": {"vpn_state": "unknown", "relay_code": None},
                "run_id": context.run_id, "generation_id": context.generation_id,
                "session_id": context.session_id, "step": step.value,
                "declared_at": str(observed.get("declared_at") or utc_now_iso()),
            }
            self.store.update_manifest(context, lambda manifest: manifest.setdefault(
                "network_attestations", {}
            ).__setitem__(step.value, attestation))
            self.store.append_event(context, {"kind": "network_attestation", **attestation})
            return None
        observed = self._observe_policy()
        state = observed.get("vpn_state")
        if connected:
            relay = str(observed.get("relay_code") or "")
            ok = state == "connected" and relay.startswith(
                self.target_relay_prefix
            )
        else:
            ok = state == "disconnected"
        if not ok:
            return self._outcome(
                context,
                step,
                OutcomeStatus.FAILED,
                utc_now_iso(),
                error_code="machine_policy_precondition",
                error_message=(
                    f"required {'connected ' + self.target_relay_prefix if connected else 'disconnected'};"
                    f" observed {state}/{observed.get('relay_code')}"
                ),
            )
        return None

    def _outcome(
        self, context, step, status, started_at, *, record=True, **values
    ) -> StepOutcome:
        factory = {
            OutcomeStatus.SUCCEEDED: StepOutcome.succeeded,
            OutcomeStatus.CANCELLED: StepOutcome.cancelled,
            OutcomeStatus.FAILED: StepOutcome.failed,
            OutcomeStatus.INCOMPLETE: StepOutcome.incomplete,
        }[status]
        outcome = factory(
            run_id=context.run_id,
            generation_id=context.generation_id,
            platform=context.platform_key,
            step=step,
            started_at=started_at,
            session_id=context.session_id,
            **values,
        )
        if record:
            try:
                self.store.record_step_attempt(context, outcome)
            except (OSError, ValueError):
                pass
        return outcome

    def _emit(self, context, step, phase, message, completed=0, total=None):
        self.progress(
            ProgressEvent(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform_key,
                step=step,
                phase=phase,
                completed=completed,
                total=total,
                message=message,
                status=ProgressStatus.RUNNING,
                session_id=context.session_id,
            )
        )

    @staticmethod
    def _sorted_urls(result: StripchatSnapshotResult) -> list[str]:
        return sorted(
            (model.canonical_url for model in result.models),
            key=model_lookup_key,
        )

    def _snapshot_step(
        self,
        context: RunContext,
        step: StepName,
        *,
        route: str,
        prefix: str,
        upstream: Mapping[str, str] | None = None,
    ) -> StepOutcome:
        started_at = utc_now_iso()
        self._emit(context, step, "snapshot", f"Collecting {route} snapshot")
        result = self._api_client(route).collect_snapshot()
        self.store.append_event(
            context,
            {
                "kind": "api_snapshot",
                "step": step.value,
                "route": route,
                "metrics": result.metrics(),
            },
        )
        if result.status is OutcomeStatus.CANCELLED:
            return self._outcome(
                context,
                step,
                OutcomeStatus.CANCELLED,
                started_at,
                error_code="api_cancelled",
                error_message="Stopped safely during the API snapshot.",
            )
        if result.status is not OutcomeStatus.SUCCEEDED:
            return self._outcome(
                context,
                step,
                OutcomeStatus.INCOMPLETE,
                started_at,
                error_code=result.error_code or "api_snapshot_unstable",
                error_message=(
                    "The API snapshot could not be proved complete."
                ),
                retryable=True,
            )
        urls = self._sorted_urls(result)
        counts: dict[str, int] = {}
        window_passes = [
            item
            for item in result.passes
            if item.pass_index in result.convergence_window
        ]
        for item in window_passes:
            for model in item.models:
                key = model.username.lower()
                counts[key] = counts.get(key, 0) + 1
        urls_record = self.store.artifact_lines(
            context,
            step,
            f"{prefix}_urls.txt",
            urls,
            logical_name=f"{step.value.split('_')[0]}.{prefix}_urls",
            schema="canonical_url_set/v1",
            upstream=upstream,
        )
        report_record = self.store.artifact_json(
            context,
            step,
            f"{prefix}_snapshot.json",
            result.metrics(),
            logical_name=f"{step.value.split('_')[0]}.{prefix}_report",
            schema="snapshot_report/v1",
            record_count=len(result.passes),
            upstream=upstream,
        )
        metadata_record = self.store.artifact_json(
            context,
            step,
            f"{prefix}_identity_counts.json",
            {"passes": len(window_passes), "counts": counts},
            logical_name=f"{step.value.split('_')[0]}.{prefix}_metadata",
            schema="vpn_metadata/v1",
            record_count=len(counts),
            upstream=upstream,
        )
        return self._outcome(
            context,
            step,
            OutcomeStatus.SUCCEEDED,
            started_at,
            output_count=len(urls),
            processed_count=len(urls),
            artifacts=(urls_record, report_record, metadata_record),
            warnings=result.warnings,
            summary_counts={
                "accepted": len(urls),
                "quarantined": len(result.quarantined),
            },
        )

    # ------------------------------------------------------------------
    # Steps

    def run_step1(self, context: RunContext) -> StepOutcome:
        failure = self._require_policy(
            context, StepName.VPN_SNAPSHOT, connected=True
        )
        if failure is not None:
            return failure
        return self._snapshot_step(
            context, StepName.VPN_SNAPSHOT, route="vpn", prefix="vpn"
        )

    def run_step2(self, context: RunContext) -> StepOutcome:
        started_at = utc_now_iso()
        try:
            self.store.require_step_success(context, StepName.VPN_SNAPSHOT)
            vpn_document, _ = self.store.load_artifact(
                context, "step1.vpn_urls"
            )
        except RunStoreError as exc:
            return self._outcome(
                context,
                StepName.LOCAL_SNAPSHOT,
                OutcomeStatus.FAILED,
                started_at,
                error_code=exc.code,
                error_message=str(exc),
            )
        failure = self._require_policy(
            context, StepName.LOCAL_SNAPSHOT, connected=False
        )
        if failure is not None:
            return failure
        return self._snapshot_step(
            context,
            StepName.LOCAL_SNAPSHOT,
            route="local",
            prefix="local",
            upstream={"step1.vpn_urls": vpn_document["sha256"]},
        )

    def run_step3(self, context: RunContext) -> StepOutcome:
        started_at = utc_now_iso()
        try:
            self.store.require_step_success(context, StepName.VPN_SNAPSHOT)
            self.store.require_step_success(context, StepName.LOCAL_SNAPSHOT)
            vpn_document, _ = self.store.load_artifact(
                context, "step1.vpn_urls"
            )
            local_document, _ = self.store.load_artifact(
                context, "step2.local_urls"
            )
            vpn_urls = self.store.read_artifact_lines(
                context, "step1.vpn_urls"
            )
            local_urls = self.store.read_artifact_lines(
                context, "step2.local_urls"
            )
        except RunStoreError as exc:
            return self._outcome(
                context,
                StepName.COMPARE,
                OutcomeStatus.FAILED,
                started_at,
                error_code=exc.code,
                error_message=str(exc),
            )
        failure = self._require_policy(
            context, StepName.COMPARE, connected=False
        )
        if failure is not None:
            return failure
        local_keys = {model_lookup_key(url) for url in local_urls}
        suspects = [
            url
            for url in vpn_urls
            if model_lookup_key(url) not in local_keys
        ]
        recheck_metrics = None
        if suspects and self.store.config["relevant_values"].get(
            "local_recheck", True
        ):
            self._emit(
                context,
                StepName.COMPARE,
                "local_recheck",
                f"Rechecking {len(suspects)} candidates locally",
            )
            recheck = self._api_client("local").collect_snapshot()
            recheck_metrics = recheck.metrics()
            self.store.append_event(
                context,
                {
                    "kind": "api_snapshot",
                    "step": StepName.COMPARE.value,
                    "route": "local_recheck",
                    "metrics": recheck_metrics,
                },
            )
            if recheck.status is OutcomeStatus.CANCELLED:
                return self._outcome(
                    context,
                    StepName.COMPARE,
                    OutcomeStatus.CANCELLED,
                    started_at,
                    error_code="api_cancelled",
                    error_message="Stopped safely during the local recheck.",
                )
            if recheck.status is not OutcomeStatus.SUCCEEDED:
                # Never merge a partial recheck: fail closed instead of
                # publishing candidates that might be false positives.
                return self._outcome(
                    context,
                    StepName.COMPARE,
                    OutcomeStatus.INCOMPLETE,
                    started_at,
                    error_code=recheck.error_code or "api_snapshot_unstable",
                    error_message=(
                        "The local recheck could not be proved complete."
                    ),
                    retryable=True,
                )
            recheck_keys = {
                model.lookup_key for model in recheck.models
            }
            suspects = [
                url
                for url in suspects
                if model_lookup_key(url) not in recheck_keys
            ]
        candidates = sorted(suspects, key=model_lookup_key)
        upstream = {
            "step1.vpn_urls": vpn_document["sha256"],
            "step2.local_urls": local_document["sha256"],
        }
        candidates_record = self.store.artifact_lines(
            context,
            StepName.COMPARE,
            "candidates.txt",
            candidates,
            logical_name="step3.candidates",
            schema="canonical_url_set/v1",
            upstream=upstream,
        )
        report = {
            "rule_version": "sc-compare-1",
            "vpn_count": len(vpn_urls),
            "local_count": len(local_urls),
            "raw_difference": len(
                [
                    url
                    for url in vpn_urls
                    if model_lookup_key(url) not in local_keys
                ]
            ),
            "candidate_count": len(candidates),
            "local_recheck": recheck_metrics,
            "upstream_sha256": upstream,
        }
        report_record = self.store.artifact_json(
            context,
            StepName.COMPARE,
            "debug.json",
            report,
            logical_name="step3.debug",
            schema="comparison_report/v1",
            record_count=len(candidates),
            upstream=upstream,
        )
        return self._outcome(
            context,
            StepName.COMPARE,
            OutcomeStatus.SUCCEEDED,
            started_at,
            input_count=len(vpn_urls),
            output_count=len(candidates),
            processed_count=len(vpn_urls),
            artifacts=(candidates_record, report_record),
            summary_counts={
                "vpn": len(vpn_urls),
                "local": len(local_urls),
                "candidates": len(candidates),
            },
        )

    def build_finalizer(
        self,
        context: RunContext,
        master_base_dir,
        *,
        shadow: bool = False,
        backup_callback=None,
    ) -> WorkflowFinalizer:
        """Two-phase publication bound to this run's generation."""
        return WorkflowFinalizer(
            self.store,
            context,
            stripchat_master_repository(master_base_dir),
            master_entry_builder=merge_blocked_into_master,
            projections=STRIPCHAT_PROJECTIONS,
            shadow=shadow,
            stop_token=self.stop_token,
            logger=self.logger,
            backup_callback=backup_callback,
        )

    def run_step4(
        self,
        context: RunContext,
        *,
        provider: StripchatBrowserProvider | None = None,
        provider_factory: Callable[..., StripchatBrowserProvider]
        | None = None,
        finalizer: Callable[[dict], StepOutcome] | None = None,
        resume: bool = True,
    ) -> StepOutcome:
        """Checkpointed tri-state verification of the Step 3 candidates.

        With ``finalizer=None`` the step stops after verification without
        publishing anything (shadow behavior); the transactional finalizer
        supplies the committed-publication outcome when enabled.
        """
        started_at = utc_now_iso()
        try:
            self.store.require_step_success(context, StepName.COMPARE)
            self.store.load_artifact(context, "step3.candidates")
        except RunStoreError as exc:
            return self._outcome(
                context,
                StepName.VERIFY,
                OutcomeStatus.FAILED,
                started_at,
                error_code=exc.code,
                error_message=str(exc),
            )
        failure = self._require_policy(
            context, StepName.VERIFY, connected=False
        )
        if failure is not None:
            return failure
        values = self.store.config["relevant_values"]
        owned_provider = provider is None
        if provider is None:
            if provider_factory is None:
                return self._outcome(
                    context,
                    StepName.VERIFY,
                    OutcomeStatus.FAILED,
                    started_at,
                    error_code="invalid_request",
                    error_message="no browser provider configured",
                )
            provider = provider_factory(
                stop_token=self.stop_token,
                on_waiting=lambda reason, deadline: self.store.set_waiting(
                    context, reason, deadline
                ),
                on_waiting_cleared=lambda: self.store.clear_waiting(context),
                navigation_timeout=values["navigation_timeout_seconds"],
                challenge_deadline=values["challenge_deadline_seconds"],
            )

        def verdict_for(url: str, attempt: int):
            observation = provider.observe(url)
            return classify_stripchat_page(
                observation,
                attempt=attempt,
                run_id=context.run_id,
                generation_id=context.generation_id,
            )

        verifier = GenericCheckpointedVerifier(
            self.store,
            context,
            verdict_for,
            identity_key=model_lookup_key,
            canonicalize_url=canonical_model_url,
            terminal_nontarget_reasons=(),
            finalizer=finalizer,
            stop_token=self.stop_token,
            progress_callback=self.progress,
            max_retry_epochs=values["max_retry_epochs"],
            attempt_budget_per_epoch=values["attempt_budget_per_epoch"],
            escalation_exceptions=(
                ChallengeDeadlineError,
                BrowserLostError,
            ),
        )
        try:
            return verifier.verify("step3.candidates", resume=resume)
        except ChallengeDeadlineError as exc:
            self.store.clear_waiting(context)
            return self._outcome(
                context,
                StepName.VERIFY,
                OutcomeStatus.INCOMPLETE,
                started_at,
                error_code=exc.reason,
                error_message=(
                    "The browser challenge was not resolved before its "
                    "deadline. Resume is available."
                ),
                resumable=True,
                retryable=True,
            )
        except BrowserLostError as exc:
            # Stop at the checkpoint instead of spending every remaining
            # candidate's retry budget on a browser that is gone.
            self.store.clear_waiting(context)
            return self._outcome(
                context,
                StepName.VERIFY,
                OutcomeStatus.INCOMPLETE,
                started_at,
                error_code="browser_lost",
                error_message=(
                    "The verification browser closed and could not be "
                    "restarted. Verified results are kept; Resume "
                    "continues from where it stopped."
                ),
                resumable=True,
                retryable=True,
            )
        finally:
            if owned_provider:
                provider.close()
