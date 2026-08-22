import type { ChunkStrategy, ParentMode, PreProcessingRule, ProcessRule, Rules, StrategyProvider, SummaryIndexSetting as SummaryIndexSettingType } from '@/models/datasets'
import { useCallback, useRef, useState } from 'react'
import { env } from '@/env'
import { ChunkingMode, DEFAULT_CUSTOM_STRATEGY, DEFAULT_LANGCHAIN_STRATEGY, DEFAULT_LLAMAINDEX_STRATEGY, getStrategyCategory, getStrategyProvider, ProcessMode, STRATEGY_PARAM_SCHEMAS } from '@/models/datasets'
import escape from './escape'
import unescape from './unescape'

// Constants
export const DEFAULT_SEGMENT_IDENTIFIER = '\\n\\n'
export const DEFAULT_MAXIMUM_CHUNK_LENGTH = 1024
export const DEFAULT_OVERLAP = 50
export const DEFAULT_CHUNK_STRATEGY: ChunkStrategy = DEFAULT_CUSTOM_STRATEGY
export const MAXIMUM_CHUNK_TOKEN_LENGTH = env.NEXT_PUBLIC_INDEXING_MAX_SEGMENTATION_TOKENS_LENGTH

export type ParentChildConfig = {
  chunkForContext: ParentMode
  parent: {
    delimiter: string
    maxLength: number
    chunkStrategy: ChunkStrategy
    strategyProvider: StrategyProvider
    strategyParams: Record<string, unknown>
  }
  child: {
    delimiter: string
    maxLength: number
    chunkStrategy: ChunkStrategy
    strategyProvider: StrategyProvider
    strategyParams: Record<string, unknown>
  }
}

export const defaultParentChildConfig: ParentChildConfig = {
  chunkForContext: 'paragraph',
  parent: {
    delimiter: '\\n\\n',
    maxLength: 1024,
    chunkStrategy: DEFAULT_CUSTOM_STRATEGY,
    strategyProvider: 'custom',
    strategyParams: {},
  },
  child: {
    delimiter: '\\n',
    maxLength: 512,
    chunkStrategy: DEFAULT_CUSTOM_STRATEGY,
    strategyProvider: 'custom',
    strategyParams: {},
  },
}

export type UseSegmentationStateOptions = {
  initialSegmentationType?: ProcessMode
  initialSummaryIndexSetting?: SummaryIndexSettingType
}

function getDefaultStrategyForProvider(provider: StrategyProvider): ChunkStrategy {
  if (provider === 'llamaindex')
    return DEFAULT_LLAMAINDEX_STRATEGY
  if (provider === 'langchain')
    return DEFAULT_LANGCHAIN_STRATEGY
  return DEFAULT_CUSTOM_STRATEGY
}

function buildDefaultStrategyParams(strategy: ChunkStrategy): Record<string, unknown> {
  const schema = STRATEGY_PARAM_SCHEMAS[strategy]
  if (!schema)
    return {}
  return Object.fromEntries(schema.params.map(p => [p.key, p.defaultValue]))
}

