import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'

import {
  AuthenticationRequired,
  httpApi,
  type Api,
  type Event,
  type Project,
  type Task,
  type TaskStatus,
  type User,
} from './api'
import { ActionsView } from './ActionsView'
import './styles.css'

const columns: { status: TaskStatus; label: string; code: string }[] = [
  { status: 'NOT_READY', label: 'Not Ready', code: 'PLAN' },
  { status: 'READY', label: 'Ready', code: 'QUEUE' },
  { status: 'IN_PROGRESS', label: 'In Progress', code: 'RUN' },
  { status: 'TEST', label: 'Test', code: 'VERIFY' },
  { status: 'DONE', label: 'Done', code: 'CLEAR' },
  { status: 'FAILED', label: 'Failed', code: 'HALT' },
]

export function App({ api = httpApi }: { api?: Api }) {
  const [user, setUser] = useState<User | null>()
  const [projects, setProjects] = useState<Project[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [selected, setSelected] = useState<Task | null>(null)
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState('')
  const [view, setView] = useState<'board' | 'actions'>('board')
  const [lastEvent, setLastEvent] = useState<Event | null>(null)

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
    void refresh()
    return api.events(
      (event) => {
        setLastEvent(event)
        void refresh()
        if (event.task_id && event.task_id === selected?.id) {
          void api.getTask(event.task_id).then(setSelected)
        }
      },
      setConnected,
      () => setUser(null),
    )
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
    }
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
        <button className={view === 'actions' ? 'active' : ''} onClick={() => { setView('actions'); setSelected(null) }}>Actions</button>
      </nav>
      {view === 'board' && <CreateStrip api={api} projects={projects} refresh={refresh} setError={setError} />}
      {error && <div className="error-banner" role="alert">{error}</div>}

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
            approve={() => selected.plan && void act(() =>
              api.approvePlan(selected.id, selected.plan!.revision, selected.version)
            )}
          />
        )}
      </main> : <ActionsView api={api} projects={projects} event={lastEvent} setError={setError} />}
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

function CreateStrip({ api, projects, refresh, setError }: {
  api: Api
  projects: Project[]
  refresh: () => Promise<void>
  setError: (message: string) => void
}) {
  const [projectOpen, setProjectOpen] = useState(false)
  const [taskOpen, setTaskOpen] = useState(false)

  async function submitProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    try {
      await api.createProject({
        name: String(data.get('name')),
        key: String(data.get('key')).toUpperCase(),
        repository_path: String(data.get('repository_path')),
      })
      event.currentTarget.reset()
      setProjectOpen(false)
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not create project')
    }
  }

  async function submitTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    try {
      await api.createTask({
        project_id: Number(data.get('project_id')),
        title: String(data.get('title')),
        goal: String(data.get('goal')),
      })
      event.currentTarget.reset()
      setTaskOpen(false)
      await refresh()
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
        <form onSubmit={submitTask}>
          <label>Project<select name="project_id">{projects.map((project) => (
            <option value={project.id} key={project.id}>{project.key}</option>
          ))}</select></label>
          <label>Title<input name="title" required /></label>
          <label>Goal<textarea name="goal" required /></label>
          <button type="submit">Create task</button>
        </form>
      )}
    </section>
  )
}

function TaskDetail({ task, close, startPlanning, approve }: {
  task: Task
  close: () => void
  startPlanning: () => void
  approve: () => void
}) {
  const validations = task.plan?.metadata.validation_commands || []
  return (
    <aside className="task-detail" aria-label={`${task.id} details`}>
      <header>
        <div><span className="task-id">{task.id}</span><h2>{task.title}</h2></div>
        <button className="close" onClick={close} aria-label="Close task detail">×</button>
      </header>
      <p className="detail-stage">{task.status.replaceAll('_', ' ')} · {task.stage.replaceAll('_', ' ')}</p>
      <section><h3>Goal</h3><p>{task.goal}</p></section>
      <section><h3>Brief</h3><pre>{task.plan?.brief_markdown || 'Planning has not produced a Brief yet.'}</pre></section>
      <section><h3>Plan</h3><pre>{task.plan?.plan_markdown || 'No Plan yet.'}</pre></section>
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
      <footer>
        {task.stage === 'created' && <button onClick={startPlanning}>Build Brief & Plan</button>}
        {task.stage === 'awaiting_approval' && task.plan && <button onClick={approve}>Approve Plan → Ready</button>}
        {task.stage === 'blocked' && Boolean(task.plan?.metadata.amendment) && <button onClick={approve}>Approve amendment</button>}
      </footer>
    </aside>
  )
}
