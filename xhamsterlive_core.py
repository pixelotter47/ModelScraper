import os

from selenium.webdriver.common.by import By

from legacy_stripchat_family import LegacyStripchatFamilyRunner


class XHamsterLivePaths:
    def __init__(self, session_folder):
        self.session_folder = session_folder
        self.vpn_list = os.path.join(session_folder, "xhl_vpn_list.txt")
        self.local_list = os.path.join(session_folder, "xhl_local_list.txt")
        self.candidates = os.path.join(session_folder, "xhl_candidates.txt")
        self.verified = os.path.join(session_folder, "FINAL_BLOCKED.txt")
        self.debug = os.path.join(session_folder, "xhl_debug_log.txt")
        self.metadata = os.path.join(session_folder, "xhl_metadata.json")


class XHamsterLiveRunner(LegacyStripchatFamilyRunner):
    platform_name = "XHamsterLive"
    site_domain = "xhamsterlive.com"
    api_host = "go.xhamsterlive.com"
    profile_dir_name = "uc_profile_xhl"

    def _build_paths(self, session_path):
        return XHamsterLivePaths(session_path)

    def _header_or_description_contains(self, driver, needle):
        try:
            elements = driver.find_elements(
                By.CSS_SELECTOR,
                ".account-hidden-header, .account-hidden-description, "
                ".account-disabled-header, .account-disabled-description",
            )
        except Exception:
            return False

        needle_text = (needle or "").strip().lower()
        for element in elements:
            try:
                text = (element.text or "").strip().lower()
            except Exception:
                text = ""
            if needle_text and needle_text in text:
                return True
        return False

    def _is_blocked_page(self, driver, page_src_lower):
        if self._has_hidden_text(page_src_lower):
            return True
        if self._header_or_description_contains(driver, "hidden"):
            return True
        return False

    def _is_disabled_page(self, driver, page_src_lower):
        # XHamsterLive reuses account-disabled classes on "Account Hidden" pages.
        if self._has_hidden_text(page_src_lower):
            return False
        if self._header_or_description_contains(driver, "hidden"):
            return False
        return super()._is_disabled_page(driver, page_src_lower)

    def compare_lists(self):
        self._ensure_session()
        if not os.path.exists(self.paths.vpn_list) or not os.path.exists(
            self.paths.local_list
        ):
            self.log("[WARN] Missing files. Run steps 1 and 2 first.")
            return

        self.log("[INFO] Loading lists...")
        with open(self.paths.vpn_list, "r", encoding="utf-8") as handle:
            vpn = set(self._normalize_url(line) for line in handle if line.strip())
        with open(self.paths.local_list, "r", encoding="utf-8") as handle:
            local = set(self._normalize_url(line) for line in handle if line.strip())

        final_candidates = sorted(vpn - local)

        self.log("[INFO] Preliminary analysis:")
        self.log(f"[INFO] VPN list: {len(vpn)}")
        self.log(f"[INFO] Local list: {len(local)}")
        self.log(f"[INFO] Missing locally: {len(final_candidates)}")
        self.log("[INFO] Saving raw VPN minus Local difference without extra checks.")

        with open(self.paths.candidates, "w", encoding="utf-8") as handle:
            for link in final_candidates:
                handle.write(link + "\n")

        with open(self.paths.debug, "w", encoding="utf-8") as handle:
            handle.write("\n".join(final_candidates))

        self.log(f"[INFO] Candidates saved: {self.paths.candidates}")
        self.log(f"[INFO] Debug log saved: {self.paths.debug}")
