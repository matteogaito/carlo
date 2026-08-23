import { FormEvent, useCallback, useEffect, useState } from 'react'
import Markdown from 'react-markdown'

import type { Api, Discovery, Event, Project } from './api'

export function DiscoveriesView({ api, projects, event, setError }: {
  api: Api
  projects: Project[]
  event: Event | null
  setError: (message: string) => void
}) {
  const [items, setItems] = useState<Discovery[]>([])
  const [selected, setSelected] = useState<Discovery | null>(null)
  const [creating, setCreating] = useState(false)
  const [contextOpen, setContextOpen] = useState(false)
  const [stream, setStream] = useState('')
  const [activity, setActivity] = useState('')

  const refresh = useCallback(async () => {
    const discoveries = await api.listDiscoveries()
    setItems(discoveries)
    if (selected) setSelected(await api.getDiscovery(selected.id))
  }, [api, selected?.id])

  useEffect(() => { void refresh().catch((error) => setError(String(error))) }, [refresh, event?.sequence])
  useEffect(() => {
    if (!selected || event?.discovery_id !== selected.id) return
    if (event.type === 'discovery.message.delta' && typeof event.payload.delta === 'string') {
      setStream((value) => value + event.payload.delta)
      setActivity('Pi is drafting a response…')
    } else if (event.type === 'discovery.tool.started') {
      const tool = String(event.payload.tool || 'Tool')
      const detail = String(event.payload.detail || '')
      setActivity(`${tool}${detail ? ` · ${detail}` : ''}`)
    } else if (event.type === 'discovery.tool.completed') {
      setActivity(`${String(event.payload.tool || 'Tool')} ${event.payload.failed ? 'failed' : 'completed'}`)
    } else if (event.type === 'discovery.turn.started') {
      setActivity('Pi is starting the repository session…')
    } else if (event.type === 'discovery.turn.completed' || event.type === 'discovery.turn.interrupted' || event.type === 'discovery.turn.failed') {
      setStream('')
      setActivity('')
    }
  }, [event?.sequence, selected?.id])

  async function open(discovery: Discovery) {
    try {
      setStream('')
      setActivity('')
      setSelected(await api.getDiscovery(discovery.id))
    } catch (error) { setError(String(error)) }
  }

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    try {
      const discovery = await api.createDiscovery({ project_id: Number(data.get('project_id')), title: String(data.get('title')), message: String(data.get('message')) })
      setCreating(false)
      setSelected(discovery)
      await refresh()
    } catch (error) { setError(String(error)) }
  }

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selected) return
    const form = event.currentTarget
    const content = String(new FormData(form).get('message')).trim()
    if (!content) return
    try {
      setSelected(await api.sendDiscoveryMessage(selected.id, content))
      form.reset()
      await refresh()
    } catch (error) { setError(String(error)) }
  }

  const state = selected?.state
  return <main className={`discoveries-page${selected ? ' conversation-open' : ''}`}>
    <aside className="discovery-list">
      <header><div><span>UNDERSTAND</span><h1>Discoveries</h1></div><button onClick={() => setCreating(true)}>New</button></header>
      {items.map((item) => <button className={selected?.id === item.id ? 'active' : ''} key={item.id} onClick={() => void open(item)}>
        <span>{projects.find((project) => project.id === item.project_id)?.key || 'PROJECT'} · {item.status}</span>
        <strong>{item.title}</strong>
        <small>{item.state.summary || 'Conversation ready'}</small>
      </button>)}
      {!items.length && <p className="empty">Start a Discovery to understand a project before changing it.</p>}
    </aside>

    {selected ? <section className="discovery-chat">
      <header>
        <button className="mobile-back" onClick={() => setSelected(null)} aria-label="Back to discoveries">←</button>
        <div><span>{selected.status}</span><h2>{selected.title}</h2></div>
        <button onClick={() => setContextOpen(true)}>Context</button>
      </header>
      <div className="message-stream">
        {selected.messages?.map((message) => message.role === 'tool' ?
          <details className="tool-message" key={message.id}><summary>{String(message.metadata.tool || 'Command output')}</summary><pre>{message.content}</pre></details> :
          <article className={`chat-message ${message.role}`} key={message.id}>
            <span>{message.role === 'user' ? 'You' : 'CARLO'}</span>
            <Markdown>{message.content}</Markdown>
          </article>)}
        {stream && <article className="chat-message assistant streaming"><span>CARLO</span><Markdown>{stream}</Markdown></article>}
        {selected.current_turn?.status === 'QUEUED' || selected.current_turn?.status === 'RUNNING' ? <div className="thinking" aria-live="polite"><i />{activity || 'Pi is exploring the repository…'} <button onClick={() => void api.stopDiscovery(selected.id).then(setSelected)}>Stop</button></div> : null}
      </div>
      {selected.status === 'OPEN' ? <form className="chat-composer" onSubmit={send}>
        <label><span>Message</span><textarea aria-label="Message" name="message" rows={2} placeholder="Continue the Discovery…" required /></label>
        <button type="submit">Send</button>
      </form> : <p className="closed-note">This Discovery is closed and read-only.</p>}
    </section> : <section className="discovery-welcome"><span>DISCOVERY</span><h2>Understand the project together.</h2><p>Explore code, run diagnostics, capture decisions, then create only the Tasks that are actually needed.</p><button onClick={() => setCreating(true)} disabled={!projects.length}>Start a Discovery</button></section>}

    {selected && <aside className={contextOpen ? 'discovery-context open' : 'discovery-context'}>
      <header><div><span>EVIDENCE</span><h2>Context</h2></div><button onClick={() => setContextOpen(false)} aria-label="Close context">×</button></header>
      <Context title="Summary" values={state?.summary ? [state.summary] : []} />
      <Context title="Findings" values={state?.findings || []} />
      <Context title="Decisions" values={state?.decisions || []} />
      <Context title="Open questions" values={state?.unresolved_questions || []} />
      <Context title="Files explored" values={state?.inspected_resources || []} code />
      <Context title="Commands" values={state?.commands || []} code />
      {!!state?.task_proposals.length && <section><h3>Tasks</h3>{state.task_proposals.map((proposal) => <article className="proposal" key={proposal.id}><strong>{proposal.title}</strong>{proposal.created_task_id ? <a href="#">{proposal.created_task_id}</a> : <button onClick={() => void api.createDiscoveryTasks(selected.id, [proposal.id]).then(refresh)}>Create task</button>}</article>)}<button className="create-all" onClick={() => void api.createDiscoveryTasks(selected.id).then(refresh)}>Create all</button></section>}
      {selected.status === 'OPEN' && <footer><button className="danger" onClick={() => void api.closeDiscovery(selected.id).then(setSelected)}>Close Discovery</button></footer>}
    </aside>}

    {creating && <div className="modal-backdrop"><section className="modal-panel discovery-create" role="dialog" aria-modal="true" aria-labelledby="new-discovery"><header><div><span>NEW CONVERSATION</span><h2 id="new-discovery">Start Discovery</h2></div><button className="close" onClick={() => setCreating(false)}>×</button></header><form onSubmit={create}><label>Project<select name="project_id">{projects.map((project) => <option key={project.id} value={project.id}>{project.key} · {project.name}</option>)}</select></label><label>Title<input name="title" required autoFocus /></label><label>First message<textarea name="message" rows={7} required /></label><footer><button type="button" onClick={() => setCreating(false)}>Cancel</button><button className="primary" type="submit">Start Discovery</button></footer></form></section></div>}
  </main>
}

function Context({ title, values, code = false }: { title: string; values: string[]; code?: boolean }) {
  if (!values.length) return null
  return <section><h3>{title}</h3><ul>{values.map((value, index) => <li key={`${value}-${index}`}>{code ? <code>{value}</code> : value}</li>)}</ul></section>
}
