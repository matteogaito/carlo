import { useEffect, useRef, useState, type ReactNode } from 'react'
import Markdown from 'react-markdown'
import type { Api, Project } from './api'
import { MermaidDiagram } from './MermaidDiagram'

type Target = { path: string; fragment?: string; symbol?: string }
const headingId = (text: string) => `project-heading-${text.toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu, '').trim().replace(/\s+/g, '-')}`
const textContent = (children: ReactNode): string => Array.isArray(children) ? children.map(textContent).join('') : typeof children === 'string' || typeof children === 'number' ? String(children) : children && typeof children === 'object' && 'props' in children ? textContent((children.props as { children: ReactNode }).children) : ''

function symbolLine(lines: string[], symbol: string): number {
  const parts = symbol.split('.')
  let start = 0
  let end = lines.length
  for (let part = 0; part < parts.length; part++) {
    const name = parts[part].replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const declaration = new RegExp(`(?:\\b(?:def|class|function|interface|type|const|let|var|struct|enum|func)\\s+${name}\\b|^\\s*(?:(?:async|public|private|protected|static|override)\\s+)*${name}\\s*\\()`)
    let found = -1
    for (let index = start; index < end; index++) {
      if (declaration.test(lines[index])) { found = index; break }
    }
    if (found < 0) return 0
    if (part === parts.length - 1) return found + 1
    const indentation = (line: string) => line.match(/^\s*/)?.[0].replace(/\t/g, '    ').length || 0
    const ownerIndent = indentation(lines[found])
    start = found + 1
    for (let index = start; index < end; index++) {
      if (lines[index].trim() && indentation(lines[index]) <= ownerIndent) { end = index; break }
    }
  }
  return 0
}

export function ProjectView({ api, projects }: { api: Api; projects: Project[] }) {
  const [projectId, setProjectId] = useState('')
  return <section className="project-view">
    <header className="project-toolbar"><label>Project <select value={projectId} onChange={event => setProjectId(event.target.value)}>
      <option value="">Select a project</option>
      {projects.map(project => <option key={project.id} value={project.id}>{project.key} · {project.name}</option>)}
    </select></label><span>Read only · Local working copy</span></header>
    {projectId ? <ProjectReader key={projectId} api={api} projectId={Number(projectId)} /> : <p className="empty">{projects.length ? 'Select a project to browse its files.' : 'No projects available.'}</p>}
  </section>
}

