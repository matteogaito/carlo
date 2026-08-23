import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from './App'
import { DiscoveriesView } from './DiscoveriesView'
import {
  AuthenticationRequired,
  type ActionCatalog,
  type ActionRun,
  type Api,
  type Discovery,
  type Project,
  type Runner,
  type Task,
  type User,
} from './api'

afterEach(cleanup)

const task: Task = {
  id: 'CAR-1',
  project_id: 1,
  title: 'Login flow',
  goal: 'Add login',
  prompt_path: null,
  status: 'IN_PROGRESS',
  stage: 'validating',
  priority: 0,
  version: 3,
  approved_plan_revision: 1,
  branch_name: 'CAR-1-login-flow',
  worktree_path: '/tmp/CAR-1-login-flow',
  checkpoint_sha: 'abc1234',
  planning_question: null,
  plan: {
    revision: 1,
    brief_markdown: '# Brief',
    plan_markdown: '# Plan',
    metadata: { validation_commands: ['pytest -q'], skills: ['testing'] },
    approved_at: '2026-08-16T08:00:00Z',
  },
  validations: [{
    command: 'pytest -q',
    exit_code: 1,
    classification: 'PARTIALLY_VERIFIED',
    summary: '2 failed',
    failure_count: 2,
    artifact_path: '/tmp/validation.log',
    created_at: '2026-08-16T08:01:00Z',
  }],
}

const api: Api = {
  me: async () => ({ username: 'admin', role: 'admin' }),
  login: async () => ({ username: 'admin', role: 'admin' }),
  logout: async () => undefined,
  listProjects: async () => [],
  listTasks: async () => [task],
  listDiscoveries: async () => [],
  getDiscovery: async () => { throw new Error('unused') },
  createDiscovery: async () => { throw new Error('unused') },
  sendDiscoveryMessage: async () => { throw new Error('unused') },
  stopDiscovery: async () => { throw new Error('unused') },
  closeDiscovery: async () => { throw new Error('unused') },
  createDiscoveryTasks: async () => [],
  getTask: async () => task,
  createProject: async () => { throw new Error('unused') },
  createTask: async () => { throw new Error('unused') },
  startPlanning: async () => task,
  answerPlanning: async () => task,
  approvePlan: async () => task,
  listProjectActions: async () => ({ project_id: 1, commit_sha: 'abc', branch: 'main', dirty_paths: [], actions: [], error: null }),
  listActionRuns: async () => [],
  getActionRun: async () => { throw new Error('unused') },
  createActionRun: async () => { throw new Error('unused') },
  getActionConsole: async () => ({ offset: 0, next_offset: 0, text: '', eof: true }),
  cancelActionRun: async () => { throw new Error('unused') },
  listRunners: async () => [],
  createRunner: async () => { throw new Error('unused') },
  updateRunner: async () => { throw new Error('unused') },
  trustRunner: async () => { throw new Error('unused') },
  testRunner: async () => { throw new Error('unused') },
  events: () => () => undefined,
}

