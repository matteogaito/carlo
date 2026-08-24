import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import Markdown from 'react-markdown'

import {
  AuthenticationRequired,
  httpApi,
  type Api,
  type Event,
  type Plan,
  type Project,
  type Task,
  type TaskStatus,
  type User,
} from './api'
import { ActionsView } from './ActionsView'
import { DiscoveriesView } from './DiscoveriesView'
import './styles.css'

const columns: { status: TaskStatus; label: string; code: string }[] = [
  { status: 'NOT_READY', label: 'Not Ready', code: 'PLAN' },
  { status: 'READY', label: 'Ready', code: 'QUEUE' },
  { status: 'IN_PROGRESS', label: 'In Progress', code: 'RUN' },
  { status: 'TEST', label: 'Test', code: 'VERIFY' },
  { status: 'DONE', label: 'Done', code: 'CLEAR' },
  { status: 'FAILED', label: 'Failed', code: 'HALT' },
]
const COLLAPSED_GOAL_LENGTH = 800

function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(new Error('Could not read Markdown file'))
    reader.readAsText(file)
  })
}

export function App({ api = httpApi }: { api?: Api }) {
  const [user, setUser] = useState<User | null>()
  const [projects, setProjects] = useState<Project[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [selected, setSelected] = useState<Task | null>(null)
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState('')
  const [view, setView] = useState<'board' | 'discoveries' | 'actions'>('board')
  const [lastEvent, setLastEvent] = useState<Event | null>(null)
  const [installUpdate, setInstallUpdate] = useState<(() => void) | null>(null)
  const [detailWidth, setDetailWidth] = useState(() => Math.round(window.innerWidth / 2))

  useEffect(() => {
    const ready = (event: globalThis.Event) => setInstallUpdate(() => (event as CustomEvent<() => void>).detail)
    window.addEventListener('carlo-update-ready', ready)
    return () => window.removeEventListener('carlo-update-ready', ready)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [nextProjects, nextTasks] = await Promise.all([api.listProjects(), api.listTasks()])
      setProjects(nextProjects)
      setTasks(nextTasks)
      setError('')
    } catch (cause) {
      if (cause instanceof AuthenticationRequired) {
        setUser(null)
        return
      }
      setError(cause instanceof Error ? cause.message : 'Could not load CARLO state')
    }
  }, [api])

  useEffect(() => {
    let active = true
    void api.me().then((nextUser) => {
      if (active) setUser(nextUser)
    }).catch((cause) => {
      if (!active) return
      if (cause instanceof AuthenticationRequired) setUser(null)
      else setError(cause instanceof Error ? cause.message : 'Could not verify session')
    })
    return () => { active = false }
  }, [api])

  useEffect(() => {
    if (!user) return
    let refreshTimer: number | undefined
    void refresh()
    const closeEvents = api.events(
      (event) => {
        setLastEvent(event)
        if (!event.task_id) return
        if (refreshTimer) window.clearTimeout(refreshTimer)
        refreshTimer = window.setTimeout(() => {
          void api.listTasks().then(setTasks)
          if (event.task_id === selected?.id) void api.getTask(event.task_id).then(setSelected)
        }, 150)
      },
      setConnected,
      () => setUser(null),
    )
    return () => {
      if (refreshTimer) window.clearTimeout(refreshTimer)
      closeEvents()
    }
  }, [api, refresh, selected?.id, user])

  const active = useMemo(() => tasks.find((task) => task.status === 'IN_PROGRESS'), [tasks])

  async function act(action: () => Promise<Task>) {
    try {
      const task = await action()
      setSelected(task)
      await refresh()
    } catch (cause) {
      if (cause instanceof AuthenticationRequired) {
        setUser(null)
        return
      }
      setError(cause instanceof Error ? cause.message : 'Action failed')
      await refresh()
    }
  }

  async function beginPlanning(task: Task) {
    setSelected(task)
    await act(() => api.startPlanning(task.id))
  }

  if (user === undefined) {
    return <main className="auth-shell"><p>Starting CARLO…</p></main>
  }

  if (user === null) {
    return <LoginForm api={api} authenticated={setUser} />
  }

  async function signOut() {
    try {
      await api.logout()
    } finally {
      setProjects([])
      setTasks([])
      setSelected(null)
      setUser(null)
    }
  }

  return (
    <div className="shell">
      <header className="masthead">
        <div className="wordmark"><span>CARLO</span><sup>v3</sup></div>
        <p>Slow but relentless.</p>
        <div className="runtime-state">
          <span className={active ? 'pulse active' : 'pulse'} aria-hidden="true" />
          <span>{active ? `${active.id} running` : 'Executor idle'}</span>
          <span className={connected ? 'connection online' : 'connection'}>
            {connected ? 'Live' : 'Reconnecting'}
          </span>
          <span className="signed-user">{user.username}</span>
          <button className="sign-out" onClick={() => void signOut()}>Sign out</button>
        </div>
      </header>

      <nav className="view-tabs" aria-label="Main views">
        <button className={view === 'board' ? 'active' : ''} onClick={() => { setView('board'); setSelected(null) }}>Board</button>
        <button className={view === 'discoveries' ? 'active' : ''} onClick={() => { setView('discoveries'); setSelected(null) }}>Discoveries</button>
        <button className={view === 'actions' ? 'active' : ''} onClick={() => { setView('actions'); setSelected(null) }}>Actions</button>
      </nav>
      {view === 'board' && <CreateStrip api={api} projects={projects} refresh={refresh} setError={setError} onTaskCreated={beginPlanning} />}
      {error && <div className="error-banner" role="alert">{error}</div>}
      {installUpdate && <div className="update-banner">A CARLO update is ready.<button onClick={installUpdate}>Update now</button></div>}

      {view === 'board' ? <main className={selected ? 'workspace detail-open' : 'workspace'}>
        <section className="board" aria-label="Task board">
          {columns.map((column) => {
            const cards = tasks.filter((task) => task.status === column.status)
            return (
              <section className="column" key={column.status} aria-labelledby={`column-${column.status}`}>
                <header className="column-header">
                  <span>{column.code}</span>
                  <h2 id={`column-${column.status}`}>{column.label}</h2>
                  <b>{cards.length}</b>
                </header>
                <div className="card-stack">
                  {cards.map((task) => (
                    <button
                      className={task.status === 'IN_PROGRESS' ? 'task-card running' : 'task-card'}
                      key={task.id}
                      onClick={() => void api.getTask(task.id).then(setSelected)}
                      aria-label={`${task.id} ${task.title}`}
                    >
                      <span className="task-id">{task.id}</span>
                      <strong>{task.title}</strong>
                      <span className="stage">{task.stage.replaceAll('_', ' ')}</span>
                      {task.checkpoint_sha && <code>{task.checkpoint_sha.slice(0, 7)}</code>}
                    </button>
                  ))}
                  {!cards.length && <p className="empty">No tasks at this stage.</p>}
                </div>
              </section>
            )
          })}
        </section>

        {selected && (
          <TaskDetail
            task={selected}
            close={() => setSelected(null)}
            startPlanning={() => void act(() => api.startPlanning(selected.id))}
            rework={() => void act(() => api.reworkTask(selected.id))}
            approve={() => selected.plan && void act(() =>
              api.approvePlan(selected.id, selected.plan!.revision, selected.version)
            )}
            answerPlanning={(answer) => void act(() => api.answerPlanning(selected.id, answer))}
            width={detailWidth}
            resize={setDetailWidth}
          />
        )}
      </main> : view === 'discoveries' ? <DiscoveriesView api={api} projects={projects} event={lastEvent} setError={setError} /> : <ActionsView api={api} projects={projects} event={lastEvent} setError={setError} />}
    </div>
  )
}

