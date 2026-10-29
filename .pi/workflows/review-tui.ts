import { Type } from "typebox";
import { defineWorkflow } from "@nothumanwork/pi-orchestration";

export default defineWorkflow({
  name: "review-tui",
  description:
    "Read-only review of Brain's interactive browser and its headless compatibility.",
  args: Type.Object({}),
  budget: { maxAgents: 2 },
  async run(ctx) {
    return ctx.parallel(
      [
        "Review src/brain/tui.py and tests/test_tui.py for real correctness, concurrency, pagination, keyboard navigation, and terminal-safety bugs. Read the query/db code as needed. Identify actionable bugs only, with file/line references. Do not edit files.",
        "Review the uncommitted Brain TUI changes for CLI/headless compatibility, Python/uv/Nix packaging, useful test coverage and documentation accuracy. Inspect diffs and relevant files. Do not edit files. Report concrete bugs or important omissions with file/line references.",
      ].map(
        (prompt, i) => () =>
          ctx.agent(prompt, {
            label: `review-${i + 1}`,
            tools: ["read", "bash"],
          }),
      ),
      { failure: "allSettled" },
    );
  },
});
