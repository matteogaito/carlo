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
    [key: string]: unknown
  }
  approved_at: string | null
}

export interface Task {
  id: string
  project_id: number
  title: string
  goal: string
  status: TaskStatus
  stage: string
  priority: number
  version: number
  approved_plan_revision: number | null
  branch_name: string | null
  worktree_path: string | null
  checkpoint_sha: string | null
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

export interface Event {
  sequence: number
  task_id: string | null
  type: string
  payload: Record<string, unknown>
  created_at: string
}

export interface Api {
  me(): Promise<User>
  login(username: string, password: string): Promise<User>
  logout(): Promise<void>
  listProjects(): Promise<Project[]>
  listTasks(): Promise<Task[]>
  getTask(id: string): Promise<Task>
  createProject(input: Pick<Project, 'name' | 'key' | 'repository_path'>): Promise<Project>
  createTask(input: Pick<Task, 'project_id' | 'title' | 'goal'>): Promise<Task>
  startPlanning(id: string): Promise<Task>
  approvePlan(id: string, revision: number, version: number): Promise<Task>
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
  getTask: (id) => request(`/api/tasks/${id}`),
  createProject: (input) => request('/api/projects', { method: 'POST', body: JSON.stringify(input) }),
  createTask: (input) => request('/api/tasks', { method: 'POST', body: JSON.stringify(input) }),
  startPlanning: (id) => request(`/api/tasks/${id}/plan`, { method: 'POST' }),
  approvePlan: (id, revision, version) => request(`/api/tasks/${id}/approve`, {
    method: 'POST',
    body: JSON.stringify({ revision, version }),
  }),
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