function LoginForm({ api, authenticated }: {
  api: Api
  authenticated: (user: User) => void
}) {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    setBusy(true)
    setError('')
    try {
      authenticated(await api.login(String(data.get('username')), String(data.get('password'))))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Sign in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="auth-shell">
      <section className="login-panel" aria-labelledby="login-title">
        <div className="wordmark"><span>CARLO</span><sup>v3</sup></div>
        <p>Slow but relentless.</p>
        <h1 id="login-title">Sign in to CARLO</h1>
        <form onSubmit={submit}>
          <label>Username<input name="username" autoComplete="username" required autoFocus /></label>
          <label>Password<input name="password" type="password" autoComplete="current-password" required /></label>
          {error && <div className="login-error" role="alert">{error}</div>}
          <button type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
        </form>
      </section>
    </main>
  )
}

function CreateStrip({ api, projects, refresh, setError, onTaskCreated }: {
  api: Api
  projects: Project[]
  refresh: () => Promise<void>
  setError: (message: string) => void
  onTaskCreated: (task: Task) => Promise<void>
}) {
  const [projectOpen, setProjectOpen] = useState(false)
  const [taskOpen, setTaskOpen] = useState(false)
  const [promptMode, setPromptMode] = useState<'text' | 'file'>('text')

  async function submitProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    try {
      await api.createProject({
        name: String(data.get('name')),
        key: String(data.get('key')).toUpperCase(),
        repository_path: String(data.get('repository_path')),
      })
      form.reset()
      setProjectOpen(false)
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not create project')
    }
  }

  async function submitTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    try {
      let goal = String(data.get('goal') || '')
      let promptFilename: string | undefined
      if (promptMode === 'file') {
        const file = (form.elements.namedItem('prompt_file') as HTMLInputElement | null)?.files?.[0]
        if (!file || !file.name.toLowerCase().endsWith('.md')) {
          throw new Error('Choose a Markdown (.md) file')
        }
        if (file.size > 1024 * 1024) throw new Error('Megaprompt must be at most 1 MiB')
        goal = await readText(file)
        promptFilename = file.name
      }
      if (!goal.trim()) throw new Error('Megaprompt cannot be empty')
      const task = await api.createTask({
        project_id: Number(data.get('project_id')),
        title: String(data.get('title')),
        goal,
        ...(promptFilename ? { prompt_filename: promptFilename } : {}),
      })
      form.reset()
      setPromptMode('text')
      setTaskOpen(false)
      await onTaskCreated(task)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not create task')
    }
  }

  return (
    <section className="create-strip" aria-label="Create">
      <button onClick={() => setProjectOpen(!projectOpen)}>+ Project</button>
      <button onClick={() => setTaskOpen(!taskOpen)} disabled={!projects.length}>+ Task</button>
      {projectOpen && (
        <form onSubmit={submitProject}>
          <label>Name<input name="name" required /></label>
          <label>Key<input name="key" required maxLength={12} /></label>
          <label>Repository path<input name="repository_path" required /></label>
          <button type="submit">Create project</button>
        </form>
      )}
      {taskOpen && (
        <div className="modal-backdrop">
          <section className="modal-panel task-create-panel" role="dialog" aria-modal="true" aria-labelledby="create-task-title">
            <header>
              <div><span>NEW GOAL</span><h2 id="create-task-title">Create task</h2></div>
              <button className="close" onClick={() => setTaskOpen(false)} aria-label="Close create task">×</button>
            </header>
            <form className="task-create-form" onSubmit={submitTask}>
              <div className="task-create-fields">
                <label>Project<select name="project_id">{projects.map((project) => (
                  <option value={project.id} key={project.id}>{project.key} · {project.name}</option>
                ))}</select></label>
                <label>Title<input name="title" required autoFocus /></label>
              </div>
              <fieldset className="prompt-source">
                <legend>Megaprompt source</legend>
                <label className={promptMode === 'text' ? 'selected' : ''}>
                  <input aria-label="Write text" type="radio" name="prompt_mode" value="text" checked={promptMode === 'text'} onChange={() => setPromptMode('text')} />
                  <span><b>Write text</b><small>Compose the goal here</small></span>
                </label>
                <label className={promptMode === 'file' ? 'selected' : ''}>
                  <input aria-label="Upload Markdown" type="radio" name="prompt_mode" value="file" checked={promptMode === 'file'} onChange={() => setPromptMode('file')} />
                  <span><b>Upload Markdown</b><small>Use an existing .md file</small></span>
                </label>
              </fieldset>
              <div className="prompt-input">
                {promptMode === 'text' ? (
                  <label>Megaprompt<textarea name="goal" rows={12} required placeholder="Describe the outcome, constraints, and evidence CARLO should use." /></label>
                ) : (
                  <label>Markdown file<input name="prompt_file" type="file" accept=".md,text/markdown" /></label>
                )}
              </div>
              <footer>
                <button type="button" onClick={() => setTaskOpen(false)}>Cancel</button>
                <button className="primary" type="submit">Create task</button>
              </footer>
            </form>
          </section>
        </div>
      )}
    </section>
  )
}

