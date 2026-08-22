import type { ParentChildConfig } from '../../hooks'
import type { PreProcessingRule } from '@/models/datasets'
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ChunkingMode } from '@/models/datasets'
import { ParentChildOptions } from '../parent-child-options'

vi.mock('@/app/components/datasets/settings/summary-index-setting', () => ({
  default: ({ onSummaryIndexSettingChange }: { onSummaryIndexSettingChange?: (val: Record<string, unknown>) => void }) => (
    <div data-testid="summary-index-setting">
      <button data-testid="summary-toggle" onClick={() => onSummaryIndexSettingChange?.({ enable: true })}>Toggle</button>
    </div>
  ),
}))

vi.mock('@/config', () => ({
  IS_CE_EDITION: true,
}))

const ns = 'datasetCreation'

const createRules = (): PreProcessingRule[] => [
  { id: 'remove_extra_spaces', enabled: true },
  { id: 'remove_urls_emails', enabled: false },
]

const createParentChildConfig = (overrides?: Partial<ParentChildConfig>): ParentChildConfig => ({
  chunkForContext: 'paragraph',
  parent: { delimiter: '\\n\\n', maxLength: 2000, chunkStrategy: 'recursive', strategyProvider: 'custom', strategyParams: {} },
  child: { delimiter: '\\n', maxLength: 500, chunkStrategy: 'recursive', strategyProvider: 'custom', strategyParams: {} },
  ...overrides,
})

const defaultProps = {
  parentChildConfig: createParentChildConfig(),
  rules: createRules(),
  currentDocForm: ChunkingMode.parentChild,
  isActive: true,
  isInUpload: false,
  isNotUploadInEmptyDataset: false,
  onDocFormChange: vi.fn(),
  onChunkForContextChange: vi.fn(),
  onParentDelimiterChange: vi.fn(),
  onParentMaxLengthChange: vi.fn(),
  onChildDelimiterChange: vi.fn(),
  onChildMaxLengthChange: vi.fn(),
  onParentStrategyProviderChange: vi.fn(),
  onChildStrategyProviderChange: vi.fn(),
  onParentStrategyChange: vi.fn(),
  onChildStrategyChange: vi.fn(),
  onParentStrategyParamChange: vi.fn(),
  onChildStrategyParamChange: vi.fn(),
  onRuleToggle: vi.fn(),
  onPreview: vi.fn(),
  onReset: vi.fn(),
}

