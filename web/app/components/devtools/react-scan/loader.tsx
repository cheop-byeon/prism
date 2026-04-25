import Script from '@/next/script'

export function ReactScanLoader() {
  if (process.env.NEXT_PUBLIC_ENABLE_REACT_SCAN !== 'true')
    return null

  return (
    <Script
      src="//unpkg.com/react-scan/dist/auto.global.js"
      crossOrigin="anonymous"
      strategy="beforeInteractive"
    />
  )
}