function TaskDetail({ task, close, startPlanning, rework, approve, answerPlanning, width, resize }: {
  task: Task
  close: () => void
  startPlanning: () => void
  rework: () => void
  approve: () => void
  answerPlanning: (answer: string) => void
  width: number
  resize: (width: number) => void
}) {
  const validations = task.plan?.metadata.validation_commands || []
  const phases = task.plan?.metadata.implementation_phases || []
  const structuredPlan = Boolean(
    task.plan?.metadata.title
    && task.plan.metadata.description
    && task.plan.metadata.implementation_tasks?.length,
  )
  const [resizing, setResizing] = useState(false)
  const planningActivity = (task.events || [])
    .filter((event) => event.type.startsWith('planning.'))
    .slice(0, 8)
    .reverse()
  return (
    <aside className="task-detail" aria-label={`${task.id} details`} style={{ width }}>
      <div
        className="task-detail-resizer"
        role="separator"
        aria-label="Resize task details"
        aria-orientation="vertical"
        aria-valuemin={360}
        aria-valuemax={Math.max(360, window.innerWidth - 320)}
        aria-valuenow={width}
        tabIndex={0}
        style={{ left: window.innerWidth - width - 5 }}
        onPointerDown={(event) => {
          setResizing(true)
          event.currentTarget.setPointerCapture?.(event.pointerId)
        }}
        onPointerMove={(event) => resizing && resize(clampDetailWidth(window.innerWidth - event.clientX))}
        onPointerUp={() => setResizing(false)}
        onPointerCancel={() => setResizing(false)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') event.preventDefault()
          if (event.key === 'ArrowLeft') resize(clampDetailWidth(width + 32))
          else if (event.key === 'ArrowRight') resize(clampDetailWidth(width - 32))
        }}
      />
      <div className="task-detail-top">
        <header>
          <div><span className="task-id">{task.id}</span><h2>{task.title}</h2></div>
          <button className="close" onClick={close} aria-label="Close task detail">×</button>
        </header>
        <p className="detail-stage">{task.status.replaceAll('_', ' ')} · {task.stage.replaceAll('_', ' ')}</p>
        <nav className="task-actions" aria-label="Task actions">
          {task.stage === 'created' && <button onClick={startPlanning}>Build Brief & Plan</button>}
          {task.stage === 'awaiting_approval' && task.plan && <button onClick={approve}>Approve Plan → Ready</button>}
          {task.status === 'IN_PROGRESS' && task.stage === 'blocked' && Boolean(task.plan?.metadata.amendment) && <button onClick={approve}>Approve amendment</button>}
          {task.status === 'FAILED' && <button onClick={rework}>Rework from original request</button>}
        </nav>
      </div>
      {task.stage === 'planning' && !task.planning_question && <section className="planning-live" aria-live="polite">
        <h3>Pi is planning</h3>
        <div className="thinking"><i aria-hidden="true" />Repository-aware planning is active</div>
        {planningActivity.map((event) => <p className="timeline-row" key={event.sequence}>
          <time>{new Date(event.created_at).toLocaleTimeString()}</time>
          {planningEventLabel(event)}
        </p>)}
      </section>}
      <TaskGoal task={task} />
      {task.planning_question && <section className="planner-question">
        <h3>Planner question</h3>
        <p>{task.planning_question.text}</p>
        <form onSubmit={(event) => {
          event.preventDefault()
          answerPlanning(String(new FormData(event.currentTarget).get('answer')))
        }}>
          <label>Planner answer<textarea name="answer" rows={4} required /></label>
          <button type="submit">Answer planner</button>
        </form>
      </section>}
      {task.prompt_path && <section><h3>Megaprompt file</h3><code>{task.prompt_path}</code></section>}
      {task.plan && structuredPlan ? <StructuredPlan plan={task.plan} /> : <>
        <section><h3>Brief</h3>{task.plan ? <TaskMarkdown>{task.plan.brief_markdown}</TaskMarkdown> : <p>Planning has not produced a Brief yet.</p>}</section>
        <section><h3>Plan</h3>{task.plan ? <TaskMarkdown>{task.plan.plan_markdown}</TaskMarkdown> : <p>No Plan yet.</p>}</section>
      </>}
      {!structuredPlan && !!phases.length && <section><h3>Implementation phases</h3><ol className="implementation-phases">
        {phases.map((phase, index) => <li key={`${index}-${phase}`}><span>{index + 1}</span><p>{phase}</p></li>)}
      </ol></section>}
      <section>
        <h3>Validation</h3>
        {task.validations?.length ? task.validations.map((validation) => (
          <article className="evidence-row" key={`${validation.command}-${validation.created_at}`}>
            <div><code>{validation.command}</code><b>{validation.classification.replaceAll('_', ' ')}</b></div>
            <pre>{validation.summary || `Exited ${validation.exit_code}`}</pre>
          </article>
        )) : validations.length ? validations.map((command) => (
          <code key={command}>{command}</code>
        )) : <p>No checks declared yet.</p>}
      </section>
      {!!task.attempts?.length && <section><h3>Attempts</h3>{task.attempts.map((attempt) => (
        <p className="timeline-row" key={attempt.number}><b>#{attempt.number}</b> {attempt.outcome || 'running'}</p>
      ))}</section>}
      {!!task.escalations?.length && <section><h3>Escalations</h3>{task.escalations.map((item) => (
        <article className="evidence-row" key={item.created_at}><b>{item.reason}</b><p>{item.diagnosis || item.status}</p></article>
      ))}</section>}
      <section className="git-evidence">
        <h3>Git evidence</h3>
        <dl>
          <dt>Branch</dt><dd>{task.branch_name || 'Not prepared'}</dd>
          <dt>Worktree</dt><dd>{task.worktree_path || 'Not prepared'}</dd>
          <dt>Checkpoint</dt><dd>{task.checkpoint_sha || 'None'}</dd>
        </dl>
      </section>
      {!!task.events?.length && <section><h3>Activity</h3>{task.events.slice(0, 12).map((event) => (
        <p className="timeline-row" key={event.sequence}><time>{new Date(event.created_at).toLocaleTimeString()}</time>{event.type.replaceAll('.', ' ')}</p>
      ))}</section>}
    </aside>
  )
}

