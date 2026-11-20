// Prism: single segment / child-chunk reads that carry the chunk's display metadata
// (author, date, archive URL, ...). These Prism-only endpoints are not in the
// generated console contract, so they are called directly.
import type { ChunkMetadata } from '@/models/datasets'
import { useQuery } from '@tanstack/react-query'
import { get } from '../base'

type ChunkMetadataResponse = { data: { metadata?: ChunkMetadata } }

const chunkMetadataKey = ['segment', 'chunkMetadata']

export const useSegmentMetadata = (
  payload: {
    datasetId: string
    documentId: string
    segmentId: string
  },
  disable?: boolean,
) => {
  const { datasetId, documentId, segmentId } = payload

  return useQuery<ChunkMetadataResponse>({
    queryKey: [...chunkMetadataKey, 'segment', datasetId, documentId, segmentId],
    queryFn: () => {
      return get<ChunkMetadataResponse>(
        `/datasets/${datasetId}/documents/${documentId}/segments/${segmentId}`,
      )
    },
    enabled: !disable,
  })
}

export const useChildChunkMetadata = (
  payload: {
    datasetId: string
    documentId: string
    segmentId: string
    childChunkId: string
  },
  disable?: boolean,
) => {
  const { datasetId, documentId, segmentId, childChunkId } = payload

  return useQuery<ChunkMetadataResponse>({
    queryKey: [...chunkMetadataKey, 'child', datasetId, documentId, segmentId, childChunkId],
    queryFn: () => {
      return get<ChunkMetadataResponse>(
        `/datasets/${datasetId}/documents/${documentId}/segments/${segmentId}/child_chunks/${childChunkId}`,
      )
    },
    enabled: !disable,
  })
}
