import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const readJson = (name: string) =>
  JSON.parse(readFileSync(new URL(`../${name}`, import.meta.url), "utf8"));

const boundedNumericConfig = {
  maxRequestBytes: 262144,
  executionTimeoutMs: 300000,
  maxGlobalActiveTurns: 32,
  maxGlobalRunningTurns: 4,
  pollPerMinute: 30,
  pollBurst: 5,
  invalidAuthPerSourcePerMinute: 10,
  invalidAuthPerSourceBurst: 5,
  invalidAuthGlobalPerMinute: 100,
};

describe("captain-remote package", () => {
  it("declares a startup plugin with strict bounded config", () => {
    const manifest = readJson("openclaw.plugin.json");
    const properties = manifest.configSchema.properties;

    expect(manifest.id).toBe("captain-remote");
    expect(manifest.activation).toEqual({ onStartup: true });
    expect(manifest.configSchema.additionalProperties).toBe(false);
    expect(Object.keys(properties).sort()).toEqual(
      ["databasePath", ...Object.keys(boundedNumericConfig)].sort(),
    );
    expect(manifest.configSchema.required ?? []).not.toContain("databasePath");
    expect(properties.databasePath.type).toBe("string");

    for (const [name, maximum] of Object.entries(boundedNumericConfig)) {
      expect(properties[name]).toMatchObject({
        type: "number",
        minimum: 1,
        maximum,
        default: maximum,
      });
    }
  });

  it("ships only a built runtime entry with an explicit host floor", () => {
    const pkg = readJson("package.json");
    expect(pkg.type).toBe("module");
    expect(pkg.openclaw.extensions).toEqual(["./dist/index.js"]);
    expect(pkg.openclaw.runtimeExtensions).toEqual(["./dist/index.js"]);
    expect(pkg.openclaw.install.minHostVersion).toBe(">=2026.7.2-beta.5");
    expect(pkg.openclaw.compat.pluginApi).toBe(">=2026.7.2-beta.5");
  });
});
