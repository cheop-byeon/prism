import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import IetfImporter from '../index'

const mockDownloadAndPreprocessMailarchiveYears = vi.fn()
const mockImportIetfDraftFile = vi.fn()
const mockImportIetfRfcFile = vi.fn()
const mockFetchIetfMailingLists = vi.fn()
const mockFetchIetfRfcSuggestions = vi.fn()
const mockFetchIetfWgDocuments = vi.fn()
const mockToastSuccess = vi.fn()
const mockToastError = vi.fn()

vi.mock('@/service/common', () => ({
  downloadAndPreprocessMailarchiveYears: (...args: unknown[]) => mockDownloadAndPreprocessMailarchiveYears(...args),
  importIetfDraftFile: (...args: unknown[]) => mockImportIetfDraftFile(...args),
  importIetfRfcFile: (...args: unknown[]) => mockImportIetfRfcFile(...args),
  fetchIetfMailingLists: (...args: unknown[]) => mockFetchIetfMailingLists(...args),
  fetchIetfRfcSuggestions: (...args: unknown[]) => mockFetchIetfRfcSuggestions(...args),
  fetchIetfWgDocuments: (...args: unknown[]) => mockFetchIetfWgDocuments(...args),
}))

vi.mock('@/service/use-common', () => ({
  useFileUploadConfig: () => ({
    data: {
      file_upload_limit: 5,
    },
  }),
}))

vi.mock('@/app/components/base/ui/toast', () => ({
  toast: {
    success: (...args: unknown[]) => mockToastSuccess(...args),
    error: (...args: unknown[]) => mockToastError(...args),
  },
}))

const createUploadedFile = () => ({
  id: 'file-1',
  name: 'ietf-httpapi.md',
  size: 128,
  mime_type: 'text/markdown',
  extension: 'md',
  url: 'https://example.com/file',
  created_by: 'user-1',
  created_at: 1700000000,
})

