'use client'

import type { FC } from 'react'
import type { ParentChildConfig } from '../hooks'
import type { ChunkStrategy, ParentMode, PreProcessingRule, StrategyProvider, SummaryIndexSetting as SummaryIndexSettingType } from '@/models/datasets'
import { RiSearchEyeLine } from '@remixicon/react'
import { useTranslation } from 'react-i18next'
import Button from '@/app/components/base/button'
import Checkbox from '@/app/components/base/checkbox'
import Divider from '@/app/components/base/divider'
import { ParentChildChunk } from '@/app/components/base/icons/src/vender/knowledge'
import RadioCard from '@/app/components/base/radio-card'
import SegmentedControl from '@/app/components/base/segmented-control'
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
import FileList from '../../assets/file-list-3-fill.svg'
import Note from '../../assets/note-mod.svg'
import BlueEffect from '../../assets/option-card-effect-blue.svg'
import s from '../index.module.css'
import { FrameworkParamInput } from './framework-param-input'
import { DelimiterInput, MaxLengthInput } from './inputs'
import { OptionCard } from './option-card'
import { getStrategyLabelKey } from './strategy-label-keys'

type TextLabelProps = {
  children: React.ReactNode
}

const TextLabel: FC<TextLabelProps> = ({ children }) => {
  return <label className="text-text-secondary system-sm-semibold">{children}</label>
}

type LevelStrategyConfig = {
  delimiter: string
  maxLength: number
  chunkStrategy: ChunkStrategy
  strategyProvider: StrategyProvider
  strategyParams: Record<string, unknown>
}

type LevelStrategySectionProps = {
  level: LevelStrategyConfig
  delimiterTooltip?: string
  onDelimiterChange: (value: string) => void
  onMaxLengthChange: (value: number) => void
  onStrategyProviderChange: (provider: StrategyProvider) => void
  onStrategyChange: (strategy: ChunkStrategy) => void
  onStrategyParamChange: (key: string, value: unknown) => void
}