describe('ParentChildOptions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('Rendering', () => {
    it('should render parent-child title', () => {
      render(<ParentChildOptions {...defaultProps} />)
      expect(screen.getByText(`${ns}.stepTwo.parentChild`)).toBeInTheDocument()
    })

    it('should render parent chunk context section when active', () => {
      render(<ParentChildOptions {...defaultProps} />)
      expect(screen.getByText(`${ns}.stepTwo.parentChunkForContext`)).toBeInTheDocument()
      expect(screen.getByText(`${ns}.stepTwo.paragraph`)).toBeInTheDocument()
      expect(screen.getByText(`${ns}.stepTwo.fullDoc`)).toBeInTheDocument()
    })

    it('should render child chunk retrieval section when active', () => {
      render(<ParentChildOptions {...defaultProps} />)
      expect(screen.getByText(`${ns}.stepTwo.childChunkForRetrieval`)).toBeInTheDocument()
    })

    it('should render rules section when active', () => {
      render(<ParentChildOptions {...defaultProps} />)
      expect(screen.getByText(`${ns}.stepTwo.removeExtraSpaces`)).toBeInTheDocument()
      expect(screen.getByText(`${ns}.stepTwo.removeUrlEmails`)).toBeInTheDocument()
    })

    it('should render preview and reset buttons when active', () => {
      render(<ParentChildOptions {...defaultProps} />)
      expect(screen.getByText(`${ns}.stepTwo.previewChunk`)).toBeInTheDocument()
      expect(screen.getByText(`${ns}.stepTwo.reset`)).toBeInTheDocument()
    })

    it('should not render body when not active', () => {
      render(<ParentChildOptions {...defaultProps} isActive={false} />)
      expect(screen.queryByText(`${ns}.stepTwo.parentChunkForContext`)).not.toBeInTheDocument()
    })
  })

  describe('User Interactions', () => {
    it('should call onPreview when preview button clicked', () => {
      const onPreview = vi.fn()
      render(<ParentChildOptions {...defaultProps} onPreview={onPreview} />)
      fireEvent.click(screen.getByText(`${ns}.stepTwo.previewChunk`))
      expect(onPreview).toHaveBeenCalledOnce()
    })

    it('should call onReset when reset button clicked', () => {
      const onReset = vi.fn()
      render(<ParentChildOptions {...defaultProps} onReset={onReset} />)
      fireEvent.click(screen.getByText(`${ns}.stepTwo.reset`))
      expect(onReset).toHaveBeenCalledOnce()
    })

    it('should call onRuleToggle when rule clicked', () => {
      const onRuleToggle = vi.fn()
      render(<ParentChildOptions {...defaultProps} onRuleToggle={onRuleToggle} />)
      fireEvent.click(screen.getByText(`${ns}.stepTwo.removeUrlEmails`))
      expect(onRuleToggle).toHaveBeenCalledWith('remove_urls_emails')
    })

    it('should call onDocFormChange with parentChild when card switched', () => {
      const onDocFormChange = vi.fn()
      render(<ParentChildOptions {...defaultProps} isActive={false} onDocFormChange={onDocFormChange} />)
      const titleEl = screen.getByText(`${ns}.stepTwo.parentChild`)
      fireEvent.click(titleEl.closest('[class*="rounded-xl"]')!)
      expect(onDocFormChange).toHaveBeenCalledWith(ChunkingMode.parentChild)
    })

    it('should call onChunkForContextChange when full-doc chosen', () => {
      const onChunkForContextChange = vi.fn()
      render(<ParentChildOptions {...defaultProps} onChunkForContextChange={onChunkForContextChange} />)
      fireEvent.click(screen.getByText(`${ns}.stepTwo.fullDoc`))
      expect(onChunkForContextChange).toHaveBeenCalledWith('full-doc')
    })

    it('should call onChunkForContextChange when paragraph chosen', () => {
      const onChunkForContextChange = vi.fn()
      const config = createParentChildConfig({ chunkForContext: 'full-doc' })
      render(<ParentChildOptions {...defaultProps} parentChildConfig={config} onChunkForContextChange={onChunkForContextChange} />)
      fireEvent.click(screen.getByText(`${ns}.stepTwo.paragraph`))
      expect(onChunkForContextChange).toHaveBeenCalledWith('paragraph')
    })
  })

  describe('Per-level chunk strategy', () => {
    it('should render an independent strategy selector for both parent and child', () => {
      render(<ParentChildOptions {...defaultProps} />)
      // Provider label + strategy label appear once per level (parent's
      // paragraph section is expanded by default, plus the child section).
      expect(screen.getAllByText(`${ns}.stepTwo.chunkStrategy`)).toHaveLength(2)
      expect(screen.getAllByText(`${ns}.stepTwo.chunkStrategyProvider`)).toHaveLength(2)
    })

    it('should let the child level select "semantic" independently of the parent', () => {
      const onChildStrategyChange = vi.fn()
      const onParentStrategyChange = vi.fn()
      render(<ParentChildOptions {...defaultProps} onChildStrategyChange={onChildStrategyChange} onParentStrategyChange={onParentStrategyChange} />)

      const strategySelects = screen.getAllByRole('combobox').filter(
        el => el.tagName === 'SELECT' && el.querySelector('option[value="semantic"]'),
      )
      // Parent's paragraph section renders first, child's second.
      expect(strategySelects).toHaveLength(2)
      const childSelect = strategySelects[1]

      fireEvent.change(childSelect, { target: { value: 'semantic' } })

      expect(onChildStrategyChange).toHaveBeenCalledWith('semantic')
      expect(onParentStrategyChange).not.toHaveBeenCalled()
    })

    it('should hide the delimiter/max-length inputs for a boundary strategy like "semantic"', () => {
      // Parent stays on the default `recursive` (character category, still
      // shows "characters"); only the child switches to `semantic`, so the
      // unit label should appear once (parent) instead of twice (parent+child).
      const config = createParentChildConfig({
        child: { delimiter: '\\n', maxLength: 500, chunkStrategy: 'semantic', strategyProvider: 'custom', strategyParams: {} },
      })
      render(<ParentChildOptions {...defaultProps} parentChildConfig={config} />)
      expect(screen.getAllByText(`${ns}.stepTwo.characters`)).toHaveLength(1)
    })
  })

  describe('Summary Index Setting', () => {
    it('should render SummaryIndexSetting when showSummaryIndexSetting is true', () => {
      render(<ParentChildOptions {...defaultProps} showSummaryIndexSetting />)
      expect(screen.getByTestId('summary-index-setting')).toBeInTheDocument()
    })

    it('should not render SummaryIndexSetting when showSummaryIndexSetting is false', () => {
      render(<ParentChildOptions {...defaultProps} showSummaryIndexSetting={false} />)
      expect(screen.queryByTestId('summary-index-setting')).not.toBeInTheDocument()
    })
  })
})
