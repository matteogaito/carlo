import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from './App'
import {
  AuthenticationRequired,
  type ActionCatalog,
  type ActionRun,
  type Api,
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
  status: 'IN_PROGRESS',
  stage: 'validating',
  priority: 0,
  version: 3,
  approved_plan_revision: 1,
  branch_name: 'CAR-1-login-flow',
  worktree_path: '/tmp/CAR-1-login-flow',
  checkpoint_sha: 'abc1234',
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
  getTask: async () => task,
  createProject: async () => { throw new Error('unused') },
  createTask: async () => { throw new Error('unused') },
  startPlanning: async () => task,
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
