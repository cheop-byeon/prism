import Script from '@/next/script'

export function ReactGrabLoader() {
  if (process.env.NEXT_PUBLIC_ENABLE_REACT_GRAB !== 'true')
    return null

  return (
    <>
      <Script
        src="//unpkg.com/react-grab/dist/index.global.js"
        crossOrigin="anonymous"
        strategy="beforeInteractive"
      />
    </>
  )
}
