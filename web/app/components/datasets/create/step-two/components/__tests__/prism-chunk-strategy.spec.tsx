// Prism: chunk-strategy controls added to the general and parent-child option cards.
import type { PreProcessingRule } from '@dify/contracts/api/console/datasets/types.gen'
import type { ParentChildConfig } from '../../hooks'
import { RadioGroup } from '@langgenius/dify-ui/radio-group'
import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { ChunkingMode } from '@/models/datasets'
import { renderWithConsoleQuery } from '@/test/console/query-data'
import { GeneralChunkingOptions } from '../general-chunking-options'
import { ParentChildOptions } from '../parent-child-options'

vi.mock('@/app/components/datasets/settings/summary-index-setting', () => ({
  default: () => <div data-testid="summary-index-setting" />,
}))

const ns = 'datasetCreation'
// The option cards are radio items of the step's chunking-mode group.
const render = (ui: React.ReactElement, docForm: ChunkingMode) =>
  renderWithConsoleQuery(
    <RadioGroup<ChunkingMode> aria-label="chunking mode" value={docForm} onValueChange={vi.fn()}>
      {ui}
    </RadioGroup>,
    { systemFeatures: { deployment_edition: 'COMMUNITY' } },
  )

const createRules = (): PreProcessingRule[] => [
  { id: 'remove_extra_spaces', enabled: true },
  { id: 'remove_urls_emails', enabled: false },
]

const generalProps = {
  segmentIdentifier: '\\n',
  maxChunkLength: 500,
  overlap: 50,
  chunkStrategy: 'recursive' as const,
  strategyProvider: 'custom' as const,
  strategyParams: {} as Record<string, unknown>,
  rules: createRules(),
  currentDocForm: ChunkingMode.text,
  docLanguage: 'English',
  isActive: true,
  isInUpload: false,
  isNotUploadInEmptyDataset: false,
  hasCurrentDatasetDocForm: false,
  onSegmentIdentifierChange: vi.fn(),
  onMaxChunkLengthChange: vi.fn(),
  onOverlapChange: vi.fn(),
  onChunkStrategyChange: vi.fn(),
  onStrategyProviderChange: vi.fn(),
  onStrategyParamChange: vi.fn(),
  onRuleToggle: vi.fn(),
  onDocFormChange: vi.fn(),
  onDocLanguageChange: vi.fn(),
  onPreview: vi.fn(),
  onReset: vi.fn(),
  locale: 'en',
}

const createParentChildConfig = (overrides?: Partial<ParentChildConfig>): ParentChildConfig => ({
  chunkForContext: 'paragraph',
  parent: {
    delimiter: '\\n\\n',
    maxLength: 2000,
    chunkStrategy: 'recursive',
    strategyProvider: 'custom',
    strategyParams: {},
  },
  child: {
    delimiter: '\\n',
    maxLength: 500,
    chunkStrategy: 'recursive',
    strategyProvider: 'custom',
    strategyParams: {},
  },
  ...overrides,
})

const parentChildProps = {
  parentChildConfig: createParentChildConfig(),
  rules: createRules(),
  currentDocForm: ChunkingMode.parentChild,
  isActive: true,
  isInUpload: false,
  isNotUploadInEmptyDataset: false,
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

beforeEach(() => {
  vi.clearAllMocks()
})

describe('GeneralChunkingOptions chunk strategy', () => {
  it('should switch the chunk strategy provider', () => {
    const onStrategyProviderChange = vi.fn()
    render(
      <GeneralChunkingOptions
        {...generalProps}
        onStrategyProviderChange={onStrategyProviderChange}
      />,
      ChunkingMode.text,
    )
    fireEvent.click(
      screen.getByRole('radio', { name: `${ns}.stepTwo.chunkStrategyProviderLlamaIndex` }),
    )
    expect(onStrategyProviderChange).toHaveBeenCalledWith('llamaindex')
  })

  it('should hide the size inputs for a boundary strategy', () => {
    render(
      <GeneralChunkingOptions {...generalProps} chunkStrategy="paragraph" />,
      ChunkingMode.text,
    )
    expect(screen.queryByText(`${ns}.stepTwo.separator`)).not.toBeInTheDocument()
    expect(screen.queryByText(`${ns}.stepTwo.characters`)).not.toBeInTheDocument()
  })
})

describe('ParentChildOptions per-level chunk strategy', () => {
  it('should render an independent strategy selector for both parent and child', () => {
    render(<ParentChildOptions {...parentChildProps} />, ChunkingMode.parentChild)
    // Provider label + strategy label appear once per level (parent's
    // paragraph section is expanded by default, plus the child section).
    expect(screen.getAllByText(`${ns}.stepTwo.chunkStrategy`)).toHaveLength(2)
    expect(screen.getAllByText(`${ns}.stepTwo.chunkStrategyProvider`)).toHaveLength(2)
  })

  it('should let the child level select "semantic" independently of the parent', () => {
    const onChildStrategyChange = vi.fn()
    const onParentStrategyChange = vi.fn()
    render(
      <ParentChildOptions
        {...parentChildProps}
        onChildStrategyChange={onChildStrategyChange}
        onParentStrategyChange={onParentStrategyChange}
      />,
      ChunkingMode.parentChild,
    )

    const strategySelects = screen
      .getAllByRole('combobox')
      .filter((el) => el.tagName === 'SELECT' && el.querySelector('option[value="semantic"]'))
    // Parent's paragraph section renders first, child's second.
    expect(strategySelects).toHaveLength(2)

    fireEvent.change(strategySelects[1]!, { target: { value: 'semantic' } })

    expect(onChildStrategyChange).toHaveBeenCalledWith('semantic')
    expect(onParentStrategyChange).not.toHaveBeenCalled()
  })

  it('should hide the delimiter/max-length inputs for a boundary strategy like "semantic"', () => {
    // Parent stays on `recursive` (character category, shows "characters");
    // only the child switches to `semantic`, so the unit appears once.
    const config = createParentChildConfig({
      child: {
        delimiter: '\\n',
        maxLength: 500,
        chunkStrategy: 'semantic',
        strategyProvider: 'custom',
        strategyParams: {},
      },
    })
    render(
      <ParentChildOptions {...parentChildProps} parentChildConfig={config} />,
      ChunkingMode.parentChild,
    )
    expect(screen.getAllByText(`${ns}.stepTwo.characters`)).toHaveLength(1)
  })
})
