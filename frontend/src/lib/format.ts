/**
 * Display helpers.
 *
 * Units are always shown explicitly and conversion is never implicit, which is
 * a product rule rather than a formatting preference (PRD 6.1).
 */

export const MM_PER_INCH = 25.4

export function mm(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value.toFixed(digits)} mm`
}

export function inches(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value / MM_PER_INCH).toFixed(digits)} in`
}

/** Both unit systems, side by side, so neither is ambiguous. */
export function dual(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${mm(value)} (${inches(value)})`
}

export function seconds(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  if (value < 60) return `${value.toFixed(1)} s`
  const minutes = Math.floor(value / 60)
  const rest = value - minutes * 60
  if (minutes < 60) return `${minutes} min ${rest.toFixed(0)} s`
  const hours = Math.floor(minutes / 60)
  return `${hours} h ${minutes - hours * 60} min`
}

export function money(value: number | null | undefined, currency = 'EUR'): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency, maximumFractionDigits: 2 }).format(value)
  } catch {
    return `${value.toFixed(2)} ${currency}`
  }
}

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(digits)} %`
}

export function when(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

export function shortHash(value: string | null | undefined, length = 12): string {
  if (!value) return '—'
  return value.length <= length ? value : `${value.slice(0, length)}…`
}

export function titleCase(value: string): string {
  return value.replace(/[_-]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/** The colour a verification status carries throughout the application. */
export const STATUS_COLOR: Record<string, string> = {
  Confirmed: '#3fb950',
  Measured: '#58a6ff',
  Inferred: '#d29922',
  'Verification Required': '#db6d28',
  Unknown: '#f85149',
}

export const SEVERITY_COLOR: Record<string, string> = {
  S1: '#f85149',
  S2: '#db6d28',
  S3: '#d29922',
  S4: '#8b949e',
}

export const SEVERITY_LABEL: Record<string, string> = {
  S1: 'S1 Stop',
  S2: 'S2 Engineer resolution',
  S3: 'S3 Warning',
  S4: 'S4 Advisory',
}
