import { FormEvent, useCallback, useEffect, useState } from 'react'

import type {
  AgentProfileSettings,
  AgentPackage,
  AgentSkill,
  Api,
  AvailableModel,
  Event,
  ModelProvider,
  PiSettings,
} from './api'

export function SettingsView({ api, event, setError }: {
  api: Api
  event: Event | null
  setError: (message: string) => void
}) {
  const [section, setSection] = useState<'models' | 'agents'>('models')
  const [providers, setProviders] = useState<ModelProvider[]>([])
  const [models, setModels] = useState<AvailableModel[]>([])
  const [profiles, setProfiles] = useState<AgentProfileSettings[]>([])
  const [skills, setSkills] = useState<AgentSkill[]>([])
  const [packages, setPackages] = useState<AgentPackage[]>([])
  const [pi, setPi] = useState<PiSettings | null>(null)
  const [adding, setAdding] = useState(false)

  const load = useCallback(async () => {
    try {
      const [nextProviders, nextModels, nextPi, nextProfiles, nextSkills, nextPackages] = await Promise.all([
        api.listModelProviders(), api.listModels(), api.getPiSettings(), api.listAgentProfiles(), api.listSkills(), api.listPackages(),
      ])
      setProviders(nextProviders)
      setModels(nextModels)
      setPi(nextPi)
      setProfiles(nextProfiles)
      setSkills(nextSkills)
      setPackages(nextPackages)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not load Settings')
    }
  }, [api, setError])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (event?.type.startsWith('model_provider.') || event?.type === 'model.updated' || event?.type === 'agent_profile.updated') {
      void load()
    }
  }, [event, load])

  async function action(operation: () => Promise<unknown>) {
    try {
      await operation()
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Settings update failed')
    }
  }

  async function saveProfile(name: string, input: Partial<AgentProfileSettings>) {
    try {
      await api.updateAgentProfile(name, input)
      await load()
      return true
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Settings update failed')
      return false
    }
  }

  async function saveDefaults(input: Partial<PiSettings>) {
    try {
      await api.updatePiSettings(input)
      await load()
      return true
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Settings update failed')
      return false
    }
  }

  return <main className="settings-page">
    <header className="settings-heading">
      <div><span>CONTROL PLANE</span><h1>Settings</h1></div>
      <nav aria-label="Settings sections">
        <button className={section === 'models' ? 'active' : ''} onClick={() => setSection('models')}>Models</button>
        <button className={section === 'agents' ? 'active' : ''} onClick={() => setSection('agents')}>Coding agents</button>
      </nav>
    </header>

    {section === 'models' ? <>
      <section className="settings-section">
        <header><div><span>CONNECTIONS</span><h2>Model providers</h2></div><button onClick={() => setAdding(true)}>+ Provider</button></header>
        <div className="provider-grid">
          {providers.map((provider) => {
            const catalog = models.filter((model) => model.model_provider_id === provider.id)
            return <article className="provider-card" key={provider.id}>
              <header>
                <div><i className={provider.last_refresh_status.toLowerCase()} /><span>{provider.slug}</span><h3>{provider.name}</h3></div>
                <button aria-label={`Refresh ${provider.name}`} onClick={() => void action(() => api.refreshModelProvider(provider.id))}>Refresh</button>
              </header>
              <dl><dt>Endpoint</dt><dd>{provider.base_url}</dd><dt>Credential</dt><dd>{provider.credential_hint || 'Not required'}</dd><dt>Catalog</dt><dd>{catalog.length} models · {provider.last_refresh_status}</dd></dl>
              {provider.last_refresh_error && <p className="provider-error">{provider.last_refresh_error}</p>}
              <div className="model-ledger">
                {catalog.map((model) => <ModelRow key={model.id} model={model} api={api} action={action} />)}
                {!catalog.length && <p>No models loaded. Refresh this provider.</p>}
              </div>
              <footer>
                <button onClick={() => void action(() => api.updateModelProvider(provider.id, { active: !provider.active }))}>{provider.active ? 'Disable' : 'Enable'}</button>
                <button className="danger" onClick={() => window.confirm(`Delete ${provider.name}?`) && void action(() => api.deleteModelProvider(provider.id))}>Delete</button>
              </footer>
            </article>
          })}
          {!providers.length && <div className="settings-empty"><b>No model providers</b><p>Add an OpenAI-compatible endpoint to let CARLO own Pi configuration.</p></div>}
        </div>
      </section>
      {pi && <CompactionSettings value={pi} save={(input) => action(() => api.updatePiSettings(input))} />}
    </> : <>{pi && <DefaultResources value={pi} packages={packages} skills={skills} save={saveDefaults} />}<AgentProfiles profiles={profiles} providers={providers} models={models} packages={packages} skills={skills} defaults={pi} save={saveProfile} /></>}

    {adding && <ProviderDialog close={() => setAdding(false)} create={async (input) => {
      await action(() => api.createModelProvider(input))
      setAdding(false)
    }} />}
  </main>
}

