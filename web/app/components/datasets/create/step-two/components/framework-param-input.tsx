'use client'

import type { FC } from 'react'
import { useTranslation } from 'react-i18next'

type TextLabelProps = {
  children: React.ReactNode
}

const TextLabel: FC<TextLabelProps> = ({ children }) => {
  return <label className="text-text-secondary system-sm-semibold">{children}</label>
}

export type FrameworkParamInputProps = {
  paramKey: string
  type: 'int' | 'float' | 'string' | 'select'
  labelKey: string
  value: unknown
  options?: string[]
  min?: number
  max?: number
  onChange: (key: string, value: unknown) => void
}

// Shared by GeneralChunkingOptions and ParentChildOptions (each level's
// framework-strategy parameter form). Kept in its own file for the same
// Fast Refresh reason as strategy-label-keys.ts.
export const FrameworkParamInput: FC<FrameworkParamInputProps> = ({
  paramKey,
  type,
  labelKey,
  value,
  options,
  min,
  max,
  onChange,
}) => {
  const { t } = useTranslation()
  const label = t(labelKey, { ns: 'datasetCreation', defaultValue: labelKey })

  if (type === 'select' && options) {
    return (
      <div className="flex flex-1 flex-col gap-1">
        <TextLabel>{label}</TextLabel>
        <select
          className="h-9 rounded-lg border border-components-panel-border bg-components-panel-bg px-3 text-sm text-text-primary"
          value={String(value ?? options[0])}
          onChange={e => onChange(paramKey, e.target.value)}
        >
          {options.map(opt => (
            <option key={opt} value={opt}>{opt}</option>
          ))}
        </select>
      </div>
    )
  }

  return (
    <div className="flex flex-1 flex-col gap-1">
      <TextLabel>{label}</TextLabel>
      <input
        type="number"
        className="h-9 rounded-lg border border-components-panel-border bg-components-panel-bg px-3 text-sm text-text-primary"
        value={String(value ?? '')}
        min={min}
        max={max}
        step={type === 'float' ? 0.01 : 1}
        onChange={e => onChange(paramKey, type === 'float' ? Number.parseFloat(e.target.value) : Number.parseInt(e.target.value, 10))}
      />
    </div>
  )
}
