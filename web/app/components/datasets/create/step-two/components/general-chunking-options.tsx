'use client'

import type { PreProcessingRule } from '@dify/contracts/api/console/datasets/types.gen'
import type { FC, MouseEventHandler } from 'react'
import type {
  ChunkStrategy,
  StrategyProvider,
  SummaryIndexSetting as SummaryIndexSettingType,
} from '@/models/datasets'
import { Alert, AlertTitle } from '@langgenius/dify-ui/alert'
import { Button } from '@langgenius/dify-ui/button'
import { Checkbox } from '@langgenius/dify-ui/checkbox'
import { Infotip, InfotipContent, InfotipTrigger } from '@langgenius/dify-ui/infotip'
import { SegmentedControl, SegmentedControlItem } from '@langgenius/dify-ui/segmented-control'
import { Separator } from '@langgenius/dify-ui/separator'
import { RiSearchEyeLine } from '@remixicon/react'
import { useSuspenseQuery } from '@tanstack/react-query'
import { useId } from 'react'
import { useTranslation } from 'react-i18next'
import SummaryIndexSetting from '@/app/components/datasets/settings/summary-index-setting'
import { systemFeaturesQueryOptions } from '@/features/system-features/client'
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
import { FrameworkParamInput } from './framework-param-input'
import { DelimiterInput, MaxLengthInput, OverlapInput } from './inputs'
import { OptionCard } from './option-card'
import { getStrategyLabelKey } from './strategy-label-keys'

type TextLabelProps = {
  children: React.ReactNode
}

