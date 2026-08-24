import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  type AvailableModel,
  type Api,
  type Discovery,
  type Project,
  type ModelProvider,
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
  stopDiscovery: async () => { throw new Error('unused') },
  closeDiscovery: async () => { throw new Error('unused') },
  createDiscoveryTasks: async () => [],
  getTask: async () => task,
  createProject: async () => { throw new Error('unused') },
  createTask: async () => { throw new Error('unused') },
  startPlanning: async () => task,
  reworkTask: async () => task,
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
  updateAgentProfile: async () => { throw new Error('unused') },
  setTaskModel: async () => { throw new Error('unused') },
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
            title: 'Add the session endpoint',
            prompt: 'Implement login through the existing authentication service.',
            intervention_points: ['backend/carlo/api.py:create_app', 'backend/tests/test_auth.py'],
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
    expect(screen.getByText('backend/carlo/api.py:create_app')).toBeTruthy()

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
      listPackages: async () => [
        { name: 'superpowers', revision: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' },
        { name: 'ponytail', revision: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' },
      ],
      listSkills: async () => [
        { name: 'carlo-planning', source: 'carlo', revision: null, required_profiles: ['brief', 'plan'] },
        { name: 'carlo-ui-design', source: 'carlo', revision: null, required_profiles: [] },
        { name: 'frontend-design', source: 'managed', revision: 'cccccccccccccccccccccccccccccccccccccccc', required_profiles: [] },
      ],
      refreshModelProvider,
      updateModelProvider,
      updateAgentProfile,
      updatePiSettings,
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
    expect(screen.getByRole('heading', { name: 'Default resources' })).toBeTruthy()
    const defaultSuperpowers = screen.getByRole('checkbox', { name: 'Default superpowers' }) as HTMLInputElement
    const defaultPonytail = screen.getByRole('checkbox', { name: 'Default ponytail' }) as HTMLInputElement
    expect(defaultSuperpowers.checked).toBe(true)
    expect(defaultPonytail.checked).toBe(true)
    expect(screen.getAllByText('bbbbbbb').length).toBeGreaterThan(0)
    await userEvent.click(defaultPonytail)
    await userEvent.click(screen.getByRole('button', { name: 'Save default resources' }))
    expect(updatePiSettings).toHaveBeenCalledWith({
      default_packages: ['superpowers'],
      default_skills: [],
    })
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

  it('previews a completed Discovery plan before creating its Ready task', async () => {
    const proposal = {
      id: 'csv', title: 'Add CSV import', megaprompt: 'Implement CSV import.', depends_on: [],
      brief_markdown: 'Reuse the **existing ingestion service**.',
      plan_markdown: 'Implement parsing and preserve the error envelope.',
      metadata: { skills: [], implementation_phases: ['Implement parsing', 'Validate imports'], validation_commands: ['pytest -q'], browser_validation: false, build_required: false, run_required: false, deployment_expected: false, risk_flags: [], affected_areas: ['src/ingest.py'] },
    }
    const discovery: Discovery = {
      id: 9, project_id: 1, title: 'Plan CSV', status: 'OPEN',
      state: { summary: 'CSV is understood.', findings: [], decisions: [], unresolved_questions: [], inspected_resources: [], commands: [], task_proposals: [proposal] },
      final_summary: null, last_active_at: '', closed_at: null, current_turn: null, messages: [],
    }
    const createDiscoveryTasks = vi.fn(async () => [])
    render(<DiscoveriesView api={{ ...api, listDiscoveries: async () => [discovery], getDiscovery: async () => discovery, createDiscoveryTasks }} projects={[]} event={null} setError={() => undefined} />)

    await userEvent.click(await screen.findByRole('button', { name: /Plan CSV/ }))
    await userEvent.click(screen.getByText('Add CSV import'))
    expect(screen.getByText('existing ingestion service')).toBeTruthy()
    expect(screen.getByText('Implement parsing and preserve the error envelope.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Create all Ready tasks' }) as HTMLButtonElement).disabled).toBe(false)
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
