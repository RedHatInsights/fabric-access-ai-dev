import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { discoverAgentProfiles } from "../../../../../dev-bot/packages/agent-dispatcher/dist/agent-profiles.js";
import { createPreflightRunContext, validateConfig } from "../../../../../dev-bot/packages/preflights/dist/index.js";
import { createPopulatedRegistry } from "../../../../../dev-bot/packages/preflights/dist/preflights/index.js";

const root = fileURLToPath(new URL("../../../../../", import.meta.url));
const jsonResponse = (value) => new Response(JSON.stringify(value), {
  headers: { "Content-Type": "application/json" },
});

test("standard profile wakes for fresh marked review-bot feedback", async () => {
  const profile = (await discoverAgentProfiles(root)).find((entry) => entry.name === "rbac-config");
  assert.ok(profile);
  const registry = createPopulatedRegistry();
  const config = validateConfig(profile.preflights, registry);
  assert.equal(config.ok, true, JSON.stringify(config));
  const entry = config.entries.find((item) => item.id === "gh-pr-status");
  assert.deepEqual(entry.params.review_bot_markers, [
    "dev-review-bot", "fabric-access-finding", "fabric-access-review",
  ]);
  assert.equal(entry.params.review_bot_author, "platex-rehor-bot");
  assert.equal(entry.params.task_key_prefix, undefined);

  const task = {
    external_key: "RHCLOUD-123",
    status: "pr_open",
    repo: "demo",
    last_addressed: "2026-07-01T10:00:00Z",
    metadata: { prs: [{ repo: "demo", number: 9, host: "github" }] },
  };
  const run = (comment) => registry.get(entry.id).run(createPreflightRunContext({
    instanceId: "test-instance",
    memory: { baseUrl: "http://memory:8080", fetch: async () => jsonResponse({ items: [task] }) },
    proxy: {
      baseUrl: "https://proxy.example/devbot-proxy",
      fetch: async (url) => url.endsWith("/graphql")
        ? jsonResponse({ data: { repository: { pullRequest: {
          state: "OPEN", mergeable: "MERGEABLE", reviews: { nodes: [] },
          statusCheckRollup: { contexts: { nodes: [] } },
        } } } })
        : jsonResponse(url.includes("/pulls/") ? [{
          user: { login: comment.author }, created_at: comment.time, body: comment.body,
        }] : []),
    },
    repos: { demo: { upstream: "https://github.com/org/demo" } },
  }), entry.params);

  const comment = { author: "platex-rehor-bot", time: "2026-07-02T10:00:00Z", body: "" };
  for (const marker of entry.params.review_bot_markers) {
    assert.equal((await run({ ...comment, body: marker })).status, "start", marker);
  }
  for (const unrelated of [
    { ...comment, body: "ordinary bot update" },
    { ...comment, author: "other-bot[bot]", body: "dev-review-bot" },
    { ...comment, time: "2026-06-30T10:00:00Z", body: "dev-review-bot" },
  ]) {
    assert.equal((await run(unrelated)).status, "skip");
  }
});
