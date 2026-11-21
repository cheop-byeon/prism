import type { CommonResponse } from '@/models/common'
import { get, post } from './base'

type LoginSuccess = {
  result: 'success'
  data?: { access_token?: string }
}
type LoginFail = {
  result: 'fail'
  data: string
  code: string
  message: string
}
type LoginResponse = LoginSuccess | LoginFail
export const webAppLogin = ({
  url,
  body,
}: {
  url: string
  body: Record<string, unknown>
}): Promise<LoginResponse> => {
  return post<LoginResponse>(url, { body }, { isPublicAPI: true })
}

export const changeWebAppPasswordWithToken = ({
  url,
  body,
}: {
  url: string
  body: { token: string; new_password: string; password_confirm: string }
}): Promise<CommonResponse> => post<CommonResponse>(url, { body }, { isPublicAPI: true })

export const uploadPublicRemoteFileInfo = (url: string, options?: { silent?: boolean }) => {
  return post<{ id: string; name: string; size: number; mime_type: string; url: string }>(
    '/remote-files/upload',
    { body: { url } },
    { isPublicAPI: true, ...options },
  )
}

export type UploadedConsoleFile = {
  id: string
  name: string
  size: number
  extension?: string
  mime_type: string
  url: string
  created_by?: string
  created_at?: number
}

export type IetfMailarchiveImportRequest = {
  mailing_list: string
  sent_after: string
  sent_before: string
  from_addr?: string
  subject?: string
  search_in: ('subject' | 'body')[]
  offset: number
  limit: number
  output_format: 'markdown' | 'mbox_text'
  fetch_all: boolean
}

export type IetfFormatInfo = {
  key: string // "txt" | "html" | "pdf" | "xml"
  label: string // "Plain Text" | "HTML" | "PDF" | "XML"
  mime_type: string
}

export type IetfRfcFormatsResponse = {
  rfc_id: string
  formats: IetfFormatInfo[]
}

export type IetfDraftFormatsResponse = {
  draft_name: string
  formats: IetfFormatInfo[]
}

export type IetfRfcImportRequest = {
  rfc_ids: string[]
  since?: string
  until?: string
  stream?: string
  wg?: string
  status?: string
  limit: number
  format?: string
}

export type IetfRfcSuggestion = {
  doc_id: string
  title: string
  draft_names: string[]
  since?: string | null
  stream?: string | null
  wg?: string | null
  area?: string | null
  status?: string | null
  draft_base_name?: string | null
  draft_max_version?: number | null
  draft_versions?: Array<{
    name: string
    version: number
    since?: string | null
    until?: string | null
  }>
}

export type IetfMailarchiveLocalDownloadResponse = {
  saved_path: string
  host_path_hint: string
  filename: string
  size: number
  message_count: number
}

export type IetfYearPreprocessResult = {
  year: number
  saved_path: string | null
  host_path_hint: string | null
  processed_path: string | null
  processed_host_path_hint: string | null
  total_emails: number
  discarded_emails: number
  preview_lines: string[]
}

export type IetfPreprocessResponse = {
  years: IetfYearPreprocessResult[]
}

export type IetfMailarchivePreprocessRequest = {
  mailing_list: string
  sent_after: string
  sent_before: string
  from_addr?: string
  subject?: string
  search_in: ('subject' | 'body')[]
  filter_emails?: boolean
  quote_handling?: 'none' | 'naive' | 'advanced'
  group_threads?: boolean
}

export type IetfPreprocessedUploadRequest = {
  processed_path: string
}

export const importIetfMailarchiveFile = (
  body: IetfMailarchiveImportRequest,
): Promise<UploadedConsoleFile> => {
  return post<UploadedConsoleFile>('/ietf/imports/mailarchive', { body })
}

export const importIetfMailarchiveAsMbox = (
  body: IetfMailarchiveImportRequest,
): Promise<UploadedConsoleFile> => {
  return post<UploadedConsoleFile>('/ietf/imports/mailarchive/mbox', { body })
}

export const downloadIetfMailarchiveAsMboxToLocal = (
  body: IetfMailarchiveImportRequest,
): Promise<IetfMailarchiveLocalDownloadResponse> => {
  return post<IetfMailarchiveLocalDownloadResponse>('/ietf/imports/mailarchive/mbox/download', {
    body,
  })
}

export const downloadAndPreprocessMailarchiveYears = (
  body: IetfMailarchivePreprocessRequest,
): Promise<IetfPreprocessResponse> => {
  return post<IetfPreprocessResponse>('/ietf/imports/mailarchive/mbox/preprocess', { body })
}

export const uploadIetfPreprocessedFile = (
  body: IetfPreprocessedUploadRequest,
): Promise<UploadedConsoleFile> => {
  return post<UploadedConsoleFile>('/ietf/imports/mailarchive/preprocessed/upload', { body })
}

export const importIetfRfcFile = (body: IetfRfcImportRequest): Promise<UploadedConsoleFile> => {
  return post<UploadedConsoleFile>('/ietf/imports/rfcs', { body })
}

export type IetfDraftImportRequest = {
  draft_names: string[]
  format?: string
}

export const importIetfDraftFile = (body: IetfDraftImportRequest): Promise<UploadedConsoleFile> => {
  return post<UploadedConsoleFile>('/ietf/imports/drafts', { body })
}