function ProjectReader({ api, projectId }: { api: Api; projectId: number }) {
  const [files, setFiles] = useState<string[]>([])
  const [treeLoading, setTreeLoading] = useState(true)
  const [treeError, setTreeError] = useState('')
  const [history, setHistory] = useState<Target[]>([])
  const target = history.at(-1)
  const [document, setDocument] = useState<{ path: string; content: string } | null>(null)
  const [fileError, setFileError] = useState('')
  const [revision, setRevision] = useState(0)
  const article = useRef<HTMLElement>(null)

  useEffect(() => {
    let cancelled = false
    setTreeLoading(true)
    setTreeError('')
    void api.listProjectFiles(projectId).then(next => {
      if (!cancelled) setFiles(next)
    }).catch(cause => {
      if (!cancelled) { setFiles([]); setTreeError(cause instanceof Error ? cause.message : 'Could not list files') }
    }).finally(() => { if (!cancelled) setTreeLoading(false) })
    return () => { cancelled = true }
  }, [api, projectId, revision])

  useEffect(() => {
    let cancelled = false
    setDocument(null)
    setFileError('')
    if (target) void api.getProjectFile(projectId, target.path).then(next => {
      if (!cancelled) setDocument(next)
    }).catch(cause => {
      if (!cancelled) setFileError(cause instanceof Error ? cause.message : 'Could not read file')
    })
    return () => { cancelled = true }
  }, [api, projectId, target, revision])

  const lines = document?.content.split('\n') || []
  let selectedLine = Number(target?.fragment?.match(/^L(\d+)/)?.[1]) || 0
  if (target?.symbol && document) {
    selectedLine = symbolLine(lines, target.symbol)
  }
  useEffect(() => {
    if (!document) return
    const id = selectedLine ? `project-L${selectedLine}` : target?.fragment ? headingId(target.fragment) : ''
    const node = id ? Array.from(article.current?.querySelectorAll('[id]') || []).find(element => element.id === id) : null
    node?.scrollIntoView?.({ block: 'center' })
    if (!id && article.current) article.current.scrollTop = 0
  }, [document, selectedLine, target])

  function navigate(next: Target) { setHistory(previous => [...previous, next]) }
  function localTarget(href: string, kor = false): Target | null {
    if (!document) return null
    try {
      const [reference, symbol] = kor ? href.split('::') : [href, undefined]
      if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(reference)) return null
      const url = new URL(reference, `https://project.local/${document.path}`)
      let path = decodeURIComponent(url.pathname.slice(1))
      if (kor && !files.includes(path) && files.includes(reference)) path = reference
      return { path, fragment: decodeURIComponent(url.hash.slice(1)), symbol }
    } catch { return null }
  }
  function link(href: string | undefined, children: ReactNode) {
    if (!href) return <span>{children}</span>
    if (/^https?:\/\//i.test(href)) return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
    const next = localTarget(href)
    return next ? <a href={href} onClick={event => { event.preventDefault(); navigate(next) }}>{children}</a> : <span>{children}</span>
  }

  return <div className="project-workspace">
    <aside className="project-files" aria-label="Project files"><header><strong>Files</strong><button onClick={() => setRevision(value => value + 1)}>Refresh</button></header>
      <p className="project-exclusions">Hidden and Git-ignored files excluded.</p>
      {treeLoading ? <p role="status">Loading files…</p> : treeError ? <p role="alert">{treeError}</p> : files.length ? <FileTree files={files} selected={target?.path} onOpen={path => navigate({ path })} /> : <p>No visible files.</p>}
    </aside>
    <article className="project-document" ref={article} aria-label="File preview">
      <header className="project-document-header"><button disabled={history.length < 2} onClick={() => setHistory(previous => previous.slice(0, -1))}>Back</button><code>{target?.path || 'No file selected'}</code></header>
      {fileError ? <p role="alert">{fileError}</p> : !target ? <p className="empty">Select a file to read.</p> : !document ? <p role="status">Loading file…</p> : <>
        {target.symbol && !selectedLine && <p role="status">Symbol “{target.symbol}” could not be located in this file.</p>}
        {/\.(md|markdown|mdown)$/i.test(document.path) ? <div className="project-markdown"><Markdown components={{
          a: ({ href, children }) => link(href, children),
          img: ({ alt }) => <span className="project-image-placeholder">{alt ? `[Image: ${alt}]` : '[Image]'}</span>,
          pre: ({ children }) => <div className="project-code-block">{children}</div>,
          code: ({ className, children }) => {
            const source = String(children).replace(/\n$/, '')
            if (className === 'language-mermaid') return <MermaidDiagram source={source} />
            const next = !className && source.includes('::') ? localTarget(source, true) : null
            return next && files.includes(next.path) ? <a href={next.path} onClick={event => { event.preventDefault(); navigate(next) }}><code>{children}</code></a> : <code className={className}>{children}</code>
          },
          h1: ({ children }) => <h1 id={headingId(textContent(children))}>{children}</h1>,
          h2: ({ children }) => <h2 id={headingId(textContent(children))}>{children}</h2>,
          h3: ({ children }) => <h3 id={headingId(textContent(children))}>{children}</h3>,
          h4: ({ children }) => <h4 id={headingId(textContent(children))}>{children}</h4>,
          h5: ({ children }) => <h5 id={headingId(textContent(children))}>{children}</h5>,
          h6: ({ children }) => <h6 id={headingId(textContent(children))}>{children}</h6>,
        }}>{document.content}</Markdown></div> : <pre className="project-source">{lines.map((line, index) => <span className={`source-line${selectedLine === index + 1 ? ' selected' : ''}`} id={`project-L${index + 1}`} key={index}><span className="line-number" aria-hidden="true">{index + 1}</span><code>{line || ' '}</code>{'\n'}</span>)}</pre>}
      </>}
    </article>
  </div>
}

function FileTree({ files, selected, onOpen, prefix = '' }: { files: string[]; selected?: string; onOpen: (path: string) => void; prefix?: string }) {
  const groups = new Map<string, string[]>()
  for (const path of files) {
    const name = path.slice(prefix.length).split('/')[0]
    const group = groups.get(name)
    if (group) group.push(path)
    else groups.set(name, [path])
  }
  return <ul>{Array.from(groups).sort(([a, aa], [b, bb]) => Number(bb[0] !== prefix + b) - Number(aa[0] !== prefix + a) || a.localeCompare(b)).map(([name, paths]) => {
    const path = prefix + name
    return <li key={path}>{paths[0] !== path ? <details key={`${path}-${selected?.startsWith(`${path}/`)}`} open={selected?.startsWith(`${path}/`) || undefined}><summary>{name}</summary><FileTree files={paths} prefix={`${path}/`} selected={selected} onOpen={onOpen} /></details> : <button className={selected === path ? 'active' : ''} aria-current={selected === path ? 'page' : undefined} onClick={() => onOpen(path)}>{name}</button>}</li>
  })}</ul>
}