const TextLabel: FC<TextLabelProps> = ({ children }) => {
  return <label className="system-sm-semibold text-text-secondary">{children}</label>
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
  onPreview: MouseEventHandler<HTMLButtonElement>
  onReset: () => void
  // Locale
  locale: string
  showSummaryIndexSetting?: boolean
  summaryIndexSetting?: SummaryIndexSettingType
  onSummaryIndexSettingChange?: (payload: SummaryIndexSettingType) => void
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
  const qaLabelId = useId()

  const { t } = useTranslation(['datasetCreation'])
  const strategyLabel = (strategy: ChunkStrategy) => {
    const labelKey = getStrategyLabelKey(strategy)
    return labelKey ? t(($) => $[labelKey], { ns: 'datasetCreation' }) : strategy
  }
  const { data: deploymentEdition } = useSuspenseQuery({
    ...systemFeaturesQueryOptions(),
    select: ({ deployment_edition }) => deployment_edition,
  })
  const isNonCloudEdition = deploymentEdition === 'COMMUNITY' || deploymentEdition === 'ENTERPRISE'

  const getRuleName = (key: string): string => {
    const ruleNameMap: Record<string, string> = {
      remove_extra_spaces: t(($) => $['stepTwo.removeExtraSpaces'], { ns: 'datasetCreation' }),
      remove_urls_emails: t(($) => $['stepTwo.removeUrlEmails'], { ns: 'datasetCreation' }),
      remove_stopwords: t(($) => $['stepTwo.removeStopwords'], { ns: 'datasetCreation' }),
    }
    return ruleNameMap[key] ?? key
  }

  const strategyCategory = getStrategyCategory(chunkStrategy)
  const isFramework = strategyCategory === 'framework'

  const activeStrategies =
    strategyProvider === 'llamaindex'
      ? LLAMAINDEX_STRATEGIES
      : strategyProvider === 'langchain'
        ? LANGCHAIN_STRATEGIES
        : CUSTOM_STRATEGIES

  const providerOptions = [
    {
      value: 'custom' as StrategyProvider,
      text: t(($) => $['stepTwo.chunkStrategyProviderCustom'], { ns: 'datasetCreation' }),
    },
    {
      value: 'llamaindex' as StrategyProvider,
      text: t(($) => $['stepTwo.chunkStrategyProviderLlamaIndex'], { ns: 'datasetCreation' }),
    },
    {
      value: 'langchain' as StrategyProvider,
      text: t(($) => $['stepTwo.chunkStrategyProviderLangChain'], { ns: 'datasetCreation' }),
    },
  ]

  const frameworkSchema = STRATEGY_PARAM_SCHEMAS[chunkStrategy]

  return (
    <OptionCard
      className="mb-2 bg-background-section"
      title={t(($) => $['stepTwo.general'], { ns: 'datasetCreation' })}
      icon={
        <img
          width={20}
          height={20}
          src={SettingCog.src}
          alt={t(($) => $['stepTwo.general'], { ns: 'datasetCreation' })}
        />
      }
      activeHeaderClassName="bg-dataset-option-card-blue-gradient"
      description={t(($) => $['stepTwo.generalTip'], { ns: 'datasetCreation' })}
      isActive={isActive}
      value={ChunkingMode.text}
      actions={
        <>
          <Button variant="secondary-accent" onClick={onPreview}>
            <RiSearchEyeLine className="size-4" />
            {t(($) => $['stepTwo.previewChunk'], { ns: 'datasetCreation' })}
          </Button>
          <Button variant="ghost" onClick={onReset}>
            {t(($) => $['stepTwo.reset'], { ns: 'datasetCreation' })}
          </Button>
        </>
      }
      noHighlight={isInUpload && isNotUploadInEmptyDataset}
    >
      <div className="@container/chunkfields flex flex-col gap-y-4">
        {/* Provider selector */}
        <div className="flex flex-col gap-2">
          <TextLabel>
            {t(($) => $['stepTwo.chunkStrategyProvider'], { ns: 'datasetCreation' })}
          </TextLabel>
          <SegmentedControl
            value={strategyProvider}
            onValueChange={(value) => onStrategyProviderChange(value as StrategyProvider)}
            aria-label={t(($) => $['stepTwo.chunkStrategyProvider'], { ns: 'datasetCreation' })}
          >
            {providerOptions.map((option) => (
              <SegmentedControlItem key={option.value} value={option.value}>
                {option.text}
              </SegmentedControlItem>
            ))}
          </SegmentedControl>
        </div>

        {/* Strategy selector */}
        <div className="flex flex-col gap-1">
          <TextLabel>{t(($) => $['stepTwo.chunkStrategy'], { ns: 'datasetCreation' })}</TextLabel>
          <select
            className="h-9 rounded-lg border border-components-panel-border bg-components-panel-bg px-3 text-sm text-text-primary"
            value={chunkStrategy}
            onChange={(e) => onChunkStrategyChange(e.target.value as ChunkStrategy)}
          >
            {activeStrategies.map((strategy) => (
              <option key={strategy} value={strategy}>
                {strategyLabel(strategy)}
              </option>
            ))}
          </select>
        </div>

        {/* Framework strategy params */}
        {isFramework && frameworkSchema && frameworkSchema.params.length > 0 && (
          <div className="flex flex-wrap gap-3">
            {frameworkSchema.params.map((param) => (
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
          <p className="system-xs-regular text-text-secondary">
            {t(($) => $['stepTwo.frameworkNoParams'], { ns: 'datasetCreation' })}
          </p>
        )}

        {/* Size inputs — only for non-framework, non-boundary custom strategies */}
        {/* Container query, not a viewport breakpoint: three across at/above a
            552px container, stacked one-per-row below (see inputs.tsx FormField). */}
        {!isFramework && strategyCategory !== 'boundary' && (
          <div className="flex flex-col gap-3 @min-[552px]/chunkfields:flex-row">
            {strategyCategory === 'character' && (
              <DelimiterInput value={segmentIdentifier} onValueChange={onSegmentIdentifierChange} />
            )}
            <MaxLengthInput
              unit={
                strategyCategory === 'token'
                  ? t(($) => $['stepTwo.tokens'], { ns: 'datasetCreation' })
                  : t(($) => $['stepTwo.characters'], { ns: 'datasetCreation' })
              }
              value={maxChunkLength}
              onChange={onMaxChunkLengthChange}
            />
            <OverlapInput
              unit={
                strategyCategory === 'token'
                  ? t(($) => $['stepTwo.tokens'], { ns: 'datasetCreation' })
                  : t(($) => $['stepTwo.characters'], { ns: 'datasetCreation' })
              }
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
              <TextLabel>{t(($) => $['stepTwo.rules'], { ns: 'datasetCreation' })}</TextLabel>
            </div>
            <Separator decorative className="my-2 h-[0.5px] grow" variant="gradient" />
          </div>
          <div className="mt-1">
            {rules.map((rule) => (
              <label key={rule.id} className={`${s.ruleItem} cursor-pointer`}>
                <Checkbox checked={rule.enabled} onCheckedChange={() => onRuleToggle(rule.id)} />
                <span className="ml-2 system-sm-regular text-text-secondary">
                  {getRuleName(rule.id)}
                </span>
              </label>
            ))}
            {showSummaryIndexSetting && isNonCloudEdition && (
              <div className="mt-3">
                <SummaryIndexSetting
                  entry="create-document"
                  summaryIndexSetting={summaryIndexSetting}
                  onSummaryIndexSettingChange={onSummaryIndexSettingChange}
                />
              </div>
            )}
            {isNonCloudEdition && (
              <>
                <Separator orientation="horizontal" className="my-4 h-[0.5px] bg-divider-subtle" />
                <div className="flex flex-wrap items-center gap-y-2 py-0.5">
                  <label
                    className={`flex items-center ${hasCurrentDatasetDocForm ? '' : 'cursor-pointer'}`}
                  >
                    <Checkbox
                      checked={currentDocForm === ChunkingMode.qa}
                      disabled={hasCurrentDatasetDocForm}
                      onCheckedChange={() => {
                        if (hasCurrentDatasetDocForm) return
                        if (currentDocForm === ChunkingMode.qa) onDocFormChange(ChunkingMode.text)
                        else onDocFormChange(ChunkingMode.qa)
                      }}
                    />
                    <span id={qaLabelId} className="ml-2 system-sm-regular text-text-secondary">
                      {t(($) => $['stepTwo.useQALanguage'], { ns: 'datasetCreation' })}
                    </span>
                  </label>
                  <LanguageSelect
                    currentLanguage={docLanguage || locale}
                    onSelect={onDocLanguageChange}
                    disabled={currentDocForm !== ChunkingMode.qa}
                  />
                  <Infotip>
                    <InfotipTrigger aria-labelledby={qaLabelId} className="size-3.5" />
                    <InfotipContent aria-labelledby={qaLabelId}>
                      {t(($) => $['stepTwo.QATip'], { ns: 'datasetCreation' })}
                    </InfotipContent>
                  </Infotip>
                </div>
                {currentDocForm === ChunkingMode.qa && (
                  <Alert type="warning" className="mt-2">
                    <AlertTitle>
                      {t(($) => $['stepTwo.QATip'], { ns: 'datasetCreation' })}
                    </AlertTitle>
                  </Alert>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </OptionCard>
  )
}
