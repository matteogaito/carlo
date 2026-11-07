import { useEffect, useId, useState } from 'react'

export function MermaidDiagram({ source }: { source: string }) {
  const id = `mermaid-${useId().replace(/[^a-z0-9]/gi, '')}`
  const [svg, setSvg] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    let cancelled = false
    setSvg('')
    setError('')
    void import('mermaid').then(async ({ default: mermaid }) => {
      mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true })
      const result = await mermaid.render(id, source)
      if (!cancelled) setSvg(result.svg)
    }).catch(cause => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : 'Could not render diagram')
    })
    return () => { cancelled = true }
  }, [id, source])
  if (error) return <div className="diagram-error"><p role="alert">{error}</p><pre>{source}</pre></div>
  if (!svg) return <p role="status">Rendering diagram…</p>
  const viewBox = new DOMParser().parseFromString(svg, 'image/svg+xml').documentElement.getAttribute('viewBox')?.split(/\s+/).map(Number)
  const height = viewBox && Number.isFinite(viewBox[3]) ? Math.min(800, Math.max(160, viewBox[3] + 24)) : 400
  return <iframe className="mermaid-diagram" title="Mermaid diagram" sandbox="" height={height}
    srcDoc={`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:"><style>body{margin:8px;font-family:system-ui}svg{max-width:100%;height:auto}</style>${svg}`} />
}
