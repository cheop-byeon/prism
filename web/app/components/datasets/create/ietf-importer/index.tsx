'use client'

import type { CustomFile, FileItem } from '@/models/datasets'
import type {
  GithubJobState,
  IetfDraftImportRequest,
  IetfDraftRelations,
  IetfFormatInfo,
  IetfPreprocessResponse,
  IetfRfcImportRequest,
  IetfRfcSuggestion,
  IetfSyncStatus,
  IetfWgDocument,
  IetfYearPreprocessResult,
  UploadedConsoleFile,
} from '@/service/common'
import { startTransition, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/app/components/base/button'
import Input from '@/app/components/base/input'
import SegmentedControl from '@/app/components/base/segmented-control'
import { Dialog, DialogCloseButton, DialogContent, DialogDescription, DialogTitle } from '@/app/components/base/ui/dialog'
import { toast } from '@/app/components/base/ui/toast'
import {
  downloadAndPreprocessMailarchiveYears,
  fetchGithubImportStatus,
  fetchIetfDraftFormats,
  fetchIetfDraftRelations,
  fetchIetfMailarchiveSyncStatus,
  fetchIetfMailingLists,
  fetchIetfRfcFormats,
  fetchIetfRfcSuggestions,
  fetchIetfWgDocuments,
  importIetfDraftFile,
  importIetfRfcFile,
  resumeGithubImport,
  startGithubImport,
  triggerIetfMailarchiveSync,
} from '@/service/common'
import { useFileUploadConfig } from '@/service/use-common'
import { PROGRESS_COMPLETE } from '../file-uploader/constants'
import { getReposForWg, WG_NAMES } from './wg-repos'

type IetfImporterProps = {
  fileList: FileItem[]
  onFileListUpdate: (files: FileItem[]) => void
  onSelectedYearsChange?: (items: IetfYearPreprocessResult[]) => void
  disabled?: boolean
  supportBatchUpload: boolean
}

type IetfImportMode = 'mailarchive' | 'rfcs' | 'github'

type MailarchiveFormState = {
  mailingList: string
  sentAfter: string
  sentBefore: string
  fromAddr: string
  subject: string
  searchIn: ('subject' | 'body')[]
}

type RfcFormState = {
  rfcId: string
}

type GithubLoader = 'graphql' | 'langchain' | 'llamaindex'

type GithubFormState = {
  wg: string
  repo: string
  includeIssues: boolean
  includePRs: boolean
  includeClosed: boolean
  githubToken: string
  loader: GithubLoader
}

const DEFAULT_MAILARCHIVE_FORM: MailarchiveFormState = {
  mailingList: '',
  sentAfter: '1970-01-01T00:00:00',
  sentBefore: '2038-01-19T03:14:07',
  fromAddr: '',
  subject: '',
  searchIn: ['subject'],
}

const DEFAULT_RFC_FORM: RfcFormState = {
  rfcId: '',
}

const GITHUB_TOKEN_STORAGE_KEY = 'ietf_github_access_token'
const GITHUB_JOB_STORAGE_KEY = 'ietf_github_last_job'

const DEFAULT_GITHUB_FORM: GithubFormState = {
  wg: '',
  repo: '',
  includeIssues: true,
  includePRs: true,
  includeClosed: true,
  githubToken: '',
  loader: 'graphql',
}

const IETF_IMPORT_FILE_LIMIT = 500

const trimOrUndefined = (value: string) => {
  const trimmed = value.trim()
  return trimmed || undefined
}

const normalizeRfcToken = (value: string): string => {
  const trimmed = value.trim()
  if (!trimmed)
    return ''
  if (/^\d+$/.test(trimmed))
    return `RFC${trimmed}`
  if (/^rfc\d+$/i.test(trimmed))
    return `RFC${trimmed.slice(3)}`
  return trimmed
}

const getCurrentRfcQuery = (value: string): string => {
  return value.trim()
}

const isNoResultsMessage = (message: string) => /No messages matched/i.test(message)

const isFileTooLargeMessage = (message: string) => /file too large|file size exceeded|maximum is \d+mb|filetoolarge|max(?:imum)? file size/i.test(message)

const extractErrorMessage = async (error: unknown): Promise<string> => {
  if (error instanceof Error && error.message)
    return error.message

  if (typeof error === 'string')
    return error

  if (error instanceof Response) {
    try {
      const data = await error.clone().json() as { message?: unknown, detail?: unknown }
      if (typeof data.message === 'string' && data.message)
        return data.message
      if (typeof data.detail === 'string' && data.detail)
        return data.detail
    }
    catch {
      try {
        const text = await error.clone().text()
        if (text)
          return text
      }
      catch {
      }
    }
  }

  if (error && typeof error === 'object') {
    const maybeError = error as { message?: unknown, detail?: unknown }
    if (typeof maybeError.message === 'string' && maybeError.message)
      return maybeError.message
    if (typeof maybeError.detail === 'string' && maybeError.detail)
      return maybeError.detail
  }

  return ''
}

const toFileItem = (file: UploadedConsoleFile): FileItem => ({
  fileID: `ietf-${file.id}`,
  file: file as unknown as CustomFile,
  progress: PROGRESS_COMPLETE,
})

function IetfImporter({
  fileList,
  onFileListUpdate,
  onSelectedYearsChange,
  disabled = false,
  supportBatchUpload,
}: IetfImporterProps) {
  const { t } = useTranslation()
  const { data: fileUploadConfig } = useFileUploadConfig()

  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<IetfImportMode>('mailarchive')
  const [githubForm, setGithubForm] = useState<GithubFormState>(() => ({
    ...DEFAULT_GITHUB_FORM,
    githubToken: (typeof window !== 'undefined' ? localStorage.getItem(GITHUB_TOKEN_STORAGE_KEY) : null) ?? '',
  }))
  const [showWgDropdown, setShowWgDropdown] = useState(false)
  const [showDraftDropdown, setShowDraftDropdown] = useState(false)
  const [wgFilter, setWgFilter] = useState('')
  const [draftFilter, setDraftFilter] = useState('')
  const wgDropdownRef = useRef<HTMLDivElement>(null)
  const draftDropdownRef = useRef<HTMLDivElement>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [mailarchiveForm, setMailarchiveForm] = useState(DEFAULT_MAILARCHIVE_FORM)
  const [rfcForm, setRfcForm] = useState(DEFAULT_RFC_FORM)
  const [availableMailingLists, setAvailableMailingLists] = useState<string[]>([])
  const [showMailingListDropdown, setShowMailingListDropdown] = useState(false)
  const [wgDocuments, setWgDocuments] = useState<IetfWgDocument[]>([])
  const [wgDocumentsLoading, setWgDocumentsLoading] = useState(false)
  const [selectedWgDoc, setSelectedWgDoc] = useState<IetfWgDocument | null>(null)
  const [showWgDocDropdown, setShowWgDocDropdown] = useState(false)
  const [wgDocFilter, setWgDocFilter] = useState('')
  const [wgDocRelations, setWgDocRelations] = useState<IetfDraftRelations | null>(null)
  const [wgDocRelationsLoading, setWgDocRelationsLoading] = useState(false)
  const keywordAutoFilledRef = useRef(false)
  const wgDocDropdownRef = useRef<HTMLDivElement>(null)
  const [rfcSuggestions, setRfcSuggestions] = useState<IetfRfcSuggestion[]>([])
  const [showRfcSuggestions, setShowRfcSuggestions] = useState(false)
  const [selectedRfc, setSelectedRfc] = useState<IetfRfcSuggestion | null>(null)
  const [importType, setImportType] = useState<'rfc' | 'draft'>('rfc')
  const [selectedDraftVersion, setSelectedDraftVersion] = useState<number | null>(null)
  const [preprocessResults, setPreprocessResults] = useState<IetfYearPreprocessResult[] | null>(null)
  const [selectedPreprocessYears, setSelectedPreprocessYears] = useState<number[]>([])
  const [availableFormats, setAvailableFormats] = useState<IetfFormatInfo[]>([])
  const [selectedFormat, setSelectedFormat] = useState<string>('txt')
  const [isFetchingFormats, setIsFetchingFormats] = useState(false)
  const [syncStatus, setSyncStatus] = useState<IetfSyncStatus | 'checking' | null>(null)
  const [syncMessageCount, setSyncMessageCount] = useState<number>(0)
  const [isTriggeringSync, setIsTriggeringSync] = useState(false)
  const syncPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [githubJobId, setGithubJobId] = useState<string | null>(null)
  const [githubJobState, setGithubJobState] = useState<GithubJobState | null>(null)
  const githubPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const mailingListDropdownRef = useRef<HTMLDivElement>(null)
  const rfcDropdownRef = useRef<HTMLDivElement>(null)
  const dropdownRef = mailingListDropdownRef
  const selectedPreprocessResults = useMemo(
    () => preprocessResults?.filter(item => selectedPreprocessYears.includes(item.year)) ?? [],
    [preprocessResults, selectedPreprocessYears],
  )

  useEffect(() => {
    onSelectedYearsChange?.(selectedPreprocessResults)
  }, [onSelectedYearsChange, selectedPreprocessResults])

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (mailingListDropdownRef.current && !mailingListDropdownRef.current.contains(event.target as Node))
        setShowMailingListDropdown(false)
      if (wgDocDropdownRef.current && !wgDocDropdownRef.current.contains(event.target as Node))
        setShowWgDocDropdown(false)
      if (rfcDropdownRef.current && !rfcDropdownRef.current.contains(event.target as Node))
        setShowRfcSuggestions(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  useEffect(() => {
    const loadMailingLists = async () => {
      try {
        const result = await fetchIetfMailingLists()
        setAvailableMailingLists(result.lists)
      }
      catch (error) {
        console.error('Failed to fetch mailing lists:', error)
      }
    }
    if (open)
      loadMailingLists()
  }, [open])

  // Fetch I-D/RFC documents for the selected WG
  useEffect(() => {
    const wg = mailarchiveForm.mailingList.trim()
    startTransition(() => {
      setSelectedWgDoc(null)
      setWgDocuments([])
      setWgDocFilter('')
      setWgDocRelations(null)
      setWgDocRelationsLoading(false)
    })
    if (!wg || mode !== 'mailarchive')
      return

    let cancelled = false
    startTransition(() => setWgDocumentsLoading(true))

    fetchIetfWgDocuments(wg)
      .then((result) => {
        if (!cancelled)
          setWgDocuments(result.documents)
      })
      .catch(() => {
        if (!cancelled)
          setWgDocuments([])
      })
      .finally(() => {
        if (!cancelled)
          setWgDocumentsLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [mailarchiveForm.mailingList, mode])

  // Check sync status whenever the mailing list selection changes
  useEffect(() => {
    if (syncPollRef.current) {
      clearInterval(syncPollRef.current)
      syncPollRef.current = null
    }

    let cancelled = false

    const run = async () => {
      const listName = mailarchiveForm.mailingList.trim()
      if (!listName || mode !== 'mailarchive') {
        setSyncStatus(null)
        setSyncMessageCount(0)
        return
      }

      setSyncStatus('checking')
      try {
        const result = await fetchIetfMailarchiveSyncStatus(listName)
        if (cancelled)
          return
        setSyncStatus(result.status)
        setSyncMessageCount(result.message_count ?? 0)
        if (result.status === 'ready' && syncPollRef.current) {
          clearInterval(syncPollRef.current)
          syncPollRef.current = null
        }
      }
      catch {
        if (!cancelled)
          setSyncStatus(null)
      }
    }

    run()

    return () => {
      cancelled = true
      if (syncPollRef.current) {
        clearInterval(syncPollRef.current)
        syncPollRef.current = null
      }
    }
  }, [mailarchiveForm.mailingList, mode])

  // Start polling when status is 'syncing'
  useEffect(() => {
    if (syncStatus !== 'syncing') {
      if (syncPollRef.current) {
        clearInterval(syncPollRef.current)
        syncPollRef.current = null
      }
      return
    }

    const listName = mailarchiveForm.mailingList.trim()
    if (!listName)
      return

    if (syncPollRef.current)
      return // already polling

    syncPollRef.current = setInterval(async () => {
      try {
        const result = await fetchIetfMailarchiveSyncStatus(listName)
        setSyncStatus(result.status)
        setSyncMessageCount(result.message_count ?? 0)
        if (result.status === 'ready' && syncPollRef.current) {
          clearInterval(syncPollRef.current)
          syncPollRef.current = null
        }
      }
      catch {
        // ignore transient errors during polling
      }
    }, 10000)

    return () => {
      if (syncPollRef.current) {
        clearInterval(syncPollRef.current)
        syncPollRef.current = null
      }
    }
  // eslint-disable-next-line react/exhaustive-deps
  }, [syncStatus])

  useEffect(() => {
    if (!open || mode !== 'rfcs')
      return

    const query = getCurrentRfcQuery(rfcForm.rfcId)

    const timer = window.setTimeout(async () => {
      try {
        const result = await fetchIetfRfcSuggestions({ query, limit: 20 })
        setRfcSuggestions(result.suggestions)
      }
      catch {
        setRfcSuggestions([])
      }
    }, 250)

    return () => window.clearTimeout(timer)
  }, [mode, open, rfcForm.rfcId])

  const availableDraftVersions = useMemo(() => {
    if (!selectedRfc)
      return []
    if (selectedRfc.draft_versions?.length)
      return [...selectedRfc.draft_versions].sort((a, b) => a.version - b.version)
    if (selectedRfc.draft_base_name && selectedRfc.draft_max_version != null) {
      return Array.from({ length: selectedRfc.draft_max_version + 1 }, (_, version) => ({
        name: `${selectedRfc.draft_base_name}-${String(version).padStart(2, '0')}`,
        version,
        since: null,
        until: null,
      }))
    }
    return []
  }, [selectedRfc])

  const selectedDraftMetadata = useMemo(
    () => availableDraftVersions.find(item => item.version === selectedDraftVersion) ?? null,
    [availableDraftVersions, selectedDraftVersion],
  )

  useEffect(() => {
    if (!selectedRfc)
      return

    const fetchFormats = async () => {
      setIsFetchingFormats(true)
      setAvailableFormats([])
      setSelectedFormat('txt')
      try {
        if (importType === 'draft' && selectedDraftMetadata) {
          const result = await fetchIetfDraftFormats(selectedDraftMetadata.name)
          const formats = result.formats.length ? result.formats : [{ key: 'txt', label: 'Plain Text', mime_type: 'text/plain' }]
          setAvailableFormats(formats)
          setSelectedFormat(formats[0].key)
        }
        else if (importType === 'rfc') {
          const result = await fetchIetfRfcFormats(selectedRfc.doc_id)
          const formats = result.formats.length ? result.formats : [{ key: 'txt', label: 'Plain Text', mime_type: 'text/plain' }]
          setAvailableFormats(formats)
          setSelectedFormat(formats[0].key)
        }
      }
      catch {
        toast.error(t('stepOne.ietf.docFormatFetchError', { ns: 'datasetCreation' }))
        setAvailableFormats([{ key: 'txt', label: 'Plain Text', mime_type: 'text/plain' }])
        setSelectedFormat('txt')
      }
      finally {
        setIsFetchingFormats(false)
      }
    }

    fetchFormats()
  }, [selectedRfc, importType, selectedDraftMetadata, t])

  const selectedMetadata = useMemo(() => {
    if (!selectedRfc)
      return null
    if (importType === 'draft' && selectedDraftMetadata) {
      return {
        since: selectedDraftMetadata.since,
        until: selectedDraftMetadata.until,
        stream: selectedRfc.stream,
        wg: selectedRfc.wg,
        status: selectedRfc.status,
        area: selectedRfc.area,
      }
    }

    return {
      since: selectedRfc.since,
      until: null,
      stream: selectedRfc.stream,
      wg: selectedRfc.wg,
      status: selectedRfc.status,
      area: selectedRfc.area,
    }
  }, [importType, selectedDraftMetadata, selectedRfc])

  const fileLimit = useMemo(() => {
    if (!supportBatchUpload)
      return 1
    return Math.max(fileUploadConfig?.file_upload_limit ?? 5, IETF_IMPORT_FILE_LIMIT)
  }, [fileUploadConfig?.file_upload_limit, supportBatchUpload])

  const isLimitReached = fileList.length >= fileLimit
  const openDisabled = disabled || isLimitReached

  const closeDialog = () => {
    if (isSubmitting)
      return
    setOpen(false)
  }

  const handleImportedFile = (file: UploadedConsoleFile) => {
    onFileListUpdate([...fileList, toFileItem(file)])
    toast.success(t('stepOne.ietf.importSuccess', { ns: 'datasetCreation', fileName: file.name }))
    setOpen(false)
  }

  // Poll GitHub background job status every 2 s while running/pending
  useEffect(() => {
    if (!githubJobId)
      return
    const poll = async () => {
      try {
        const state = await fetchGithubImportStatus(githubJobId)
        setGithubJobState(state)
        if (state.status === 'done') {
          if (githubPollRef.current) {
            clearInterval(githubPollRef.current)
            githubPollRef.current = null
          }
          localStorage.removeItem(GITHUB_JOB_STORAGE_KEY)
          setIsSubmitting(false)
          if (state.file)
            handleImportedFile(state.file)
        }
        else if (state.status === 'error') {
          if (githubPollRef.current) {
            clearInterval(githubPollRef.current)
            githubPollRef.current = null
          }
          if (!state.resumable)
            localStorage.removeItem(GITHUB_JOB_STORAGE_KEY)
          setIsSubmitting(false)
        }
      }
      catch {
        // ignore transient poll errors
      }
    }
    if (githubPollRef.current)
      clearInterval(githubPollRef.current)
    poll()
    githubPollRef.current = setInterval(poll, 2000)
    return () => {
      if (githubPollRef.current) {
        clearInterval(githubPollRef.current)
        githubPollRef.current = null
      }
    }
  // eslint-disable-next-line react/exhaustive-deps
  }, [githubJobId])

  // Auto-reconnect: when the dialog opens on the GitHub tab, check localStorage for a
  // previously started job and resume polling it if it is still active.
  useEffect(() => {
    if (!open || mode !== 'github' || githubJobId)
      return
    try {
      const saved = localStorage.getItem(GITHUB_JOB_STORAGE_KEY)
      if (!saved)
        return
      const { job_id } = JSON.parse(saved) as { job_id: string }
      if (job_id) {
        startTransition(() => {
          setGithubJobId(job_id)
          setIsSubmitting(true)
        })
      }
    }
    catch {
      // ignore corrupt localStorage entry
    }
  // eslint-disable-next-line react/exhaustive-deps
  }, [open, mode])

  const handleRequestError = async (error: unknown) => {
    const fallbackMessage = t('stepOne.ietf.importFailed', { ns: 'datasetCreation' })
    const parsedMessage = await extractErrorMessage(error)
    if (isNoResultsMessage(parsedMessage)) {
      toast.error(t('stepOne.ietf.importNoResults', { ns: 'datasetCreation' }))
      return
    }
    if (isFileTooLargeMessage(parsedMessage)) {
      toast.error(t('stepOne.ietf.importFileTooLarge', { ns: 'datasetCreation' }))
      return
    }
    const errorMessage = parsedMessage || fallbackMessage
    toast.error(errorMessage)
  }

  const handleWgDocSelect = (doc: IetfWgDocument) => {
    setSelectedWgDoc(doc)
    setWgDocFilter('')
    setShowWgDocDropdown(false)
    setWgDocRelations(null)

    // Immediately auto-fill with the draft name (relations not yet loaded)
    keywordAutoFilledRef.current = true
    setMailarchiveForm(prev => ({ ...prev, subject: doc.name }))

    // Fetch predecessor relations and update keyword when loaded
    setWgDocRelationsLoading(true)
    fetchIetfDraftRelations(doc.name)
      .then((result) => {
        setWgDocRelations(result)
        if (keywordAutoFilledRef.current) {
          const parts: string[] = [doc.name, ...result.predecessors]
          if (doc.rfc_number)
            parts.push(`RFC${doc.rfc_number}`)
          setMailarchiveForm(prev => ({ ...prev, subject: parts.join(' OR ') }))
        }
      })
      .catch(() => setWgDocRelations({ predecessors: [] }))
      .finally(() => setWgDocRelationsLoading(false))
  }

  const handleStartSync = async () => {
    const listName = mailarchiveForm.mailingList.trim()
    if (!listName)
      return
    setIsTriggeringSync(true)
    try {
      await triggerIetfMailarchiveSync(listName)
      setSyncStatus('syncing')
    }
    catch (error) {
      const msg = await extractErrorMessage(error)
      toast.error(msg || t('stepOne.ietf.importFailed', { ns: 'datasetCreation' }))
    }
    finally {
      setIsTriggeringSync(false)
    }
  }

  const submitMailarchiveImport = async () => {
    const mailingList = mailarchiveForm.mailingList.trim()
    if (!mailingList) {
      toast.error(t('stepOne.ietf.validation.mailarchiveRequired', { ns: 'datasetCreation' }))
      return
    }

    const sentAfter = mailarchiveForm.sentAfter.trim() || DEFAULT_MAILARCHIVE_FORM.sentAfter
    const sentBefore = mailarchiveForm.sentBefore.trim() || DEFAULT_MAILARCHIVE_FORM.sentBefore

    setPreprocessResults(null)
    setSelectedPreprocessYears([])
    setIsSubmitting(true)
    try {
      const result: IetfPreprocessResponse = await downloadAndPreprocessMailarchiveYears({
        mailing_list: mailingList,
        sent_after: sentAfter,
        sent_before: sentBefore,
        from_addr: trimOrUndefined(mailarchiveForm.fromAddr),
        subject: trimOrUndefined(mailarchiveForm.subject),
        search_in: mailarchiveForm.searchIn,
      })
      setPreprocessResults(result.years)
      setOpen(false)
    }
    catch (error) {
      await handleRequestError(error)
    }
    finally {
      setIsSubmitting(false)
    }
  }

  const submitRfcs = async () => {
    if (importType === 'draft') {
      if (!selectedRfc?.draft_base_name || selectedDraftVersion == null) {
        toast.error(t('stepOne.ietf.validation.draftVersionRequired', { ns: 'datasetCreation' }))
        return
      }
      const draftNames = [`${selectedRfc.draft_base_name}-${String(selectedDraftVersion).padStart(2, '0')}`]
      const payload: IetfDraftImportRequest = { draft_names: draftNames, format: selectedFormat }
      setIsSubmitting(true)
      try {
        const file = await importIetfDraftFile(payload)
        handleImportedFile(file)
      }
      catch (error) {
        await handleRequestError(error)
      }
      finally {
        setIsSubmitting(false)
      }
      return
    }

    const normalizedRfcId = selectedRfc?.doc_id ?? normalizeRfcToken(rfcForm.rfcId)

    const payload: IetfRfcImportRequest = {
      rfc_ids: normalizedRfcId ? [normalizedRfcId] : [],
      limit: 20,
      format: selectedFormat,
    }

    if (!payload.rfc_ids.length) {
      toast.error(t('stepOne.ietf.validation.rfcQueryRequired', { ns: 'datasetCreation' }))
      return
    }

    setIsSubmitting(true)
    try {
      const file = await importIetfRfcFile(payload)
      handleImportedFile(file)
    }
    catch (error) {
      await handleRequestError(error)
    }
    finally {
      setIsSubmitting(false)
    }
  }

  const submitGithub = async () => {
    const repo = githubForm.repo.trim()
    if (!repo || !repo.includes('/')) {
      toast.error(t('stepOne.ietf.validation.githubRepoRequired', { ns: 'datasetCreation' }))
      return
    }
    if (!githubForm.includeIssues && !githubForm.includePRs) {
      toast.error(t('stepOne.ietf.validation.githubAtLeastOne', { ns: 'datasetCreation' }))
      return
    }
    const token = githubForm.githubToken.trim()
    if (!token) {
      toast.error(t('stepOne.ietf.validation.githubTokenRequired', { ns: 'datasetCreation' }))
      return
    }
    localStorage.setItem(GITHUB_TOKEN_STORAGE_KEY, token)
    setIsSubmitting(true)
    setGithubJobId(null)
    setGithubJobState(null)
    try {
      const { job_id } = await startGithubImport({
        repo,
        include_issues: githubForm.includeIssues,
        include_prs: githubForm.includePRs,
        include_closed: githubForm.includeClosed,
        github_token: token,
        loader: githubForm.loader,
      })
      setGithubJobId(job_id)
      localStorage.setItem(GITHUB_JOB_STORAGE_KEY, JSON.stringify({ job_id, repo, loader: githubForm.loader }))
      // isSubmitting stays true until the polling effect resolves the job
    }
    catch (error) {
      await handleRequestError(error)
      setIsSubmitting(false)
    }
  }

  const handleGithubResume = async () => {
    if (!githubJobId)
      return
    const token = githubForm.githubToken.trim()
    if (!token) {
      toast.error(t('stepOne.ietf.validation.githubTokenRequired', { ns: 'datasetCreation' }))
      return
    }
    setIsSubmitting(true)
    setGithubJobState(null)
    try {
      const { job_id } = await resumeGithubImport(githubJobId, token)
      setGithubJobId(job_id)
      localStorage.setItem(GITHUB_JOB_STORAGE_KEY, JSON.stringify({ job_id, repo: githubForm.repo, loader: githubForm.loader }))
    }
    catch (error) {
      await handleRequestError(error)
      setIsSubmitting(false)
    }
  }

  return (
    <>
      <div className="mb-5 flex max-w-[640px] items-center justify-between gap-4 rounded-2xl border border-divider-subtle bg-background-default-subtle px-4 py-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold leading-5 text-text-secondary">
            {t('stepOne.ietf.title', { ns: 'datasetCreation' })}
          </div>
          <div className="mt-1 text-xs leading-4 text-text-tertiary">
            {t('stepOne.ietf.description', { ns: 'datasetCreation' })}
          </div>
          {isLimitReached && (
            <div className="mt-1 text-xs leading-4 text-text-warning">
              {t('stepOne.ietf.limitReached', { ns: 'datasetCreation' })}
            </div>
          )}
        </div>
        <Button
          variant="secondary"
          onClick={() => setOpen(true)}
          disabled={openDisabled}
          data-testid="ietf-import-open"
        >
          {t('stepOne.ietf.open', { ns: 'datasetCreation' })}
        </Button>
      </div>

      {preprocessResults && preprocessResults.length > 0 && (
        <div className="mb-5 max-w-[720px] rounded-2xl border border-divider-subtle bg-background-default-subtle p-4" data-testid="ietf-preprocess-results-panel">
          <div className="text-sm font-semibold leading-5 text-text-secondary">
            {t('stepOne.ietf.preprocessResults', { ns: 'datasetCreation' })}
          </div>
          <div className="mt-1 text-xs leading-4 text-text-tertiary">
            {t('stepOne.ietf.preprocessResultsHint', { ns: 'datasetCreation' })}
          </div>
          <div className="mt-1 text-xs leading-4 text-text-tertiary">
            {t('stepOne.ietf.preprocessSelectedCount', {
              ns: 'datasetCreation',
              count: selectedPreprocessYears.length,
            })}
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            {preprocessResults.filter(item => item.total_emails > 0).map(item => (
              <button
                key={item.year}
                type="button"
                className={`rounded-xl border px-3 py-2 text-left text-xs transition-colors ${selectedPreprocessYears.includes(item.year) ? 'border-primary-600 bg-primary-50 text-text-primary' : 'border-divider-subtle bg-background-default text-text-secondary hover:bg-background-default-hover'}`}
                onClick={() => {
                  setSelectedPreprocessYears((prev) => {
                    if (prev.includes(item.year))
                      return prev.filter(year => year !== item.year)
                    return [...prev, item.year].sort((a, b) => a - b)
                  })
                }}
                data-testid={`ietf-preprocess-year-${item.year}`}
              >
                {t('stepOne.ietf.preprocessYearSummary', {
                  ns: 'datasetCreation',
                  year: item.year,
                  count: item.total_emails,
                  discarded: item.discarded_emails,
                })}
              </button>
            ))}
          </div>
          <div className="mt-3 text-xs leading-5 text-text-tertiary">
            {t('stepOne.ietf.discardedExplanation', { ns: 'datasetCreation' })}
          </div>
        </div>
      )}

      <Dialog
        open={open}
        onOpenChange={nextOpen => nextOpen ? setOpen(true) : closeDialog()}
      >
        <DialogContent className="w-[720px] max-w-[calc(100vw-2rem)] p-0">
          <div className="border-b border-divider-subtle px-6 py-5">
            <DialogCloseButton data-testid="ietf-import-close" className="right-5 top-5" />
            <DialogTitle className="pr-10 text-text-primary title-2xl-semi-bold">
              {t('stepOne.ietf.open', { ns: 'datasetCreation' })}
            </DialogTitle>
            <DialogDescription className="mt-2 pr-10 text-sm leading-5 text-text-tertiary">
              {t('stepOne.ietf.modeDescription', { ns: 'datasetCreation' })}
            </DialogDescription>
          </div>

          <div className="space-y-5 px-6 py-5">
            <div className="flex gap-2">
              <Button
                variant={mode === 'mailarchive' ? 'primary' : 'secondary'}
                onClick={() => setMode('mailarchive')}
              >
                <span className="flex items-center gap-1.5">
                  <span className="i-ri-mail-download-line size-4" />
                  {t('stepOne.ietf.mailarchive', { ns: 'datasetCreation' })}
                </span>
              </Button>
              <Button
                variant={mode === 'rfcs' ? 'primary' : 'secondary'}
                onClick={() => {
                  setMode('rfcs')
                  setShowRfcSuggestions(true)
                }}
              >
                {' '}
                <span className="flex items-center gap-1.5">
                  <span className="i-ri-article-line size-4" />
                  {t('stepOne.ietf.rfcs', { ns: 'datasetCreation' })}
                </span>
              </Button>
              <Button
                variant={mode === 'github' ? 'primary' : 'secondary'}
                onClick={() => setMode('github')}
              >
                <span className="flex items-center gap-1.5">
                  <span className="i-ri-github-line size-4" />
                  {t('stepOne.ietf.github', { ns: 'datasetCreation' })}
                </span>
              </Button>
            </div>

            {mode === 'mailarchive' && (
              <div className="space-y-4" data-testid="ietf-mailarchive-form">
                <div className="grid grid-cols-2 gap-4">
                  <div className="col-span-2">
                    <div className="mb-1 text-xs font-medium text-text-secondary">
                      {t('stepOne.ietf.mailingList', { ns: 'datasetCreation' })}
                    </div>
                    <div ref={dropdownRef} className="relative">
                      <Input
                        value={mailarchiveForm.mailingList}
                        onChange={(e) => {
                          setMailarchiveForm(prev => ({ ...prev, mailingList: e.target.value }))
                          setShowMailingListDropdown(true)
                        }}
                        onFocus={() => setShowMailingListDropdown(true)}
                        placeholder={t('stepOne.ietf.mailarchiveHint', { ns: 'datasetCreation' }) || ''}
                      />
                      {showMailingListDropdown && availableMailingLists.length > 0 && (
                        <div className="absolute left-0 right-0 top-full z-10 mt-1 max-h-48 overflow-y-auto rounded-lg border border-components-panel-border bg-components-panel-bg shadow-lg">
                          {availableMailingLists
                            .filter(list => list.toLowerCase().includes(mailarchiveForm.mailingList.toLowerCase()))
                            .map(list => (
                              <div
                                key={list}
                                onClick={() => {
                                  setMailarchiveForm(prev => ({ ...prev, mailingList: list }))
                                  setShowMailingListDropdown(false)
                                }}
                                className="cursor-pointer px-3 py-2 text-sm text-text-primary hover:bg-components-panel-border"
                              >
                                {list}
                              </div>
                            ))}
                        </div>
                      )}
                    </div>
                  </div>

                  {/* I-D / RFC selector — shown once a WG is selected */}
                  {mailarchiveForm.mailingList.trim() && (
                    <div className="col-span-2">
                      <div className="mb-1 text-xs font-medium text-text-secondary">
                        {t('stepOne.ietf.wgDoc', { ns: 'datasetCreation' })}
                      </div>
                      <div ref={wgDocDropdownRef} className="relative">
                        <Input
                          value={wgDocFilter}
                          onChange={(e) => {
                            setWgDocFilter(e.target.value)
                            setShowWgDocDropdown(true)
                          }}
                          onFocus={() => setShowWgDocDropdown(true)}
                          placeholder={
                            wgDocumentsLoading
                              ? (t('stepOne.ietf.wgDocLoading', { ns: 'datasetCreation' }) || '')
                              : (t('stepOne.ietf.wgDocPlaceholder', { ns: 'datasetCreation' }) || '')
                          }
                          readOnly={wgDocumentsLoading}
                        />
                        {showWgDocDropdown && wgDocuments.length > 0 && (
                          <div className="absolute left-0 right-0 top-full z-10 mt-1 max-h-56 overflow-y-auto rounded-lg border border-components-panel-border bg-components-panel-bg shadow-lg">
                            {wgDocuments
                              .filter((doc) => {
                                const q = wgDocFilter.toLowerCase()
                                if (!q)
                                  return true
                                return doc.name.toLowerCase().includes(q) || doc.title.toLowerCase().includes(q)
                              })
                              .map(doc => (
                                <div
                                  key={doc.name}
                                  onClick={() => handleWgDocSelect(doc)}
                                  className="cursor-pointer px-3 py-2 text-sm hover:bg-components-panel-border"
                                >
                                  <div className="font-medium text-text-primary">{doc.name}</div>
                                  <div className="truncate text-xs text-text-tertiary">{doc.title}</div>
                                </div>
                              ))}
                          </div>
                        )}
                        {/* 3-field info card shown after selection */}
                        {selectedWgDoc && !wgDocFilter && (
                          <div className="mt-2 grid grid-cols-3 gap-3 rounded-lg border border-divider-subtle bg-background-section-burn p-2">
                            <div className="min-w-0">
                              <div className="text-[10px] text-text-tertiary">
                                {t('stepOne.ietf.wgDocSelectedDraft', { ns: 'datasetCreation' })}
                              </div>
                              <div title={selectedWgDoc.name} className="truncate text-xs font-medium text-text-secondary">
                                {selectedWgDoc.name}
                              </div>
                            </div>
                            <div className="min-w-0">
                              <div className="text-[10px] text-text-tertiary">
                                {t('stepOne.ietf.wgDocWas', { ns: 'datasetCreation' })}
                              </div>
                              {wgDocRelationsLoading
                                ? <div className="text-xs font-medium text-text-secondary">…</div>
                                : (wgDocRelations?.predecessors.length
                                    ? wgDocRelations.predecessors.map(p => (
                                        <div key={p} title={p} className="truncate text-xs font-medium text-text-secondary">{p}</div>
                                      ))
                                    : <div className="text-xs font-medium text-text-secondary">—</div>
                                  )}
                            </div>
                            <div className="min-w-0">
                              <div className="text-[10px] text-text-tertiary">
                                {t('stepOne.ietf.wgDocPublishedAs', { ns: 'datasetCreation' })}
                              </div>
                              <div className="text-xs font-medium text-text-secondary">
                                {selectedWgDoc.rfc_number ? `RFC ${selectedWgDoc.rfc_number}` : '—'}
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                  <div>
                    <div className="mb-1 text-xs font-medium text-text-secondary">
                      {t('stepOne.ietf.sentAfter', { ns: 'datasetCreation' })}
                    </div>
                    <Input
                      value={mailarchiveForm.sentAfter}
                      onChange={e => setMailarchiveForm(prev => ({ ...prev, sentAfter: e.target.value }))}
                    />
                  </div>
                  <div>
                    <div className="mb-1 text-xs font-medium text-text-secondary">
                      {t('stepOne.ietf.sentBefore', { ns: 'datasetCreation' })}
                    </div>
                    <Input
                      value={mailarchiveForm.sentBefore}
                      onChange={e => setMailarchiveForm(prev => ({ ...prev, sentBefore: e.target.value }))}
                    />
                  </div>
                  <div>
                    <div className="mb-1 text-xs font-medium text-text-secondary">
                      {t('stepOne.ietf.fromAddr', { ns: 'datasetCreation' })}
                    </div>
                    <Input
                      value={mailarchiveForm.fromAddr}
                      onChange={e => setMailarchiveForm(prev => ({ ...prev, fromAddr: e.target.value }))}
                    />
                  </div>
                  <div className="col-span-2">
                    <div className="mb-1 text-xs font-medium text-text-secondary">
                      {t('stepOne.ietf.subject', { ns: 'datasetCreation' })}
                    </div>
                    <Input
                      value={mailarchiveForm.subject}
                      onChange={(e) => {
                        keywordAutoFilledRef.current = false
                        setMailarchiveForm(prev => ({ ...prev, subject: e.target.value }))
                      }}
                    />
                    <div className="mt-2 flex items-center gap-4">
                      <span className="text-xs text-text-tertiary">
                        {t('stepOne.ietf.searchIn', { ns: 'datasetCreation' })}
                        :
                      </span>
                      {(['subject', 'body'] as const).map(scope => (
                        <label key={scope} className="flex cursor-pointer items-center gap-1.5 text-xs text-text-secondary">
                          <input
                            type="checkbox"
                            checked={mailarchiveForm.searchIn.includes(scope)}
                            onChange={(e) => {
                              setMailarchiveForm((prev) => {
                                const next = e.target.checked
                                  ? [...prev.searchIn, scope]
                                  : prev.searchIn.filter(s => s !== scope)
                                // require at least one option
                                return { ...prev, searchIn: next.length ? next : prev.searchIn }
                              })
                            }}
                          />
                          {scope === 'subject'
                            ? t('stepOne.ietf.searchInSubject', { ns: 'datasetCreation' })
                            : t('stepOne.ietf.searchInBody', { ns: 'datasetCreation' })}
                        </label>
                      ))}
                    </div>
                  </div>
                </div>
                {mailarchiveForm.mailingList.trim() && (
                  <div
                    className={`flex items-start gap-3 rounded-xl border px-3 py-2.5 text-xs ${
                      syncStatus === 'ready'
                        ? 'border-state-success-border bg-state-success-hover text-text-success'
                        : syncStatus === 'syncing'
                          ? 'border-state-warning-border bg-state-warning-hover text-text-warning'
                          : syncStatus === 'not_found'
                            ? 'border-state-destructive-border bg-state-destructive-hover text-text-destructive'
                            : 'border-divider-subtle bg-background-default-subtle text-text-tertiary'
                    }`}
                    data-testid="ietf-sync-status-banner"
                  >
                    <div className="flex-1">
                      {syncStatus === 'checking' && (
                        <span>{t('stepOne.ietf.syncCheckingStatus', { ns: 'datasetCreation' })}</span>
                      )}
                      {syncStatus === 'ready' && (
                        <span>{t('stepOne.ietf.syncReady', { ns: 'datasetCreation', count: syncMessageCount })}</span>
                      )}
                      {syncStatus === 'syncing' && (
                        <div>
                          <div className="font-medium">{t('stepOne.ietf.syncSyncing', { ns: 'datasetCreation' })}</div>
                          <div className="mt-0.5 text-text-tertiary">{t('stepOne.ietf.syncSyncingHint', { ns: 'datasetCreation' })}</div>
                        </div>
                      )}
                      {syncStatus === 'not_found' && (
                        <div>
                          <div className="font-medium">{t('stepOne.ietf.syncRequired', { ns: 'datasetCreation' })}</div>
                          <div className="mt-0.5 text-text-tertiary">{t('stepOne.ietf.syncRequiredHint', { ns: 'datasetCreation' })}</div>
                        </div>
                      )}
                    </div>
                    {syncStatus === 'not_found' && (
                      <Button
                        variant="secondary"
                        size="small"
                        loading={isTriggeringSync}
                        onClick={handleStartSync}
                        data-testid="ietf-sync-start-button"
                      >
                        {t('stepOne.ietf.syncStartButton', { ns: 'datasetCreation' })}
                      </Button>
                    )}
                  </div>
                )}
                <div className="text-xs text-text-tertiary">
                  {t('stepOne.ietf.preprocessImportHint', { ns: 'datasetCreation' })}
                </div>
              </div>
            )}

            {mode === 'rfcs' && (
              <div className="space-y-4" data-testid="ietf-rfc-form">
                <div className="text-xs leading-4 text-text-tertiary">
                  {t('stepOne.ietf.rfcFiltersHint', { ns: 'datasetCreation' })}
                </div>
                <div className="space-y-4">
                  <div>
                    <div className="mb-1 text-xs font-medium text-text-secondary">
                      {t('stepOne.ietf.rfcId', { ns: 'datasetCreation' })}
                    </div>
                    <div ref={rfcDropdownRef} className="relative">
                      <Input
                        value={rfcForm.rfcId}
                        onChange={(e) => {
                          setRfcForm({ rfcId: e.target.value })
                          setSelectedRfc(null)
                          setImportType('rfc')
                          setSelectedDraftVersion(null)
                          setShowRfcSuggestions(true)
                        }}
                        onFocus={() => setShowRfcSuggestions(true)}
                        placeholder={t('stepOne.ietf.rfcIdHint', { ns: 'datasetCreation' }) || ''}
                      />
                      {showRfcSuggestions && rfcSuggestions.length > 0 && (
                        <div className="absolute left-0 right-0 top-full z-10 mt-1 max-h-56 overflow-y-auto rounded-lg border border-components-panel-border bg-components-panel-bg shadow-lg">
                          {rfcSuggestions.map(item => (
                            <button
                              key={item.doc_id}
                              type="button"
                              className="block w-full px-3 py-2 text-left hover:bg-components-panel-border"
                              onClick={() => {
                                setRfcForm({ rfcId: item.doc_id })
                                setSelectedRfc(item)
                                setImportType('rfc')
                                const latestVersion = item.draft_versions?.length
                                  ? item.draft_versions[item.draft_versions.length - 1].version
                                  : item.draft_max_version ?? null
                                setSelectedDraftVersion(latestVersion)
                                setShowRfcSuggestions(false)
                              }}
                            >
                              <div className="text-sm text-text-primary">{item.doc_id}</div>
                              <div className="text-xs text-text-tertiary">{item.title}</div>
                              {item.draft_names.length > 0 && (
                                <div className="mt-0.5 text-xs text-text-tertiary">
                                  {item.draft_names.join(', ')}
                                </div>
                              )}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </div>

                {selectedRfc && (
                  <div className="space-y-4 rounded-2xl border border-divider-subtle bg-background-default-subtle p-4">
                    <div className="space-y-1">
                      <div className="text-xs font-medium text-text-tertiary">
                        {selectedRfc.doc_id}
                      </div>
                      <div className="text-base font-semibold leading-6 text-text-primary">
                        {t('stepOne.ietf.selectedRfcTitle', { ns: 'datasetCreation' })}
                      </div>
                      <div className="text-sm leading-5 text-text-secondary">{selectedRfc.title}</div>
                    </div>

                    <div className="space-y-2">
                      <div className="text-xs font-medium text-text-secondary">
                        {t('stepOne.ietf.importType', { ns: 'datasetCreation' })}
                      </div>
                      <SegmentedControl
                        options={[
                          { value: 'rfc', text: selectedRfc.doc_id },
                          {
                            value: 'draft',
                            text: t('stepOne.ietf.importTypeInternetDraft', { ns: 'datasetCreation' }),
                            disabled: !availableDraftVersions.length,
                          },
                        ]}
                        value={importType}
                        onChange={value => setImportType(value as 'rfc' | 'draft')}
                        className="w-fit"
                      />
                    </div>

                    {importType === 'draft' && selectedRfc.draft_base_name && availableDraftVersions.length > 0 && (
                      <div className="space-y-3 rounded-xl border border-divider-subtle bg-background-default p-3">
                        <div>
                          <div className="text-xs font-medium text-text-secondary">
                            {t('stepOne.ietf.draftBaseName', { ns: 'datasetCreation' })}
                          </div>
                          <div className="mt-1 text-sm text-text-primary">
                            {selectedRfc.draft_base_name}
                          </div>
                        </div>
                        <div>
                          <div className="text-xs font-medium text-text-secondary">
                            {t('stepOne.ietf.draftVersion', { ns: 'datasetCreation' })}
                          </div>
                          <div className="mt-2 flex flex-wrap gap-1.5">
                            {availableDraftVersions.map(item => (
                              <button
                                key={item.version}
                                type="button"
                                onClick={() => setSelectedDraftVersion(item.version)}
                                className={`rounded-lg border px-2.5 py-1.5 font-mono text-xs transition-colors ${
                                  selectedDraftVersion === item.version
                                    ? 'border-primary-600 bg-primary-50 text-primary-700'
                                    : 'border-divider-subtle bg-background-default text-text-secondary hover:bg-background-default-hover'
                                }`}
                              >
                                {String(item.version).padStart(2, '0')}
                              </button>
                            ))}
                          </div>
                        </div>

                      </div>
                    )}

                    {/* Format picker — shown once formats are loaded */}
                    <div className="space-y-2">
                      <div className="text-xs font-medium text-text-secondary">
                        {t('stepOne.ietf.docFormat', { ns: 'datasetCreation' })}
                      </div>
                      {isFetchingFormats
                        ? (
                            <div className="text-xs text-text-tertiary">…</div>
                          )
                        : (
                            <div className="flex flex-wrap gap-1.5">
                              {availableFormats.map(fmt => (
                                <button
                                  key={fmt.key}
                                  type="button"
                                  onClick={() => setSelectedFormat(fmt.key)}
                                  className={`rounded-lg border px-2.5 py-1.5 text-xs transition-colors ${
                                    selectedFormat === fmt.key
                                      ? 'border-primary-600 bg-primary-50 text-primary-700'
                                      : 'border-divider-subtle bg-background-default text-text-secondary hover:bg-background-default-hover'
                                  }`}
                                >
                                  {fmt.label}
                                </button>
                              ))}
                            </div>
                          )}
                    </div>

                    {selectedMetadata && (
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <div className="text-xs font-medium text-text-secondary">
                            {t('stepOne.ietf.since', { ns: 'datasetCreation' })}
                          </div>
                          <div className="mt-1 rounded-lg border border-divider-subtle bg-background-default px-3 py-2 text-sm text-text-primary">
                            {selectedMetadata.since || t('stepOne.ietf.notAvailable', { ns: 'datasetCreation' })}
                          </div>
                        </div>
                        <div>
                          <div className="text-xs font-medium text-text-secondary">
                            {t('stepOne.ietf.until', { ns: 'datasetCreation' })}
                          </div>
                          <div className="mt-1 rounded-lg border border-divider-subtle bg-background-default px-3 py-2 text-sm text-text-primary">
                            {selectedMetadata.until || t('stepOne.ietf.notAvailable', { ns: 'datasetCreation' })}
                          </div>
                        </div>
                        <div>
                          <div className="text-xs font-medium text-text-secondary">
                            {t('stepOne.ietf.stream', { ns: 'datasetCreation' })}
                          </div>
                          <div className="mt-1 rounded-lg border border-divider-subtle bg-background-default px-3 py-2 text-sm text-text-primary">
                            {selectedMetadata.stream || t('stepOne.ietf.notAvailable', { ns: 'datasetCreation' })}
                          </div>
                        </div>
                        <div>
                          <div className="text-xs font-medium text-text-secondary">
                            {t('stepOne.ietf.wg', { ns: 'datasetCreation' })}
                          </div>
                          <div className="mt-1 rounded-lg border border-divider-subtle bg-background-default px-3 py-2 text-sm text-text-primary">
                            {selectedMetadata.wg || t('stepOne.ietf.notAvailable', { ns: 'datasetCreation' })}
                          </div>
                        </div>
                        <div>
                          <div className="text-xs font-medium text-text-secondary">
                            {t('stepOne.ietf.status', { ns: 'datasetCreation' })}
                          </div>
                          <div className="mt-1 rounded-lg border border-divider-subtle bg-background-default px-3 py-2 text-sm text-text-primary">
                            {selectedMetadata.status || t('stepOne.ietf.notAvailable', { ns: 'datasetCreation' })}
                          </div>
                        </div>
                        <div>
                          <div className="text-xs font-medium text-text-secondary">
                            {t('stepOne.ietf.area', { ns: 'datasetCreation' })}
                          </div>
                          <div className="mt-1 rounded-lg border border-divider-subtle bg-background-default px-3 py-2 text-sm text-text-primary">
                            {selectedMetadata.area || t('stepOne.ietf.notAvailable', { ns: 'datasetCreation' })}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
            {mode === 'github' && (
              <div className="space-y-4" data-testid="ietf-github-form">
                <div className="text-xs leading-4 text-text-tertiary">
                  {t('stepOne.ietf.githubFiltersHint', { ns: 'datasetCreation' })}
                </div>

                {/* WG combobox */}
                <div>
                  <div className="mb-1 text-xs font-medium text-text-secondary">
                    {t('stepOne.ietf.githubWg', { ns: 'datasetCreation' })}
                  </div>
                  <div className="relative" ref={wgDropdownRef}>
                    <Input
                      value={wgFilter || githubForm.wg}
                      onChange={(e) => {
                        setWgFilter(e.target.value)
                        setShowWgDropdown(true)
                      }}
                      onFocus={() => setShowWgDropdown(true)}
                      onBlur={() => setTimeout(() => setShowWgDropdown(false), 150)}
                      placeholder={t('stepOne.ietf.githubWgPlaceholder', { ns: 'datasetCreation' }) || ''}
                    />
                    {showWgDropdown && (
                      <div className="absolute z-50 mt-1 max-h-48 w-full overflow-y-auto rounded-lg border border-divider-subtle bg-components-panel-bg shadow-lg">
                        {WG_NAMES
                          .filter(wg => !wgFilter || wg.toLowerCase().includes(wgFilter.toLowerCase()))
                          .map(wg => (
                            <div
                              key={wg}
                              className="cursor-pointer px-3 py-2 text-sm text-text-primary hover:bg-state-base-hover"
                              onMouseDown={() => {
                                setGithubForm(prev => ({ ...prev, wg, repo: '' }))
                                setWgFilter('')
                                setDraftFilter('')
                                setShowWgDropdown(false)
                              }}
                            >
                              {wg}
                            </div>
                          ))}
                      </div>
                    )}
                  </div>
                </div>

                {/* Draft / repo combobox */}
                <div>
                  <div className="mb-1 text-xs font-medium text-text-secondary">
                    {t('stepOne.ietf.githubDraft', { ns: 'datasetCreation' })}
                  </div>
                  <div className="relative" ref={draftDropdownRef}>
                    <Input
                      value={draftFilter || githubForm.repo}
                      onChange={(e) => {
                        setDraftFilter(e.target.value)
                        setGithubForm(prev => ({ ...prev, repo: e.target.value }))
                        setShowDraftDropdown(true)
                      }}
                      onFocus={() => setShowDraftDropdown(true)}
                      onBlur={() => setTimeout(() => setShowDraftDropdown(false), 150)}
                      placeholder={t('stepOne.ietf.githubDraftPlaceholder', { ns: 'datasetCreation' }) || ''}
                    />
                    {showDraftDropdown && githubForm.wg && (
                      <div className="absolute z-50 mt-1 max-h-48 w-full overflow-y-auto rounded-lg border border-divider-subtle bg-components-panel-bg shadow-lg">
                        {getReposForWg(githubForm.wg)
                          .filter(r => !draftFilter || r.toLowerCase().includes(draftFilter.toLowerCase()))
                          .map(r => (
                            <div
                              key={r}
                              className="cursor-pointer px-3 py-2 text-sm text-text-primary hover:bg-state-base-hover"
                              onMouseDown={() => {
                                setGithubForm(prev => ({ ...prev, repo: r }))
                                setDraftFilter('')
                                setShowDraftDropdown(false)
                              }}
                            >
                              {r}
                            </div>
                          ))}
                      </div>
                    )}
                  </div>
                </div>

                {/* Checkboxes */}
                <div className="flex flex-wrap gap-4">
                  <label className="flex cursor-pointer items-center gap-1.5 text-xs text-text-secondary">
                    <input
                      type="checkbox"
                      checked={githubForm.includeIssues}
                      onChange={e => setGithubForm(prev => ({ ...prev, includeIssues: e.target.checked }))}
                    />
                    {t('stepOne.ietf.githubIncludeIssues', { ns: 'datasetCreation' })}
                  </label>
                  <label className="flex cursor-pointer items-center gap-1.5 text-xs text-text-secondary">
                    <input
                      type="checkbox"
                      checked={githubForm.includePRs}
                      onChange={e => setGithubForm(prev => ({ ...prev, includePRs: e.target.checked }))}
                    />
                    {t('stepOne.ietf.githubIncludePRs', { ns: 'datasetCreation' })}
                  </label>
                  <label className="flex cursor-pointer items-center gap-1.5 text-xs text-text-secondary">
                    <input
                      type="checkbox"
                      checked={githubForm.includeClosed}
                      onChange={e => setGithubForm(prev => ({ ...prev, includeClosed: e.target.checked }))}
                    />
                    {t('stepOne.ietf.githubIncludeClosed', { ns: 'datasetCreation' })}
                  </label>
                </div>

                {/* Loader selector */}
                <div>
                  <div className="mb-1 text-xs font-medium text-text-secondary">
                    {t('stepOne.ietf.githubLoader', { ns: 'datasetCreation' })}
                  </div>
                  <div className="flex gap-2">
                    {(
                      [
                        ['graphql', t('stepOne.ietf.githubLoaderGraphql', { ns: 'datasetCreation' })],
                        ['langchain', t('stepOne.ietf.githubLoaderLangchain', { ns: 'datasetCreation' })],
                        ['llamaindex', t('stepOne.ietf.githubLoaderLlamaindex', { ns: 'datasetCreation' })],
                      ] as [GithubLoader, string][]
                    ).map(([l, label]) => (
                      <button
                        key={l}
                        type="button"
                        onClick={() => setGithubForm(prev => ({ ...prev, loader: l }))}
                        className={`rounded-md border px-3 py-1 text-xs transition-colors ${
                          githubForm.loader === l
                            ? 'border-components-button-primary-bg bg-components-button-primary-bg text-components-button-primary-text'
                            : 'border-divider-subtle bg-background-default text-text-secondary hover:bg-state-base-hover'
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Token */}
                <div>
                  <div className="mb-1 text-xs font-medium text-text-secondary">
                    {t('stepOne.ietf.githubToken', { ns: 'datasetCreation' })}
                  </div>
                  <Input
                    type="password"
                    value={githubForm.githubToken}
                    onChange={e => setGithubForm(prev => ({ ...prev, githubToken: e.target.value }))}
                    placeholder={t('stepOne.ietf.githubTokenHint', { ns: 'datasetCreation' }) || ''}
                  />
                </div>
              </div>
            )}
          </div>

          {/* GitHub background import progress */}
          {mode === 'github' && githubJobState && (
            <div className="mx-6 mb-2 rounded-lg border border-divider-subtle bg-background-default-subtle px-4 py-3 text-xs">
              {githubJobState.status === 'error'
                ? (
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-text-warning">{githubJobState.error}</span>
                      {githubJobState.resumable && (
                        <Button variant="secondary" size="small" onClick={handleGithubResume}>
                          {t('stepOne.ietf.githubImportResume', { ns: 'datasetCreation' })}
                        </Button>
                      )}
                    </div>
                  )
                : (
                    <div className="space-y-1">
                      <div className="flex items-center justify-between text-text-secondary">
                        <span>
                          {githubJobState.phase === 'pending' && t('stepOne.ietf.githubImportPending', { ns: 'datasetCreation' })}
                          {githubJobState.phase === 'fetching' && t('stepOne.ietf.githubImportFetching', { ns: 'datasetCreation', count: githubJobState.fetched })}
                          {githubJobState.phase === 'fetching_comments' && t('stepOne.ietf.githubImportFetchingComments', { ns: 'datasetCreation', count: githubJobState.fetched })}
                          {githubJobState.phase === 'uploading' && t('stepOne.ietf.githubImportUploading', { ns: 'datasetCreation' })}
                        </span>
                        <span className="text-text-tertiary">
                          {githubJobState.elapsed_seconds}
                          s
                        </span>
                      </div>
                      {githubJobState.elapsed_seconds >= 30 && (
                        <p className="text-text-tertiary">{t('stepOne.ietf.githubImportSlow', { ns: 'datasetCreation' })}</p>
                      )}
                    </div>
                  )}
            </div>
          )}

          <div className="flex justify-end gap-2 border-t border-divider-subtle px-6 py-4">
            <Button variant="secondary" onClick={closeDialog} disabled={isSubmitting}>
              {t('stepOne.ietf.cancel', { ns: 'datasetCreation' })}
            </Button>
            <Button
              variant="primary"
              loading={isSubmitting}
              disabled={isSubmitting || (mode === 'mailarchive' && syncStatus !== null && syncStatus !== 'ready')}
              onClick={mode === 'mailarchive' ? submitMailarchiveImport : mode === 'rfcs' ? submitRfcs : submitGithub}
              data-testid="ietf-import-submit"
            >
              {t('stepOne.ietf.import', { ns: 'datasetCreation' })}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}

export default IetfImporter
