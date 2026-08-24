from carlo.provider import AgentEventHandler, AgentProfile, AgentResult
from carlo.models import AgentProfile as AgentProfileRecord, AvailableModel, ModelProvider
from uuid import uuid4


async def add_managed_profiles(session, *names: str) -> dict[str, AgentProfileRecord]:
    key = uuid4().hex[:12]
    provider = ModelProvider(
        name="Test models",
        slug=f"test-{key}",
        kind="openai-compatible",
        base_url="http://models.test/v1",
    )
    model = AvailableModel(
        model_provider=provider,
        external_id="test-model",
        status="AVAILABLE",
        discovered_context_window=65_536,
        discovered_max_tokens=16_384,
    )
    session.add_all([provider, model])
    await session.flush()
    profiles = {
        name: AgentProfileRecord(
            name=name,
            provider="pi",
            available_model_id=model.id,
        )
        for name in names
    }
    session.add_all(profiles.values())
    await session.flush()
    return profiles


class FakeProvider:
    def __init__(self, output: str) -> None:
        self.output = output
        self.events: tuple[dict[str, object], ...] = ()
        self.used_skills: tuple[str, ...] = ()
        self.resource_revisions: dict[str, str] = {}
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
        if on_event:
            for event in self.events:
                await on_event(event)
        return AgentResult(
            session_id=session_id,
            output=self.output,
            events=self.events,
            exit_code=0,
            used_skills=self.used_skills,
            resource_revisions=self.resource_revisions,
        )