function DefaultResources({ value, packages, skills, save }: {
  value: PiSettings
  packages: AgentPackage[]
  skills: AgentSkill[]
  save: (input: Partial<PiSettings>) => Promise<boolean>
}) {
  const [saved, setSaved] = useState(false)
  return <section className="settings-section default-resources">
    <header><div><span>EVERY SESSION</span><h2>Default resources</h2></div></header>
    <p>These packages and skills are loaded for every Pi profile. Profiles can add resources, never exclude defaults.</p>
    <form key={`${value.default_packages.join(',')}|${value.default_skills.join(',')}`} onChange={() => setSaved(false)} onSubmit={(event) => {
      event.preventDefault()
      setSaved(false)
      const data = new FormData(event.currentTarget)
      void save({
        default_packages: data.getAll('packages').map(String),
        default_skills: data.getAll('skills').map(String),
      }).then(setSaved)
    }}>
      <ResourceChoices legend="Packages" name="packages" values={packages} selected={value.default_packages} labelPrefix="Default" />
      <ResourceChoices legend="Skills" name="skills" values={skills} selected={value.default_skills} labelPrefix="Default" />
      <div className="profile-save"><button type="submit">Save default resources</button>{saved && <span role="status" aria-label="Default resources saved">Saved ✓</span>}</div>
    </form>
  </section>
}

function ResourceChoices({ legend, name, values, selected, disabled = [], labelPrefix }: {
  legend: string
  name: string
  values: Array<{ name: string; revision: string | null }>
  selected: string[]
  disabled?: string[]
  labelPrefix: string
}) {
  return <fieldset><legend>{legend}</legend><div className="profile-skills">{values.map((resource) => {
    const inherited = disabled.includes(resource.name)
    return <label key={resource.name}><input aria-label={`${labelPrefix} ${resource.name}`} name={name} value={resource.name} type="checkbox" defaultChecked={inherited || selected.includes(resource.name)} disabled={inherited} />{resource.name}{resource.revision && <small>{resource.revision.slice(0, 7)}</small>}{inherited && <small>default</small>}</label>
  })}</div></fieldset>
}

function ModelRow({ model, api, action }: {
  model: AvailableModel
  api: Api
  action: (operation: () => Promise<unknown>) => Promise<void>
}) {
  return <details className="model-row">
    <summary><span><b>{model.display_name || model.external_id}</b><small>{model.external_id}</small></span><span className="model-row-state"><em className={model.selectable ? 'available' : ''}>{model.status}</em></span></summary>
    <div>
      <p>{formatTokens(model.effective_context_window)} context · {formatTokens(model.effective_max_tokens)} output</p>
      {model.reserve_tokens != null && <p><b>{model.reserve_tokens.toLocaleString()} reserved</b> · {model.keep_recent_tokens?.toLocaleString()} recent</p>}
      <form onSubmit={(event) => {
        event.preventDefault()
        const data = new FormData(event.currentTarget)
        void action(() => api.updateModel(model.id, {
          context_window_override: optionalNumber(data.get('context')),
          max_tokens_override: optionalNumber(data.get('output')),
        }))
      }}>
        <label>Context override<input name="context" type="number" min="1" defaultValue={model.context_window_override || ''} placeholder={String(model.discovered_context_window || '')} /></label>
        <label>Output override<input name="output" type="number" min="1" defaultValue={model.max_tokens_override || ''} placeholder={String(model.discovered_max_tokens || '')} /></label>
        <button type="submit">Save limits</button>
      </form>
    </div>
  </details>
}

function CompactionSettings({ value, save }: { value: PiSettings; save: (input: Partial<PiSettings>) => Promise<void> }) {
  return <section className="settings-section compaction-card">
    <header><div><span>PI RUNTIME</span><h2>Context compaction</h2></div></header>
    <p>CARLO calculates token limits from the selected model. Output capacity is always protected.</p>
    <form onSubmit={(event) => {
      event.preventDefault()
      const data = new FormData(event.currentTarget)
      void save({
        compaction_enabled: data.get('enabled') === 'on',
        reserve_percent: Number(data.get('reserve')),
        keep_recent_percent: Number(data.get('recent')),
      })
    }}>
      <label className="switch"><input name="enabled" type="checkbox" defaultChecked={value.compaction_enabled} />Automatic compaction</label>
      <label>Reserve context<input name="reserve" type="number" min="1" max="90" defaultValue={value.reserve_percent} /><span>%</span></label>
      <label>Keep recent<input name="recent" type="number" min="1" max="90" defaultValue={value.keep_recent_percent} /><span>%</span></label>
      <button type="submit">Save compaction</button>
    </form>
  </section>
}

