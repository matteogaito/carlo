import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from './App'
import { ActionsView } from './ActionsView'
import { DiscoveriesView } from './DiscoveriesView'
import {
  AuthenticationRequired,
  type ActionCatalog,
  type ActionRun,
  type AgentProfileSettings,
  type AgentPackage,
  type AvailableModel,
  type Api,
  type Discovery,
  type Project,
  type ModelProvider,
  type Runner,
  type Task,
  type User,
} from './api'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

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
  updated_at: '2026-09-05T08:00:00Z',
  plan: {
    revision: 1,
    brief_markdown: '# Brief',
    plan_markdown: '# Plan',
    metadata: { validation_commands: ['pytest -q'], skills: ['testing'], implementation_phases: ['Add the login endpoint', 'Validate the browser flow'] },
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
  uploadDiscoveryScreenshot: async () => { throw new Error('unused') },
  stopDiscovery: async () => { throw new Error('unused') },
  closeDiscovery: async () => { throw new Error('unused') },
  createDiscoveryTasks: async () => [],
  reworkChat: async () => { throw new Error('unused') },
  applyDiscoveryFix: async () => { throw new Error('unused') },
  getTask: async () => task,
  createProject: async () => { throw new Error('unused') },
  createTask: async () => { throw new Error('unused') },
  startPlanning: async () => task,
  replanTask: async () => task,
  reworkTask: async () => task,
  retrySubtask: async () => task,
  stopTask: async () => task,
  holdTask: async () => task,
  resumeTask: async () => task,
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
  listModelProviders: async () => [],
  createModelProvider: async () => { throw new Error('unused') },
  updateModelProvider: async () => { throw new Error('unused') },
  deleteModelProvider: async () => undefined,
  refreshModelProvider: async () => undefined,
  listModels: async () => [],
  updateModel: async () => { throw new Error('unused') },
  getPiSettings: async () => ({ compaction_enabled: true, reserve_percent: 10, keep_recent_percent: 20, default_packages: ['superpowers', 'ponytail'], default_skills: [] }),
  updatePiSettings: async () => ({ compaction_enabled: true, reserve_percent: 10, keep_recent_percent: 20, default_packages: ['superpowers', 'ponytail'], default_skills: [] }),
  listAgentProfiles: async () => [],
  listSkills: async () => [],
  listPackages: async () => [],
  createPackage: async (input) => ({ name: input.source, revision: null }),
  updatePackage: async () => ({ name: 'package', revision: null }),
  refreshPackage: async () => ({ name: 'package', revision: null }),
  deletePackage: async () => undefined,
  updateAgentProfile: async () => { throw new Error('unused') },
  setTaskModel: async () => { throw new Error('unused') },
  events: () => () => undefined,
}