export type GithubImportRequest = {
  repo: string
  include_issues?: boolean
  include_prs?: boolean
  include_closed?: boolean
  github_token: string
  loader?: 'graphql' | 'langchain' | 'llamaindex'
}

export type GithubJobStatus = 'pending' | 'running' | 'done' | 'error'

export type GithubJobState = {
  job_id: string
  status: GithubJobStatus
  repo: string
  loader: string
  fetched: number
  phase: string
  elapsed_seconds: number
  resumable: boolean
  file?: UploadedConsoleFile
  error?: string
}

export const startGithubImport = (
  body: GithubImportRequest,
): Promise<{ job_id: string; status: GithubJobStatus }> => {
  return post('/ietf/imports/github', { body })
}

export const fetchGithubImportStatus = (jobId: string): Promise<GithubJobState> => {
  return get(`/ietf/imports/github/${jobId}/status`)
}

export const resumeGithubImport = (
  jobId: string,
  github_token: string,
): Promise<{ job_id: string; status: GithubJobStatus }> => {
  return post(`/ietf/imports/github/${jobId}/resume`, { body: { github_token } })
}

export type IetfSyncStatus = 'ready' | 'syncing' | 'not_found'

export type IetfSyncStatusResponse = {
  name: string
  status: IetfSyncStatus
  message_count: number
}

export const fetchIetfMailingLists = (): Promise<{ lists: string[] }> => {
  return get<{ lists: string[] }>('/ietf/mailarchive/lists')
}

export type IetfWgDocument = {
  name: string
  title: string
  rfc_number: number | null
}

export type IetfDraftRelations = {
  replaces: string[]
  replaced_by: string[]
  first_rev_date: string | null
  last_rev_date: string | null
}

export const fetchIetfWgDocuments = (
  wgAcronym: string,
): Promise<{ documents: IetfWgDocument[] }> => {
  return get<{ documents: IetfWgDocument[] }>(`/ietf/wg/${encodeURIComponent(wgAcronym)}/documents`)
}

export const fetchIetfDraftRelations = (draftName: string): Promise<IetfDraftRelations> => {
  return get<IetfDraftRelations>('/ietf/draft/relations', { params: { draft_name: draftName } })
}

export const importIetfDraftEvolution = (
  draftName: string,
  diffFormat: 'prose' | 'patch' = 'prose',
  skipMinorEdits = false,
): Promise<UploadedConsoleFile> => {
  return post<UploadedConsoleFile>('/ietf/imports/draft-evolution', {
    body: { draft_name: draftName, diff_format: diffFormat, skip_minor_edits: skipMinorEdits },
  })
}

export const importIetfDraftAnnotatedRfc = (draftName: string): Promise<UploadedConsoleFile> => {
  return post<UploadedConsoleFile>('/ietf/imports/draft-annotated-rfc', {
    body: { draft_name: draftName },
  })
}

export const fetchIetfMailarchiveSyncStatus = (
  listName: string,
): Promise<IetfSyncStatusResponse> => {
  return get<IetfSyncStatusResponse>(`/ietf/mailarchive/${encodeURIComponent(listName)}/status`)
}

export const triggerIetfMailarchiveSync = (listName: string): Promise<IetfSyncStatusResponse> => {
  return post<IetfSyncStatusResponse>(`/ietf/mailarchive/${encodeURIComponent(listName)}/sync`, {})
}

export const fetchIetfRfcSuggestions = (params: {
  query?: string
  limit?: number
}): Promise<{ suggestions: IetfRfcSuggestion[] }> => {
  return get<{ suggestions: IetfRfcSuggestion[] }>('/ietf/rfcs/suggestions', { params })
}

export const fetchIetfRfcFormats = (rfcId: string): Promise<IetfRfcFormatsResponse> => {
  return get<IetfRfcFormatsResponse>('/ietf/rfcs/formats', { params: { rfc_id: rfcId } })
}

export const fetchIetfDraftFormats = (draftName: string): Promise<IetfDraftFormatsResponse> => {
  return get<IetfDraftFormatsResponse>('/ietf/drafts/formats', {
    params: { draft_name: draftName },
  })
}
export const sendWebAppEMailLoginCode = (
  email: string,
  language = 'en-US',
): Promise<CommonResponse & { data: string }> =>
  post<CommonResponse & { data: string }>(
    '/email-code-login',
    { body: { email, language } },
    { isPublicAPI: true },
  )

export const webAppEmailLoginWithCode = (data: {
  email: string
  code: string
  token: string
}): Promise<LoginResponse> =>
  post<LoginResponse>('/email-code-login/validity', { body: data }, { isPublicAPI: true })

export const sendWebAppResetPasswordCode = (
  email: string,
  language = 'en-US',
): Promise<CommonResponse & { data: string; message?: string; code?: string }> =>
  post<CommonResponse & { data: string; message?: string; code?: string }>(
    '/forgot-password',
    { body: { email, language } },
    { isPublicAPI: true },
  )

export const verifyWebAppResetPasswordCode = (body: {
  email: string
  code: string
  token: string
}): Promise<CommonResponse & { is_valid: boolean; token: string }> =>
  post<CommonResponse & { is_valid: boolean; token: string }>(
    '/forgot-password/validity',
    { body },
    { isPublicAPI: true },
  )
