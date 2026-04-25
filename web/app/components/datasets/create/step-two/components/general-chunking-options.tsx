'use client'

import type { FC } from 'react'
import type { ChunkStrategy, PreProcessingRule, StrategyProvider, SummaryIndexSetting as SummaryIndexSettingType } from '@/models/datasets'
import { useTranslation } from 'react-i18next'
import Button from '@/app/components/base/button'
import Checkbox from '@/app/components/base/checkbox'
import Divider from '@/app/components/base/divider'
import SegmentedControl from '@/app/components/base/segmented-control'
import Tooltip from '@/app/components/base/tooltip'
import SummaryIndexSetting from '@/app/components/datasets/settings/summary-index-setting'
import { IS_CE_EDITION } from '@/config'
import {
  ChunkingMode,
  CUSTOM_STRATEGIES,
  getStrategyCategory,
  LANGCHAIN_STRATEGIES,
  LLAMAINDEX_STRATEGIES,
  STRATEGY_PARAM_SCHEMAS,
} from '@/models/datasets'
import SettingCog from '../../assets/setting-gear-mod.svg'
import s from '../index.module.css'
import LanguageSelect from '../language-select'
import { DelimiterInput, MaxLengthInput, OverlapInput } from './inputs'
import { OptionCard } from './option-card'

type TextLabelProps = {
  children: React.ReactNode
}

const TextLabel: FC<TextLabelProps> = ({ children }) => {
  return <label className="text-text-secondary system-sm-semibold">{children}</label>
}

type GeneralChunkingOptionsProps = {
  // State
  segmentIdentifier: string
  maxChunkLength: number
  overlap: number
  chunkStrategy: ChunkStrategy
  strategyProvider: StrategyProvider
  strategyParams: Record<string, unknown>
  rules: PreProcessingRule[]
  currentDocForm: ChunkingMode
  docLanguage: string
  // Flags
  isActive: boolean
  isInUpload: boolean
  isNotUploadInEmptyDataset: boolean
  hasCurrentDatasetDocForm: boolean
  // Actions
  onSegmentIdentifierChange: (value: string) => void
  onMaxChunkLengthChange: (value: number) => void
  onOverlapChange: (value: number) => void
  onChunkStrategyChange: (value: ChunkStrategy) => void
  onStrategyProviderChange: (value: StrategyProvider) => void
  onStrategyParamChange: (key: string, value: unknown) => void
  onRuleToggle: (id: string) => void
  onDocFormChange: (form: ChunkingMode) => void
  onDocLanguageChange: (lang: string) => void
  onPreview: () => void
  onReset: () => void
  // Locale
  locale: string
  showSummaryIndexSetting?: boolean
  summaryIndexSetting?: SummaryIndexSettingType
  onSummaryIndexSettingChange?: (payload: SummaryIndexSettingType) => void
}

const CUSTOM_STRATEGY_LABEL_KEYS: Record<string, string> = {
  recursive: 'stepTwo.chunkStrategyRecursive',
  paragraph: 'stepTwo.chunkStrategyParagraph',
  sentence: 'stepTwo.chunkStrategySentence',
  markdown: 'stepTwo.chunkStrategyMarkdown',
  html: 'stepTwo.chunkStrategyHtml',
  json: 'stepTwo.chunkStrategyJson',
  code: 'stepTwo.chunkStrategyCode',
  latex: 'stepTwo.chunkStrategyLatex',
  token: 'stepTwo.chunkStrategyToken',
  semantic: 'stepTwo.chunkStrategySemantic',
}

const LLAMAINDEX_STRATEGY_LABEL_KEYS: Record<string, string> = {
  li_sentence: 'stepTwo.chunkStrategyLiSentence',
  li_token: 'stepTwo.chunkStrategyLiToken',
  li_fixed: 'stepTwo.chunkStrategyLiFixed',
  li_semantic: 'stepTwo.chunkStrategyLiSemantic',
  li_markdown: 'stepTwo.chunkStrategyLiMarkdown',
  li_html: 'stepTwo.chunkStrategyLiHtml',
  li_code: 'stepTwo.chunkStrategyLiCode',
}

const LANGCHAIN_STRATEGY_LABEL_KEYS: Record<string, string> = {
  lc_recursive: 'stepTwo.chunkStrategyLcRecursive',
  lc_character: 'stepTwo.chunkStrategyLcCharacter',
  lc_token: 'stepTwo.chunkStrategyLcToken',
  lc_markdown_header: 'stepTwo.chunkStrategyLcMarkdownHeader',
  lc_html_header: 'stepTwo.chunkStrategyLcHtmlHeader',
  lc_semantic: 'stepTwo.chunkStrategyLcSemantic',
  lc_python: 'stepTwo.chunkStrategyLcPython',
}

