import { Type } from "@earendil-works/pi-ai";
import { allowedDiscoveryCommand } from "./discovery-policy.mjs";

function declaredCommands() {
  try {
    const value = JSON.parse(process.env.CARLO_DISCOVERY_COMMANDS || "[]");
    return Array.isArray(value) && value.every((item) => typeof item === "string") ? value : [];
  } catch {
    return [];
  }
}

export default function (pi) {
  const allowed = declaredCommands();

  pi.on("tool_call", async (event) => {
    if (event.toolName === "edit" || event.toolName === "write") {
      return { block: true, reason: "Discovery is read-only: source changes are forbidden." };
    }
    if (event.toolName === "bash" && !allowedDiscoveryCommand(String(event.input.command || ""), allowed)) {
      return { block: true, reason: "Discovery command blocked by the read-only policy." };
    }
    return undefined;
  });

  pi.registerTool({
    name: "discovery_state",
    label: "Discovery state",
    description: "Record the complete structured state learned during the current Discovery turn.",
    parameters: Type.Object({
      summary: Type.String(),
      findings: Type.Array(Type.String()),
      decisions: Type.Array(Type.String()),
      unresolved_questions: Type.Array(Type.String()),
      inspected_resources: Type.Array(Type.String()),
      commands: Type.Array(Type.String()),
      task_proposals: Type.Array(Type.Object({
        title: Type.String(),
        megaprompt: Type.String(),
        depends_on: Type.Array(Type.String()),
      })),
    }, { additionalProperties: false }),
    async execute(_toolCallId, params) {
      return {
        content: [{ type: "text", text: "Discovery state recorded." }],
        details: params,
      };
    },
  });
}
