from pathlib import Path
import unittest


BASE_DIR = Path(__file__).resolve().parent


class WebStaticTests(unittest.TestCase):
    def test_static_files_reference_browser_api_contract(self):
        index = (BASE_DIR / "web_static" / "index.html").read_text(encoding="utf-8")
        script = (BASE_DIR / "web_static" / "app.js").read_text(encoding="utf-8")
        styles = (BASE_DIR / "web_static" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("Run Full Auto Flow", index)
        self.assertIn("/api/tasks/full-auto", script)
        self.assertIn("/api/blocked-list/load-text", script)
        self.assertIn("/ws/events", script)
        self.assertIn("PROFILE", script)
        self.assertIn("RESTRICTED PROFILE", script)
        self.assertIn("--accent", styles)
        self.assertIn(".location-profile-flag", styles)
        self.assertIn(".restricted-location-profile-flag", styles)
        self.assertIn("/api/settings", script)
        self.assertIn('id="targetCountries"', index)
        self.assertIn("payload.countryChoices", script)
        self.assertNotIn("Romanian profile confirmed geo-blocked", script)

    def test_error_envelope_is_rendered_as_a_message(self):
        script = (BASE_DIR / "web_static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("detail.message", script)
        self.assertNotIn("throw new Error(detail);", script)

    def test_untouched_headless_toggle_submits_null(self):
        script = (BASE_DIR / "web_static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("indeterminate", script)

    def test_stripchat_forces_visible_browser_controls(self):
        script = (BASE_DIR / "web_static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('data.platform === "Stripchat"', script)
        self.assertIn("headlessToggle.checked = false", script)
        self.assertIn("headlessToggle.disabled", script)
        self.assertIn("isStripchat\n    ? false", script)

    def test_resume_posts_a_body_and_waiting_reason_stays_visible(self):
        script = (BASE_DIR / "web_static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('if (task === "resume")', script)
        self.assertIn('runSelection: "resume"', script)
        self.assertIn("progress.waitingReason", script)

    def test_task_buttons_start_disabled_until_state_loads(self):
        index = (BASE_DIR / "web_static" / "index.html").read_text(
            encoding="utf-8"
        )
        task_buttons = [
            fragment.split("</button>", 1)[0]
            for fragment in index.split("<button")[1:]
            if "data-task=" in fragment.split("</button>", 1)[0]
        ]
        self.assertTrue(task_buttons)
        self.assertTrue(all(" disabled" in button for button in task_buttons))

    def test_launcher_starts_local_web_app(self):
        launcher = (BASE_DIR / "Start ModelScraper Web.bat").read_text(encoding="utf-8")

        self.assertIn("MODEL_SCRAPER_WEB_PORT", launcher)
        self.assertIn("web_app.py", launcher)
        self.assertIn("http://127.0.0.1:", launcher)


PROBE = r"""
import fs from "node:fs";
import vm from "node:vm";
const src = fs.readFileSync(process.argv[2], "utf8");
class El {
  constructor(){ this._checked=false; this.indeterminate=false;
    this.disabled=false; this.textContent=""; this.innerHTML=""; this.value="";
    this.style={}; this.classList={toggle(){},add(){},remove(){},
    contains(){return false;}}; }
  get checked(){ return this._checked; }
  set checked(v){ this._checked = !!v; }
  addEventListener(){} appendChild(){} querySelector(){ return new El(); }
  querySelectorAll(){ return []; } focus(){} setAttribute(){} removeAttribute(){}
}
const els = new Map();
const el = (id) => { if(!els.has(id)) els.set(id, new El()); return els.get(id); };
const ctx = {
  console, JSON, Math, Date, setTimeout, clearTimeout, setInterval,
  clearInterval,
  localStorage: { getItem: () => null, setItem(){} },
  location: { host: "127.0.0.1", protocol: "http:" },
  WebSocket: function(){ this.addEventListener=()=>{}; this.close=()=>{}; },
  fetch: async () => ({ ok: true, text: async () => "{}" }),
  document: { querySelector: (s) => el(s), querySelectorAll: () => [],
    getElementById: (i) => el("#"+i), createElement: () => new El(),
    addEventListener(){}, body: new El() },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src + ["", "globalThis.__state = state;"].join("\n"), ctx);
const S = ctx.__state;
const toggle = el("#headlessToggle");
const visit = (p) => {
  S.data = { platform: p, busy: false, status: "Idle", progress: {},
    lastOutcome: {}, logs: [], platforms: [], resume: { available: false },
    machinePolicy: {}, blockedModels: [], masterListModels: [] };
  try { ctx.render(); } catch (e) {}
  return ctx.taskPayload("step1").headless;
};
toggle.checked = true;
const seen = [visit("Chaturbate"), visit("Stripchat"), visit("Chaturbate"),
  visit("XHamsterLive")];
console.log(JSON.stringify(seen));
"""


class HeadlessToggleBehaviorTests(unittest.TestCase):
    """Visiting Stripchat must not disarm hide-browser for other platforms."""

    def test_preference_is_restored_after_leaving_stripchat(self):
        import json
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp, "probe.mjs")
            probe.write_text(PROBE, encoding="utf-8")
            result = subprocess.run(
                [node, str(probe), str(BASE_DIR / "web_static" / "app.js")],
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-800:])
            observed = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(
            observed,
            [True, False, True, True],
            "Stripchat forces visible, other platforms keep the user's choice",
        )

    def test_location_settings_and_generic_profile_rows_render(self):
        import json
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        probe_source = PROBE[:PROBE.index("const S = ctx.__state;")]
        probe_source = probe_source.replace(
            'this.style={};', 'this.style={}; this.dataset={}; this.children=[];'
        ).replace(
            'addEventListener(){} appendChild(){}',
            'addEventListener(){} appendChild(e){this.children.push(e);} append(...items){this.children.push(...items);}'
        )
        probe_source += r'''
const taskButtons = ["full-auto","resume","step1","step2","step3","step4","verify-master"].map(task => {
  const button=el("#task-"+task); button.dataset.task=task; return button;
});
ctx.document.querySelectorAll = selector => selector === "[data-task]" ? taskButtons : [];
ctx.applySettings({settings:{targetCountries:"JP",targetLocationTerms:"Tokyo",vpnRelayLocation:"ie",vpnProvider:"manual"},
  countryChoices:[{code:"JP",name:"Japan"},{code:"CA",name:"Canada"}],
  vpnLocations:[{code:"ie",name:"Ireland"}]});
ctx.applyState({platform:"Chaturbate",platforms:[],busy:false,logs:[],blockedModels:[],
  masterListModels:[{name:"example_model",country:"JP",vpn_location_profile_match:true,
    vpn_profile_match:{country:"JP",location:"Tokyo",match_reasons:["country_code"]},
    verification_reason:"access_restricted"}]});
const row = el("#masterTableBody").children[0];
const countrySelect = row.children[2].children[0];
const badge = row.children[0].children[1];
const beforeBusy = el("#locationSettingsFields").disabled;
const manualAutoDisabled = taskButtons[0].disabled && taskButtons[1].disabled;
const manualRelayDisabled = el("#vpnRelayLocation").disabled;
const manualConfirmation = ctx.taskPayload("step1").manualNetworkConfirmed;
ctx.__state.settings.vpnProvider="mullvad";
el("#vpnProvider").value="mullvad";
ctx.render();
const automaticAutoEnabled = !taskButtons[0].disabled && !taskButtons[1].disabled;
const automaticRelayEnabled = !el("#vpnRelayLocation").disabled;
const automaticConfirmation = ctx.taskPayload("step1").manualNetworkConfirmed;
ctx.applyState({...ctx.__state.data,busy:true});
console.log(JSON.stringify({countries:countrySelect.children.map(o=>o.value),
  selected:countrySelect.value,badge:badge.textContent,tooltip:badge.title,
  profile:el("#targetCountries").value,alias:el("#targetLocationTerms").value,
  beforeBusy,afterBusy:el("#locationSettingsFields").disabled,manualAutoDisabled,
  manualRelayDisabled,manualConfirmation,automaticAutoEnabled,automaticRelayEnabled,automaticConfirmation}));
'''
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp, "location-probe.mjs")
            probe.write_text(probe_source, encoding="utf-8")
            result = subprocess.run(
                [node, str(probe), str(BASE_DIR / "web_static" / "app.js")],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        observed = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(observed["countries"], ["", "JP", "CA"])
        self.assertEqual(observed["selected"], "JP")
        self.assertEqual(observed["profile"], "JP")
        self.assertEqual(observed["alias"], "Tokyo")
        self.assertEqual(observed["badge"], "RESTRICTED PROFILE")
        self.assertIn("Country: JP", observed["tooltip"])
        self.assertIn("Observed reason: access_restricted", observed["tooltip"])
        self.assertFalse(observed["beforeBusy"])
        self.assertTrue(observed["afterBusy"])
        self.assertTrue(observed["manualAutoDisabled"])
        self.assertTrue(observed["manualRelayDisabled"])
        self.assertTrue(observed["manualConfirmation"])
        self.assertTrue(observed["automaticAutoEnabled"])
        self.assertTrue(observed["automaticRelayEnabled"])
        self.assertFalse(observed["automaticConfirmation"])


if __name__ == "__main__":
    unittest.main()
