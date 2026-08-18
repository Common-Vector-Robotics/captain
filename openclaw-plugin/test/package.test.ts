import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const readJson = (name: string) =>
  JSON.parse(readFileSync(new URL(`../${name}`, import.meta.url), "utf8"));

describe("captain-remote package", () => {
  it("declares a startup plugin with strict bounded config", () => {
    const manifest = readJson("openclaw.plugin.json");
    const properties = manifest.configSchema.properties;
    expect(manifest.id).toBe("captain-remote");
    expect(manifest.activation).toEqual({ onStartup: true });
    expect(manifest.configSchema.additionalProperties).toBe(false);
    expect(properties.maxRequestBytes.maximum).toBe(262144);
    expect(properties.executionTimeoutMs.maximum).toBe(300000);
    expect(properties.maxGlobalActiveTurns.maximum).toBe(32);
    expect(properties.maxGlobalRunningTurns.maximum).toBe(4);
    expect(properties.pollPerMinute.maximum).toBe(30);
    expect(properties.invalidAuthGlobalPerMinute.maximum).toBe(100);
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