export const useSegmentationState = (options: UseSegmentationStateOptions = {}) => {
  const { initialSegmentationType, initialSummaryIndexSetting } = options

  // Segmentation type (general or parent-child)
  const [segmentationType, setSegmentationType] = useState<ProcessMode>(
    initialSegmentationType ?? ProcessMode.general,
  )

  // General chunking settings
  const [segmentIdentifier, setSegmentIdentifier] = useState(DEFAULT_SEGMENT_IDENTIFIER)
  const [maxChunkLength, setMaxChunkLength] = useState(DEFAULT_MAXIMUM_CHUNK_LENGTH)
  const [limitMaxChunkLength, setLimitMaxChunkLength] = useState(MAXIMUM_CHUNK_TOKEN_LENGTH)
  const [overlap, setOverlap] = useState(DEFAULT_OVERLAP)
  const [chunkStrategy, setChunkStrategy] = useState<ChunkStrategy>(DEFAULT_CHUNK_STRATEGY)
  const [strategyProviderState, setStrategyProviderState] = useState<StrategyProvider>('custom')
  const [strategyParams, setStrategyParams] = useState<Record<string, unknown>>(
    () => buildDefaultStrategyParams(DEFAULT_CHUNK_STRATEGY),
  )

  const setStrategyProvider = useCallback((provider: StrategyProvider) => {
    setStrategyProviderState(provider)
    const newStrategy = getDefaultStrategyForProvider(provider)
    setChunkStrategy(newStrategy)
    setStrategyParams(buildDefaultStrategyParams(newStrategy))
  }, [])

  const handleStrategyChange = useCallback((strategy: ChunkStrategy) => {
    setChunkStrategy(strategy)
    setStrategyParams(buildDefaultStrategyParams(strategy))
  }, [])

  const handleStrategyParamChange = useCallback((key: string, value: unknown) => {
    setStrategyParams(prev => ({ ...prev, [key]: value }))
  }, [])

  // Pre-processing rules
  const [rules, setRules] = useState<PreProcessingRule[]>([])
  const [defaultConfig, setDefaultConfig] = useState<Rules>()
  const [summaryIndexSetting, setSummaryIndexSetting] = useState<SummaryIndexSettingType | undefined>(initialSummaryIndexSetting)
  const summaryIndexSettingRef = useRef<SummaryIndexSettingType | undefined>(initialSummaryIndexSetting)
  const handleSummaryIndexSettingChange = useCallback((payload: SummaryIndexSettingType) => {
    setSummaryIndexSetting((prev) => {
      const newSetting = { ...prev, ...payload }
      summaryIndexSettingRef.current = newSetting
      return newSetting
    })
  }, [])

  // Parent-child config
  const [parentChildConfig, setParentChildConfig] = useState<ParentChildConfig>(defaultParentChildConfig)

  // Escaped segment identifier setter
  const updateSegmentIdentifier = useCallback((value: string, canEmpty?: boolean) => {
    if (value) {
      setSegmentIdentifier(escape(value))
    }
    else {
      setSegmentIdentifier(canEmpty ? '' : DEFAULT_SEGMENT_IDENTIFIER)
    }
  }, [])

  // Rule toggle handler
  const toggleRule = useCallback((id: string) => {
    setRules(prev => prev.map(rule =>
      rule.id === id ? { ...rule, enabled: !rule.enabled } : rule,
    ))
  }, [])

  // Reset to defaults
  const resetToDefaults = useCallback(() => {
    if (defaultConfig) {
      updateSegmentIdentifier(defaultConfig.segmentation.separator ?? '')
      setMaxChunkLength(defaultConfig.segmentation.max_tokens ?? DEFAULT_MAXIMUM_CHUNK_LENGTH)
      setOverlap(defaultConfig.segmentation.chunk_overlap ?? DEFAULT_OVERLAP)
      const strategy = defaultConfig.segmentation.chunk_strategy || DEFAULT_CHUNK_STRATEGY
      setChunkStrategy(strategy)
      setStrategyParams(
        defaultConfig.segmentation.strategy_params as Record<string, unknown>
        ?? buildDefaultStrategyParams(strategy),
      )
      setStrategyProviderState(getStrategyProvider(strategy))
      setRules(defaultConfig.pre_processing_rules)
    }
    setParentChildConfig(defaultParentChildConfig)
  }, [defaultConfig, updateSegmentIdentifier])

  // Apply config from document detail
  const applyConfigFromRules = useCallback((rulesConfig: Rules, isHierarchical: boolean) => {
    const separator = rulesConfig.segmentation.separator
    const max = rulesConfig.segmentation.max_tokens
    const chunkOverlap = rulesConfig.segmentation.chunk_overlap
    const strategy = rulesConfig.segmentation.chunk_strategy || DEFAULT_CHUNK_STRATEGY

    updateSegmentIdentifier(separator ?? '')
    setMaxChunkLength(max ?? DEFAULT_MAXIMUM_CHUNK_LENGTH)
    setOverlap(chunkOverlap ?? DEFAULT_OVERLAP)
    setChunkStrategy(strategy)
    setStrategyParams(
      rulesConfig.segmentation.strategy_params as Record<string, unknown>
      ?? buildDefaultStrategyParams(strategy),
    )
    setStrategyProviderState(getStrategyProvider(strategy))
    setRules(rulesConfig.pre_processing_rules)
    setDefaultConfig(rulesConfig)

    if (isHierarchical) {
      const parentStrategy = rulesConfig.segmentation.chunk_strategy || DEFAULT_CUSTOM_STRATEGY
      const childStrategy = rulesConfig.subchunk_segmentation?.chunk_strategy || DEFAULT_CUSTOM_STRATEGY
      setParentChildConfig({
        chunkForContext: rulesConfig.parent_mode || 'paragraph',
        parent: {
          delimiter: escape(rulesConfig.segmentation.separator ?? '\\n\\n'),
          maxLength: rulesConfig.segmentation.max_tokens ?? 1024,
          chunkStrategy: parentStrategy,
          strategyProvider: getStrategyProvider(parentStrategy),
          strategyParams: rulesConfig.segmentation.strategy_params as Record<string, unknown>
            ?? buildDefaultStrategyParams(parentStrategy),
        },
        child: {
          delimiter: escape(rulesConfig.subchunk_segmentation?.separator ?? '\\n'),
          maxLength: rulesConfig.subchunk_segmentation?.max_tokens ?? 512,
          chunkStrategy: childStrategy,
          strategyProvider: getStrategyProvider(childStrategy),
          strategyParams: rulesConfig.subchunk_segmentation?.strategy_params as Record<string, unknown>
            ?? buildDefaultStrategyParams(childStrategy),
        },
      })
    }
  }, [updateSegmentIdentifier])

  // Get process rule for API
  const getProcessRule = useCallback((docForm: ChunkingMode): ProcessRule => {
    const category = getStrategyCategory(chunkStrategy)
    const isBoundary = category === 'boundary'
    const isFramework = category === 'framework'

    if (docForm === ChunkingMode.parentChild) {
      const buildLevelSegmentation = (level: ParentChildConfig['parent'] | ParentChildConfig['child']): Segmentation => {
        const levelCategory = getStrategyCategory(level.chunkStrategy)
        if (levelCategory === 'framework') {
          return {
            chunk_strategy: level.chunkStrategy,
            strategy_params: level.strategyParams,
          }
        }
        if (levelCategory === 'boundary')
          return { chunk_strategy: level.chunkStrategy }

        return {
          separator: unescape(level.delimiter),
          max_tokens: level.maxLength,
          chunk_strategy: level.chunkStrategy,
        }
      }

      return {
        rules: {
          pre_processing_rules: rules,
          segmentation: buildLevelSegmentation(parentChildConfig.parent),
          parent_mode: parentChildConfig.chunkForContext,
          subchunk_segmentation: buildLevelSegmentation(parentChildConfig.child),
        },
        mode: 'hierarchical',
        summary_index_setting: summaryIndexSettingRef.current,
      } as ProcessRule
    }

    let segmentation: Segmentation
    if (isFramework) {
      segmentation = {
        chunk_strategy: chunkStrategy,
        strategy_params: strategyParams,
      }
    }
    else if (isBoundary) {
      segmentation = { chunk_strategy: chunkStrategy }
    }
    else {
      segmentation = {
        separator: unescape(segmentIdentifier),
        max_tokens: maxChunkLength,
        chunk_overlap: overlap,
        chunk_strategy: chunkStrategy,
      }
    }

    return {
      rules: {
        pre_processing_rules: rules,
        segmentation,
      },
      mode: segmentationType,
      summary_index_setting: summaryIndexSettingRef.current,
    } as ProcessRule
  }, [rules, parentChildConfig, segmentIdentifier, maxChunkLength, overlap, segmentationType, chunkStrategy, strategyParams])

  // Update parent config field
  const updateParentConfig = useCallback((field: 'delimiter' | 'maxLength', value: string | number) => {
    setParentChildConfig((prev) => {
      let newValue: string | number
      if (field === 'delimiter')
        newValue = value ? escape(value as string) : ''
      else
        newValue = value
      return {
        ...prev,
        parent: { ...prev.parent, [field]: newValue },
      }
    })
  }, [])

  // Update child config field
  const updateChildConfig = useCallback((field: 'delimiter' | 'maxLength', value: string | number) => {
    setParentChildConfig((prev) => {
      let newValue: string | number
      if (field === 'delimiter')
        newValue = value ? escape(value as string) : ''
      else
        newValue = value
      return {
        ...prev,
        child: { ...prev.child, [field]: newValue },
      }
    })
  }, [])

  // Set chunk for context mode
  const setChunkForContext = useCallback((mode: ParentMode) => {
    setParentChildConfig(prev => ({ ...prev, chunkForContext: mode }))
  }, [])

  // Per-level (parent/child) strategy handlers — each level's strategy is
  // independent, e.g. child can use `semantic` while parent stays `paragraph`.
  const updateLevelStrategyProvider = useCallback((level: 'parent' | 'child', provider: StrategyProvider) => {
    const newStrategy = getDefaultStrategyForProvider(provider)
    setParentChildConfig(prev => ({
      ...prev,
      [level]: {
        ...prev[level],
        strategyProvider: provider,
        chunkStrategy: newStrategy,
        strategyParams: buildDefaultStrategyParams(newStrategy),
      },
    }))
  }, [])

  const updateLevelStrategy = useCallback((level: 'parent' | 'child', strategy: ChunkStrategy) => {
    setParentChildConfig(prev => ({
      ...prev,
      [level]: {
        ...prev[level],
        chunkStrategy: strategy,
        strategyParams: buildDefaultStrategyParams(strategy),
      },
    }))
  }, [])

  const updateLevelStrategyParam = useCallback((level: 'parent' | 'child', key: string, value: unknown) => {
    setParentChildConfig(prev => ({
      ...prev,
      [level]: {
        ...prev[level],
        strategyParams: { ...prev[level].strategyParams, [key]: value },
      },
    }))
  }, [])

  const updateParentStrategyProvider = useCallback((provider: StrategyProvider) => updateLevelStrategyProvider('parent', provider), [updateLevelStrategyProvider])
  const updateChildStrategyProvider = useCallback((provider: StrategyProvider) => updateLevelStrategyProvider('child', provider), [updateLevelStrategyProvider])
  const updateParentStrategy = useCallback((strategy: ChunkStrategy) => updateLevelStrategy('parent', strategy), [updateLevelStrategy])
  const updateChildStrategy = useCallback((strategy: ChunkStrategy) => updateLevelStrategy('child', strategy), [updateLevelStrategy])
  const updateParentStrategyParam = useCallback((key: string, value: unknown) => updateLevelStrategyParam('parent', key, value), [updateLevelStrategyParam])
  const updateChildStrategyParam = useCallback((key: string, value: unknown) => updateLevelStrategyParam('child', key, value), [updateLevelStrategyParam])

  return {
    // General chunking state
    segmentationType,
    setSegmentationType,
    segmentIdentifier,
    setSegmentIdentifier: updateSegmentIdentifier,
    maxChunkLength,
    setMaxChunkLength,
    limitMaxChunkLength,
    setLimitMaxChunkLength,
    overlap,
    setOverlap,
    chunkStrategy,
    setChunkStrategy: handleStrategyChange,
    strategyProvider: strategyProviderState,
    setStrategyProvider,
    strategyParams,
    handleStrategyParamChange,

    // Rules
    rules,
    setRules,
    defaultConfig,
    setDefaultConfig,
    toggleRule,
    summaryIndexSetting,
    handleSummaryIndexSettingChange,

    // Parent-child config
    parentChildConfig,
    setParentChildConfig,
    updateParentConfig,
    updateChildConfig,
    setChunkForContext,
    updateParentStrategyProvider,
    updateChildStrategyProvider,
    updateParentStrategy,
    updateChildStrategy,
    updateParentStrategyParam,
    updateChildStrategyParam,

    // Actions
    resetToDefaults,
    applyConfigFromRules,
    getProcessRule,
  }
}

// Local alias to avoid import cycle — mirrors Segmentation from models/datasets
type Segmentation = {
  separator?: string
  max_tokens?: number
  chunk_overlap?: number
  chunk_strategy?: ChunkStrategy
  strategy_params?: Record<string, unknown>
}

export type SegmentationState = ReturnType<typeof useSegmentationState>
