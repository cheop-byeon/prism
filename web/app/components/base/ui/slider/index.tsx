'use client'

import * as React from 'react'
import { cn } from '@/utils/classnames'

type SliderOnValueChange = (
  value: number,
  event: React.SyntheticEvent<HTMLInputElement> | React.KeyboardEvent<HTMLInputElement>,
) => void

type SliderBaseProps = Pick<
  React.InputHTMLAttributes<HTMLInputElement>,
  'disabled' | 'name'
> & {
  'min'?: number
  'max'?: number
  'step'?: number
  'onValueChange'?: SliderOnValueChange
  'aria-label'?: string
  'aria-labelledby'?: string
  'className'?: string
}

type ControlledSliderProps = SliderBaseProps & {
  value: number
  defaultValue?: never
}

type UncontrolledSliderProps = SliderBaseProps & {
  value?: never
  defaultValue?: number
}

export type SliderProps = ControlledSliderProps | UncontrolledSliderProps

const sliderRootClassName = 'group/slider relative inline-flex w-full data-[disabled]:opacity-30'
const sliderInputClassName = cn(
  'h-5 w-full cursor-pointer appearance-none bg-transparent',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-components-slider-knob-border-hover focus-visible:ring-offset-0',
  'disabled:cursor-not-allowed',
  '[&::-webkit-slider-runnable-track]:h-1 [&::-webkit-slider-runnable-track]:rounded-full',
  '[&::-webkit-slider-runnable-track]:bg-[var(--slider-track,var(--color-components-slider-track))]',
  '[&::-webkit-slider-thumb]:-mt-2 [&::-webkit-slider-thumb]:h-5 [&::-webkit-slider-thumb]:w-2 [&::-webkit-slider-thumb]:appearance-none',
  '[&::-webkit-slider-thumb]:rounded-[3px] [&::-webkit-slider-thumb]:border-[0.5px]',
  '[&::-webkit-slider-thumb]:border-[var(--slider-knob-border,var(--color-components-slider-knob-border))]',
  '[&::-webkit-slider-thumb]:bg-[var(--slider-knob,var(--color-components-slider-knob))] [&::-webkit-slider-thumb]:shadow-sm',
  '[&::-moz-range-track]:h-1 [&::-moz-range-track]:rounded-full',
  '[&::-moz-range-track]:bg-[var(--slider-track,var(--color-components-slider-track))]',
  '[&::-moz-range-thumb]:h-5 [&::-moz-range-thumb]:w-2 [&::-moz-range-thumb]:rounded-[3px] [&::-moz-range-thumb]:border-[0.5px]',
  '[&::-moz-range-thumb]:border-[var(--slider-knob-border,var(--color-components-slider-knob-border))]',
  '[&::-moz-range-thumb]:bg-[var(--slider-knob,var(--color-components-slider-knob))] [&::-moz-range-thumb]:shadow-sm',
)

const getSafeValue = (value: number | undefined, min: number) => {
  if (value === undefined)
    return undefined

  return Number.isFinite(value) ? value : min
}

export function Slider({
  value,
  defaultValue,
  onValueChange,
  min = 0,
  max = 100,
  step = 1,
  disabled = false,
  name,
  className,
  'aria-label': ariaLabel,
  'aria-labelledby': ariaLabelledby,
}: SliderProps) {
  const safeValue = getSafeValue(value, min)
  const safeDefaultValue = getSafeValue(defaultValue, min)
  const [internalValue, setInternalValue] = React.useState<number>(safeDefaultValue ?? min)

  const currentValue = safeValue ?? internalValue

  const emitChange = (
    nextValue: number,
    event: React.SyntheticEvent<HTMLInputElement> | React.KeyboardEvent<HTMLInputElement>,
  ) => {
    if (safeValue === undefined)
      setInternalValue(nextValue)
    onValueChange?.(nextValue, event)
  }

  const handleChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    emitChange(event.currentTarget.valueAsNumber, event)
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (disabled)
      return

    const stepSize = step ?? 1
    let delta = 0

    if (event.key === 'ArrowRight' || event.key === 'ArrowUp')
      delta = stepSize
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowDown')
      delta = -stepSize
    else
      return

    event.preventDefault()
    const nextValue = Math.min(max, Math.max(min, currentValue + delta))
    emitChange(nextValue, event)
  }

  return (
    <div className={cn(sliderRootClassName, className)} data-disabled={disabled ? '' : undefined}>
      <input
        type="range"
        value={currentValue}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        name={name}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledby}
        aria-valuenow={currentValue}
        className={sliderInputClassName}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
      />
    </div>
  )
}
