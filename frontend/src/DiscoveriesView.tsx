import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'

import type { Api, Discovery, DiscoveryProposal, Event, Project } from './api'

export function DiscoveriesView({ api, projects, event, setError, openDiscoveryId, onOpenedDiscovery }: {
  api: Api
  projects: Project[]
  event: Event | null
  setError: (message: string) => void
  openDiscoveryId?: number | null
  onOpenedDiscovery?: () => void
}) {
  const [items, setItems] = useState<Discovery[]>([])
  const [selected, setSelected] = useState<Discovery | null>(null)
  const [creating, setCreating] = useState(false)
  const [contextOpen, setContextOpen] = useState(false)
  const [stream, setStream] = useState('')
  const [activity, setActivity] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  const [attachments, setAttachments] = useState<File[]>([])
  const [sending, setSending] = useState(false)

  const refresh = useCallback(async () => {
    const discoveries = await api.listDiscoveries()
    setItems(discoveries)
    if (selected) setSelected(await api.getDiscovery(selected.id))
  }, [api, selected?.id])

  useEffect(() => { void refresh().catch((error) => setError(String(error))) }, [refresh, event?.sequence])
  useEffect(() => {
    if (openDiscoveryId == null) return
    setStream('')
    setActivity('')
    setAttachments([])
    void api.getDiscovery(openDiscoveryId)
      .then(setSelected)
      .catch((error) => setError(String(error)))
      .finally(() => onOpenedDiscovery?.())
  }, [openDiscoveryId])
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

  async function createTasks(discoveryId: number, proposalIds?: string[]) {
    try {
      await api.createDiscoveryTasks(discoveryId, proposalIds)
    } catch (error) {
      setError(String(error))
    } finally {
      await refresh()
    }
  }

  async function open(discovery: Discovery) {
    try {
      setStream('')
      setActivity('')
      setAttachments([])
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
    if (!content && !attachments.length) return
    try {
      setSending(true)
      setSelected(await (attachments.length
        ? api.sendDiscoveryMessage(selected.id, content, attachments)
        : api.sendDiscoveryMessage(selected.id, content)))
      form.reset()
      setAttachments([])
      await refresh()
    } catch (error) { setError(String(error)) } finally { setSending(false) }
  }

  function attach(files: FileList | null) {
    const incoming = Array.from(files || [])
    if (attachments.length + incoming.length > 10) {
      setError('Puoi allegare al massimo 10 file.')
      return
    }
    const allowed = /\.(json|ya?ml|png|jpe?g|gif|webp|heic|heif)$/i
    const invalid = incoming.find((file) => !allowed.test(file.name) || file.size > 20 * 1024 * 1024)
    if (invalid) {
      setError(`${invalid.name}: formato non supportato o file oltre 20 MB.`)
      return
    }
    setAttachments((current) => [...current, ...incoming])
  }

  const state = selected?.state
  const pendingProposals = state?.task_proposals?.filter((proposal) => !proposal.created_task_id) || []
  const allProposalsReady = pendingProposals.length > 0 && pendingProposals.every(proposalReady)
  const fixProposal = state?.fix_proposal
  const fixReady = Boolean(fixProposal?.action && (fixProposal.package || fixProposal.packages?.length) && !fixProposal.validation_error)
  return <main className={`discoveries-page${selected ? ' conversation-open' : ''}`}>
    <aside className="discovery-list">
      <header><div><span>UNDERSTAND</span><h1>Discoveries</h1></div><button onClick={() => setCreating(true)}>New</button></header>
      {items.map((item) => <button className={selected?.id === item.id ? 'active' : ''} key={item.id} onClick={() => void open(item)}>
        <span>{projects.find((project) => project.id === item.project_id)?.key || 'PROJECT'} · {item.status}{item.task_id ? ` · Fix ${item.task_id}` : ''}</span>
        <strong>{item.title}</strong>
        <small>{item.state.summary || 'Conversation ready'}</small>
      </button>)}
      {!items.length && <p className="empty">Start a Discovery to understand a project before changing it.</p>}
    </aside>

    {selected ? <section className="discovery-chat">
      <header>
        <button className="mobile-back" onClick={() => setSelected(null)} aria-label="Back to discoveries">←</button>
        <div><span>{selected.status}{selected.task_id ? ` · Fix ${selected.task_id}` : ''}</span><h2>{selected.title}</h2></div>
        <button onClick={() => setContextOpen(true)}>Context</button>
      </header>
      <div className="message-stream">
        {selected.messages?.map((message) => message.role === 'tool' ?
          <details className="tool-message" key={message.id}><summary>{String(message.metadata?.tool || 'Command output')}</summary><pre>{message.content}</pre></details> :
          message.role === 'system' ?
          <article className="chat-message system" key={message.id}><span>Errore</span><Markdown>{message.content}</Markdown></article> :
          <article className={`chat-message ${message.role}`} key={message.id}>
            <span>{message.role === 'user' ? 'You' : 'CARLO'}</span>
            <Markdown>{message.content}</Markdown>
            {typeof message.metadata?.image_name === 'string' && <img className="chat-screenshot" src={`/api/discoveries/${selected.id}/screenshots/${encodeURIComponent(message.metadata.image_name)}`} alt="screenshot allegato" />}
            {messageAttachments(message.metadata).map((attachment) => attachment.kind === 'image'
              ? <img key={attachment.stored_name} className="chat-screenshot" src={`/api/discoveries/${selected.id}/attachments/${encodeURIComponent(attachment.stored_name)}`} alt={attachment.name} />
              : <a key={attachment.stored_name} className="chat-attachment" href={`/api/discoveries/${selected.id}/attachments/${encodeURIComponent(attachment.stored_name)}`} download={attachment.name}>📄 {attachment.name}</a>)}
          </article>)}
        {stream && <article className="chat-message assistant streaming"><span>CARLO</span><Markdown>{stream}</Markdown></article>}
        {selected.current_turn?.status === 'QUEUED' || selected.current_turn?.status === 'RUNNING' ? <div className="thinking" aria-live="polite"><i />{activity || 'Pi is exploring the repository…'} <button onClick={() => void api.stopDiscovery(selected.id).then(setSelected)}>Stop</button></div> : null}
        {!!pendingProposals.length && <section className="discovery-actions" aria-label="Ready task actions">
          <span>READY TO BUILD</span>
          <h3>{pendingProposals.length === 1 ? '1 task is ready' : `${pendingProposals.length} tasks are ready`}</h3>
          {pendingProposals.map((proposal) => <div key={proposal.id}>
            <strong>{proposal.title}</strong>
            {proposal.validation_error && <p className="proposal-error">⚠️ {proposal.validation_error}</p>}
            <button disabled={!proposalReady(proposal)} onClick={() => void createTasks(selected.id, [proposal.id])}>Create task</button>
          </div>)}
          <button className="create-all" disabled={!allProposalsReady} onClick={() => void createTasks(selected.id)}>Create all Ready tasks</button>
        </section>}
        {fixProposal && <section className="discovery-actions" aria-label="Fix proposal">
          <span>PROPOSTA DI CORREZIONE</span>
          <h3>{fixProposal.action === 'revise_parent' ? 'Ristruttura il task padre' : 'Rivedi questo task'}</h3>
          <p>{fixProposal.summary}</p>
          {fixProposal.validation_error && <p className="proposal-error">⚠️ {fixProposal.validation_error}</p>}
          <button className="create-all" disabled={!fixReady} onClick={() => void api.applyDiscoveryFix(selected.id).then(refresh).catch((error) => setError(String(error)))}>Applica e riprendi</button>
        </section>}
      </div>
      {selected.status === 'OPEN' ? <form className="chat-composer" onSubmit={send}>
        <input ref={fileRef} type="file" multiple accept=".json,.yaml,.yml,image/png,image/jpeg,image/gif,image/webp,image/heic,image/heif" className="composer-file" aria-label="Allega file" onChange={(event) => { attach(event.currentTarget.files); event.currentTarget.value = '' }} />
        <button type="button" className="attach" onClick={() => fileRef.current?.click()} disabled={sending} aria-label="Scegli allegati">📎</button>
        <div className="composer-input">
          {!!attachments.length && <div className="composer-attachments">{attachments.map((file, index) => <DraftAttachment file={file} key={`${file.name}-${file.lastModified}-${index}`} onRemove={() => setAttachments((current) => current.filter((_, item) => item !== index))} />)}</div>}
          <label><span>Message</span><textarea aria-label="Message" name="message" rows={2} placeholder="Continue the Discovery…" /></label>
        </div>
        <button type="submit" disabled={sending}>{sending ? '…' : 'Send'}</button>
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
      {!!state?.task_proposals?.length && <section><h3>Ready tasks</h3>{state.task_proposals.map((proposal) => <article className="proposal" key={proposal.id}>
        {proposal.created_task_id ? <><strong>{proposal.title}</strong><span className="proposal-state">Created · {proposal.created_task_id}</span></> : <>
          <details>
            <summary><strong>{proposal.title}</strong><span className="proposal-state">{proposal.validation_error ? 'Needs a fix' : proposalReady(proposal) ? 'Plan ready' : 'Planning in Discovery…'}</span></summary>
            {proposal.validation_error && <p className="proposal-error">⚠️ {proposal.validation_error}</p>}
            {proposalReady(proposal) && <div className="proposal-plan"><h4>Brief</h4><Markdown>{proposal.brief_markdown}</Markdown><h4>Plan</h4><Markdown>{proposal.plan_markdown}</Markdown>
              {!!proposal.metadata?.implementation_phases?.length && <ol>{proposal.metadata.implementation_phases.map((phase) => <li key={phase}>{phase}</li>)}</ol>}
            </div>}
          </details>
          <button disabled={!proposalReady(proposal)} onClick={() => void createTasks(selected.id, [proposal.id])}>Create Ready task</button>
        </>}
      </article>)}<button className="create-all" disabled={!allProposalsReady} onClick={() => void createTasks(selected.id)}>Create all Ready tasks</button></section>}
      {selected.status === 'OPEN' && <footer><button className="danger" onClick={() => void api.closeDiscovery(selected.id).then(setSelected)}>Close Discovery</button></footer>}
    </aside>}

    {creating && <div className="modal-backdrop"><section className="modal-panel discovery-create" role="dialog" aria-modal="true" aria-labelledby="new-discovery"><header><div><span>NEW CONVERSATION</span><h2 id="new-discovery">Start Discovery</h2></div><button className="close" onClick={() => setCreating(false)}>×</button></header><form onSubmit={create}><label>Project<select name="project_id">{projects.map((project) => <option key={project.id} value={project.id}>{project.key} · {project.name}</option>)}</select></label><label>Title<input name="title" required autoFocus /></label><label>First message<textarea name="message" rows={7} required /></label><footer><button type="button" onClick={() => setCreating(false)}>Cancel</button><button className="primary" type="submit">Start Discovery</button></footer></form></section></div>}
  </main>
}

function proposalReady(proposal: DiscoveryProposal): boolean {
  const metadata = proposal.metadata
  return Boolean(
    !proposal.validation_error
    && proposal.brief_markdown?.trim()
    && proposal.plan_markdown?.trim()
    && (metadata?.implementation_tasks?.length || metadata?.implementation_phases?.length),
  )
}

type MessageAttachment = { name: string; stored_name: string; kind: 'image' | 'file' }

function DraftAttachment({ file, onRemove }: { file: File; onRemove: () => void }) {
  const image = file.type.startsWith('image/')
  const [preview, setPreview] = useState('')
  useEffect(() => {
    if (!image) return
    const url = URL.createObjectURL(file)
    setPreview(url)
    return () => URL.revokeObjectURL(url)
  }, [file, image])
  return <figure className={image ? 'composer-attachment image' : 'composer-attachment document'}>
    {preview ? <img src={preview} alt={file.name} /> : <span className="file-mark">{'{ }'}</span>}
    <figcaption>{file.name}</figcaption>
    <button type="button" aria-label={`Rimuovi ${file.name}`} onClick={onRemove}>×</button>
  </figure>
}

function messageAttachments(metadata: Record<string, unknown>): MessageAttachment[] {
  if (!Array.isArray(metadata.attachments)) return []
  return metadata.attachments.filter((item): item is MessageAttachment => {
    if (!item || typeof item !== 'object') return false
    const value = item as Record<string, unknown>
    return typeof value.name === 'string' && typeof value.stored_name === 'string' && (value.kind === 'image' || value.kind === 'file')
  })
}

function Context({ title, values, code = false }: { title: string; values: string[]; code?: boolean }) {
  if (!values.length) return null
  return <section><h3>{title}</h3><ul>{values.map((value, index) => <li key={`${value}-${index}`}>{code ? <code>{value}</code> : value}</li>)}</ul></section>
}
