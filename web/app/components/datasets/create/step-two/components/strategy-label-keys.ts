import type { ChunkStrategy } from '@/models/datasets'

// Shared by GeneralChunkingOptions and ParentChildOptions (each level's
// strategy dropdown). Kept in its own non-component file — a component file
// mixing component and non-component exports breaks React Fast Refresh.
export const CUSTOM_STRATEGY_LABEL_KEYS: Record<string, string> = {
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

export const LLAMAINDEX_STRATEGY_LABEL_KEYS: Record<string, string> = {
  li_sentence: 'stepTwo.chunkStrategyLiSentence',
  li_token: 'stepTwo.chunkStrategyLiToken',
  li_fixed: 'stepTwo.chunkStrategyLiFixed',
  li_semantic: 'stepTwo.chunkStrategyLiSemantic',
  li_markdown: 'stepTwo.chunkStrategyLiMarkdown',
  li_html: 'stepTwo.chunkStrategyLiHtml',
  li_code: 'stepTwo.chunkStrategyLiCode',
}

export const LANGCHAIN_STRATEGY_LABEL_KEYS: Record<string, string> = {
  lc_recursive: 'stepTwo.chunkStrategyLcRecursive',
  lc_character: 'stepTwo.chunkStrategyLcCharacter',
  lc_token: 'stepTwo.chunkStrategyLcToken',
  lc_markdown_header: 'stepTwo.chunkStrategyLcMarkdownHeader',
  lc_html_header: 'stepTwo.chunkStrategyLcHtmlHeader',
  lc_semantic: 'stepTwo.chunkStrategyLcSemantic',
  lc_python: 'stepTwo.chunkStrategyLcPython',
}

export function getStrategyLabelKey(strategy: ChunkStrategy): string {
  return (
    CUSTOM_STRATEGY_LABEL_KEYS[strategy]
    ?? LLAMAINDEX_STRATEGY_LABEL_KEYS[strategy]
    ?? LANGCHAIN_STRATEGY_LABEL_KEYS[strategy]
    ?? strategy
  )
}
