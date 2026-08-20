import { FormEvent, useCallback, useEffect, useState } from 'react'

import type { ActionCatalog, ActionDefinition, ActionRun, Api, Event, Project, Runner } from './api'

export function ActionsView({ api, projects, event, setError }: {
  api: Api
  projects: Project[]
  event: Event | null
  setError: (message: string) => void
}) {
  const [catalogs, setCatalogs] = useState<Record<number, ActionCatalog>>({})
  const [runs, setRuns] = useState<ActionRun[]>([])
  const [runners, setRunners] = useState<Runner[]>([])
  const [confirm, setConfirm] = useState<{ project: Project; catalog: ActionCatalog; action: ActionDefinition } | null>(null)
  const [selected, setSelected] = useState<ActionRun | null>(null)
  const [consoleText, setConsoleText] = useState('')
  const [consoleOffset, setConsoleOffset] = useState(0)
  const [runnersOpen, setRunnersOpen] = useState(false)

  const load = useCallback(async () => {
    try {
      const [nextRuns, nextRunners, ...nextCatalogs] = await Promise.all([
        api.listActionRuns(), api.listRunners(), ...projects.map((project) => api.listProjectActions(project.id)),
      ])
      setRuns(nextRuns)
      setRunners(nextRunners)
      setCatalogs(Object.fromEntries(nextCatalogs.map((catalog) => [catalog.project_id, catalog])))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not load actions')
    }
  }, [api, projects, setError])

  const openRun = useCallback(async (run: ActionRun) => {
    const [fresh, chunk] = await Promise.all([api.getActionRun(run.id), api.getActionConsole(run.id, 0)])
    setSelected(fresh)
    setConsoleText(chunk.text)
    setConsoleOffset(chunk.next_offset)
  }, [api])

  useEffect(() => { void load() }, [load])

  useEffect(() => {
    if (!event?.type.startsWith('action.')) return
    void load()
    if (!selected || Number(event.payload.run_id) !== selected.id) return
    void api.getActionRun(selected.id).then(setSelected)
    if (event.type === 'action.output_available') {
      void api.getActionConsole(selected.id, consoleOffset).then((chunk) => {
        setConsoleText((current) => current + chunk.text)
        setConsoleOffset(chunk.next_offset)
      })
    }
  }, [api, consoleOffset, event, load, selected])

  async function queueRun() {
    if (!confirm) return
    try {
      const run = await api.createActionRun(confirm.project.id, confirm.action.key)
      setConfirm(null)
      await openRun(run)
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not queue action')
    }
  }

  async function cancelRun() {
    if (!selected || !window.confirm(`Kill run #${selected.id}?`)) return
    try {
      setSelected(await api.cancelActionRun(selected.id))
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not cancel run')
    }
  }

  return (
    <main className={selected ? 'actions-workspace detail-open' : 'actions-workspace'}>
      <section className="actions-page" aria-label="Project actions">
        <header className="actions-heading">
          <div><span>AUTOMATION</span><h1>Project actions</h1></div>
          <button onClick={() => setRunnersOpen(true)}>Manage runners</button>
        </header>
        <div className="project-action-grid">
          {projects.map((project) => {
            const catalog = catalogs[project.id]
            return (
              <article className="project-action-card" key={project.id}>
                <header><div><span>{project.key}</span><h2>{project.name}</h2></div><code>{catalog?.commit_sha?.slice(0, 8) || '—'}</code></header>
                {catalog?.error && <p className="action-warning">{catalog.error}</p>}
                {!!catalog?.dirty_paths.length && <p className="action-warning">Working tree has pending changes</p>}
                <div className="action-list">
                  {catalog?.actions.map((action) => {
                    const last = runs.find((run) => run.project_id === project.id && run.action_key === action.key)
                    return (
                      <div className="action-row" key={action.key}>
                        <span className={`status-rail ${last?.status || 'idle'}`} aria-hidden="true" />
                        <div><strong>{action.name}</strong><small>{action.runner} · {action.commands.length} step{action.commands.length === 1 ? '' : 's'}</small></div>
                        {last && <button className="run-history" onClick={() => void openRun(last)}>#{last.id} {last.status}</button>}
                        <button
                          aria-label={`Run ${action.name}`}
                          disabled={Boolean(catalog.error || catalog.dirty_paths.length)}
                          onClick={() => setConfirm({ project, catalog, action })}
                        >Run</button>
                      </div>
                    )
                  })}
                  {catalog && !catalog.actions.length && !catalog.error && <p className="empty">No actions declared.</p>}
                  {!catalog && <p className="empty">Reading carlo-actions.yaml…</p>}
                </div>
              </article>
            )
          })}
          {!projects.length && <p className="empty">Create a project to expose its actions.</p>}
        </div>
        {!!runs.length && <section className="run-history-list"><h2>Recent runs</h2>{runs.map((run) => (
          <button key={run.id} onClick={() => void openRun(run)}><b>#{run.id}</b><span>{run.action_name}</span><em>{run.status}</em></button>
        ))}</section>}
      </section>

      {selected && <aside className="task-detail action-detail" aria-label={`Run ${selected.id} details`}>
        <header><div><span className="task-id">RUN #{selected.id}</span><h2>{selected.action_name}</h2></div><button className="close" onClick={() => setSelected(null)} aria-label="Close run detail">×</button></header>
        <p className="detail-stage">{selected.status} · {selected.internal_stage.replaceAll('_', ' ')}</p>
        <section className="git-evidence"><h3>Execution evidence</h3><dl>
          <dt>Commit</dt><dd>{selected.commit_sha}</dd><dt>Branch</dt><dd>{selected.branch_name || 'detached'}</dd><dt>Runner</dt><dd>{selected.runner_name}</dd><dt>Environment</dt><dd>{selected.env_file || 'none'}</dd>
        </dl></section>
        <section><h3>Steps</h3>{selected.steps.map((step) => <p className="action-step" key={step.position}><b>{step.position}</b><code>{step.command}</code><span>{step.status}</span></p>)}</section>
        <section><h3>Console</h3><pre className="action-console" aria-live="polite">{consoleText || 'Waiting for output…'}</pre></section>
        {(selected.status === 'queued' || selected.status === 'running') && <footer><button className="danger" onClick={() => void cancelRun()}>Kill run</button></footer>}
      </aside>}

      {confirm && <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="confirm-run-title">
        <section className="modal-panel"><header><div><span>CONFIRM</span><h2 id="confirm-run-title">Run {confirm.action.name}</h2></div><button className="close" onClick={() => setConfirm(null)} aria-label="Close confirmation">×</button></header>
          <dl><dt>Project</dt><dd>{confirm.project.name}</dd><dt>Commit</dt><dd>{confirm.catalog.commit_sha}</dd><dt>Runner</dt><dd>{confirm.action.runner}</dd><dt>Environment</dt><dd>{confirm.action.env_file || 'none'}</dd></dl>
          <ol>{confirm.action.commands.map((command) => <li key={command}><code>{command}</code></li>)}</ol>
          <footer><button onClick={() => setConfirm(null)}>Cancel</button><button className="primary" onClick={() => void queueRun()}>Queue run</button></footer>
        </section>
      </div>}

      {runnersOpen && <RunnerDialog api={api} runners={runners} close={() => setRunnersOpen(false)} reload={load} setError={setError} />}
    </main>
  )
}

function RunnerDialog({ api, runners, close, reload, setError }: {
  api: Api; runners: Runner[]; close: () => void; reload: () => Promise<void>; setError: (message: string) => void
}) {
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    try {
      await api.createRunner({
        name: String(data.get('name')), type: 'ssh', host: String(data.get('host')),
        port: Number(data.get('port')), username: String(data.get('username')),
        identity_file: String(data.get('identity_file')), workspace_root: String(data.get('workspace_root')),
      })
      event.currentTarget.reset()
      await reload()
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Could not create runner') }
  }

  async function apply(action: () => Promise<Runner>) {
    try { await action(); await reload() } catch (cause) { setError(cause instanceof Error ? cause.message : 'Runner action failed') }
  }

  return <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="runners-title"><section className="modal-panel runner-panel">
    <header><div><span>EXECUTION HOSTS</span><h2 id="runners-title">Runners</h2></div><button className="close" onClick={close} aria-label="Close runners">×</button></header>
    <div className="runner-list">{runners.map((runner) => <article key={runner.name}><div><strong>{runner.name}</strong><small>{runner.type === 'local' ? 'This host' : `${runner.username}@${runner.host}:${runner.port}`}</small></div>
      {runner.fingerprint && <code>{runner.fingerprint}</code>}
      {runner.id && <div className="runner-actions">
        {!runner.last_checked_at && runner.fingerprint && <button onClick={() => void apply(() => api.trustRunner(runner.id!, runner.fingerprint!))}>Trust</button>}
        <button onClick={() => void apply(() => api.testRunner(runner.id!))}>Test</button>
        <button onClick={() => void apply(() => api.updateRunner(runner.id!, { enabled: !runner.enabled }))}>{runner.enabled ? 'Disable' : 'Enable'}</button>
      </div>}
    </article>)}</div>
    <form className="runner-form" onSubmit={create}><h3>Add SSH runner</h3>
      <label>Name<input name="name" required /></label><label>Host<input name="host" required /></label><label>Port<input name="port" type="number" defaultValue="22" required /></label>
      <label>User<input name="username" required /></label><label>Private key path<input name="identity_file" required /></label><label>Workspace root<input name="workspace_root" defaultValue="carlo-projects" required /></label>
      <button className="primary" type="submit">Scan host</button>
    </form>
  </section></div>
}