function TaskGoal({ task }: { task: Task }) {
  if (task.goal.length <= COLLAPSED_GOAL_LENGTH) {
    return <section><h3>Goal</h3><TaskMarkdown>{task.goal}</TaskMarkdown></section>
  }
  const label = task.prompt_path ? 'Markdown megaprompt' : 'Long request'
  return <section>
    <h3>Goal</h3>
    <details className="original-request">
      <summary>{label} · {task.goal.length.toLocaleString()} characters</summary>
      <TaskMarkdown>{task.goal}</TaskMarkdown>
    </details>
  </section>
}

function StructuredPlan({ plan }: { plan: Plan }) {
  const metadata = plan.metadata
  const tasks = metadata.implementation_tasks || []
  const planner = metadata.planner_profile
  const implementationSkills = metadata.skills || []
  return <>
    <section className="plan-summary">
      <h3>Plan</h3>
      <h4>{metadata.title}</h4>
      <p>{metadata.description}</p>
      {!!metadata.key_points?.length && <ul className="plan-key-points">
        {metadata.key_points.map((point) => <li key={point}>{point}</li>)}
      </ul>}
      {metadata.amendment && <aside
        className={`plan-amendment${plan.approved_at ? ' approved' : ''}`}
        role={plan.approved_at ? undefined : 'alert'}
      >
        <b>{plan.approved_at ? 'Approved plan amendment' : 'Plan amendment needs approval'}</b>
        <strong>{metadata.amendment.summary}</strong>
        <p>{metadata.amendment.reason}</p>
      </aside>}
      <div className="skill-ledger">
        <SkillChips label="Used to plan" skills={planner?.skills || []} empty="Not recorded" />
        <SkillChips label="For implementation" skills={implementationSkills} empty="None selected" />
      </div>
      {planner && <details className="planning-runtime">
        <summary>Planning runtime</summary>
        <dl>
          <dt>Profile</dt><dd>{planner.name}</dd>
          <dt>Provider</dt><dd>{planner.provider}</dd>
          <dt>Model</dt><dd>{planner.model || 'Default'}</dd>
          <dt>Effort</dt><dd>{planner.effort || 'Default'}</dd>
          <dt>Tools</dt><dd>{planner.tools.join(', ') || 'None'}</dd>
        </dl>
      </details>}
    </section>
    <section className="plan-task-list">
      <h3>Implementation tasks</h3>
      {tasks.map((item, index) => <details className="plan-task" key={`${index}-${item.title}`}>
        <summary><span>{index + 1}</span><strong>{item.title}</strong></summary>
        <div className="plan-task-body">
          <h4>Prompt</h4>
          <TaskMarkdown>{item.prompt}</TaskMarkdown>
          {!!item.intervention_points.length && <>
            <h4>Intervention points</h4>
            <ul>{item.intervention_points.map((point) => <li key={point}><code>{point}</code></li>)}</ul>
          </>}
        </div>
      </details>)}
    </section>
    <section className="plan-source-material">
      <details><summary>Repository Brief</summary><TaskMarkdown>{plan.brief_markdown}</TaskMarkdown></details>
      <details><summary>Full technical plan</summary><TaskMarkdown>{plan.plan_markdown}</TaskMarkdown></details>
    </section>
  </>
}

function SkillChips({ label, skills, empty }: { label: string; skills: string[]; empty: string }) {
  return <div><b>{label}</b><span>{skills.length ? skills.map((skill) => <code key={skill}>{skill}</code>) : <i>{empty}</i>}</span></div>
}

function clampDetailWidth(width: number): number {
  return Math.min(Math.max(width, 360), Math.max(360, window.innerWidth - 320))
}

function TaskMarkdown({ children }: { children: string }) {
  return <div className="task-markdown"><Markdown>{children}</Markdown></div>
}

function planningEventLabel(event: Event): string {
  const tool = String(event.payload.tool || 'Tool')
  const detail = String(event.payload.detail || '')
  if (event.type === 'planning.exploring') return 'Exploring repository'
  if (event.type === 'planning.tool.started') return `${tool}${detail ? ` · ${detail}` : ''}`
  if (event.type === 'planning.tool.completed') return `${tool} ${event.payload.failed ? 'failed' : 'completed'}`
  if (event.type === 'planning.drafting') return 'Drafting Brief and Plan'
  return event.type.replaceAll('.', ' ')
}
