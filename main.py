"""AC Full Power - Decky Loader plugin.

While a charger is connected, every performance limit that is currently active
(TDP, manual GPU clock, CPU clock cap) is lifted. When the charger is removed,
the exact values that were active before are written back.

Nothing is configured in the plugin: it snapshots whatever Steam / SteamOS has
applied to the hardware at the moment the charger is plugged in.
"""

import asyncio
import glob
import json
import os
import re

import decky

POLL_SECONDS = 2
STATE_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "state.json")
SETTINGS_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")


# --------------------------------------------------------------------------
# small sysfs helpers
# --------------------------------------------------------------------------
def read(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def read_int(path):
    v = read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def write(path, value):
    try:
        with open(path, "w") as f:
            f.write(f"{value}\n")
        return True
    except Exception as e:
        decky.logger.error(f"write {path} <- {value!r} failed: {e}")
        return False


def boot_id():
    return read("/proc/sys/kernel/random/boot_id") or ""


def on_charger():
    """True if any external power source (not battery / not a peripheral) is online."""
    for d in glob.glob("/sys/class/power_supply/*"):
        t = read(f"{d}/type")
        if t is None or t == "Battery":
            continue
        if read(f"{d}/scope") == "Device":  # e.g. wireless controllers
            continue
        if read(f"{d}/online") == "1":
            return True
    return False


# --------------------------------------------------------------------------
# tunables: each knows how to read its current state, what "unlimited" looks
# like, and how to write a state back
# --------------------------------------------------------------------------
class Tdp:
    id = "tdp"

    def _caps(self):
        res = []
        for d in glob.glob("/sys/class/hwmon/hwmon*"):
            for n in ("power1_cap", "power2_cap"):
                p = f"{d}/{n}"
                if os.path.exists(p) and os.path.exists(p + "_max"):
                    res.append(p)
        return res

    def current(self):
        st = {p: read_int(p) for p in self._caps()}
        return st if st and all(v is not None for v in st.values()) else None

    def released(self):
        st = {p: read_int(p + "_max") for p in self._caps()}
        return st if st and all(v is not None for v in st.values()) else None

    def apply(self, state):
        for p, v in state.items():
            write(p, v)


class Gpu:
    id = "gpu"

    def _dev(self):
        for p in sorted(glob.glob("/sys/class/drm/card*/device/pp_od_clk_voltage")):
            return os.path.dirname(p)
        return None

    def _parse(self):
        dev = self._dev()
        if not dev:
            return None
        txt = read(f"{dev}/pp_od_clk_voltage")
        level = read(f"{dev}/power_dpm_force_performance_level")
        if not txt or not level:
            return None
        cur = re.search(r"OD_SCLK:\s*\n\s*0:\s*(\d+)\s*Mhz\s*\n\s*1:\s*(\d+)\s*Mhz", txt, re.I)
        rng = re.search(r"OD_RANGE:.*?SCLK:\s*(\d+)\s*Mhz\s*(\d+)\s*Mhz", txt, re.I | re.S)
        if not cur or not rng:
            return None
        return dev, level, (int(cur.group(1)), int(cur.group(2))), (int(rng.group(1)), int(rng.group(2)))

    def current(self):
        p = self._parse()
        if not p:
            return None
        _, level, (lo, hi), _ = p
        return {"level": level, "min": lo, "max": hi}

    def released(self):
        p = self._parse()
        if not p:
            return None
        _, _, _, (rlo, rhi) = p
        return {"level": "auto", "min": rlo, "max": rhi}

    def apply(self, state):
        dev = self._dev()
        if not dev:
            return
        write(f"{dev}/power_dpm_force_performance_level", "manual")
        write(f"{dev}/pp_od_clk_voltage", f"s 0 {state['min']}")
        write(f"{dev}/pp_od_clk_voltage", f"s 1 {state['max']}")
        write(f"{dev}/pp_od_clk_voltage", "c")
        write(f"{dev}/power_dpm_force_performance_level", state["level"])


class Cpu:
    id = "cpu"

    def _paths(self):
        return glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_max_freq")

    def current(self):
        st = {p: read_int(p) for p in self._paths()}
        return st if st and all(v is not None for v in st.values()) else None

    def released(self):
        st = {p: read_int(os.path.dirname(p) + "/cpuinfo_max_freq") for p in self._paths()}
        return st if st and all(v is not None for v in st.values()) else None

    def apply(self, state):
        for p, v in state.items():
            write(p, v)


# --------------------------------------------------------------------------
# plugin
# --------------------------------------------------------------------------
class Plugin:
    tunables = [Tdp(), Gpu(), Cpu()]
    saved = {}      # tunable id -> user's state, only while limits are lifted
    enabled = True
    plugged = False
    task = None

    # ---- persistence -----------------------------------------------------
    def _load_settings(self):
        try:
            with open(SETTINGS_FILE) as f:
                self.enabled = bool(json.load(f).get("enabled", True))
        except Exception:
            self.enabled = True

    def _save_settings(self):
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            with open(SETTINGS_FILE, "w") as f:
                json.dump({"enabled": self.enabled}, f)
        except Exception as e:
            decky.logger.error(f"saving settings failed: {e}")

    def _load_state(self):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            if data.get("boot_id") == boot_id():
                self.saved = data.get("saved", {})
        except Exception:
            self.saved = {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump({"boot_id": boot_id(), "saved": self.saved}, f)
        except Exception as e:
            decky.logger.error(f"saving state failed: {e}")

    # ---- core logic ------------------------------------------------------
    def _lift_limits(self):
        changed = False
        for t in self.tunables:
            cur, rel = t.current(), t.released()
            if cur is None or rel is None:
                continue
            if cur != rel:
                # Something limits us: either the user's profile or Steam just
                # (re)applied one. Remember it and lift it.
                self.saved[t.id] = cur
                t.apply(rel)
                changed = True
                decky.logger.info(f"lifted {t.id}: {cur} -> {rel}")
        if changed:
            self._save_state()

    def _restore_limits(self):
        for t in self.tunables:
            if t.id in self.saved:
                decky.logger.info(f"restoring {t.id}: {self.saved[t.id]}")
                t.apply(self.saved[t.id])
        self.saved = {}
        self._save_state()

    def _tick(self):
        self.plugged = on_charger()
        if self.enabled and self.plugged:
            self._lift_limits()
        elif self.saved:
            self._restore_limits()

    async def _loop(self):
        while True:
            try:
                self._tick()
            except Exception as e:
                decky.logger.error(f"tick failed: {e}")
            await asyncio.sleep(POLL_SECONDS)

    # ---- Decky lifecycle -------------------------------------------------
    async def _main(self):
        self._load_settings()
        self._load_state()
        decky.logger.info("AC Full Power started")
        self.task = asyncio.get_event_loop().create_task(self._loop())

    async def _unload(self):
        if self.task:
            self.task.cancel()
        if self.saved:
            self._restore_limits()
        decky.logger.info("AC Full Power unloaded")

    async def _uninstall(self):
        pass

    # ---- frontend API ----------------------------------------------------
    async def get_status(self):
        return {
            "enabled": self.enabled,
            "plugged": self.plugged,
            "lifted": list(self.saved.keys()),
            "detected": [t.id for t in self.tunables if t.current() is not None],
        }

    async def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)
        self._save_settings()
        if not self.enabled and self.saved:
            self._restore_limits()
        return True