function AgentProfiles({ profiles, providers, models, packages, skills, defaults, save }: {
  profiles: AgentProfileSettings[]
  providers: ModelProvider[]
  models: AvailableModel[]
  packages: AgentPackage[]
  skills: AgentSkill[]
  defaults: PiSettings | null
  save: (name: string, input: Partial<AgentProfileSettings>) => Promise<boolean>
}) {
  const [saved, setSaved] = useState<string | null>(null)
  return <section className="settings-section agent-settings">
    <header><div><span>PI PROFILES</span><h2>Coding agents</h2></div></header>
    <p>Each new session receives an isolated snapshot of the selected model and proportional context policy.</p>
    <div className="profile-list">{profiles.map((profile) => {
      const initial = profile.available_model_id ? String(profile.available_model_id) : ''
      const inheritedPackages = defaults?.default_packages || []
      const inheritedSkills = defaults?.default_skills || []
      return <form key={`${profile.name}|${inheritedPackages.join(',')}|${inheritedSkills.join(',')}`} onChange={() => setSaved(null)} onSubmit={(event) => {
        event.preventDefault()
        setSaved(null)
        const data = new FormData(event.currentTarget)
        const selection = String(data.get('model'))
        const selectedPackages = Array.from(new Set([
          ...profile.default_packages.filter((name) => inheritedPackages.includes(name)),
          ...data.getAll('packages').map(String),
        ]))
        const selectedSkills = Array.from(new Set([
          ...profile.required_skills,
          ...profile.default_skills.filter((name) => inheritedSkills.includes(name)),
          ...data.getAll('skills').map(String),
        ]))
        void save(profile.name, {
          available_model_id: Number(selection),
          default_packages: selectedPackages,
          default_skills: selectedSkills,
        }).then((ok) => ok && setSaved(profile.name))
      }}>
        <div><span>{profile.provider}</span><h3>{profile.name}</h3><small>{profile.default_skills.join(' · ') || 'No skills'}</small></div>
        <div className="profile-controls"><label>Model for {profile.name}<select name="model" defaultValue={initial} required>
          <option value="">Not configured — choose a managed model</option>
          {providers.filter((provider) => provider.active).map((provider) => {
            const available = models.filter((model) => model.model_provider_id === provider.id && model.selectable)
            return available.length ? <optgroup label={provider.name} key={provider.id}>
              {available.map((model) => <option value={model.id} key={model.id}>{provider.slug} — {model.external_id}</option>)}
            </optgroup> : null
          })}
        </select></label>
        <ResourceChoices legend={`Packages for ${profile.name}`} name="packages" values={packages} selected={profile.default_packages} disabled={inheritedPackages} labelPrefix={`${profile.name} package`} />
        <fieldset><legend>Skills for {profile.name}</legend><div className="profile-skills">{skills.map((skill) => {
          const required = profile.required_skills.includes(skill.name)
          const inherited = inheritedSkills.includes(skill.name)
          return <label key={skill.name}><input aria-label={`${profile.name} skill ${skill.name}`} name="skills" value={skill.name} type="checkbox" defaultChecked={required || inherited || profile.default_skills.includes(skill.name)} disabled={required || inherited} />{skill.name}{required && <small>core</small>}{inherited && <small>default</small>}</label>
        })}</div></fieldset>
        </div>
        <div className="profile-save"><button type="submit">Save {profile.name}</button>{saved === profile.name && <span role="status" aria-label={`${profile.name} saved`}>Saved ✓</span>}</div>
      </form>
    })}</div>
  </section>
}

function ProviderDialog({ close, create }: {
  close: () => void
  create: (input: { name: string; slug: string; base_url: string; api_key?: string }) => Promise<void>
}) {
  const [requiresKey, setRequiresKey] = useState(false)
  const [showKey, setShowKey] = useState(false)
  return <div className="modal-backdrop"><section className="modal-panel provider-dialog" role="dialog" aria-modal="true" aria-labelledby="provider-title">
    <header><div><span>OPENAI COMPATIBLE</span><h2 id="provider-title">Add model provider</h2></div><button className="close" onClick={close} aria-label="Close provider form">×</button></header>
    <form onSubmit={(event: FormEvent<HTMLFormElement>) => {
      event.preventDefault()
      const data = new FormData(event.currentTarget)
      void create({
        name: String(data.get('name')),
        slug: String(data.get('slug')),
        base_url: String(data.get('base_url')),
        ...(requiresKey ? { api_key: String(data.get('api_key')) } : {}),
      })
    }}>
      <label>Name<input name="name" required autoFocus placeholder="Local OMLX" /></label>
      <label>Pi provider ID<input name="slug" required pattern="[a-z][a-z0-9-]*" placeholder="omlx" /></label>
      <label>Base URL<input name="base_url" type="url" required placeholder="http://127.0.0.1:11435/v1" /></label>
      <label className="provider-auth"><input type="checkbox" checked={requiresKey} onChange={(event) => setRequiresKey(event.target.checked)} />Requires API key</label>
      {requiresKey && <label>API key<span className="secret-field"><input name="api_key" type={showKey ? 'text' : 'password'} required autoComplete="new-password" /><button type="button" onClick={() => setShowKey((value) => !value)}>{showKey ? 'Hide' : 'Show'} API key</button></span></label>}
      <footer><button type="button" onClick={close}>Cancel</button><button className="primary" type="submit">Add provider</button></footer>
    </form>
  </section></div>
}

function optionalNumber(value: FormDataEntryValue | null): number | null {
  return value ? Number(value) : null
}

function formatTokens(value: number | null): string {
  return value == null ? 'Unknown' : value.toLocaleString()
}