describe('CARLO board', () => {
  it('maps tasks to six fixed columns and opens task detail', async () => {
    render(<App api={api} />)

    for (const name of ['Not Ready', 'Ready', 'In Progress', 'Test', 'Done', 'Failed']) {
      expect(await screen.findByRole('heading', { name })).toBeTruthy()
    }
    await userEvent.click(screen.getByRole('button', { name: /CAR-1.*Login flow/i }))
    expect(await screen.findByRole('heading', { name: 'Validation' })).toBeTruthy()
    expect(screen.getByText('pytest -q')).toBeTruthy()
    expect(screen.getByText('2 failed')).toBeTruthy()
  })

  it('renders Task Markdown and resizes the half-screen detail panel', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 })
    render(<App api={api} />)

    await userEvent.click(await screen.findByRole('button', { name: /CAR-1.*Login flow/i }))
    expect(screen.getByRole('heading', { name: 'Brief', level: 1 })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Plan', level: 1 })).toBeTruthy()

    const detail = screen.getByRole('complementary', { name: 'CAR-1 details' })
    const separator = screen.getByRole('separator', { name: 'Resize task details' })
    expect(detail.style.width).toBe('512px')
    await userEvent.type(separator, '{ArrowLeft}')
    expect(detail.style.width).toBe('544px')
    fireEvent.pointerDown(separator, { pointerId: 1 })
    fireEvent.pointerMove(separator, { pointerId: 1, clientX: 400 })
    fireEvent.pointerUp(separator, { pointerId: 1 })
    expect(detail.style.width).toBe('624px')
  })

  it('creates a task from an uploaded Markdown megaprompt', async () => {
    const project: Project = {
      id: 1,
      name: 'ECADMO',
      key: 'ECA',
      repository_path: '/Users/Shared/Projects/ecadmo',
      default_branch: 'main',
      integration_branch: 'carlo-Dev',
      validation_commands: [],
    }
    const createdTask: Task = {
      ...task,
      id: 'ECA-1',
      project_id: 1,
      title: 'Vinted integration',
      goal: '# Megaprompt\nImplement Vinted.',
      prompt_path: 'prompts/2026-08-21-ECA-1-vinted-integration.md',
      status: 'NOT_READY',
      stage: 'created',
      plan: null,
    }
    const createTask = vi.fn(async () => createdTask)
    const startPlanning = vi.fn(async () => ({
      ...createdTask,
      stage: 'planning',
      events: [{
        sequence: 1,
        task_id: 'ECA-1',
        discovery_id: null,
        type: 'planning.exploring',
        payload: {},
        created_at: '2026-08-23T08:00:00Z',
      }],
    }))
    render(<App api={{
      ...api,
      listProjects: async () => [project],
      listTasks: async () => [],
      createTask,
      startPlanning,
    }} />)

    await userEvent.click(await screen.findByRole('button', { name: '+ Task' }))
    expect(screen.getByRole('dialog', { name: 'Create task' })).toBeTruthy()
    await userEvent.type(screen.getByLabelText('Title'), 'Vinted integration')
    await userEvent.click(screen.getByRole('radio', { name: 'Upload Markdown' }))
    const fileInput = screen.getByLabelText('Markdown file') as HTMLInputElement
    await userEvent.upload(
      fileInput,
      new File(['# Megaprompt\nImplement Vinted.'], 'vinted.md', { type: 'text/markdown' }),
    )
    expect(fileInput.files).toHaveLength(1)
    expect((screen.getByLabelText('Title') as HTMLInputElement).value).toBe('Vinted integration')
    const form = screen.getByRole('dialog', { name: 'Create task' }).querySelector('form')!
    expect(Array.from(form.elements)
      .filter((element) => 'checkValidity' in element && !(element as HTMLInputElement).checkValidity())
      .map((element) => (element as HTMLInputElement).name)).toEqual([])
    await userEvent.click(screen.getByRole('button', { name: 'Create task' }))

    await waitFor(() => {
      expect(createTask).toHaveBeenCalledWith({
        project_id: 1,
        title: 'Vinted integration',
        goal: '# Megaprompt\nImplement Vinted.',
        prompt_filename: 'vinted.md',
      })
      expect(startPlanning).toHaveBeenCalledWith('ECA-1')
      expect(screen.queryByRole('dialog', { name: 'Create task' })).toBeNull()
    })
    expect(await screen.findByRole('complementary', { name: 'ECA-1 details' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Pi is planning' })).toBeTruthy()
    expect(screen.getByText('Exploring repository')).toBeTruthy()
  })

  it('answers a focused planner question from task detail', async () => {
    const questioning = { ...task, status: 'NOT_READY' as const, stage: 'planning', planning_question: { text: 'Which error envelope?' } }
    const answerPlanning = vi.fn(async () => ({ ...questioning, planning_question: null }))
    render(<App api={{ ...api, listTasks: async () => [questioning], getTask: async () => questioning, answerPlanning }} />)
    await userEvent.click(await screen.findByRole('button', { name: /CAR-1.*Login flow/i }))
    await userEvent.type(await screen.findByLabelText('Planner answer'), 'Use the existing envelope')
    await userEvent.click(screen.getByRole('button', { name: 'Answer planner' }))
    expect(answerPlanning).toHaveBeenCalledWith('CAR-1', 'Use the existing envelope')
  })

  it('groups repository actions, confirms a run and opens its console history', async () => {
    const project: Project = {
      id: 1,
      name: 'ECADMO',
      key: 'ECA',
      repository_path: '/Users/Shared/Projects/ecadmo',
      default_branch: 'main',
      integration_branch: 'carlo-Dev',
      validation_commands: [],
    }
    const catalog: ActionCatalog = {
      project_id: 1,
      commit_sha: 'abcdef1234567890',
      branch: 'main',
      dirty_paths: [],
      error: null,
      actions: [{ key: 'deploy-dev', name: 'Deploy to Dev', runner: 'linux-build', env_file: '.env.dev', commands: ['make test', 'make deploy'] }],
    }
    const run: ActionRun = {
      id: 42,
      project_id: 1,
      action_key: 'deploy-dev',
      action_name: 'Deploy to Dev',
      definition: catalog.actions[0],
      runner_name: 'linux-build',
      runner_snapshot: { type: 'ssh' },
      status: 'queued',
      internal_stage: 'queued',
      commit_sha: catalog.commit_sha!,
      branch_name: 'main',
      origin: 'ssh://git/example',
      env_file: '.env.dev',
      env_names: ['TOKEN'],
      requested_at: '2026-08-20T08:00:00Z',
      started_at: null,
      finished_at: null,
      cancel_requested_at: null,
      current_step: null,
      workspace_path: null,
      artifact_path: '/artifacts/42/console.log',
      secret_path: null,
      log_offset: 5,
      recent_output: 'ready',
      exit_code: null,
      error: null,
      cleanup_pending: false,
      steps: [{ position: 1, command: 'make test', status: 'pending', started_at: null, finished_at: null, exit_code: null, log_start: null, log_end: null }],
    }
    const createActionRun = vi.fn(async () => run)
    const actionApi: Api = {
      ...api,
      listProjects: async () => [project],
      listProjectActions: async () => catalog,
      listActionRuns: async () => [run],
      getActionRun: async () => run,
      createActionRun,
      getActionConsole: async () => ({ offset: 0, next_offset: 5, text: 'ready', eof: true }),
      listRunners: async () => [{ id: null, name: 'local', type: 'local', enabled: true, last_check_ok: true }],
    }

    render(<App api={actionApi} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Actions' }))
    expect(await screen.findByRole('heading', { name: 'ECADMO' })).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Run Deploy to Dev' }))
    expect(screen.getByText('abcdef1234567890')).toBeTruthy()
    expect(screen.getByText('make deploy')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Queue run' }))
    expect(createActionRun).toHaveBeenCalledWith(1, 'deploy-dev')
    expect(await screen.findByRole('heading', { name: 'Console' })).toBeTruthy()
    expect(screen.getByText('ready')).toBeTruthy()
  })

  it('shows runner management inside Actions', async () => {
    const runner: Runner = { id: null, name: 'local', type: 'local', enabled: true, last_check_ok: true }
    render(<App api={{ ...api, listRunners: async () => [runner] }} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Actions' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Manage runners' }))
    expect(screen.getByRole('heading', { name: 'Runners' })).toBeTruthy()
    expect(screen.getByText('local')).toBeTruthy()
    expect(screen.queryByLabelText(/private key contents/i)).toBeNull()
  })

  it('continues a persistent Discovery conversation', async () => {
    const discovery: Discovery = {
      id: 7, project_id: 1, title: 'CSV direction', status: 'OPEN',
      state: { summary: 'Reuse ingestion.', findings: ['src/ingest.py'], decisions: [], unresolved_questions: [], inspected_resources: ['src/ingest.py'], commands: [], task_proposals: [] },
      final_summary: null, last_active_at: '2026-08-23T08:00:00Z', closed_at: null,
      current_turn: null,
      messages: [{ id: 1, sequence: 1, role: 'assistant', content: '**Use ingestion.**', metadata: {}, created_at: '2026-08-23T08:00:00Z' }],
    }
    const sendDiscoveryMessage = vi.fn(async () => discovery)
    render(<App api={{
      ...api,
      listProjects: async () => [{ id: 1, name: 'Repo', key: 'REP', repository_path: '/repo', default_branch: 'main', integration_branch: 'carlo-Dev', validation_commands: [] }],
      listDiscoveries: async () => [discovery],
      getDiscovery: async () => discovery,
      sendDiscoveryMessage,
    }} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Discoveries' }))
    await userEvent.click(await screen.findByRole('button', { name: /CSV direction/ }))
    expect(screen.getByText('Use ingestion.')).toBeTruthy()
    await userEvent.type(screen.getByLabelText('Message'), 'What about errors?')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(sendDiscoveryMessage).toHaveBeenCalledWith(7, 'What about errors?')
  })

  it('renders aggregated realtime Discovery deltas', async () => {
    const discovery: Discovery = {
      id: 8, project_id: 1, title: 'Streaming', status: 'OPEN',
      state: { summary: '', findings: [], decisions: [], unresolved_questions: [], inspected_resources: [], commands: [], task_proposals: [] },
      final_summary: null, last_active_at: '2026-08-23T08:00:00Z', closed_at: null,
      current_turn: { id: 4, status: 'RUNNING', kind: 'CHAT', cancel_requested_at: null, error: null }, messages: [],
    }
    const discoveryApi = { ...api, listDiscoveries: async () => [discovery], getDiscovery: async () => discovery }
    const { rerender } = render(<DiscoveriesView api={discoveryApi} projects={[]} event={null} setError={() => undefined} />)
    await userEvent.click(await screen.findByRole('button', { name: /Streaming/ }))
    rerender(<DiscoveriesView api={discoveryApi} projects={[]} event={{ sequence: 98, task_id: null, discovery_id: 8, type: 'discovery.tool.started', payload: { tool: 'read', detail: 'src/ingest.py' }, created_at: '2026-08-23T08:00:30Z' }} setError={() => undefined} />)
    expect(await screen.findByText('read · src/ingest.py')).toBeTruthy()
    rerender(<DiscoveriesView api={discoveryApi} projects={[]} event={{ sequence: 99, task_id: null, discovery_id: 8, type: 'discovery.message.delta', payload: { turn_id: 4, delta: 'Repository evidence' }, created_at: '2026-08-23T08:01:00Z' }} setError={() => undefined} />)
    expect(await screen.findByText('Repository evidence')).toBeTruthy()
  })

  it('shows login before loading the board', async () => {
    const user: User = { username: 'admin', role: 'admin' }
    const login = vi.fn(async () => user)
    const unauthenticatedApi = {
      ...api,
      me: async () => { throw new AuthenticationRequired() },
      login,
      listTasks: vi.fn(api.listTasks),
    }

    render(<App api={unauthenticatedApi} />)

    expect(await screen.findByRole('heading', { name: 'Sign in to CARLO' })).toBeTruthy()
    expect(unauthenticatedApi.listTasks).not.toHaveBeenCalled()
    await userEvent.type(screen.getByLabelText('Username'), 'admin')
    await userEvent.type(screen.getByLabelText('Password'), 'admin-password')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(login).toHaveBeenCalledWith('admin', 'admin-password')
    expect(await screen.findByRole('heading', { name: 'Not Ready' })).toBeTruthy()
  })

  it('logs out to the login form', async () => {
    const logout = vi.fn(async () => undefined)
    render(<App api={{ ...api, logout }} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }))

    expect(logout).toHaveBeenCalledOnce()
    expect(await screen.findByRole('heading', { name: 'Sign in to CARLO' })).toBeTruthy()
  })
})
