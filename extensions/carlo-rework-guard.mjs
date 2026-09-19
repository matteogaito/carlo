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

const ImplementationTask = Type.Object({
  id: Type.String(),
  title: Type.String(),
  position: Type.Integer({ minimum: 0 }),
  objective: Type.String(),
  files: Type.Array(Type.Object({
    path: Type.String(),
    mode: Type.Union([Type.Literal("edit"), Type.Literal("read_only"), Type.Literal("create")]),
    ranges: Type.Optional(Type.Array(Type.Object({
      start: Type.Integer({ minimum: 1 }),
      end: Type.Integer({ minimum: 1 }),
    }))),
    symbols: Type.Optional(Type.Array(Type.String())),
    reason: Type.String(),
  })),
  interfaces: Type.Array(Type.String()),
  changes: Type.Record(Type.String(), Type.String()),
  constraints: Type.Array(Type.String()),
  verification: Type.Object({
    commands: Type.Array(Type.String()),
    success: Type.String(),
  }),
  done_when: Type.Array(Type.String()),
  budget: Type.Object({ max_tool_calls: Type.Integer({ minimum: 1, maximum: 30 }) }),
});

export default function (pi) {
  const allowed = declaredCommands();

  pi.on("tool_call", async (event) => {
    if (event.toolName === "edit" || event.toolName === "write") {
      return { block: true, reason: "Rework is read-only: source changes are forbidden." };
    }
    if (event.toolName === "bash" && !allowedDiscoveryCommand(String(event.input.command || ""), allowed)) {
      return { block: true, reason: "Rework command blocked by the read-only policy." };
    }
    return undefined;
  });

  pi.registerTool({
    name: "task_fix_proposal",
    label: "Task fix proposal",
    description: "Record the fix for a failed Task, once the user has explicitly agreed to it.",
    parameters: Type.Object({
      action: Type.Union([Type.Literal("revise_task"), Type.Literal("revise_parent")]),
      summary: Type.String(),
      brief_markdown: Type.String(),
      plan_markdown: Type.String(),
      package: Type.Optional(ImplementationTask),
      packages: Type.Optional(Type.Array(ImplementationTask)),
    }, { additionalProperties: false }),
    async execute(_toolCallId, params) {
      return {
        content: [{ type: "text", text: "Fix proposal recorded." }],
        details: params,
      };
    },
  });
}
