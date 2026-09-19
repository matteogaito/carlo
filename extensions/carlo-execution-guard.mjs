import { createHash } from "node:crypto";

export default function (pi, options = {}) {
  const maxToolCalls = options.maxToolCalls ?? Number(process.env.CARLO_MAX_TOOL_CALLS || 0);
  let calls = 0;
  pi.on("tool_call", (event, context) => {
    if (++calls > maxToolCalls) {
      process.stderr.write("CARLO_TOOL_BUDGET_EXCEEDED\n");
      context?.abort();
      return { block: true, reason: `Carlo tool-call budget exceeded (${maxToolCalls})` };
    }
    return undefined;
  });

  if (process.env.CARLO_PI_REQUEST_DIAGNOSTICS === "true") {
    pi.on("before_provider_request", (event) => {
      const payload = event.payload || {};
      const blocks = [];
      for (const message of payload.messages || []) {
        const parts = Array.isArray(message.content) ? message.content : [message.content];
        for (const part of parts) {
          const value = typeof part === "string" ? part : JSON.stringify(part ?? null);
          blocks.push({
            role: message.role,
            length: value.length,
            sha256: createHash("sha256").update(value).digest("hex"),
          });
        }
      }
      process.stderr.write(`CARLO_PI_REQUEST_DIAGNOSTIC ${JSON.stringify({
        blocks,
        tools: Array.isArray(payload.tools) ? payload.tools.length : 0,
        params: {
          ...Object.fromEntries(["model", "max_tokens", "temperature", "top_p", "stream"]
            .filter((key) => Object.hasOwn(payload, key)).map((key) => [key, payload[key]])),
          ...(typeof payload.chat_template_kwargs?.enable_thinking === "boolean"
            ? { chat_template_kwargs: { enable_thinking: payload.chat_template_kwargs.enable_thinking } }
            : {}),
        },
      })}\n`);
    });
  }
}
