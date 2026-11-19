import { DropdownMenuLinkItem } from '@langgenius/dify-ui/dropdown-menu'
import { ExternalLinkIndicator, MenuItemContent } from '@/app/components/header/account-dropdown/menu-item-content'
import { PRISM_CLASSIC_URL, PRISM_SPECTRUM_URL } from '@/config'

// Prism: links from Dify's help menu out to this deployment's Prism IETF
// research tool. Hidden unless NEXT_PUBLIC_PRISM_CLASSIC_URL/_SPECTRUM_URL
// are configured, so they never appear on any other Dify deployment. The
// labels are deployment-specific names, not Dify product strings, so they
// don't go through t().
export default function PrismLinks() {
  const links = [
    { href: PRISM_CLASSIC_URL, label: 'Prism (Classic)' },
    { href: PRISM_SPECTRUM_URL, label: 'Prism (Spectrum)' },
  ]

  return (
    <>
      {links.map(({ href, label }) => href && (
        <DropdownMenuLinkItem
          key={label}
          className="mx-0 h-8 gap-1 px-3 py-1"
          href={href}
          rel="noopener noreferrer"
          target="_blank"
        >
          <MenuItemContent
            iconClassName="i-ri-flask-line"
            label={label}
            trailing={<ExternalLinkIndicator />}
          />
        </DropdownMenuLinkItem>
      ))}
    </>
  )
}
