import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { App } from './App'
import type { Api, Task } from './api'

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
})
