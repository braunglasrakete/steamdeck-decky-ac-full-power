import { definePlugin, callable } from "@decky/api";
import { PanelSection, PanelSectionRow, ToggleField, Field } from "@decky/ui";
import { useEffect, useState } from "react";
import { FaBolt } from "react-icons/fa";

type Status = {
  enabled: boolean;
  plugged: boolean;
  lifted: string[];
  detected: string[];
};

const getStatus = callable<[], Status>("get_status");
const setEnabled = callable<[boolean], boolean>("set_enabled");

const LABELS: Record<string, string> = { tdp: "TDP", gpu: "GPU clock", cpu: "CPU clock" };
const names = (ids: string[]) => (ids.length ? ids.map((i) => LABELS[i] ?? i).join(", ") : "none");

function Content() {
  const [status, setStatus] = useState<Status | null>(null);

  useEffect(() => {
    let alive = true;
    const refresh = async () => {
      try {
        const s = await getStatus();
        if (alive) setStatus(s);
      } catch (e) {
        console.error("AC Full Power: status failed", e);
      }
    };
    refresh();
    const id = setInterval(refresh, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!status) return <PanelSection title="AC Full Power">Loading…</PanelSection>;

  return (
    <PanelSection title="AC Full Power">
      <PanelSectionRow>
        <ToggleField
          label="Lift limits while charging"
          description="Restores your per-game limits when unplugged"
          checked={status.enabled}
          onChange={async (v: boolean) => {
            await setEnabled(v);
            setStatus({ ...status, enabled: v });
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <Field label="Power source">{status.plugged ? "Charging" : "Battery"}</Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <Field label="Currently lifted">{names(status.lifted)}</Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <Field label="Detected controls">{names(status.detected)}</Field>
      </PanelSectionRow>
    </PanelSection>
  );
}

export default definePlugin(() => ({
  name: "AC Full Power",
  titleView: <div>AC Full Power</div>,
  content: <Content />,
  icon: <FaBolt />,
  onDismount() {},
}));
