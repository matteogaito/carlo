const shellSyntax = /[\n\r;&|><`$(){}]/;

const safePatterns = [
  /^(pwd|ls|cat|head|tail|sed|wc|file|stat)(?:\s|$)/,
  /^git\s+(status|log|diff|show|rev-parse)(?:\s|$)/,
  /^git\s+branch\s+--show-current(?:\s|$)/,
  /^(pytest|ruff|mypy|pyright)(?:\s|$)/,
  /^python(?:3)?\s+-m\s+pytest(?:\s|$)/,
  /^uv\s+run\s+(pytest|ruff|mypy|pyright)(?:\s|$)/,
  /^(npm|pnpm|yarn)\s+(test|build|lint|typecheck)(?:\s|$)/,
  /^(npm|pnpm|yarn)\s+run\s+(test|build|lint|typecheck)(?:\s|$)/,
  /^go\s+(test|vet|build)(?:\s|$)/,
  /^cargo\s+(test|check|clippy|build)(?:\s|$)/,
  /^cargo\s+fmt\s+--(?:\s*)check(?:\s|$)/,
  /^make\s+(test|check|lint|build)(?:\s|$)/,
  /^(ps|lsof)(?:\s|$)/,
];

export function allowedDiscoveryCommand(command, declaredCommands = []) {
  const value = command.trim();
  if (!value || shellSyntax.test(value)) return false;
  if (declaredCommands.includes(value)) return true;
  return safePatterns.some((pattern) => pattern.test(value));
}
