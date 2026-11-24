// brain's Pi extension: a new session starts with a brief of the latest earlier sessions in its
// project, as `brain brief` prints it, added with the first prompt as context the user doesn't
// see. `brain install --pi` copies it into ~/.pi/agent/extensions.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// `brain install` writes the full path of `brain` in place of the name, so Pi needs no PATH for it.
const BRAIN = process.env.BRAIN_BIN || "brain";

export default function (pi: ExtensionAPI) {
  let pending = false;

  pi.on("session_start", (event, ctx) => {
    // Only a session someone starts in the terminal, not a print run, bot or workflow agent; and
    // not one resumed, reloaded or forked, which has its context already.
    pending = ctx.mode === "tui" && (event.reason === "startup" || event.reason === "new") &&
      !ctx.sessionManager.getBranch().some((e) => e.type === "message");
  });

  pi.on("before_agent_start", async (event, ctx) => {
    if (!pending || event.messageType !== "user") return;
    pending = false;
    const args = ["brief", ctx.cwd, "--exclude", ctx.sessionManager.getSessionId()];
    const result = await pi.exec(BRAIN, args, { timeout: 5000 })
      .catch(() => undefined);
    const brief = result?.code === 0 ? result.stdout.trim() : "";
    // `brain` leaves this message type out of its index, so briefs don't repeat in later ones.
    if (brief) return { message: { customType: "brain-brief", content: brief, display: false } };
  });
}
