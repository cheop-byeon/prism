import {
  Menu,
  MenuContent,
  MenuTrigger,
} from '@langgenius/dify-ui/menu'
import { render, screen } from '@testing-library/react'
import PrismLinks from '../prism-links'

const { mockConfig } = vi.hoisted(() => ({
  mockConfig: {
    classicUrl: undefined as string | undefined,
    spectrumUrl: undefined as string | undefined,
  },
}))

vi.mock('@/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/config')>()
  return {
    ...actual,
    get PRISM_CLASSIC_URL() {
      return mockConfig.classicUrl
    },
    get PRISM_SPECTRUM_URL() {
      return mockConfig.spectrumUrl
    },
  }
})

const renderLinks = () =>
  render(
    <Menu open>
      <MenuTrigger>open</MenuTrigger>
      <MenuContent>
        <PrismLinks />
      </MenuContent>
    </Menu>,
  )

describe('PrismLinks', () => {
  beforeEach(() => {
    mockConfig.classicUrl = undefined
    mockConfig.spectrumUrl = undefined
  })

  it('renders nothing when no Prism URL is configured', () => {
    renderLinks()

    expect(screen.queryByText(/Prism/)).not.toBeInTheDocument()
  })

  it('links to each configured Prism UI in a new tab', () => {
    mockConfig.classicUrl = 'https://prism.example/classic/'
    mockConfig.spectrumUrl = 'https://prism.example/spectrum/'

    renderLinks()

    expect(screen.getByText('Prism (Classic)').closest('a')).toHaveAttribute(
      'href',
      'https://prism.example/classic/',
    )
    expect(screen.getByText('Prism (Spectrum)').closest('a')).toHaveAttribute(
      'href',
      'https://prism.example/spectrum/',
    )
    expect(screen.getByText('Prism (Spectrum)').closest('a')).toHaveAttribute('target', '_blank')
  })

  it('shows only the configured link', () => {
    mockConfig.spectrumUrl = 'https://prism.example/spectrum/'

    renderLinks()

    expect(screen.queryByText('Prism (Classic)')).not.toBeInTheDocument()
    expect(screen.getByText('Prism (Spectrum)')).toBeInTheDocument()
  })
})