describe('IetfImporter', () => {
  const onFileListUpdate = vi.fn()
  const onSelectedYearsChange = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchIetfMailingLists.mockResolvedValue({ lists: [] })
    mockFetchIetfRfcSuggestions.mockResolvedValue({ suggestions: [] })
    mockFetchIetfWgDocuments.mockResolvedValue({ documents: [] })
  })

  it('should require a mailing list before preprocessing mailarchive content', async () => {
    render(
      <IetfImporter
        fileList={[]}
        onFileListUpdate={onFileListUpdate}
        onSelectedYearsChange={onSelectedYearsChange}
        supportBatchUpload
      />,
    )

    fireEvent.click(screen.getByTestId('ietf-import-open'))
    fireEvent.click(screen.getByTestId('ietf-import-submit'))

    await waitFor(() => {
      expect(mockToastError).toHaveBeenCalledWith('datasetCreation.stepOne.ietf.validation.mailarchiveRequired')
    })
    expect(mockDownloadAndPreprocessMailarchiveYears).not.toHaveBeenCalled()
  })

  it('should preprocess mailarchive content and allow selecting multiple years', async () => {
    mockDownloadAndPreprocessMailarchiveYears.mockResolvedValue({
      years: [
        {
          year: 2017,
          saved_path: '/tmp/2017.mbox',
          host_path_hint: 'docker/volumes/app/storage/ietf-downloads/2017.mbox',
          processed_path: '/tmp/2017.jsonl',
          processed_host_path_hint: 'docker/volumes/app/storage/ietf-downloads/preprocessed/2017.jsonl',
          total_emails: 1000,
          discarded_emails: 10,
          preview_lines: ['2017 first line', '2017 second line'],
        },
        {
          year: 2018,
          saved_path: '/tmp/2018.mbox',
          host_path_hint: 'docker/volumes/app/storage/ietf-downloads/2018.mbox',
          processed_path: '/tmp/2018.jsonl',
          processed_host_path_hint: 'docker/volumes/app/storage/ietf-downloads/preprocessed/2018.jsonl',
          total_emails: 1200,
          discarded_emails: 5,
          preview_lines: ['2018 first line', '2018 second line'],
        },
      ],
    })

    render(
      <IetfImporter
        fileList={[]}
        onFileListUpdate={onFileListUpdate}
        onSelectedYearsChange={onSelectedYearsChange}
        supportBatchUpload
      />,
    )

    fireEvent.click(screen.getByTestId('ietf-import-open'))
    fireEvent.change(screen.getByPlaceholderText('datasetCreation.stepOne.ietf.mailarchiveHint'), {
      target: { value: 'quic' },
    })
    fireEvent.click(screen.getByTestId('ietf-import-submit'))

    await waitFor(() => {
      expect(mockDownloadAndPreprocessMailarchiveYears).toHaveBeenCalledWith(expect.objectContaining({
        mailing_list: 'quic',
        quote_handling: 'none',
      }))
    })

    expect(screen.getByTestId('ietf-preprocess-results-panel')).toBeInTheDocument()
    expect(screen.getByTestId('ietf-preprocess-year-2017')).toBeInTheDocument()
    expect(screen.getByTestId('ietf-preprocess-year-2018')).toBeInTheDocument()
    expect(screen.queryByTestId('ietf-preprocess-preview')).not.toBeInTheDocument()
    expect(screen.getByText(/datasetCreation\.stepOne\.ietf\.preprocessSelectedCount/)).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('ietf-preprocess-year-2017'))
    await waitFor(() => {
      expect(onSelectedYearsChange).toHaveBeenLastCalledWith([
        expect.objectContaining({ year: 2017 }),
      ])
    })

    fireEvent.click(screen.getByTestId('ietf-preprocess-year-2018'))
    await waitFor(() => {
      expect(onSelectedYearsChange).toHaveBeenLastCalledWith([
        expect.objectContaining({ year: 2017 }),
        expect.objectContaining({ year: 2018 }),
      ])
    })

    fireEvent.click(screen.getByTestId('ietf-preprocess-year-2017'))
    await waitFor(() => {
      expect(onSelectedYearsChange).toHaveBeenLastCalledWith([
        expect.objectContaining({ year: 2018 }),
      ])
    })

    expect(onFileListUpdate).not.toHaveBeenCalled()
  })

  it('should require an RFC query before submitting the RFC form', async () => {
    render(
      <IetfImporter
        fileList={[]}
        onFileListUpdate={onFileListUpdate}
        onSelectedYearsChange={onSelectedYearsChange}
        supportBatchUpload
      />,
    )

    fireEvent.click(screen.getByTestId('ietf-import-open'))
    fireEvent.click(screen.getByRole('button', { name: /datasetCreation.stepOne.ietf.rfcs/i }))
    fireEvent.click(screen.getByTestId('ietf-import-submit'))

    await waitFor(() => {
      expect(mockToastError).toHaveBeenCalledWith('datasetCreation.stepOne.ietf.validation.rfcQueryRequired')
    })
    expect(mockImportIetfRfcFile).not.toHaveBeenCalled()
  })

  it('should disable opening when the file limit is already reached', () => {
    render(
      <IetfImporter
        fileList={[{
          fileID: 'existing-file',
          file: createUploadedFile() as unknown as File,
          progress: 100,
        }]}
        onFileListUpdate={onFileListUpdate}
        onSelectedYearsChange={onSelectedYearsChange}
        supportBatchUpload={false}
      />,
    )

    expect(screen.getByTestId('ietf-import-open')).toBeDisabled()
    expect(screen.getByText('datasetCreation.stepOne.ietf.limitReached')).toBeInTheDocument()
  })

  it('should import RFC content and append a completed file item', async () => {
    mockImportIetfRfcFile.mockResolvedValue(createUploadedFile())

    render(
      <IetfImporter
        fileList={[]}
        onFileListUpdate={onFileListUpdate}
        onSelectedYearsChange={onSelectedYearsChange}
        supportBatchUpload
      />,
    )

    fireEvent.click(screen.getByTestId('ietf-import-open'))
    fireEvent.click(screen.getByRole('button', { name: /datasetCreation.stepOne.ietf.rfcs/i }))
    fireEvent.change(screen.getByPlaceholderText('datasetCreation.stepOne.ietf.rfcIdHint'), {
      target: { value: 'RFC9000' },
    })
    fireEvent.click(screen.getByTestId('ietf-import-submit'))

    await waitFor(() => {
      expect(mockImportIetfRfcFile).toHaveBeenCalledWith(expect.objectContaining({
        rfc_ids: ['RFC9000'],
      }))
    })
    expect(onFileListUpdate).toHaveBeenCalledWith([
      expect.objectContaining({
        fileID: 'ietf-file-1',
        progress: 100,
      }),
    ])
  })

  it('should autofill readonly metadata and import a single selected Internet-Draft version', async () => {
    mockFetchIetfRfcSuggestions.mockResolvedValue({
      suggestions: [
        {
          doc_id: 'RFC9000',
          title: 'QUIC: A UDP-Based Multiplexed and Secure Transport',
          draft_names: ['draft-ietf-quic-transport-34'],
          since: '2021-05',
          stream: 'IETF',
          wg: 'quic',
          area: 'wit',
          status: 'PROPOSED STANDARD',
          draft_base_name: 'draft-ietf-quic-transport',
          draft_max_version: 34,
          draft_versions: [
            { name: 'draft-ietf-quic-transport-02', version: 2, since: '2018-03', until: '2018-06' },
            { name: 'draft-ietf-quic-transport-03', version: 3, since: '2018-06', until: '2018-10' },
            { name: 'draft-ietf-quic-transport-34', version: 34, since: '2021-01', until: null },
          ],
        },
      ],
    })
    mockImportIetfDraftFile.mockResolvedValue(createUploadedFile())

    render(
      <IetfImporter
        fileList={[]}
        onFileListUpdate={onFileListUpdate}
        onSelectedYearsChange={onSelectedYearsChange}
        supportBatchUpload
      />,
    )

    fireEvent.click(screen.getByTestId('ietf-import-open'))
    fireEvent.click(screen.getByRole('button', { name: /datasetCreation.stepOne.ietf.rfcs/i }))
    fireEvent.change(screen.getByPlaceholderText('datasetCreation.stepOne.ietf.rfcIdHint'), {
      target: { value: '9000' },
    })

    const suggestion = await screen.findByRole('button', { name: /RFC9000/i })
    fireEvent.click(suggestion)

    expect(screen.getByDisplayValue('RFC9000')).toBeInTheDocument()
    expect(screen.getByText('QUIC: A UDP-Based Multiplexed and Secure Transport')).toBeInTheDocument()
    expect(screen.getByText('2021-05')).toBeInTheDocument()
    expect(screen.getByText('IETF')).toBeInTheDocument()
    expect(screen.getByText('quic')).toBeInTheDocument()
    expect(screen.getByText('PROPOSED STANDARD')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /datasetCreation.stepOne.ietf.importTypeInternetDraft/i }))
    fireEvent.click(screen.getByRole('button', { name: '02' }))

    expect(screen.queryByText('datasetCreation.stepOne.ietf.selectedDraftName')).not.toBeInTheDocument()
    expect(screen.queryByText('datasetCreation.stepOne.ietf.draftRangeHint')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('ietf-import-submit'))

    await waitFor(() => {
      expect(mockImportIetfDraftFile).toHaveBeenCalledWith({
        draft_names: ['draft-ietf-quic-transport-02'],
        format: 'txt',
      })
    })

    expect(screen.getAllByText('2018-03').length).toBeGreaterThan(0)
    expect(screen.getAllByText('2018-06').length).toBeGreaterThan(0)
  })

  it('should fetch and show RFC suggestions even when query is empty', async () => {
    mockFetchIetfRfcSuggestions.mockResolvedValue({
      suggestions: [
        {
          doc_id: 'RFC9000',
          title: 'QUIC: A UDP-Based Multiplexed and Secure Transport',
          draft_names: ['draft-ietf-quic-transport'],
          draft_versions: [],
        },
      ],
    })

    render(
      <IetfImporter
        fileList={[]}
        onFileListUpdate={onFileListUpdate}
        onSelectedYearsChange={onSelectedYearsChange}
        supportBatchUpload
      />,
    )

    fireEvent.click(screen.getByTestId('ietf-import-open'))
    fireEvent.click(screen.getByRole('button', { name: /datasetCreation.stepOne.ietf.rfcs/i }))

    await waitFor(() => {
      expect(mockFetchIetfRfcSuggestions).toHaveBeenCalledWith({ query: '', limit: 20 })
    })

    expect(await screen.findByText('RFC9000')).toBeInTheDocument()
    expect(screen.getByText('draft-ietf-quic-transport')).toBeInTheDocument()
  })
})
