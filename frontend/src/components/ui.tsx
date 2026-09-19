/**
 * Shared presentation components.
 *
 * Findings, gates and warnings are rendered from one place so a severity or a
 * blocked gate always looks the same wherever it appears (PRD 6.1).
 */

import type { ReactNode } from 'react'
import type { Finding, GateResult, Severity } from '@/lib/api'
import { SEVERITY_COLOR, SEVERITY_LABEL, STATUS_COLOR, titleCase } from '@/lib/format'

export function Panel({
  title,
  subtitle,
  actions,
  children,
  tone,
}: {
  title: string
  subtitle?: ReactNode
  actions?: ReactNode
  children: ReactNode
  tone?: 'default' | 'warning' | 'danger'
}) {
  return (
    <section className={`panel ${tone && tone !== 'default' ? `panel-${tone}` : ''}`}>
      <header className="panel-head">
        <div>
          <h2>{title}</h2>
          {subtitle ? <p className="muted">{subtitle}</p> : null}
        </div>
        {actions ? <div className="panel-actions">{actions}</div> : null}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  )
}

export function Badge({ children, color, title }: { children: ReactNode; color?: string; title?: string }) {
  return (
    <span className="badge" style={color ? { borderColor: color, color } : undefined} title={title}>
      {children}
    </span>
  )
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <Badge color={STATUS_COLOR[status] ?? '#8b949e'} title={`Verification status: ${status}`}>
      {status}
    </Badge>
  )
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <Badge color={SEVERITY_COLOR[severity]} title={SEVERITY_LABEL[severity]}>
      {SEVERITY_LABEL[severity]}
    </Badge>
  )
}

export function StaleBadge({ reason }: { reason?: string | null }) {
  return (
    <Badge color="#db6d28" title={reason ?? 'An upstream input changed after this was produced'}>
      Stale
    </Badge>
  )
}

/**
 * A finding, shown in full. The affected object, the severity, the consequence
 * and the recommended resolution are all required by the interaction standards,
 * so none of them is collapsed away.
 */
export function FindingCard({ finding }: { finding: Finding }) {
  return (
    <article className={`finding finding-${finding.severity.toLowerCase()}`}>
      <header>
        <SeverityBadge severity={finding.severity} />
        <code>{finding.code}</code>
        {finding.object_ref ? <span className="muted">on {finding.object_ref}</span> : null}
        {finding.waived_by ? <Badge color="#8b949e">Waived</Badge> : null}
        {!finding.waivable ? <Badge color="#f85149">Cannot be waived</Badge> : null}
      </header>
      <p className="finding-message">{finding.message}</p>
      <dl>
        <dt>Consequence</dt>
        <dd>{finding.consequence || '—'}</dd>
        <dt>Recommended resolution</dt>
        <dd>{finding.recommendation || '—'}</dd>
      </dl>
      {Object.keys(finding.detail ?? {}).length ? (
        <details>
          <summary>Detail</summary>
          <pre>{JSON.stringify(finding.detail, null, 2)}</pre>
        </details>
      ) : null}
    </article>
  )
}

export function GateCard({ gate }: { gate: GateResult }) {
  const blocking = gate.findings.filter((f) => f.blocking)
  const advisory = gate.findings.filter((f) => !f.blocking)
  return (
    <div className={`gate ${gate.passed ? 'gate-pass' : 'gate-block'}`}>
      <header>
        <strong>{gate.gate}</strong>
        <Badge color={gate.passed ? '#3fb950' : '#f85149'}>{gate.passed ? 'Passed' : 'Blocked'}</Badge>
        {!gate.passed ? <span className="muted">blocks {gate.blocks}</span> : null}
      </header>
      {blocking.length ? (
        <div className="gate-findings">
          {blocking.map((finding) => (
            <FindingCard key={`${finding.code}-${finding.object_ref}`} finding={finding} />
          ))}
        </div>
      ) : null}
      {advisory.length ? (
        <details className="gate-advisory">
          <summary>
            {advisory.length} non-blocking finding{advisory.length === 1 ? '' : 's'}
          </summary>
          {advisory.map((finding) => (
            <FindingCard key={`${finding.code}-${finding.object_ref}`} finding={finding} />
          ))}
        </details>
      ) : null}
      {gate.passed && !gate.findings.length ? <p className="muted">No findings.</p> : null}
    </div>
  )
}

export function Empty({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {hint ? <p className="muted">{hint}</p> : null}
    </div>
  )
}

export function Loading({ what = 'data' }: { what?: string }) {
  return <div className="loading">Loading {what}…</div>
}

export function ErrorNote({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const apiError = error as { code?: string; message?: string; detail?: unknown; findings?: Finding[] }
  const findings = apiError?.findings ?? []
  return (
    <div className="error-note">
      <header>
        <Badge color="#f85149">{apiError?.code ?? 'error'}</Badge>
        <span>{apiError?.message ?? String(error)}</span>
        {onRetry ? (
          <button type="button" onClick={onRetry}>
            Retry
          </button>
        ) : null}
      </header>
      {findings.length ? (
        <div className="gate-findings">
          {findings.map((finding, index) => (
            <FindingCard key={`${finding.code}-${index}`} finding={finding} />
          ))}
        </div>
      ) : apiError?.detail ? (
        <details>
          <summary>Detail</summary>
          <pre>{JSON.stringify(apiError.detail, null, 2)}</pre>
        </details>
      ) : null}
    </div>
  )
}

export function KeyValue({ rows }: { rows: Array<[string, ReactNode]> }) {
  return (
    <dl className="kv">
      {rows.map(([label, value]) => (
        <div key={label}>
          <dt>{titleCase(label)}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export function DataTable<T>({
  rows,
  columns,
  onRowClick,
  selectedId,
  rowKey,
  empty,
}: {
  rows: T[]
  columns: Array<{ header: string; cell: (row: T) => ReactNode; width?: string; align?: 'left' | 'right' }>
  onRowClick?: (row: T) => void
  selectedId?: string
  rowKey: (row: T) => string
  empty?: ReactNode
}) {
  if (!rows.length) return <>{empty ?? <Empty title="Nothing to show yet" />}</>
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.header} style={{ width: column.width, textAlign: column.align ?? 'left' }}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const id = rowKey(row)
            return (
              <tr
                key={id}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={`${onRowClick ? 'clickable' : ''} ${selectedId === id ? 'selected' : ''}`}
              >
                {columns.map((column) => (
                  <td key={column.header} style={{ textAlign: column.align ?? 'left' }}>
                    {column.cell(row)}
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function ProgressBar({ percent, label }: { percent: number; label?: string }) {
  return (
    <div className="progress" role="progressbar" aria-valuenow={Math.round(percent)} aria-valuemin={0} aria-valuemax={100}>
      <div className="progress-fill" style={{ width: `${Math.max(2, Math.min(100, percent))}%` }} />
      {label ? <span className="progress-label">{label}</span> : null}
    </div>
  )
}