// Shared by the parent and child sections below — each level (parent/child)
// picks its own strategy independently, e.g. child can use `semantic` while
// parent stays on `paragraph`/`recursive`. Mirrors GeneralChunkingOptions'
// provider + strategy + framework-param-form pattern, just scoped to one level.
const LevelStrategySection: FC<LevelStrategySectionProps> = ({
  level,
  delimiterTooltip,
  onDelimiterChange,
  onMaxLengthChange,
  onStrategyProviderChange,
  onStrategyChange,
  onStrategyParamChange,
}) => {
  const { t } = useTranslation()

  const strategyCategory = getStrategyCategory(level.chunkStrategy)
  const isFramework = strategyCategory === 'framework'
  const activeStrategies
    = level.strategyProvider === 'llamaindex'
      ? LLAMAINDEX_STRATEGIES
      : level.strategyProvider === 'langchain'
        ? LANGCHAIN_STRATEGIES
        : CUSTOM_STRATEGIES
  const providerOptions = [
    { value: 'custom' as StrategyProvider, text: t('stepTwo.chunkStrategyProviderCustom', { ns: 'datasetCreation' }) },
    { value: 'llamaindex' as StrategyProvider, text: t('stepTwo.chunkStrategyProviderLlamaIndex', { ns: 'datasetCreation' }) },
    { value: 'langchain' as StrategyProvider, text: t('stepTwo.chunkStrategyProviderLangChain', { ns: 'datasetCreation' }) },
  ]
  const frameworkSchema = STRATEGY_PARAM_SCHEMAS[level.chunkStrategy]

  return (
    <div className="mt-2 flex flex-col gap-3">
      <div className="flex flex-col gap-2">
        <TextLabel>{t('stepTwo.chunkStrategyProvider', { ns: 'datasetCreation' })}</TextLabel>
        <SegmentedControl
          options={providerOptions}
          value={level.strategyProvider}
          onChange={value => onStrategyProviderChange(value as StrategyProvider)}
        />
      </div>

      <div className="flex flex-col gap-1">
        <TextLabel>{t('stepTwo.chunkStrategy', { ns: 'datasetCreation' })}</TextLabel>
        <select
          className="h-9 rounded-lg border border-components-panel-border bg-components-panel-bg px-3 text-sm text-text-primary"
          value={level.chunkStrategy}
          onChange={e => onStrategyChange(e.target.value as ChunkStrategy)}
        >
          {activeStrategies.map(strategy => (
            <option key={strategy} value={strategy}>
              {t(getStrategyLabelKey(strategy), { ns: 'datasetCreation', defaultValue: getStrategyLabelKey(strategy) })}
            </option>
          ))}
        </select>
      </div>

      {isFramework && frameworkSchema && frameworkSchema.params.length > 0 && (
        <div className="flex flex-wrap gap-3">
          {frameworkSchema.params.map(param => (
            <FrameworkParamInput
              key={param.key}
              paramKey={param.key}
              type={param.type}
              labelKey={param.labelKey}
              value={level.strategyParams[param.key] ?? param.defaultValue}
              options={param.options}
              min={param.min}
              max={param.max}
              onChange={onStrategyParamChange}
            />
          ))}
        </div>
      )}

      {isFramework && frameworkSchema && frameworkSchema.params.length === 0 && (
        <p className="text-text-secondary system-xs-regular">
          {t('stepTwo.frameworkNoParams', { ns: 'datasetCreation' })}
        </p>
      )}

      {!isFramework && strategyCategory !== 'boundary' && (
        <div className="flex gap-3">
          {strategyCategory === 'character' && (
            <DelimiterInput
              value={level.delimiter}
              tooltip={delimiterTooltip}
              onChange={e => onDelimiterChange(e.target.value)}
            />
          )}
          <MaxLengthInput
            unit={strategyCategory === 'token'
              ? t('stepTwo.tokens', { ns: 'datasetCreation' })
              : t('stepTwo.characters', { ns: 'datasetCreation' })}
            value={level.maxLength}
            onChange={onMaxLengthChange}
          />
        </div>
      )}
    </div>
  )
}

type ParentChildOptionsProps = {
  // State
  parentChildConfig: ParentChildConfig
  rules: PreProcessingRule[]
  summaryIndexSetting?: SummaryIndexSettingType
  onSummaryIndexSettingChange?: (payload: SummaryIndexSettingType) => void
  currentDocForm: ChunkingMode
  // Flags
  isActive: boolean
  isInUpload: boolean
  isNotUploadInEmptyDataset: boolean
  // Actions
  onDocFormChange: (form: ChunkingMode) => void
  onChunkForContextChange: (mode: ParentMode) => void
  onParentDelimiterChange: (value: string) => void
  onParentMaxLengthChange: (value: number) => void
  onChildDelimiterChange: (value: string) => void
  onChildMaxLengthChange: (value: number) => void
  onParentStrategyProviderChange: (provider: StrategyProvider) => void
  onChildStrategyProviderChange: (provider: StrategyProvider) => void
  onParentStrategyChange: (strategy: ChunkStrategy) => void
  onChildStrategyChange: (strategy: ChunkStrategy) => void
  onParentStrategyParamChange: (key: string, value: unknown) => void
  onChildStrategyParamChange: (key: string, value: unknown) => void
  onRuleToggle: (id: string) => void
  onPreview: () => void
  onReset: () => void
  showSummaryIndexSetting?: boolean
}