describe('CARLO board', () => {
  it('filters by project and hides done tasks untouched for 14 days', async () => {
    const projects: Project[] = [
      { id: 1, name: 'CARLO', key: 'CAR', repository_path: '/tmp/carlo', default_branch: 'main', integration_branch: 'main', validation_commands: [] },
      { id: 2, name: 'Operations', key: 'OPS', repository_path: '/tmp/ops', default_branch: 'main', integration_branch: 'main', validation_commands: [] },
    ]
    const carTask = { ...task, id: 'CAR-1', title: 'Current CARLO work', project_id: 1 }
    const opsTask = { ...task, id: 'OPS-1', title: 'Operations work', project_id: 2, status: 'READY' as const }
    const recentDone = { ...task, id: 'CAR-2', title: 'Recent result', project_id: 1, status: 'DONE' as const, updated_at: new Date(Date.now() - 13 * 86400000).toISOString() }
    const oldDone = { ...task, id: 'CAR-3', title: 'Old result', project_id: 1, status: 'DONE' as const, updated_at: new Date(Date.now() - 15 * 86400000).toISOString() }
    render(<App api={{ ...api, listProjects: async () => projects, listTasks: async () => [carTask, opsTask, recentDone, oldDone] }} />)

    const projectBar = await screen.findByRole('navigation', { name: 'Projects' })
    const carFilter = await within(projectBar).findByRole('button', { name: 'CAR · CARLO' })
    expect(carFilter.style.backgroundColor).not.toBe('')
    expect(screen.getByRole('button', { name: /CAR-2.*Recent result/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /CAR-3.*Old result/ })).toBeNull()

    await userEvent.click(within(projectBar).getByRole('button', { name: 'OPS · Operations' }))
    expect(screen.getByRole('button', { name: /OPS-1.*Operations work/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /CAR-1.*Current CARLO work/ })).toBeNull()
  })

  it('switches between execution tasks and goals without hiding standalone tasks', async () => {
    const project: Project = { id: 1, name: 'CARLO', key: 'CAR', repository_path: '/tmp/carlo', default_branch: 'main', integration_branch: 'main', validation_commands: [] }
    const parent = { ...task, id: 'CAR-1', title: 'Parent task', subtask_count: 2 }
    const apiChild = { ...task, id: 'CAR-2', title: 'Add API', parent_task_id: 'CAR-1', parent_title: 'Parent task', subtask_position: 0, subtask_count: 0 }
    const uiChild = { ...task, id: 'CAR-3', title: 'Add UI', status: 'READY' as const, parent_task_id: 'CAR-1', parent_title: 'Parent task', subtask_position: 1, subtask_count: 0 }
    const standalone = { ...task, id: 'CAR-4', title: 'Standalone task', status: 'TEST' as const, parent_task_id: null, subtask_count: 0 }
    const superseded = { ...task, id: 'CAR-5', title: 'Old split', parent_task_id: 'CAR-1', parent_title: 'Parent task', subtask_position: 0, subtask_count: 0, superseded_at: '2026-09-06T08:00:00Z' }
    render(<App api={{ ...api, listProjects: async () => [project], listTasks: async () => [parent, apiChild, uiChild, standalone, superseded] }} />)

    await screen.findByRole('button', { name: /CAR-2 Add API Parent task/ })
    expect(screen.getByRole('button', { name: /CAR-3 Add UI Parent task/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /CAR-1 Parent task/ })).toBeNull()
    expect(screen.getByRole('button', { name: /CAR-4 Standalone task/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /CAR-5 Old split/ })).toBeNull()
    expect(screen.getAllByText('Parent task')).toHaveLength(2)

    const toggle = screen.getByRole('button', { name: 'Execution tasks → Goals' })
    expect(toggle.nextElementSibling).toBe(screen.getByRole('button', { name: 'CAR · CARLO' }))
    await userEvent.click(toggle)
    expect(screen.getByRole('button', { name: /CAR-1 Parent task/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /CAR-2 Add API/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /CAR-3 Add UI/ })).toBeNull()
    expect(screen.getByRole('button', { name: /CAR-4 Standalone task/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /CAR-5 Old split/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Goals → Execution tasks' })).toBeTruthy()
  })

  it('replans eligible aggregate tasks from the detail action bar', async () => {
    const parent = { ...task, status: 'READY' as const, stage: 'queued', subtask_count: 2, replan_allowed: true }
    const replanTask = vi.fn(async () => ({ ...parent, status: 'NOT_READY' as const, stage: 'planning', replan_allowed: false }))
    render(<App api={{ ...api, listTasks: async () => [parent], getTask: async () => parent, replanTask }} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Execution tasks → Goals' }))
    await userEvent.click(await screen.findByRole('button', { name: /CAR-1.*Login flow/i }))
    await userEvent.click(screen.getByRole('button', { name: 'Replan subtasks' }))
    expect(replanTask).toHaveBeenCalledWith('CAR-1')
  })

  it('labels replan approval as replacing subtasks', async () => {
    const replanned: Task = {
      ...task,
      status: 'NOT_READY',
      stage: 'awaiting_approval',
      subtask_count: 2,
      replan_allowed: false,
      plan: { ...task.plan!, approved_at: null, metadata: { ...task.plan!.metadata, replan: true } },
    }
    render(<App api={{ ...api, listTasks: async () => [replanned], getTask: async () => replanned }} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Execution tasks → Goals' }))
    await userEvent.click(await screen.findByRole('button', { name: /CAR-1.*Login flow/i }))
    expect(screen.getByRole('button', { name: 'Approve replan → Replace subtasks' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Replan subtasks' })).toBeNull()
  })

  it('opens the aggregate parent from a child card detail', async () => {
    const parent: Task = { ...task, id: 'CAR-1', title: 'Parent task', subtask_count: 2, status: 'READY', stage: 'queued', parent_task_id: null as string | null, parent_title: null as string | null }
    const child: Task = { ...task, id: 'CAR-2', title: 'Add API', parent_task_id: 'CAR-1', parent_title: 'Parent task', subtask_position: 0, subtask_count: 0 }
    const getTask = async (id: string) => (id === 'CAR-2' ? child : parent)
    const resumeTask = vi.fn(async () => ({ ...parent, status: 'IN_PROGRESS' as const, stage: 'implementing' }))
    render(<App api={{ ...api, listTasks: async () => [parent, child], getTask, resumeTask }} />)

    await userEvent.click(await screen.findByRole('button', { name: /CAR-2.*Add API Parent task/ }))
    const link = screen.getByRole('button', { name: /Part of Parent task/ })
    expect(screen.getByRole('heading', { name: 'Add API' }).nextElementSibling).toBe(link)
    await userEvent.click(link)

    expect(await screen.findByRole('heading', { name: 'Parent task' })).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Resume execution' }))
    expect(resumeTask).toHaveBeenCalledWith('CAR-1')
  })

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
    expect(screen.getByText('Add the login endpoint')).toBeTruthy()
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

  it('collapses a long Markdown megaprompt without hiding its contents', async () => {
    const megaprompt = `# Marketplace requirements\n\n${'Preserve every original requirement. '.repeat(30)}`
    const prompted: Task = {
      ...task,
      goal: megaprompt,
      prompt_path: 'prompts/2026-08-24-CAR-1-marketplace.md',
    }
    render(<App api={{ ...api, listTasks: async () => [prompted], getTask: async () => prompted }} />)

    await userEvent.click(await screen.findByRole('button', { name: /CAR-1.*Login flow/i }))
    const summary = screen.getByText(/Markdown megaprompt · .* characters/)
    const disclosure = summary.closest('details') as HTMLDetailsElement
    expect(disclosure.open).toBe(false)

    await userEvent.click(summary)
    expect(disclosure.open).toBe(true)
    expect(screen.getByRole('heading', { name: 'Marketplace requirements' })).toBeTruthy()
    expect(screen.getByText(/Preserve every original requirement/)).toBeTruthy()
  })

  it('summarizes a structured Plan and expands its implementation tasks', async () => {
    const planned: Task = {
      ...task,
      status: 'NOT_READY',
      stage: 'awaiting_approval',
      plan: {
        ...task.plan!,
        approved_at: null,
        metadata: {
          title: 'Deliver secure login',
          description: 'Reuse the existing session boundary without changing public errors.',
          key_points: ['Keep current clients compatible', 'Validate the complete login flow'],
          implementation_tasks: [{
            id: 'task-1',
            title: 'Add the session endpoint',
            position: 0,
            objective: 'Implement login through the existing authentication service.',
            files: [{ path: 'backend/carlo/api.py', mode: 'edit', ranges: [], symbols: ['create_app'], reason: 'Wire the session endpoint.' }],
            interfaces: ['POST /api/session'],
            changes: { 'backend/carlo/api.py': 'Add session endpoint handler.' },
            constraints: ['Keep the public error envelope unchanged.'],
            verification: { commands: ['pytest backend/tests/test_auth.py -q'], success: 'Login flow returns a valid session.' },
            done_when: ['Session endpoint returns 200 with a valid token.'],
            budget: { max_tool_calls: 20 },
          }],
          planner_profile: {
            name: 'plan', provider: 'pi', model: 'openai/gpt-5.6-sol', effort: 'high',
            tools: ['read', 'grep'], skills: ['carlo-planning', 'python-backend'],
          },
          amendment: {
            summary: 'The public response must change',
            reason: 'The existing envelope cannot represent the required state.',
          },
          skills: ['never-used'],
          validation_commands: ['pytest -q'],
        },
      },
      events: [{
        sequence: 21,
        task_id: 'CAR-1',
        discovery_id: null,
        type: 'agent.completed',
        payload: { skills: ['testing'] },
        created_at: new Date().toISOString(),
      }],
      used_skills: ['carlo-planning', 'frontend-design', 'testing'],
      skill_revisions: {
        superpowers: ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'],
        'frontend-design': ['bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
      },
    }
    render(<App api={{ ...api, listTasks: async () => [planned], getTask: async () => planned }} />)

    await userEvent.click(await screen.findByRole('button', { name: /CAR-1.*Login flow/i }))
    expect(screen.getByRole('heading', { name: 'Deliver secure login' })).toBeTruthy()
    expect(screen.getByText('Reuse the existing session boundary without changing public errors.')).toBeTruthy()
    expect(screen.getByText('Keep current clients compatible')).toBeTruthy()
    expect(screen.getByText('carlo-planning')).toBeTruthy()
    expect(screen.getByText('Skills used')).toBeTruthy()
    expect(screen.getByText('frontend-design')).toBeTruthy()
    expect(screen.getByText('@bbbbbbb')).toBeTruthy()
    expect(screen.getByText('superpowers')).toBeTruthy()
    expect(screen.getByText('@aaaaaaa')).toBeTruthy()
    expect(screen.getByText('testing')).toBeTruthy()
    expect(screen.queryByText('python-backend')).toBeNull()
    expect(screen.queryByText('never-used')).toBeNull()
    expect(screen.queryByText('For implementation')).toBeNull()
    expect(screen.getByText('Plan amendment needs approval')).toBeTruthy()
    expect(screen.getByText('The public response must change')).toBeTruthy()
    expect(screen.getByText('The existing envelope cannot represent the required state.')).toBeTruthy()

    const taskTitle = screen.getByText('Add the session endpoint')
    const disclosure = taskTitle.closest('details') as HTMLDetailsElement
    expect(disclosure.open).toBe(false)
    await userEvent.click(taskTitle)
    expect(disclosure.open).toBe(true)
    expect(screen.getByText('Implement login through the existing authentication service.')).toBeTruthy()
    expect(screen.getByText('backend/carlo/api.py')).toBeTruthy()
    expect(screen.getByText('Keep the public error envelope unchanged.')).toBeTruthy()
    expect(screen.getByText('Session endpoint returns 200 with a valid token.')).toBeTruthy()

    cleanup()
    const approved = { ...planned, plan: { ...planned.plan!, approved_at: '2026-08-23T20:00:00Z' } }
    render(<App api={{ ...api, listTasks: async () => [approved], getTask: async () => approved }} />)
    await userEvent.click(await screen.findByRole('button', { name: /CAR-1.*Login flow/i }))
    expect(screen.getByText('Approved plan amendment')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
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

  it('starts rework from the failed task action bar at the top', async () => {
    const failed = { ...task, status: 'FAILED' as const, stage: 'blocked' }
    const reworkTask = vi.fn(async () => ({
      ...failed,
      status: 'NOT_READY' as const,
      stage: 'planning',
      approved_plan_revision: null,
      branch_name: null,
      worktree_path: null,
      checkpoint_sha: null,
    }))
    render(<App api={{
      ...api,
      listTasks: async () => [failed],
      getTask: async () => failed,
      reworkTask,
    } as Api} />)

    await userEvent.click(await screen.findByRole('button', { name: /CAR-1.*Login flow/i }))
    const rework = await screen.findByRole('button', { name: 'Rework from original request' })
    const goal = screen.getByRole('heading', { name: 'Goal' })
    expect(rework.compareDocumentPosition(goal) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    await userEvent.click(rework)
    expect(reworkTask).toHaveBeenCalledWith('CAR-1')
  })

  it('opens a rework chat for a failed task and switches to the Discoveries view', async () => {
    const failed = { ...task, status: 'FAILED' as const, stage: 'blocked' }
    const discovery: Discovery = {
      // A freshly created rework-chat discovery starts with an empty state object
      // (no task_proposals, no summary/findings/...) — only fix_proposal ever appears there.
      id: 42, project_id: 1, task_id: 'CAR-1', title: 'Fix: Login flow', status: 'OPEN',
      state: {},
      final_summary: null, last_active_at: '2026-09-19T08:00:00Z', closed_at: null,
      current_turn: { id: 1, status: 'QUEUED', kind: 'CHAT', cancel_requested_at: null, error: null },
      messages: [{ id: 1, sequence: 1, role: 'user', content: 'Il task è fallito. Mostrami la diagnosi e proponi una soluzione.', metadata: {}, created_at: '2026-09-19T08:00:00Z' }],
    }
    const reworkChat = vi.fn(async () => discovery)
    render(<App api={{
      ...api,
      listTasks: async () => [failed],
      getTask: async () => failed,
      listDiscoveries: async () => [discovery],
      getDiscovery: async () => discovery,
      reworkChat,
    } as Api} />)

    await userEvent.click(await screen.findByRole('button', { name: /CAR-1.*Login flow/i }))
    await userEvent.click(await screen.findByRole('button', { name: 'Discuti e correggi' }))
    expect(reworkChat).toHaveBeenCalledWith('CAR-1')
    expect(await screen.findByRole('heading', { name: 'Fix: Login flow' })).toBeTruthy()
    expect(screen.getByText('Il task è fallito. Mostrami la diagnosi e proponi una soluzione.')).toBeTruthy()

    // Opening the Context panel must not crash even though state has none of the
    // repository-Discovery fields (this reproduces a real "Cannot read properties
    // of undefined (reading 'filter')" crash from an unguarded state.task_proposals access).
    await userEvent.click(screen.getByRole('button', { name: 'Context' }))
    expect(screen.getByRole('heading', { name: 'Context' })).toBeTruthy()
  })

  it('retries a failed subtask without planning', async () => {
    const failed = {
      ...task,
      id: 'CAR-2',
      status: 'FAILED' as const,
      stage: 'blocked',
      parent_task_id: 'CAR-1',
      approved_plan_revision: 1,
      branch_name: 'CAR-2-login',
      worktree_path: '/tmp/CAR-2-login',
    }
    const retrySubtask = vi.fn(async () => ({
      ...failed,
      status: 'READY' as const,
      stage: 'queued',
    }))
    render(<App api={{
      ...api,
      listTasks: async () => [failed],
      getTask: async () => failed,
      retrySubtask,
    } as Api} />)

    await userEvent.click(await screen.findByRole('button', { name: /CAR-2.*Login flow/i }))
    await userEvent.click(screen.getByRole('button', { name: 'Retry subtask' }))
    expect(retrySubtask).toHaveBeenCalledWith('CAR-2')
    expect(screen.queryByRole('button', { name: 'Rework from original request' })).toBeNull()
  })

  it('offers Retry subtask even when the subtask never reached a checkout', async () => {
    // A subtask can fail before ever getting a branch/worktree (e.g. a shared
    // checkout collision) — the backend already resets to a fresh checkout on
    // retry in that case, so the button must not require branch_name/worktree_path.
    const neverStarted = {
      ...task,
      id: 'CAR-2',
      status: 'FAILED' as const,
      stage: 'blocked',
      parent_task_id: 'CAR-1',
      approved_plan_revision: 1,
      branch_name: null,
      worktree_path: null,
    }
    const retrySubtask = vi.fn(async () => ({
      ...neverStarted,
      status: 'READY' as const,
      stage: 'queued',
    }))
    render(<App api={{
      ...api,
      listTasks: async () => [neverStarted],
      getTask: async () => neverStarted,
      retrySubtask,
    } as Api} />)

    await userEvent.click(await screen.findByRole('button', { name: /CAR-2.*Login flow/i }))
    await userEvent.click(screen.getByRole('button', { name: 'Retry subtask' }))
    expect(retrySubtask).toHaveBeenCalledWith('CAR-2')
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

  it('reads one console chunk without reloading Actions for an output event', async () => {
    const project: Project = { id: 1, name: 'Repo', key: 'REP', repository_path: '/repo', default_branch: 'main', integration_branch: 'carlo-Dev', validation_commands: [] }
    const run: ActionRun = {
      id: 7, project_id: 1, action_key: 'test', action_name: 'Test',
      definition: { key: 'test', name: 'Test', runner: 'local', env_file: null, commands: ['make test'] },
      runner_name: 'local', runner_snapshot: {}, status: 'running', internal_stage: 'running',
      commit_sha: 'abc', branch_name: 'main', origin: null, env_file: null, env_names: [],
      requested_at: '', started_at: '', finished_at: null, cancel_requested_at: null,
      current_step: 1, workspace_path: null, artifact_path: '', secret_path: null,
      log_offset: 0, recent_output: '', exit_code: null, error: null, cleanup_pending: false,
      steps: [],
    }
    const listActionRuns = vi.fn(async () => [run])
    const getActionConsole = vi.fn(async () => ({ offset: 0, next_offset: 4, text: 'line', eof: false }))
    const actionApi = {
      ...api,
      listActionRuns,
      listProjectActions: async () => ({ project_id: 1, commit_sha: 'abc', branch: 'main', dirty_paths: [], actions: [run.definition], error: null }),
      getActionRun: async () => ({ ...run }),
      getActionConsole,
    }
    const projects = [project]
    const setError = () => undefined
    const view = render(<ActionsView api={actionApi} projects={projects} event={null} setError={setError} />)
    await userEvent.click(await screen.findByRole('button', { name: /#7 running/ }))
    listActionRuns.mockClear()
    getActionConsole.mockClear()

    view.rerender(<ActionsView api={actionApi} projects={projects} event={{ sequence: 1, task_id: null, discovery_id: null, type: 'action.output_available', payload: { run_id: 7 }, created_at: '' }} setError={setError} />)

    await waitFor(() => expect(getActionConsole).toHaveBeenCalledTimes(1))
    expect(listActionRuns).not.toHaveBeenCalled()
  })

  it('does not reload Projects and Tasks for Action output events', async () => {
    let receive: ((event: import('./api').Event) => void) | undefined
    const listProjects = vi.fn(async () => [])
    const listTasks = vi.fn(async () => [])
    render(<App api={{
      ...api,
      listProjects,
      listTasks,
      events: (onEvent) => { receive = onEvent; return () => undefined },
    }} />)
    await screen.findByRole('heading', { name: 'Not Ready' })
    await waitFor(() => expect(listProjects).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(1))
    listProjects.mockClear()
    listTasks.mockClear()

    for (let sequence = 1; sequence <= 20; sequence += 1) {
      receive?.({ sequence, task_id: null, discovery_id: null, type: 'action.output_available', payload: { run_id: 7 }, created_at: '' })
    }

    await new Promise((resolve) => window.setTimeout(resolve, 200))
    expect(listProjects).not.toHaveBeenCalled()
    expect(listTasks).not.toHaveBeenCalled()
  })

  it('manages model providers, proportional Pi compaction and agent profiles', async () => {
    const provider: ModelProvider = {
      id: 1, name: 'Local OMLX', slug: 'omlx', kind: 'openai-compatible',
      base_url: 'http://127.0.0.1:11435/v1', credential_configured: true,
      credential_hint: '…ocal', compatibility: {}, refresh_interval_minutes: 15,
      last_refresh_status: 'SUCCESS', last_refresh_error: null,
      active: true,
    }
    const model: AvailableModel = {
      id: 2, model_provider_id: 1, model_provider_name: 'Local OMLX',
      external_id: 'Qwen3.8-27B', display_name: 'Qwen3.8-27B', status: 'AVAILABLE',
      discovered_context_window: 65536, discovered_max_tokens: 16384,
      context_window_override: null, max_tokens_override: null,
      effective_context_window: 65536, effective_max_tokens: 16384,
      reserve_tokens: 16384, keep_recent_tokens: 13107, selectable: true,
    }
    const profile: AgentProfileSettings = {
      name: 'plan', provider: 'pi', effort: null,
      permissions: {}, default_packages: ['ponytail'], default_skills: ['carlo-planning'], required_skills: ['carlo-planning'], context_policy: {}, active: true,
      available_model_id: null,
    }
    const refreshModelProvider = vi.fn(async () => undefined)
    const updateModelProvider = vi.fn(async () => provider)
    const updateAgentProfile = vi.fn(async () => profile)
    const packageSettings: AgentPackage[] = [
      { id: 1, name: 'superpowers', source: 'git:superpowers', revision: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', active_version: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', enabled: true, pinned: false, is_default: true, resources: {}, last_update_status: 'SUCCESS' },
      { id: 2, name: 'ponytail', source: 'git:ponytail', revision: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', active_version: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', enabled: true, pinned: false, is_default: true, resources: { skills: ['ponytail'] }, last_update_status: 'SUCCESS' },
    ]
    const updatePackage = vi.fn(async (id: number, input: { enabled?: boolean; is_default?: boolean }) => {
      const item = packageSettings.find((candidate) => candidate.id === id)!
      Object.assign(item, input)
      return item
    })
    const createPackage = vi.fn(async (input: { source: string; is_default: boolean }) => {
      const item: AgentPackage = { id: 3, name: input.source, source: input.source, revision: '1.0.0', active_version: '1.0.0', enabled: true, pinned: false, is_default: input.is_default, resources: {}, last_update_status: 'SUCCESS' }
      packageSettings.push(item)
      return item
    })
    const deletePackage = vi.fn(async (id: number) => {
      packageSettings.splice(packageSettings.findIndex((item) => item.id === id), 1)
    })
    let piSettings = { compaction_enabled: true, reserve_percent: 10, keep_recent_percent: 20, default_packages: ['superpowers', 'ponytail'], default_skills: [] }
    const updatePiSettings = vi.fn(async (input) => {
      piSettings = { ...piSettings, ...input }
      return piSettings
    })
    render(<App api={{
      ...api,
      getPiSettings: async () => piSettings,
      listModelProviders: async () => [provider],
      listModels: async () => [model],
      listAgentProfiles: async () => [profile],
      listPackages: async () => packageSettings,
      listSkills: async () => [
        { name: 'carlo-planning', source: 'carlo', revision: null, required_profiles: ['plan'] },
        { name: 'carlo-ui-design', source: 'carlo', revision: null, required_profiles: [] },
        { name: 'frontend-design', source: 'managed', revision: 'cccccccccccccccccccccccccccccccccccccccc', required_profiles: [] },
      ],
      refreshModelProvider,
      updateModelProvider,
      updateAgentProfile,
      updatePiSettings,
      updatePackage,
      createPackage,
      deletePackage,
    }} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Settings' }))
    expect(await screen.findByRole('heading', { name: 'Model providers' })).toBeTruthy()
    expect(screen.getAllByText('Qwen3.8-27B').length).toBeGreaterThan(0)
    expect(screen.getByText('16,384 reserved')).toBeTruthy()
    expect(screen.queryByLabelText('Default model')).toBeNull()
    expect(screen.queryByRole('button', { name: /as default/i })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Local OMLX' }))
    expect(refreshModelProvider).toHaveBeenCalledWith(1)
    await userEvent.click(screen.getByRole('button', { name: 'Coding agents' }))
    expect(screen.getByRole('heading', { name: 'Global Pi packages' })).toBeTruthy()
    const [defaultSuperpowers, defaultPonytail] = screen.getAllByRole('checkbox', { name: 'Default' }) as HTMLInputElement[]
    expect(defaultSuperpowers.checked).toBe(true)
    expect(defaultPonytail.checked).toBe(true)
    expect(screen.getByText('Skills: ponytail')).toBeTruthy()
    await userEvent.click(defaultPonytail)
    await waitFor(() => expect(updatePackage).toHaveBeenCalledWith(2, { is_default: false }))
    await userEvent.click(screen.getByRole('button', { name: '+ Package' }))
    await userEvent.type(screen.getByLabelText('Package source'), 'npm:pippo')
    await userEvent.click(screen.getByRole('button', { name: 'Install package' }))
    await waitFor(() => expect(createPackage).toHaveBeenCalledWith({ source: 'npm:pippo', is_default: true }))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await userEvent.click(screen.getByRole('button', { name: 'Delete npm:pippo' }))
    expect((await screen.findByRole('status', { name: 'Package deleted' })).textContent).toBe('Deleted ✓')
    expect(screen.queryByText('npm:pippo')).toBeNull()
    expect(screen.queryByText(/Legacy Pi/i)).toBeNull()
    expect(screen.getByRole('option', { name: 'Not configured — choose a managed model' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'omlx — Qwen3.8-27B' })).toBeTruthy()
    await userEvent.selectOptions(screen.getByLabelText('Model for plan'), '2')
    const coreSkill = screen.getByRole('checkbox', { name: 'plan skill carlo-planning' }) as HTMLInputElement
    expect(coreSkill.checked).toBe(true)
    expect(coreSkill.disabled).toBe(true)
    const inheritedPonytail = screen.getByRole('checkbox', { name: 'plan package ponytail' }) as HTMLInputElement
    expect(inheritedPonytail.checked).toBe(true)
    expect(inheritedPonytail.disabled).toBe(false)
    await userEvent.click(screen.getByRole('checkbox', { name: 'plan skill carlo-ui-design' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save plan' }))
    expect(updateAgentProfile).toHaveBeenCalledWith('plan', {
      available_model_id: 2,
      default_packages: ['ponytail'],
      default_skills: ['carlo-planning', 'carlo-ui-design'],
    })
    expect((await screen.findByRole('status', { name: 'plan saved' })).textContent).toBe('Saved ✓')
    updateAgentProfile.mockRejectedValueOnce(new Error('save failed'))
    await userEvent.click(screen.getByRole('button', { name: 'Save plan' }))
    await waitFor(() => expect(screen.queryByRole('status', { name: 'plan saved' })).toBeNull())
  })

  it('sets a concrete model override before task execution', async () => {
    const pending = { ...task, status: 'NOT_READY' as const, stage: 'created', available_model_id: null }
    const model: AvailableModel = {
      id: 7, model_provider_id: 3, model_provider_name: 'OpenRouter',
      external_id: 'openai/gpt-5.6-sol', display_name: 'GPT-5.6 Sol', status: 'AVAILABLE',
      discovered_context_window: 262144, discovered_max_tokens: 32768,
      context_window_override: null, max_tokens_override: null,
      effective_context_window: 262144, effective_max_tokens: 32768,
      reserve_tokens: 32768, keep_recent_tokens: 52428, selectable: true,
    }
    const setTaskModel = vi.fn(async () => ({ ...pending, available_model_id: 7 }))
    render(<App api={{
      ...api,
      listTasks: async () => [pending],
      getTask: async () => pending,
      listModels: async () => [model],
      setTaskModel,
    }} />)

    await userEvent.click(await screen.findByRole('button', { name: /CAR-1.*Login flow/i }))
    await userEvent.selectOptions(await screen.findByLabelText('Task model'), '7')
    expect(setTaskModel).toHaveBeenCalledWith('CAR-1', 7)
  })

  it('only asks for a provider API key when authentication is enabled', async () => {
    const createModelProvider = vi.fn(async () => ({
      id: 1, name: 'Local', slug: 'local', kind: 'openai-compatible' as const,
      base_url: 'http://127.0.0.1:11435/v1', credential_configured: false,
      credential_hint: null, compatibility: {}, refresh_interval_minutes: 15,
      last_refresh_status: 'NEVER', last_refresh_error: null,
      default_model_id: null, active: true,
    }))
    render(<App api={{ ...api, createModelProvider }} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Settings' }))
    await userEvent.click(await screen.findByRole('button', { name: '+ Provider' }))
    expect(screen.queryByLabelText('API key')).toBeNull()
    await userEvent.click(screen.getByLabelText('Requires API key'))
    expect(screen.getByLabelText('API key')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Show API key' })).toBeTruthy()
    await userEvent.click(screen.getByLabelText('Requires API key'))
    await userEvent.type(screen.getByLabelText('Name'), 'Local')
    await userEvent.type(screen.getByLabelText('Pi provider ID'), 'local')
    await userEvent.type(screen.getByLabelText('Base URL'), 'http://127.0.0.1:11435/v1')
    await userEvent.click(screen.getByRole('button', { name: 'Add provider' }))
    expect(createModelProvider).toHaveBeenCalledWith({
      name: 'Local', slug: 'local', base_url: 'http://127.0.0.1:11435/v1',
    })
  })

  it('continues a persistent Discovery conversation', async () => {
    const discovery: Discovery = {
      id: 7, project_id: 1, title: 'CSV direction', status: 'OPEN', task_id: null,
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

  it('keeps Discovery attachments local until the message is sent', async () => {
    const discovery: Discovery = {
      id: 7, project_id: 1, title: 'CSV direction', status: 'OPEN', task_id: null,
      state: { summary: '', findings: [], decisions: [], unresolved_questions: [], inspected_resources: [], commands: [], task_proposals: [] },
      final_summary: null, last_active_at: '', closed_at: null, current_turn: null, messages: [],
    }
    const sendDiscoveryMessage = vi.fn(async () => discovery)
    render(<DiscoveriesView api={{
      ...api,
      listDiscoveries: async () => [discovery],
      getDiscovery: async () => discovery,
      sendDiscoveryMessage,
    }} projects={[]} event={null} setError={() => undefined} />)

    await userEvent.click(await screen.findByRole('button', { name: /CSV direction/ }))
    const input = screen.getByLabelText('Allega file')
    const file = new File(['{"ok":true}'], 'sample.json', { type: 'application/json' })
    await userEvent.upload(input, file)

    expect(screen.getByText('sample.json')).toBeTruthy()
    expect(sendDiscoveryMessage).not.toHaveBeenCalled()
    await userEvent.type(screen.getByLabelText('Message'), 'Analizza questo')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(sendDiscoveryMessage).toHaveBeenCalledWith(7, 'Analizza questo', [file])
  })

  it('shows an image attachment inside the message draft', async () => {
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:local-preview'),
      revokeObjectURL: vi.fn(),
    })
    const discovery: Discovery = {
      id: 7, project_id: 1, title: 'Image review', status: 'OPEN', task_id: null,
      state: { summary: '', findings: [], decisions: [], unresolved_questions: [], inspected_resources: [], commands: [], task_proposals: [] },
      final_summary: null, last_active_at: '', closed_at: null, current_turn: null, messages: [],
    }
    render(<DiscoveriesView api={{
      ...api,
      listDiscoveries: async () => [discovery],
      getDiscovery: async () => discovery,
    }} projects={[]} event={null} setError={() => undefined} />)

    await userEvent.click(await screen.findByRole('button', { name: /Image review/ }))
    const file = new File(['image'], 'preview.png', { type: 'image/png' })
    await userEvent.upload(screen.getByLabelText('Allega file'), file)

    const composer = screen.getByLabelText('Message').closest('form')!
    const preview = within(composer).getByRole('img', { name: 'preview.png' })
    expect(preview.getAttribute('src')).toBe('blob:local-preview')
  })

  it('shows completed Discovery proposals as task actions in the chat', async () => {
    const proposal = {
      id: 'csv', title: 'Add CSV import', megaprompt: 'Implement CSV import.', depends_on: [],
      brief_markdown: 'Reuse the **existing ingestion service**.',
      plan_markdown: 'Implement parsing and preserve the error envelope.',
      metadata: { skills: [], implementation_phases: ['Implement parsing', 'Validate imports'], validation_commands: ['pytest -q'], browser_validation: false, build_required: false, run_required: false, deployment_expected: false, risk_flags: [], affected_areas: ['src/ingest.py'] },
    }
    const discovery: Discovery = {
      id: 9, project_id: 1, title: 'Plan CSV', status: 'OPEN', task_id: null,
      state: { summary: 'CSV is understood.', findings: [], decisions: [], unresolved_questions: [], inspected_resources: [], commands: [], task_proposals: [proposal] },
      final_summary: null, last_active_at: '', closed_at: null, current_turn: null, messages: [],
    }
    const createDiscoveryTasks = vi.fn(async () => [])
    render(<DiscoveriesView api={{ ...api, listDiscoveries: async () => [discovery], getDiscovery: async () => discovery, createDiscoveryTasks }} projects={[]} event={null} setError={() => undefined} />)

    await userEvent.click(await screen.findByRole('button', { name: /Plan CSV/ }))
    const chat = document.querySelector<HTMLElement>('.message-stream')!
    expect(within(chat).getByText('Add CSV import')).toBeTruthy()
    await userEvent.click(within(chat).getByRole('button', { name: 'Create all Ready tasks' }))
    expect(createDiscoveryTasks).toHaveBeenCalledWith(9, undefined)
  })

  it('surfaces the queued fix request in chat when Create task fails validation', async () => {
    const proposal = {
      id: 'archive', title: 'Archivio destinazioni', megaprompt: 'Build it.', depends_on: [],
      brief_markdown: 'Brief', plan_markdown: 'Plan',
      metadata: { skills: [], implementation_tasks: [{
        id: 'wp-1', title: 'Archivio', position: 0, objective: 'Persist destinations.',
        files: [{ path: 'src/archive.py', mode: 'create' as const, ranges: [], symbols: [], reason: 'New archive' }],
        interfaces: [], changes: { docs: 'stray key' }, constraints: [],
        verification: { commands: [], success: 'Pass' }, done_when: [], budget: { max_tool_calls: 20 },
      }], implementation_phases: ['Archivio'], validation_commands: ['pytest -q'], browser_validation: false, build_required: false, run_required: false, deployment_expected: false, risk_flags: [], affected_areas: [] },
    }
    const before: Discovery = {
      id: 11, project_id: 1, title: 'Archivio', status: 'OPEN', task_id: null,
      state: { summary: '', findings: [], decisions: [], unresolved_questions: [], inspected_resources: [], commands: [], task_proposals: [proposal] },
      final_summary: null, last_active_at: '', closed_at: null, current_turn: null,
      messages: [{ id: 1, sequence: 1, role: 'user', content: 'Plan it', metadata: {}, created_at: '2026-09-19T09:00:00Z' }],
    }
    const after: Discovery = {
      ...before,
      messages: [
        ...before.messages!,
        { id: 2, sequence: 2, role: 'system', content: 'La proposta "Archivio destinazioni" non è creabile: changes must describe every edit/create file and no other file. Correggila e richiama discovery_state con lo stato completo aggiornato.', metadata: {}, created_at: '2026-09-19T09:05:00Z' },
      ],
    }
    const createDiscoveryTasks = vi.fn(async () => {
      throw new Error("task proposal 'Archivio destinazioni' is not fully planned: changes must describe every edit/create file and no other file — ho chiesto a Pi di correggerla, guarda la chat.")
    })
    const getDiscovery = vi.fn(async () => after)
    const setError = vi.fn()
    render(<DiscoveriesView api={{
      ...api, listDiscoveries: async () => [before], getDiscovery, createDiscoveryTasks,
    }} projects={[]} event={null} setError={setError} />)

    await userEvent.click(await screen.findByRole('button', { name: /Archivio/ }))
    const chat = document.querySelector<HTMLElement>('.message-stream')!
    await userEvent.click(await within(chat).findByRole('button', { name: 'Create all Ready tasks' }))

    expect(setError).toHaveBeenCalledWith(expect.stringContaining('ho chiesto a Pi'))
    expect(getDiscovery).toHaveBeenCalled()
    expect(await screen.findByText(/non è creabile/)).toBeTruthy()
  })

  it('keeps Discovery task actions disabled until subtasks are planned', async () => {
    const discovery: Discovery = {
      id: 10, project_id: 1, title: 'Incomplete plan', status: 'OPEN', task_id: null,
      state: { summary: '', findings: [], decisions: [], unresolved_questions: [], inspected_resources: [], commands: [], task_proposals: [{
        id: 'csv', title: 'Add CSV import', megaprompt: 'Implement CSV import.', depends_on: [],
        brief_markdown: 'Reuse ingestion.', plan_markdown: 'Implement parsing.',
        metadata: { skills: [], implementation_phases: [], validation_commands: ['pytest -q'], browser_validation: false, build_required: false, run_required: false, deployment_expected: false, risk_flags: [], affected_areas: ['src/ingest.py'] },
      }] },
      final_summary: null, last_active_at: '', closed_at: null, current_turn: null, messages: [],
    }
    render(<DiscoveriesView api={{ ...api, listDiscoveries: async () => [discovery], getDiscovery: async () => discovery }} projects={[]} event={null} setError={() => undefined} />)

    await userEvent.click(await screen.findByRole('button', { name: /Incomplete plan/ }))
    const chat = document.querySelector<HTMLElement>('.message-stream')!
    expect((within(chat).getByRole('button', { name: 'Create all Ready tasks' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('renders aggregated realtime Discovery deltas', async () => {
    const discovery: Discovery = {
      id: 8, project_id: 1, title: 'Streaming', status: 'OPEN', task_id: null,
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
