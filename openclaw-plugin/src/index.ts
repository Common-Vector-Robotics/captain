import {
  definePluginEntry,
  type OpenClawPluginDefinition,
} from "openclaw/plugin-sdk/plugin-entry";

const plugin: OpenClawPluginDefinition = definePluginEntry({
  id: "captain-remote",
  name: "Captain Remote",
  description: "Authenticated Captain-only report ingress for coding agents.",
  register() {},
});

export default plugin;
