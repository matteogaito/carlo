import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from './App'
import { AuthenticationRequired, type Api, type Task, type User } from './api'

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
