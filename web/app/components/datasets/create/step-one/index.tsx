'use client'

import type { DataSourceAuth } from '@/app/components/header/account-setting/data-source-page-new/types'
import type { DataSourceProvider, NotionPage } from '@/models/common'
import type { CrawlOptions, CrawlResultItem, CustomFile, FileItem } from '@/models/datasets'
import type { IetfYearPreprocessResult, UploadedConsoleFile } from '@/service/common'
import { useBoolean } from 'ahooks'
import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import NotionConnector from '@/app/components/base/notion-connector'
import { NotionPageSelector } from '@/app/components/base/notion-page-selector'
import { toast } from '@/app/components/base/ui/toast'
import { Plan } from '@/app/components/billing/type'
import VectorSpaceFull from '@/app/components/billing/vector-space-full'
import { useDatasetDetailContextWithSelector } from '@/context/dataset-detail'
import { useProviderContext } from '@/context/provider-context'
import { DataSourceType } from '@/models/datasets'
import { uploadIetfPreprocessedFile } from '@/service/common'
import { cn } from '@/utils/classnames'
import EmptyDatasetCreationModal from '../empty-dataset-creation-modal'
import FileUploader from '../file-uploader'
import { PROGRESS_COMPLETE } from '../file-uploader/constants'
import IetfImporter from '../ietf-importer'
import Website from '../website'
import { DataSourceTypeSelector, NextStepButton, PreviewPanel } from './components'
import { usePreviewState } from './hooks'
import s from './index.module.css'
import UpgradeCard from './upgrade-card'

type IStepOneProps = {
  datasetId?: string
  dataSourceType?: DataSourceType
  dataSourceTypeDisable: boolean
  onSetting: () => void
  files: FileItem[]
  updateFileList: (files: FileItem[]) => void
  updateFile: (fileItem: FileItem, progress: number, list: FileItem[]) => void
  notionPages?: NotionPage[]
  notionCredentialId: string
  updateNotionPages: (value: NotionPage[]) => void
  updateNotionCredentialId: (credentialId: string) => void
  onStepChange: () => void
  changeType: (type: DataSourceType) => void
  websitePages?: CrawlResultItem[]
  updateWebsitePages: (value: CrawlResultItem[]) => void
  onWebsiteCrawlProviderChange: (provider: DataSourceProvider) => void
  onWebsiteCrawlJobIdChange: (jobId: string) => void
  crawlOptions: CrawlOptions
  onCrawlOptionsChange: (payload: CrawlOptions) => void
  authedDataSourceList: DataSourceAuth[]
}

// Helper function to check if notion is authenticated
function checkNotionAuth(authedDataSourceList: DataSourceAuth[]): boolean {
  const notionSource = authedDataSourceList.find(item => item.provider === 'notion_datasource')
  return Boolean(notionSource && notionSource.credentials_list.length > 0)
}

// Helper function to get notion credential list
function getNotionCredentialList(authedDataSourceList: DataSourceAuth[]) {
  return authedDataSourceList.find(item => item.provider === 'notion_datasource')?.credentials_list || []
}

// Lookup table for checking multiple items by data source type
const MULTIPLE_ITEMS_CHECK: Record<DataSourceType, (props: { files: FileItem[], notionPages: NotionPage[], websitePages: CrawlResultItem[] }) => boolean> = {
  [DataSourceType.FILE]: ({ files }) => files.length > 1,
  [DataSourceType.NOTION]: ({ notionPages }) => notionPages.length > 1,
  [DataSourceType.WEB]: ({ websitePages }) => websitePages.length > 1,
}

const toFileItem = (file: UploadedConsoleFile): FileItem => ({
  fileID: `ietf-${file.id}`,
  file: file as unknown as CustomFile,
  progress: PROGRESS_COMPLETE,
})

