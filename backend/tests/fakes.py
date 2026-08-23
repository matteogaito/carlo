from carlo.provider import AgentEventHandler, AgentProfile, AgentResult


class FakeProvider:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[AgentProfile, str, str, str]] = []

    async def run(
        self,
        profile: AgentProfile,
        instruction: str,
        cwd: str,
        session_id: str,
        on_event: AgentEventHandler | None = None,
    ) -> AgentResult:
        self.calls.append((profile, instruction, cwd, session_id))
        return AgentResult(session_id=session_id, output=self.output, events=(), exit_code=0)
