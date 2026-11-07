import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ProjectView } from './ProjectView'
import type { Api, Project } from './api'

vi.mock('mermaid', () => ({ default: { initialize: vi.fn(), render: vi.fn(async (_id: string, source: string) => {
  if (source === 'bad') throw new Error('Invalid diagram')
  return { svg: '<svg viewBox="0 0 100 60"><text>Diagram</text></svg>' }
}) } }))
afterEach(cleanup)

const projects: Project[] = [1, 2].map(id => ({ id, key: `P${id}`, name: `Project ${id}`, repository_path: `/project${id}`, default_branch: 'main', integration_branch: 'dev', validation_commands: [] }))
const files: Record<string, string> = {
  'docs/CODE_INDEX.md': '# Index\n\n[Guide](guide.md)\n\n`src/app.py::hello`\n\n`src/provider.py::Implementation.run`\n\n```mermaid\ngraph LR; A-->B\n```',
  'docs/guide.md': '# Guide\n\n[Source](../src/app.py#L3)\n\n```mermaid\nbad\n```',
  'src/app.py': '# hello is implemented below\n\ndef hello():\n    pass',
  'src/provider.py': 'class Protocol:\n    def run(self): pass\n\nclass Implementation:\n    async def run(self): pass',
}
function makeApi() {
  return { listProjectFiles: vi.fn(async () => Object.keys(files)), getProjectFile: vi.fn(async (_project: number, path: string) => ({ path, content: files[path] })) } as unknown as Api
}

describe('Project reader', () => {
  it('requires project selection, follows relative links and Kor symbols, and renders diagrams', async () => {
    const api = makeApi()
    render(<ProjectView api={api} projects={projects} />)
    expect(api.listProjectFiles).not.toHaveBeenCalled()
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Project' }), '1')
    await userEvent.click(await screen.findByText('docs', { selector: 'summary' }))
    await userEvent.click(screen.getByRole('button', { name: 'CODE_INDEX.md' }))
    await screen.findByRole('heading', { name: 'Index' })
    expect(await screen.findByTitle('Mermaid diagram')).toBeTruthy()
    await userEvent.click(screen.getByRole('link', { name: 'src/app.py::hello' }))
    await waitFor(() => expect(document.querySelector('.source-line.selected')?.id).toBe('project-L3'))
    await userEvent.click(screen.getByRole('button', { name: 'Back' }))
    await userEvent.click(await screen.findByRole('link', { name: 'Guide' }))
    expect(await screen.findByRole('heading', { name: 'Guide' })).toBeTruthy()
    expect(await screen.findByText('Invalid diagram')).toBeTruthy()
    await userEvent.click(screen.getByRole('link', { name: 'Source' }))
    await waitFor(() => expect(document.querySelector('.source-line.selected')?.id).toBe('project-L3'))
    expect(api.getProjectFile).toHaveBeenLastCalledWith(1, 'src/app.py')
  })

  it('shows file errors and refreshes current content', async () => {
    const api = makeApi()
    render(<ProjectView api={api} projects={projects} />)
    await userEvent.selectOptions(screen.getByLabelText('Project'), '1')
    await userEvent.click(await screen.findByText('src', { selector: 'summary' }))
    await userEvent.click(screen.getByRole('button', { name: 'app.py' }))
    await screen.findByText('def hello():')
    vi.mocked(api.getProjectFile).mockRejectedValueOnce(new Error('Binary files cannot be previewed'))
    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Binary files cannot be previewed')
  })

  it('resolves qualified Kor methods within the named class', async () => {
    render(<ProjectView api={makeApi()} projects={projects} />)
    await userEvent.selectOptions(screen.getByLabelText('Project'), '1')
    await userEvent.click(await screen.findByText('docs', { selector: 'summary' }))
    await userEvent.click(screen.getByRole('button', { name: 'CODE_INDEX.md' }))
    await userEvent.click(await screen.findByRole('link', { name: 'src/provider.py::Implementation.run' }))
    await waitFor(() => expect(document.querySelector('.source-line.selected')?.id).toBe('project-L5'))
  })

  it('clears the document on project change and ignores a stale file response', async () => {
    const api = makeApi()
    let finish!: (value: { path: string; content: string }) => void
    vi.mocked(api.getProjectFile).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    render(<ProjectView api={api} projects={projects} />)
    await userEvent.selectOptions(screen.getByLabelText('Project'), '1')
    await userEvent.click(await screen.findByText('docs', { selector: 'summary' }))
    await userEvent.click(screen.getByRole('button', { name: 'CODE_INDEX.md' }))
    await userEvent.selectOptions(screen.getByLabelText('Project'), '2')
    finish({ path: 'docs/CODE_INDEX.md', content: '# Old project' })
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Old project' })).toBeNull())
    expect(within(screen.getByRole('article')).getByText('Select a file to read.')).toBeTruthy()
  })
})
