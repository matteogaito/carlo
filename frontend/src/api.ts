export type TaskStatus = 'NOT_READY' | 'READY' | 'IN_PROGRESS' | 'TEST' | 'DONE' | 'FAILED'

export interface User {
  username: string
  role: 'admin' | 'member'
}

export class AuthenticationRequired extends Error {}

export interface Project {
  id: number
  name: string
  key: string
  repository_path: string
  default_branch: string
  integration_branch: string
  validation_commands: string[]
}

export interface Plan {
  revision: number
  brief_markdown: string
  plan_markdown: string
  metadata: {
    validation_commands?: string[]
    skills?: string[]
    implementation_phases?: string[]
    [key: string]: unknown
  }
  approved_at: string | null
}

export interface Task {
  id: string
  project_id: number
  title: string
  goal: string
  prompt_path: string | null
  status: TaskStatus
  stage: string
  priority: number
  version: number
  approved_plan_revision: number | null
  branch_name: string | null
  worktree_path: string | null
  checkpoint_sha: string | null
  planning_question: { text: string } | null
  plan: Plan | null
  attempts?: {
    number: number
    outcome: string | null
    error_fingerprint: string | null
    progress: Record<string, unknown>
    artifact_path: string | null
    created_at: string
  }[]
  validations?: {
    command: string
    exit_code: number | null
    classification: string
    summary: string
    failure_count: number | null
    artifact_path: string | null
    created_at: string
  }[]
  escalations?: {
    reason: string
    status: string
    diagnosis: string | null
    strategy: string | null
    created_at: string
  }[]
  events?: Event[]
}

export interface DiscoveryProposal {
  id: string
  title: string
  megaprompt: string
  depends_on: string[]
  created_task_id?: string
}

export interface DiscoveryState {
  summary: string
  findings: string[]
  decisions: string[]
  unresolved_questions: string[]
  inspected_resources: string[]
  commands: string[]
  task_proposals: DiscoveryProposal[]
}

export interface DiscoveryMessage {
  id: number
  sequence: number
  role: 'user' | 'assistant' | 'tool' | 'system'
  content: string
  metadata: Record<string, unknown>
  created_at: string
}

export interface Discovery {
  id: number
  project_id: number
  title: string
  status: 'OPEN' | 'CLOSED'
  state: DiscoveryState
  final_summary: string | null
  last_active_at: string
  closed_at: string | null
  current_turn: { id: number; status: string; kind: string; cancel_requested_at: string | null; error: string | null } | null
  messages?: DiscoveryMessage[]
}

export interface Event {
  sequence: number
  task_id: string | null
  discovery_id: number | null
  type: string
  payload: Record<string, unknown>
  created_at: string
}

export interface ActionDefinition {
  key: string
  name: string
  runner: string
  env_file: string | null
  commands: string[]
}

export interface ActionCatalog {
  project_id: number
  commit_sha: string | null
  branch: string | null
  dirty_paths: string[]
  actions: ActionDefinition[]
  error: string | null
}

export interface ActionStep {
  position: number
  command: string
  status: string
  started_at: string | null
  finished_at: string | null
  exit_code: number | null
  log_start: number | null
  log_end: number | null
}

export interface ActionRun {
  id: number
  project_id: number
  action_key: string
  action_name: string
  definition: ActionDefinition
  runner_name: string
  runner_snapshot: Record<string, unknown>
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
  internal_stage: string
  commit_sha: string
  branch_name: string | null
  origin: string | null
  env_file: string | null
  env_names: string[]
  requested_at: string
  started_at: string | null
  finished_at: string | null
  cancel_requested_at: string | null
  current_step: number | null
  workspace_path: string | null
  artifact_path: string
  secret_path: string | null
  log_offset: number
  recent_output: string
  exit_code: number | null
  error: string | null
  cleanup_pending: boolean
  steps: ActionStep[]
}

export interface ConsoleChunk {
  offset: number
  next_offset: number
  text: string
  eof: boolean
}

export interface Runner {
  id: number | null
  name: string
  type: 'local' | 'ssh'
  host?: string
  port?: number
  username?: string
  identity_file?: string
  workspace_root?: string
  enabled: boolean
  fingerprint?: string | null
  pending_fingerprint?: string | null
  last_check_ok: boolean | null
  last_check_error?: string | null
  last_checked_at?: string | null
}