const StepOne = ({
  datasetId,
  dataSourceType: inCreatePageDataSourceType,
  dataSourceTypeDisable,
  changeType,
  onSetting,
  onStepChange: doOnStepChange,
  files,
  updateFileList,
  updateFile,
  notionPages = [],
  notionCredentialId,
  updateNotionPages,
  updateNotionCredentialId,
  websitePages = [],
  updateWebsitePages,
  onWebsiteCrawlProviderChange,
  onWebsiteCrawlJobIdChange,
  crawlOptions,
  onCrawlOptionsChange,
  authedDataSourceList,
}: IStepOneProps) => {
  const { t } = useTranslation()
  const dataset = useDatasetDetailContextWithSelector(state => state.dataset)
  const { plan, enableBilling } = useProviderContext()

  // Preview state management
  // eslint-disable-next-line react/use-state
  const {
    currentFile,
    currentNotionPage,
    currentWebsite,
    showFilePreview,
    hideFilePreview,
    showNotionPagePreview,
    hideNotionPagePreview,
    showWebsitePreview,
    hideWebsitePreview,
  } = usePreviewState()

  // Empty dataset modal state
  const showModalState = useBoolean(false)
  const showModal = showModalState[0]
  const { setTrue: openModal, setFalse: closeModal } = showModalState[1]

  // Plan upgrade modal state
  const planUpgradeModalState = useBoolean(false)
  const isShowPlanUpgradeModal = planUpgradeModalState[0]
  const { setTrue: showPlanUpgradeModal, setFalse: hidePlanUpgradeModal } = planUpgradeModalState[1]
  const [selectedIetfYears, setSelectedIetfYears] = useState<IetfYearPreprocessResult[]>([])
  const [isPreparingIetfFiles, setIsPreparingIetfFiles] = useState(false)

  // Computed values
  const shouldShowDataSourceTypeList = !datasetId || (datasetId && !dataset?.data_source_type)
  const isInCreatePage = shouldShowDataSourceTypeList
  // Default to FILE type when no type is provided from either source
  const dataSourceType = isInCreatePage
    ? (inCreatePageDataSourceType ?? DataSourceType.FILE)
    : (dataset?.data_source_type ?? DataSourceType.FILE)

  const allFileLoaded = files.length > 0 && files.every(file => file.file.id)
  const hasNotion = notionPages.length > 0
  const isVectorSpaceFull = plan.usage.vectorSpace >= plan.total.vectorSpace
  const isShowVectorSpaceFull = (allFileLoaded || hasNotion) && isVectorSpaceFull && enableBilling
  const supportBatchUpload = !enableBilling || plan.type !== Plan.sandbox

  const isNotionAuthed = useMemo(() => checkNotionAuth(authedDataSourceList), [authedDataSourceList])
  const notionCredentialList = useMemo(() => getNotionCredentialList(authedDataSourceList), [authedDataSourceList])

  const fileNextDisabled = useMemo(() => {
    if (isShowVectorSpaceFull)
      return true

    if (isPreparingIetfFiles)
      return true

    if (files.length > 0)
      return files.some(file => !file.file.id)

    return selectedIetfYears.length === 0
  }, [files, isPreparingIetfFiles, isShowVectorSpaceFull, selectedIetfYears.length])

  // Clear previews when switching data source type
  const handleClearPreviews = useCallback((newType: DataSourceType) => {
    if (newType !== DataSourceType.FILE)
      hideFilePreview()
    if (newType !== DataSourceType.NOTION)
      hideNotionPagePreview()
    if (newType !== DataSourceType.WEB)
      hideWebsitePreview()
  }, [hideFilePreview, hideNotionPagePreview, hideWebsitePreview])

  // Handle step change with batch upload check
  const onStepChange = useCallback(async () => {
    if (!supportBatchUpload && dataSourceType) {
      const hasMultipleItems = dataSourceType === DataSourceType.FILE
        ? files.length + selectedIetfYears.length > 1
        : MULTIPLE_ITEMS_CHECK[dataSourceType]?.({ files, notionPages, websitePages })

      if (hasMultipleItems) {
        showPlanUpgradeModal()
        return
      }
    }

    if (dataSourceType === DataSourceType.FILE && files.length === 0 && selectedIetfYears.length > 0) {
      const validSelections = selectedIetfYears.filter(item => item.processed_path)
      if (!validSelections.length) {
        toast.error(t('stepOne.ietf.importFailed', { ns: 'datasetCreation' }))
        return
      }

      setIsPreparingIetfFiles(true)
      try {
        const uploaded = await Promise.all(
          validSelections.map(item => uploadIetfPreprocessedFile({ processed_path: item.processed_path! })),
        )
        updateFileList(uploaded.map(toFileItem))
      }
      catch {
        toast.error(t('stepOne.ietf.importFailed', { ns: 'datasetCreation' }))
        return
      }
      finally {
        setIsPreparingIetfFiles(false)
      }
    }

    doOnStepChange()
  }, [
    dataSourceType,
    doOnStepChange,
    files,
    notionPages,
    selectedIetfYears,
    showPlanUpgradeModal,
    supportBatchUpload,
    t,
    updateFileList,
    websitePages,
  ])

  return (
    <div className="h-full w-full overflow-x-auto">
      <div className="flex h-full w-full min-w-[1440px]">
        {/* Left Panel - Form */}
        <div className="relative h-full w-1/2 overflow-y-auto">
          <div className="flex justify-end">
            <div className={cn(s.form)}>
              {shouldShowDataSourceTypeList && (
                <>
                  <div className={cn(s.stepHeader, 'system-md-semibold text-text-secondary')}>
                    {t('steps.one', { ns: 'datasetCreation' })}
                  </div>
                  <DataSourceTypeSelector
                    currentType={dataSourceType}
                    disabled={dataSourceTypeDisable}
                    onChange={changeType}
                    onClearPreviews={handleClearPreviews}
                  />
                </>
              )}

              {/* File Data Source */}
              {dataSourceType === DataSourceType.FILE && (
                <>
                  <IetfImporter
                    fileList={files}
                    onFileListUpdate={updateFileList}
                    onSelectedYearsChange={setSelectedIetfYears}
                    disabled={isShowVectorSpaceFull}
                    supportBatchUpload={supportBatchUpload}
                  />
                  <FileUploader
                    fileList={files}
                    titleClassName={!shouldShowDataSourceTypeList ? 'mt-[30px] !mb-[44px] !text-lg' : undefined}
                    prepareFileList={updateFileList}
                    onFileListUpdate={updateFileList}
                    onFileUpdate={updateFile}
                    onPreview={showFilePreview}
                    supportBatchUpload={supportBatchUpload}
                  />
                  {isShowVectorSpaceFull && (
                    <div className="mb-4 max-w-[640px]">
                      <VectorSpaceFull />
                    </div>
                  )}
                  <NextStepButton disabled={fileNextDisabled} onClick={onStepChange} />
                  {enableBilling && plan.type === Plan.sandbox && files.length > 0 && (
                    <div className="mt-5">
                      <div className="mb-4 h-px bg-divider-subtle" />
                      <UpgradeCard />
                    </div>
                  )}
                </>
              )}

              {/* Notion Data Source */}
              {dataSourceType === DataSourceType.NOTION && (
                <>
                  {!isNotionAuthed && <NotionConnector onSetting={onSetting} />}
                  {isNotionAuthed && (
                    <>
                      <div className="mb-8 w-[640px]">
                        <NotionPageSelector
                          value={notionPages.map(page => page.page_id)}
                          onSelect={updateNotionPages}
                          onPreview={showNotionPagePreview}
                          credentialList={notionCredentialList}
                          onSelectCredential={updateNotionCredentialId}
                          datasetId={datasetId}
                        />
                      </div>
                      {isShowVectorSpaceFull && (
                        <div className="mb-4 max-w-[640px]">
                          <VectorSpaceFull />
                        </div>
                      )}
                      <NextStepButton
                        disabled={isShowVectorSpaceFull || !notionPages.length}
                        onClick={onStepChange}
                      />
                    </>
                  )}
                </>
              )}

              {/* Web Data Source */}
              {dataSourceType === DataSourceType.WEB && (
                <>
                  <div className={cn('mb-8 w-[640px]', !shouldShowDataSourceTypeList && 'mt-12')}>
                    <Website
                      onPreview={showWebsitePreview}
                      checkedCrawlResult={websitePages}
                      onCheckedCrawlResultChange={updateWebsitePages}
                      onCrawlProviderChange={onWebsiteCrawlProviderChange}
                      onJobIdChange={onWebsiteCrawlJobIdChange}
                      crawlOptions={crawlOptions}
                      onCrawlOptionsChange={onCrawlOptionsChange}
                      authedDataSourceList={authedDataSourceList}
                    />
                  </div>
                  {isShowVectorSpaceFull && (
                    <div className="mb-4 max-w-[640px]">
                      <VectorSpaceFull />
                    </div>
                  )}
                  <NextStepButton
                    disabled={isShowVectorSpaceFull || !websitePages.length}
                    onClick={onStepChange}
                  />
                </>
              )}

              {/* Empty Dataset Creation Link */}
              {!datasetId && (
                <>
                  <div className="my-8 h-px max-w-[640px] bg-divider-regular" />
                  <span
                    className="inline-flex cursor-pointer items-center text-[13px] leading-4 text-text-accent"
                    onClick={openModal}
                  >
                    <span className="i-ri-folder-6-line mr-1 size-4" />
                    {t('stepOne.emptyDatasetCreation', { ns: 'datasetCreation' })}
                  </span>
                </>
              )}
            </div>
            <EmptyDatasetCreationModal show={showModal} onHide={closeModal} />
          </div>
        </div>

        {/* Right Panel - Preview */}
        <PreviewPanel
          currentFile={currentFile}
          currentNotionPage={currentNotionPage}
          currentWebsite={currentWebsite}
          notionCredentialId={notionCredentialId}
          isShowPlanUpgradeModal={isShowPlanUpgradeModal}
          hideFilePreview={hideFilePreview}
          hideNotionPagePreview={hideNotionPagePreview}
          hideWebsitePreview={hideWebsitePreview}
          hidePlanUpgradeModal={hidePlanUpgradeModal}
        />
      </div>
    </div>
  )
}

export default StepOne
