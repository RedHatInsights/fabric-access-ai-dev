import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { discoverAgentProfiles } from "../../../../../dev-bot/packages/agent-dispatcher/dist/agent-profiles.js";
import {
  containsSecretTemplate,
  renderSecretTemplates,
  scanSecretReferences,
  SecretRenderError,
} from "../../../../../dev-bot/packages/agent-dispatcher/dist/secret-rendering.js";
import { validateConfig, validateResolvedParams } from "../../../../../dev-bot/packages/preflights/dist/index.js";
import { createPopulatedRegistry } from "../../../../../dev-bot/packages/preflights/dist/preflights/index.js";

const reference = (key) => "${{ secret('fabric-access-rehor', '" + key + "') }}";

// Synthetic, schema-conforming stand-ins for the two Secret keys
// (`fabric-project-state` requires `^[A-Za-z0-9:_-]+$` for project and
// `^customfield_\d+$` for teamFieldId). The reader below is the only
// Secret source these tests touch; real Secret values are never read.
const SYNTHETIC = new Map([
  ["bot-jira-project", Buffer.from("SYNTHPRJ")],
  ["bot-team-field-id", Buffer.from("customfield_424242")],
]);
const syntheticReader = async (name) => {
  assert.equal(name, "fabric-access-rehor");
  return SYNTHETIC;
};

const secretReferencesOf = (value, found = []) => {
  if (typeof value === "string") found.push(...scanSecretReferences(value));
  else if (Array.isArray(value)) for (const item of value) secretReferencesOf(item, found);
  else if (value !== null && typeof value === "object")
    for (const item of Object.values(value)) secretReferencesOf(item, found);
  return found;
};

test("coordinator preflight takes project and Team field from the shared Secret", async () => {
  const profiles = await discoverAgentProfiles(fileURLToPath(new URL("../../../../../", import.meta.url)));
  const coordinator = profiles.find((profile) => profile.name === "rbac-project-coordinator");
  assert.ok(coordinator, "rbac-project-coordinator profile is missing");
  const entry = coordinator.preflights.find((item) => item.id === "fabric-project-state");
  assert.ok(entry, "fabric-project-state entry is missing");
  assert.deepEqual(entry.with, {
    controlLabel: "dev-bot-coordinator",
    teamFieldId: reference("bot-team-field-id"),
    implementationLabel: "hcc-ai-platform-accessmanagement",
    project: reference("bot-jira-project"),
    includeBacklog: true,
  });
  // The preflight parameters and the sandbox deliveries resolve from the
  // same Secret keys. Each render still reads the Secret fresh, so a
  // rotation after a start result is checked at sandbox launch and a
  // mismatch refuses launch. Skip and error results do not bind values.
  assert.equal(entry.with.project, coordinator.sandboxEnv.FABRIC_JIRA_PROJECT);
  assert.equal(entry.with.teamFieldId, coordinator.sandboxEnv.FABRIC_TEAM_FIELD_ID);
});

test("no other preflight entry consumes any Secret reference", async () => {
  const profiles = await discoverAgentProfiles(fileURLToPath(new URL("../../../../../", import.meta.url)));
  const consumers = [];
  for (const profile of profiles) {
    for (const entry of profile.preflights ?? []) {
      for (const item of secretReferencesOf(entry)) {
        consumers.push(`${profile.name}:${entry.id} -> ${item.name}/${item.key}`);
      }
    }
  }
  assert.deepEqual(consumers.sort(), [
    "rbac-project-coordinator:fabric-project-state -> fabric-access-rehor/bot-jira-project",
    "rbac-project-coordinator:fabric-project-state -> fabric-access-rehor/bot-team-field-id",
  ]);
});

test("deferred validation accepts every profile; the coordinator entry renders and validates per entry", async () => {
  const profiles = await discoverAgentProfiles(fileURLToPath(new URL("../../../../../", import.meta.url)));
  const registry = createPopulatedRegistry();
  for (const profile of profiles) {
    const config = validateConfig(profile.preflights, registry, {
      deferValue: containsSecretTemplate,
    });
    assert.equal(config.ok, true, `${profile.name}: ${JSON.stringify(config)}`);
  }
  const coordinator = profiles.find((profile) => profile.name === "rbac-project-coordinator");
  const declared = coordinator.preflights.find((item) => item.id === "fabric-project-state");
  assert.ok(declared, "fabric-project-state entry is missing");
  // Engine flow (preflight-engine-ts.ts): an entry whose with-map carries
  // placeholders skips the declaration-time schema, renders the map
  // immediately before it executes, then runs the schema exactly once on
  // the rendered values.
  assert.equal(containsSecretTemplate(declared.with), true);
  const rendered = await renderSecretTemplates(declared.with, syntheticReader);
  assert.equal(rendered.project, "SYNTHPRJ");
  assert.equal(rendered.teamFieldId, "customfield_424242");
  assert.equal(rendered.controlLabel, "dev-bot-coordinator");
  assert.equal(rendered.includeBacklog, true);
  const resolved = validateResolvedParams(registry.get("fabric-project-state"), rendered);
  assert.equal(resolved.ok, true, JSON.stringify(resolved));
  assert.equal(resolved.params.project, "SYNTHPRJ");
  assert.equal(resolved.params.teamFieldId, "customfield_424242");
});

test("a missing Secret key fails the coordinator render closed", async () => {
  const profiles = await discoverAgentProfiles(fileURLToPath(new URL("../../../../../", import.meta.url)));
  const coordinator = profiles.find((profile) => profile.name === "rbac-project-coordinator");
  const declared = coordinator.preflights.find((item) => item.id === "fabric-project-state");
  assert.ok(declared, "fabric-project-state entry is missing");
  const incomplete = new Map(SYNTHETIC);
  incomplete.delete("bot-team-field-id");
  await assert.rejects(
    renderSecretTemplates(declared.with, async () => incomplete),
    (error) =>
      error instanceof SecretRenderError &&
      error.failure === "missing-key" &&
      error.category === "configuration",
  );
});