export interface Api {
  me(): Promise<User>
  login(username: string, password: string): Promise<User>
  logout(): Promise<void>
  listProjects(): Promise<Project[]>
  listTasks(): Promise<Task[]>
  listDiscoveries(projectId?: number): Promise<Discovery[]>
  getDiscovery(id: number): Promise<Discovery>
  createDiscovery(input: { project_id: number; title: string; message: string }): Promise<Discovery>
  sendDiscoveryMessage(id: number, content: string): Promise<Discovery>
  stopDiscovery(id: number): Promise<Discovery>
  closeDiscovery(id: number): Promise<Discovery>
  createDiscoveryTasks(id: number, proposalIds?: string[]): Promise<Task[]>
  getTask(id: string): Promise<Task>
  createProject(input: Pick<Project, 'name' | 'key' | 'repository_path'>): Promise<Project>
  createTask(input: Pick<Task, 'project_id' | 'title' | 'goal'> & { prompt_filename?: string }): Promise<Task>
  startPlanning(id: string): Promise<Task>
  answerPlanning(id: string, answer: string): Promise<Task>
  approvePlan(id: string, revision: number, version: number): Promise<Task>
  listProjectActions(projectId: number): Promise<ActionCatalog>
  listActionRuns(projectId?: number): Promise<ActionRun[]>
  getActionRun(id: number): Promise<ActionRun>
  createActionRun(projectId: number, actionKey: string): Promise<ActionRun>
  getActionConsole(id: number, offset?: number): Promise<ConsoleChunk>
  cancelActionRun(id: number): Promise<ActionRun>
  listRunners(): Promise<Runner[]>
  createRunner(input: Omit<Runner, 'id' | 'enabled' | 'last_check_ok'>): Promise<Runner>
  updateRunner(id: number, input: Partial<Runner>): Promise<Runner>
  trustRunner(id: number, fingerprint: string): Promise<Runner>
  testRunner(id: number): Promise<Runner>
  events(
    onEvent: (event: Event) => void,
    onStatus?: (connected: boolean) => void,
    onAuthenticationRequired?: () => void,
  ): () => void
}

async function request<T>(path: string, init?: RequestInit, requireAuthentication = true): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    if (response.status === 401 && requireAuthentication) throw new AuthenticationRequired()
    const detail = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(detail.detail || `Request failed (${response.status})`)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const httpApi: Api = {
  me: () => request('/api/auth/me'),
  login: (username, password) => request('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  }, false),
  logout: () => request('/api/auth/logout', { method: 'POST' }),
  listProjects: () => request('/api/projects'),
  listTasks: () => request('/api/tasks'),
  listDiscoveries: (projectId) => request(`/api/discoveries${projectId ? `?project_id=${projectId}` : ''}`),
  getDiscovery: (id) => request(`/api/discoveries/${id}`),
  createDiscovery: (input) => request('/api/discoveries', { method: 'POST', body: JSON.stringify(input) }),
  sendDiscoveryMessage: (id, content) => request(`/api/discoveries/${id}/messages`, { method: 'POST', body: JSON.stringify({ content }) }),
  stopDiscovery: (id) => request(`/api/discoveries/${id}/stop`, { method: 'POST' }),
  closeDiscovery: (id) => request(`/api/discoveries/${id}/close`, { method: 'POST' }),
  createDiscoveryTasks: (id, proposalIds = []) => request(`/api/discoveries/${id}/tasks`, { method: 'POST', body: JSON.stringify({ proposal_ids: proposalIds }) }),
  getTask: (id) => request(`/api/tasks/${id}`),
  createProject: (input) => request('/api/projects', { method: 'POST', body: JSON.stringify(input) }),
  createTask: (input) => request('/api/tasks', { method: 'POST', body: JSON.stringify(input) }),
  startPlanning: (id) => request(`/api/tasks/${id}/plan`, { method: 'POST' }),
  answerPlanning: (id, answer) => request(`/api/tasks/${id}/plan/answer`, { method: 'POST', body: JSON.stringify({ answer }) }),
  approvePlan: (id, revision, version) => request(`/api/tasks/${id}/approve`, {
    method: 'POST',
    body: JSON.stringify({ revision, version }),
  }),
  listProjectActions: (projectId) => request(`/api/projects/${projectId}/actions`),
  listActionRuns: (projectId) => request(`/api/action-runs${projectId ? `?project_id=${projectId}` : ''}`),
  getActionRun: (id) => request(`/api/action-runs/${id}`),
  createActionRun: (projectId, actionKey) => request(
    `/api/projects/${projectId}/actions/${encodeURIComponent(actionKey)}/runs`,
    { method: 'POST' },
  ),
  getActionConsole: (id, offset = 0) => request(`/api/action-runs/${id}/console?offset=${offset}`),
  cancelActionRun: (id) => request(`/api/action-runs/${id}/cancel`, { method: 'POST' }),
  listRunners: () => request('/api/runners'),
  createRunner: (input) => request('/api/runners', { method: 'POST', body: JSON.stringify(input) }),
  updateRunner: (id, input) => request(`/api/runners/${id}`, { method: 'PATCH', body: JSON.stringify(input) }),
  trustRunner: (id, fingerprint) => request(`/api/runners/${id}/trust`, {
    method: 'POST', body: JSON.stringify({ fingerprint }),
  }),
  testRunner: (id) => request(`/api/runners/${id}/test`, { method: 'POST' }),
  events(onEvent, onStatus, onAuthenticationRequired) {
    let closed = false
    let socket: WebSocket | undefined
    let sequence = 0
    let retry: number | undefined
    const connect = () => {
      const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${protocol}://${location.host}/api/ws?after=${sequence}`)
      socket.onopen = () => onStatus?.(true)
      socket.onmessage = (message) => {
        const event = JSON.parse(message.data) as Event
        sequence = event.sequence
        onEvent(event)
      }
      socket.onclose = (event) => {
        onStatus?.(false)
        if (event.code === 4401) {
          onAuthenticationRequired?.()
          return
        }
        if (!closed) retry = window.setTimeout(connect, 1500)
      }
    }
    connect()
    return () => {
      closed = true
      if (retry) window.clearTimeout(retry)
      socket?.close()
    }
  },
}