export const ParentChildOptions: FC<ParentChildOptionsProps> = ({
  parentChildConfig,
  rules,
  summaryIndexSetting,
  currentDocForm: _currentDocForm,
  isActive,
  isInUpload,
  isNotUploadInEmptyDataset,
  onDocFormChange,
  onChunkForContextChange,
  onParentDelimiterChange,
  onParentMaxLengthChange,
  onChildDelimiterChange,
  onChildMaxLengthChange,
  onParentStrategyProviderChange,
  onChildStrategyProviderChange,
  onParentStrategyChange,
  onChildStrategyChange,
  onParentStrategyParamChange,
  onChildStrategyParamChange,
  onRuleToggle,
  onSummaryIndexSettingChange,
  onPreview,
  onReset,
  showSummaryIndexSetting,
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

  return (
    <OptionCard
      title={t('stepTwo.parentChild', { ns: 'datasetCreation' })}
      icon={<ParentChildChunk className="h-[20px] w-[20px]" />}
      effectImg={BlueEffect.src}
      className="text-util-colors-blue-light-blue-light-500"
      activeHeaderClassName="bg-dataset-option-card-blue-gradient"
      description={t('stepTwo.parentChildTip', { ns: 'datasetCreation' })}
      isActive={isActive}
      onSwitched={() => onDocFormChange(ChunkingMode.parentChild)}
      actions={(
        <>
          <Button variant="secondary-accent" onClick={onPreview}>
            <RiSearchEyeLine className="mr-0.5 h-4 w-4" />
            {t('stepTwo.previewChunk', { ns: 'datasetCreation' })}
          </Button>
          <Button variant="ghost" onClick={onReset}>
            {t('stepTwo.reset', { ns: 'datasetCreation' })}
          </Button>
        </>
      )}
      noHighlight={isInUpload && isNotUploadInEmptyDataset}
    >
      <div className="flex flex-col gap-4">
        {/* Parent chunk for context */}
        <div>
          <div className="flex items-center gap-x-2">
            <div className="inline-flex shrink-0">
              <TextLabel>{t('stepTwo.parentChunkForContext', { ns: 'datasetCreation' })}</TextLabel>
            </div>
            <Divider className="grow" bgStyle="gradient" />
          </div>
          <RadioCard
            className="mt-1"
            icon={<img src={Note.src} alt="" />}
            title={t('stepTwo.paragraph', { ns: 'datasetCreation' })}
            description={t('stepTwo.paragraphTip', { ns: 'datasetCreation' })}
            isChosen={parentChildConfig.chunkForContext === 'paragraph'}
            onChosen={() => onChunkForContextChange('paragraph')}
            chosenConfig={(
              <LevelStrategySection
                level={parentChildConfig.parent}
                delimiterTooltip={t('stepTwo.parentChildDelimiterTip', { ns: 'datasetCreation' })!}
                onDelimiterChange={onParentDelimiterChange}
                onMaxLengthChange={onParentMaxLengthChange}
                onStrategyProviderChange={onParentStrategyProviderChange}
                onStrategyChange={onParentStrategyChange}
                onStrategyParamChange={onParentStrategyParamChange}
              />
            )}
          />
          <RadioCard
            className="mt-2"
            icon={<img src={FileList.src} alt="" />}
            title={t('stepTwo.fullDoc', { ns: 'datasetCreation' })}
            description={t('stepTwo.fullDocTip', { ns: 'datasetCreation' })}
            onChosen={() => onChunkForContextChange('full-doc')}
            isChosen={parentChildConfig.chunkForContext === 'full-doc'}
          />
        </div>

        {/* Child chunk for retrieval */}
        <div>
          <div className="flex items-center gap-x-2">
            <div className="inline-flex shrink-0">
              <TextLabel>{t('stepTwo.childChunkForRetrieval', { ns: 'datasetCreation' })}</TextLabel>
            </div>
            <Divider className="grow" bgStyle="gradient" />
          </div>
          <LevelStrategySection
            level={parentChildConfig.child}
            delimiterTooltip={t('stepTwo.parentChildChunkDelimiterTip', { ns: 'datasetCreation' })!}
            onDelimiterChange={onChildDelimiterChange}
            onMaxLengthChange={onChildMaxLengthChange}
            onStrategyProviderChange={onChildStrategyProviderChange}
            onStrategyChange={onChildStrategyChange}
            onStrategyParamChange={onChildStrategyParamChange}
          />
        </div>

        {/* Rules */}
        <div>
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
          </div>
        </div>
      </div>
    </OptionCard>
  )
}