function getStrategyLabelKey(strategy: ChunkStrategy): string {
  return (
    CUSTOM_STRATEGY_LABEL_KEYS[strategy]
    ?? LLAMAINDEX_STRATEGY_LABEL_KEYS[strategy]
    ?? LANGCHAIN_STRATEGY_LABEL_KEYS[strategy]
    ?? strategy
  )
}

type FrameworkParamInputProps = {
  paramKey: string
  type: 'int' | 'float' | 'string' | 'select'
  labelKey: string
  value: unknown
  options?: string[]
  min?: number
  max?: number
  onChange: (key: string, value: unknown) => void
}

const FrameworkParamInput: FC<FrameworkParamInputProps> = ({
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

export const GeneralChunkingOptions: FC<GeneralChunkingOptionsProps> = ({
  segmentIdentifier,
  maxChunkLength,
  overlap,
  chunkStrategy,
  strategyProvider,
  strategyParams,
  rules,
  currentDocForm,
  docLanguage,
  isActive,
  isInUpload,
  isNotUploadInEmptyDataset,
  hasCurrentDatasetDocForm,
  onSegmentIdentifierChange,
  onMaxChunkLengthChange,
  onOverlapChange,
  onChunkStrategyChange,
  onStrategyProviderChange,
  onStrategyParamChange,
  onRuleToggle,
  onDocFormChange,
  onDocLanguageChange,
  onPreview,
  onReset,
  locale,
  showSummaryIndexSetting,
  summaryIndexSetting,
  onSummaryIndexSettingChange,
}) => {
  const { t } = useTranslation()

  const getRuleName = (key: string): string => {
    const ruleNameMap: Record<string, string> = {
      remove_extra_spaces: t('stepTwo.removeExtraSpaces', { ns: 'datasetCreation' }),
      remove_urls_emails: t('stepTwo.removeUrlEmails', { ns: 'datasetCreation' }),
      remove_stopwords: t('stepTwo.removeStopwords', { ns: 'datasetCreation' }),
    }
    return ruleNameMap[key] ?? key
  }

  const strategyCategory = getStrategyCategory(chunkStrategy)
  const isFramework = strategyCategory === 'framework'

  const activeStrategies
    = strategyProvider === 'llamaindex'
      ? LLAMAINDEX_STRATEGIES
      : strategyProvider === 'langchain'
        ? LANGCHAIN_STRATEGIES
        : CUSTOM_STRATEGIES

  const providerOptions = [
    { value: 'custom' as StrategyProvider, text: t('stepTwo.chunkStrategyProviderCustom', { ns: 'datasetCreation' }) },
    { value: 'llamaindex' as StrategyProvider, text: t('stepTwo.chunkStrategyProviderLlamaIndex', { ns: 'datasetCreation' }) },
    { value: 'langchain' as StrategyProvider, text: t('stepTwo.chunkStrategyProviderLangChain', { ns: 'datasetCreation' }) },
  ]

  const frameworkSchema = STRATEGY_PARAM_SCHEMAS[chunkStrategy]

  return (
    <OptionCard
      className="mb-2 bg-background-section"
      title={t('stepTwo.general', { ns: 'datasetCreation' })}
      icon={<img width={20} height={20} src={SettingCog.src} alt={t('stepTwo.general', { ns: 'datasetCreation' })} />}
      activeHeaderClassName="bg-dataset-option-card-blue-gradient"
      description={t('stepTwo.generalTip', { ns: 'datasetCreation' })}
      isActive={isActive}
      onSwitched={() => onDocFormChange(ChunkingMode.text)}
      actions={(
        <>
          <Button variant="secondary-accent" onClick={onPreview}>
            <span className="i-ri-search-eye-line mr-0.5 h-4 w-4" />
            {t('stepTwo.previewChunk', { ns: 'datasetCreation' })}
          </Button>
          <Button variant="ghost" onClick={onReset}>
            {t('stepTwo.reset', { ns: 'datasetCreation' })}
          </Button>
        </>
      )}
      noHighlight={isInUpload && isNotUploadInEmptyDataset}
    >
      <div className="flex flex-col gap-y-4">
        {/* Provider selector */}
        <div className="flex flex-col gap-2">
          <TextLabel>{t('stepTwo.chunkStrategyProvider', { ns: 'datasetCreation' })}</TextLabel>
          <SegmentedControl
            options={providerOptions}
            value={strategyProvider}
            onChange={value => onStrategyProviderChange(value as StrategyProvider)}
          />
        </div>

        {/* Strategy selector */}
        <div className="flex flex-col gap-1">
          <TextLabel>{t('stepTwo.chunkStrategy', { ns: 'datasetCreation' })}</TextLabel>
          <select
            className="h-9 rounded-lg border border-components-panel-border bg-components-panel-bg px-3 text-sm text-text-primary"
            value={chunkStrategy}
            onChange={e => onChunkStrategyChange(e.target.value as ChunkStrategy)}
          >
            {activeStrategies.map(strategy => (
              <option key={strategy} value={strategy}>
                {t(getStrategyLabelKey(strategy), { ns: 'datasetCreation', defaultValue: getStrategyLabelKey(strategy) })}
              </option>
            ))}
          </select>
        </div>

        {/* Framework strategy params */}
        {isFramework && frameworkSchema && frameworkSchema.params.length > 0 && (
          <div className="flex flex-wrap gap-3">
            {frameworkSchema.params.map(param => (
              <FrameworkParamInput
                key={param.key}
                paramKey={param.key}
                type={param.type}
                labelKey={param.labelKey}
                value={strategyParams[param.key] ?? param.defaultValue}
                options={param.options}
                min={param.min}
                max={param.max}
                onChange={onStrategyParamChange}
              />
            ))}
          </div>
        )}

        {/* Framework no-params notice */}
        {isFramework && frameworkSchema && frameworkSchema.params.length === 0 && (
          <p className="text-text-secondary system-xs-regular">
            {t('stepTwo.frameworkNoParams', { ns: 'datasetCreation' })}
          </p>
        )}

        {/* Size inputs — only for non-framework, non-boundary custom strategies */}
        {!isFramework && strategyCategory !== 'boundary' && (
          <div className="flex gap-3">
            {strategyCategory === 'character' && (
              <DelimiterInput
                value={segmentIdentifier}
                onChange={e => onSegmentIdentifierChange(e.target.value)}
              />
            )}
            <MaxLengthInput
              unit={strategyCategory === 'token'
                ? t('stepTwo.tokens', { ns: 'datasetCreation' })
                : t('stepTwo.characters', { ns: 'datasetCreation' })}
              value={maxChunkLength}
              onChange={onMaxChunkLengthChange}
            />
            <OverlapInput
              unit={strategyCategory === 'token'
                ? t('stepTwo.tokens', { ns: 'datasetCreation' })
                : t('stepTwo.characters', { ns: 'datasetCreation' })}
              value={overlap}
              min={1}
              onChange={onOverlapChange}
            />
          </div>
        )}

        {/* Pre-processing rules */}
        <div className="flex w-full flex-col">
          <div className="flex items-center gap-x-2">
            <div className="inline-flex shrink-0">
              <TextLabel>{t('stepTwo.rules', { ns: 'datasetCreation' })}</TextLabel>
            </div>
            <Divider className="grow" bgStyle="gradient" />
          </div>
          <div className="mt-1">
            {rules.map(rule => (
              <div
                key={rule.id}
                className={s.ruleItem}
                onClick={() => onRuleToggle(rule.id)}
              >
                <Checkbox checked={rule.enabled} />
                <label className="ml-2 cursor-pointer text-text-secondary system-sm-regular">
                  {getRuleName(rule.id)}
                </label>
              </div>
            ))}
            {
              showSummaryIndexSetting && IS_CE_EDITION && (
                <div className="mt-3">
                  <SummaryIndexSetting
                    entry="create-document"
                    summaryIndexSetting={summaryIndexSetting}
                    onSummaryIndexSettingChange={onSummaryIndexSettingChange}
                  />
                </div>
              )
            }
            {IS_CE_EDITION && (
              <>
                <Divider type="horizontal" className="my-4 bg-divider-subtle" />
                <div className="flex items-center py-0.5">
                  <div
                    className="flex items-center"
                    onClick={() => {
                      if (hasCurrentDatasetDocForm)
                        return
                      if (currentDocForm === ChunkingMode.qa)
                        onDocFormChange(ChunkingMode.text)
                      else
                        onDocFormChange(ChunkingMode.qa)
                    }}
                  >
                    <Checkbox
                      checked={currentDocForm === ChunkingMode.qa}
                      disabled={hasCurrentDatasetDocForm}
                    />
                    <label className="ml-2 cursor-pointer text-text-secondary system-sm-regular">
                      {t('stepTwo.useQALanguage', { ns: 'datasetCreation' })}
                    </label>
                  </div>
                  <LanguageSelect
                    currentLanguage={docLanguage || locale}
                    onSelect={onDocLanguageChange}
                    disabled={currentDocForm !== ChunkingMode.qa}
                  />
                  <Tooltip popupContent={t('stepTwo.QATip', { ns: 'datasetCreation' })} />
                </div>
                {currentDocForm === ChunkingMode.qa && (
                  <div
                    style={{
                      background: 'linear-gradient(92deg, rgba(247, 144, 9, 0.1) 0%, rgba(255, 255, 255, 0.00) 100%)',
                    }}
                    className="mt-2 flex h-10 items-center gap-2 rounded-xl border border-components-panel-border px-3 text-xs shadow-xs backdrop-blur-[5px]"
                  >
                    <span className="i-ri-alert-fill size-4 text-text-warning-secondary" />
                    <span className="text-text-primary system-xs-medium">
                      {t('stepTwo.QATip', { ns: 'datasetCreation' })}
                    </span>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </OptionCard>
  )
}
