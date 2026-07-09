import type { FC } from 'react'
import type { ChildChunkDetail, ChunkingMode } from '@/models/datasets'
import * as React from 'react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Divider from '@/app/components/base/divider'
import { useEventEmitterContextContext } from '@/context/event-emitter'
import { useChildChunkMetadata } from '@/service/knowledge/use-segment'
import { cn } from '@/utils/classnames'
import { formatNumber } from '@/utils/format'
import { formatTime } from '@/utils/time'
import { useDocumentContext } from '../context'
import ActionButtons from './common/action-buttons'
import ChunkContent from './common/chunk-content'
import Dot from './common/dot'
import { SegmentIndexTag } from './common/segment-index-tag'
import { useSegmentListContext } from './index'

type IChildSegmentDetailProps = {
  chunkId: string
  childChunkInfo?: Partial<ChildChunkDetail> & { id: string }
  onUpdate: (segmentId: string, childChunkId: string, content: string) => void
  onCancel: () => void
  docForm: ChunkingMode
}

const SKIP_METADATA_KEYS = new Set(['source', 'line', 'content', 'body', 'record_type', 'subject_line', 'email_id'])

/**
 * Show all the contents of the segment
 */
const ChildSegmentDetail: FC<IChildSegmentDetailProps> = ({
  chunkId,
  childChunkInfo,
  onUpdate,
  onCancel,
  docForm,
}) => {
  const { t } = useTranslation()
  const [content, setContent] = useState(childChunkInfo?.content || '')
  const { eventEmitter } = useEventEmitterContextContext()
  const [loading, setLoading] = useState(false)
  const fullScreen = useSegmentListContext(s => s.fullScreen)
  const toggleFullScreen = useSegmentListContext(s => s.toggleFullScreen)
  const datasetId = useDocumentContext(s => s.datasetId)
  const documentId = useDocumentContext(s => s.documentId)

  const { data: metadataResponse } = useChildChunkMetadata(
    {
      datasetId: datasetId || '',
      documentId: documentId || '',
      segmentId: chunkId,
      childChunkId: childChunkInfo?.id || '',
    },
    !datasetId || !documentId || !chunkId || !childChunkInfo?.id,
  )

  const metadata = metadataResponse?.data?.metadata || {}
  const normalizedMetadata: Record<string, string | null | undefined> = {
    ...metadata,
    ...(metadata.subject_line && !metadata.subject ? { subject: metadata.subject_line } : {}),
    ...(metadata.email_id && !metadata.id ? { id: metadata.email_id } : {}),
  }
  const metadataFieldLabels: Record<string, string> = {
    url: t('segment.metadataUrl', { ns: 'datasetDocuments' }),
    subject: t('segment.metadataSubject', { ns: 'datasetDocuments' }),
    id: t('segment.metadataId', { ns: 'datasetDocuments' }),
    date: t('segment.metadataDate', { ns: 'datasetDocuments' }),
    draft_name: t('segment.metadataDraftName', { ns: 'datasetDocuments' }),
    from_rev: t('segment.metadataFromRev', { ns: 'datasetDocuments' }),
    to_rev: t('segment.metadataToRev', { ns: 'datasetDocuments' }),
    section_id: t('segment.metadataSectionId', { ns: 'datasetDocuments' }),
    section_title: t('segment.metadataSectionTitle', { ns: 'datasetDocuments' }),
    change_type: t('segment.metadataChangeType', { ns: 'datasetDocuments' }),
    final_rev: t('segment.metadataFinalRev', { ns: 'datasetDocuments' }),
    first_seen_rev: t('segment.metadataFirstSeenRev', { ns: 'datasetDocuments' }),
    last_changed_rev: t('segment.metadataLastChangedRev', { ns: 'datasetDocuments' }),
    change_count: t('segment.metadataChangeCount', { ns: 'datasetDocuments' }),
  }
  const metadataItems = Object.entries(normalizedMetadata)
    .filter(([key, value]) => !SKIP_METADATA_KEYS.has(key) && value != null && value !== '')
    .map(([key, value]) => ({
      key,
      label: metadataFieldLabels[key] ?? key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
      value: String(value),
    }))

  eventEmitter?.useSubscription((v) => {
    if (v === 'update-child-segment')
      setLoading(true)
    if (v === 'update-child-segment-done')
      setLoading(false)
  })

  const handleCancel = () => {
    onCancel()
  }

  const handleSave = () => {
    onUpdate(chunkId, childChunkInfo?.id || '', content)
  }

  const wordCountText = useMemo(() => {
    const count = content.length
    return `${formatNumber(count)} ${t('segment.characters', { ns: 'datasetDocuments', count })}`
  }, [content.length, t])

  const EditTimeText = useMemo(() => {
    const timeText = formatTime({
      date: (childChunkInfo?.updated_at ?? 0) * 1000,
      dateFormat: `${t('segment.dateTimeFormat', { ns: 'datasetDocuments' })}`,
    })
    return `${t('segment.editedAt', { ns: 'datasetDocuments' })} ${timeText}`
  }, [childChunkInfo?.updated_at, t])

  return (
    <div className="flex h-full flex-col">
      <div className={cn('flex items-center justify-between', fullScreen ? 'border border-divider-subtle py-3 pl-6 pr-4' : 'pl-4 pr-3 pt-3')}>
        <div className="flex flex-col">
          <div className="text-text-primary system-xl-semibold">{t('segment.editChildChunk', { ns: 'datasetDocuments' })}</div>
          <div className="flex items-center gap-x-2">
            <SegmentIndexTag positionId={childChunkInfo?.position || ''} labelPrefix={t('segment.childChunk', { ns: 'datasetDocuments' }) as string} />
            <Dot />
            <span className="text-text-tertiary system-xs-medium">{wordCountText}</span>
            <Dot />
            <span className="text-text-tertiary system-xs-medium">
              {EditTimeText}
            </span>
          </div>
        </div>
        <div className="flex items-center">
          {fullScreen && (
            <>
              <ActionButtons
                handleCancel={handleCancel}
                handleSave={handleSave}
                loading={loading}
                isChildChunk={true}
              />
              <Divider type="vertical" className="ml-4 mr-2 h-3.5 bg-divider-regular" />
            </>
          )}
          <div className="mr-1 flex h-8 w-8 cursor-pointer items-center justify-center p-1.5" onClick={toggleFullScreen}>
            {fullScreen ? <span className="i-ri-collapse-diagonal-line h-4 w-4 text-text-tertiary" /> : <span className="i-ri-expand-diagonal-line h-4 w-4 text-text-tertiary" />}
          </div>
          <div className="flex h-8 w-8 cursor-pointer items-center justify-center p-1.5" onClick={onCancel}>
            <span className="i-ri-close-line h-4 w-4 text-text-tertiary" />
          </div>
        </div>
      </div>
      <div className={cn('flex w-full grow', fullScreen ? 'flex-row justify-center gap-x-8 px-6 pt-6' : 'flex-col gap-y-3 px-4 py-3')}>
        <div className={cn('overflow-hidden whitespace-pre-line break-all', fullScreen ? 'h-full w-1/2' : 'h-0 grow')}>
          <ChunkContent
            docForm={docForm}
            question={content}
            onQuestionChange={content => setContent(content)}
            isEditMode={true}
          />
        </div>
        <div className={cn('shrink-0', fullScreen ? 'w-[320px]' : 'w-full')}>
          <div className="rounded-xl border border-components-panel-border bg-components-panel-bg px-3 py-2">
            <div className="mb-2 text-text-tertiary system-xs-semibold-uppercase">{t('segment.metadata', { ns: 'datasetDocuments' })}</div>
            <div className="space-y-2">
              {metadataItems.length === 0
                ? <div className="text-text-tertiary system-xs-regular">-</div>
                : metadataItems.map(item => (
                    <div key={item.key} className="space-y-1">
                      <div className="text-text-tertiary system-2xs-medium-uppercase">{item.label}</div>
                      <div className="break-all text-text-primary system-xs-regular">{item.value}</div>
                    </div>
                  ))}
            </div>
          </div>
        </div>
      </div>
      {!fullScreen && (
        <div className="flex items-center justify-end border-t-[1px] border-t-divider-subtle p-4 pt-3">
          <ActionButtons
            handleCancel={handleCancel}
            handleSave={handleSave}
            loading={loading}
            isChildChunk={true}
          />
        </div>
      )}
    </div>
  )
}

export default React.memo(ChildSegmentDetail)
