import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { discoverAgentProfiles } from "../../../../../dev-bot/packages/agent-dispatcher/dist/agent-profiles.js";
import { renderSandboxEnvironment, scanSecretReferences } from "../../../../../dev-bot/packages/agent-dispatcher/dist/secret-rendering.js";

const SECRET_NAME = "fabric-access-rehor";
const reference = (key) => "${{ secret('fabric-access-rehor', '" + key + "') }}";

// The exact per-profile sandbox.env maps. Every FABRIC_ name is a nonreserved
// alias the pinned dispatcher accepts where reserved BOT_ names are rejected;
// in the sandbox each delivery is required (helpers fail closed when missing).
const EXPECTED_SANDBOX = {
  "rbac-config": {
    SLACK_WEBHOOK_URL: reference("slack-webhook-url"),
  },
  "rbac-config-konflux": {},
  "rbac-config-pr-label": {
    SLACK_WEBHOOK_URL: reference("slack-webhook-url"),
    FABRIC_JIRA_PROJECT: reference("bot-jira-project"),
  },
  "rbac-config-review": {
    FABRIC_TEAM_FIELD_ID: reference("bot-team-field-id"),
    FABRIC_JIRA_PROJECT_INSIGHTS_RBAC: reference("bot-jira-project-insights-rbac"),
    FABRIC_JIRA_TEAM_ID_INSIGHTS_RBAC: reference("bot-jira-team-id-insights-rbac"),
    FABRIC_JIRA_TEAM_NAME_INSIGHTS_RBAC: reference("bot-jira-team-name-insights-rbac"),
    FABRIC_JIRA_LABELS_INSIGHTS_RBAC: reference("bot-jira-labels-insights-rbac"),
    FABRIC_JIRA_PROJECT_INVENTORY_API: reference("bot-jira-project-inventory-api"),
    FABRIC_JIRA_TEAM_ID_INVENTORY_API: reference("bot-jira-team-id-inventory-api"),
    FABRIC_JIRA_TEAM_NAME_INVENTORY_API: reference("bot-jira-team-name-inventory-api"),
    FABRIC_JIRA_LABELS_INVENTORY_API: reference("bot-jira-labels-inventory-api"),
    FABRIC_JIRA_PROJECT_INSIGHTS_CHROME: reference("bot-jira-project-insights-chrome"),
    FABRIC_JIRA_TEAM_ID_INSIGHTS_CHROME: reference("bot-jira-team-id-insights-chrome"),
    FABRIC_JIRA_TEAM_NAME_INSIGHTS_CHROME: reference("bot-jira-team-name-insights-chrome"),
    FABRIC_JIRA_COMPONENT_ID_INSIGHTS_CHROME: reference("bot-jira-component-id-insights-chrome"),
    FABRIC_JIRA_LABELS_INSIGHTS_CHROME: reference("bot-jira-labels-insights-chrome"),
  },
  "rbac-project-coordinator": {
    FABRIC_JIRA_PROJECT: reference("bot-jira-project"),
    FABRIC_TEAM_FIELD_ID: reference("bot-team-field-id"),
  },
};

// The 16 keys mapped by app-interface draft MR !220723: the profiles must
// reference exactly this union — nothing invented, nothing forgotten.
const EXPECTED_KEYS = [
  "bot-jira-component-id-insights-chrome",
  "bot-jira-labels-insights-chrome",
  "bot-jira-labels-insights-rbac",
  "bot-jira-labels-inventory-api",
  "bot-jira-project",
  "bot-jira-project-insights-chrome",
  "bot-jira-project-insights-rbac",
  "bot-jira-project-inventory-api",
  "bot-jira-team-id-insights-chrome",
  "bot-jira-team-id-insights-rbac",
  "bot-jira-team-id-inventory-api",
  "bot-jira-team-name-insights-chrome",
  "bot-jira-team-name-insights-rbac",
  "bot-jira-team-name-inventory-api",
  "bot-team-field-id",
  "slack-webhook-url",
].sort();

test("every profile declares its exact sandbox.env map over the 16 mapped keys", async () => {
  const profiles = await discoverAgentProfiles(fileURLToPath(new URL("../../../../../", import.meta.url)));
  assert.equal(profiles.length, 5);
  const byName = new Map(profiles.map((profile) => [profile.name, profile]));
  assert.deepEqual([...byName.keys()].sort(), Object.keys(EXPECTED_SANDBOX).sort());
  const referenced = new Set();
  for (const [name, expected] of Object.entries(EXPECTED_SANDBOX)) {
    const profile = byName.get(name);
    assert.deepEqual(profile.sandboxEnv, expected, `sandbox.env map mismatch for ${name}`);
    for (const references of Object.values(profile.sandboxEnv).map((template) => scanSecretReferences(template))) {
      for (const item of references) {
        assert.equal(item.name, SECRET_NAME, `unexpected Secret name in ${name}`);
        referenced.add(item.key);
      }
    }
  }
  assert.deepEqual([...referenced].sort(), EXPECTED_KEYS, "referenced keys must be exactly the mapped key union");
});

test("every reference renders from synthetic data and one missing Jira key fails the whole render", async () => {
  const profiles = await discoverAgentProfiles(fileURLToPath(new URL("../../../../../", import.meta.url)));
  const synthetic = new Map(EXPECTED_KEYS.map((key) => [key, Buffer.from(`synthetic:${key}`)]));
  const reader = async (name) => {
    assert.equal(name, SECRET_NAME);
    return synthetic;
  };
  for (const profile of profiles) {
    if (Object.keys(profile.sandboxEnv).length === 0) continue;
    const rendered = await renderSandboxEnvironment(profile.sandboxEnv, reader);
    assert.equal(Object.keys(rendered).length, Object.keys(profile.sandboxEnv).length);
    for (const [name, value] of Object.entries(rendered)) {
      assert.ok(value.startsWith("synthetic:"), `${profile.name}:${name} rendered unexpectedly`);
    }
  }
  // One missing Jira key (the RBAC team name) rejects the entire review
  // render before any sandbox is created.
  const review = profiles.find((profile) => profile.name === "rbac-config-review");
  const withoutRbacName = new Map(synthetic);
  withoutRbacName.delete("bot-jira-team-name-insights-rbac");
  await assert.rejects(
    renderSandboxEnvironment(review.sandboxEnv, async () => withoutRbacName),
    (error) => error.failure === "missing-key" && error.category === "configuration",
  );
});
