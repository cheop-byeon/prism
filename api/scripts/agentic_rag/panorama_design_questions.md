# Panorama design-decision question bank

A working list of "why was X designed this way" questions for Prism's Panorama
search tier, across the "big family" protocols (IPv4, IPv6, BGP, IPsec, HTTP,
OAuth). Panorama's job is cross-RFC/citation-based investigation, so these are
picked to need more than one document to answer well — not single-RFC lookups.

Each question is marked:

- **✅ Tested** — actually run against the live agent (`ask_decision_agent.py`,
  Panorama tier, `claude-haiku-4-5-20251001`, `max_iterations=20`, with the
  temperature/top_p and forced-final-turn fixes applied). The answer shown is
  the agent's real output, grounded in RFC text it fetched live — not
  something written from memory.
- **Untested** — not yet run through the agent. The answer given is my own
  domain-knowledge summary, meant as a sanity check on whether the question is
  likely to have a real, citable answer — not a substitute for actually
  running it. Treat these as drafts to verify, not ground truth.

Known gap on this deployment: no WG mailing-list archives are imported yet
(zero Datasets on this instance), so every live test's evidence comes from
RFC/draft full text fetched from rfc-editor.org / ietf.org, not indexed
mail discussion. Several tested answers explicitly flag this (BGP, IPsec)
as the reason they couldn't reach the *why this and not that* debate, only
the technical tradeoff visible in the specs themselves.

---

## IPv4

### 1. Why was the address space originally divided into Class A/B/C/D/E (RFC 791/950) instead of a flat, unstructured scheme?
**Untested.** Classful addressing let routers infer network-vs-host boundaries from the first bits of an address alone, with no separate mask to carry or configure — important when routing tables and router CPUs were tiny by today's standards. The tradeoff was coarse-grained allocation (a "Class B" grant wasted enormous address space for most actual organizations), which is exactly what CIDR later fixed.

### 2. Why did classful addressing get replaced by CIDR (RFC 1518/1519) rather than patched incrementally, and what routing-table-growth crisis triggered it?
**Untested.** By the early 1990s, core router routing tables were growing faster than router memory/CPU could keep up (documented in RFC 1338, "Supernetting: an Address Assignment and Aggregation Strategy"), largely because Class B allocations were being handed out to any organization that merely looked too big for a Class C. CIDR replaced fixed class boundaries with arbitrary-length prefixes plus explicit masks, enabling route aggregation and slowing table growth — an emergency operational fix, not a clean redesign.

### 3. Why is the IPv4 header checksum recalculated at every router hop, and what per-router processing cost did that impose?
**Untested.** Because TTL is decremented at every hop, the checksum (which covers the whole header including TTL) becomes invalid unless recomputed — an unavoidable consequence of putting TTL in checksummed header space. RFC 1071 (and its update RFC 1141) exist specifically because the recompute-every-hop cost was significant enough to warrant an incremental-update algorithm instead of a full recalculation, showing this wasn't a free design choice.

### 4. Why was fragmentation built into the network layer instead of leaving it to hosts via Path MTU Discovery from the start?
**Untested.** In 1981 there was no reliable, universal way for a host to learn a path's minimum MTU before sending, so router-level fragmentation was the only mechanism that could guarantee delivery across heterogeneous link MTUs. PMTUD (RFC 1191, 1990) only became viable once ICMP "fragmentation needed" messages were reliably usable — and even then, operational experience (RFC 4459, on tunnels breaking PMTUD; RFC 8900, deprecating IP fragmentation) showed router fragmentation caused enough performance/security problems that the community reversed course decades later.

### 5. ✅ Why did NAT (RFC 1631→3022) become the dominant response to address exhaustion instead of accelerating IPv6, and how did "explicitly short-term" language in RFC 1631 age?

> Based on the RFC evidence gathered: NAT was adopted as a short-term solution because it could be deployed incrementally without requiring changes to hosts or routers, while IPv6 deployment faced fundamental barriers that made it unsuitable as an immediate solution.
>
> RFC 1631 (May 1994) explicitly states: *"The huge advantage of this approach is that it can be installed incrementally, without changes to either hosts or routers... this solution can serve to provide temporary relief while other, more complex and far-reaching solutions are worked out."* NAT was framed as a complement to CIDR, buying time while IPv6 (RFC 1883, published over a year later, December 1995) matured.
>
> **Alternatives considered:** RFC 1335 proposed "Dual Network Addressing" (internal + shareable external addresses via an allocation service) — rejected as needing more coordination than NAT's simple border-router model. RFC 1347 (TUBA) proposed migrating to CLNP/NSAP addressing for more space without translation overhead — rejected as requiring an entirely new protocol stack, a much longer timeline than NAT.
>
> **How it aged:** RFC 4864 (2007) states *"IPv6 was designed with the intention of making NAT unnecessary,"* rejecting NAT as a long-term approach. Yet RFC 5902 (2010) documents that even as IPv4 exhaustion became imminent, whether to standardize NAT *within* IPv6 itself had become a live, contentious question — showing how deeply the "temporary" mechanism had become embedded in Internet operations two decades on.

### 6. Why was RFC 1918 private addressing fixed to three specific blocks rather than a flexible allocation scheme?
**Untested.** Fixed, well-known blocks (10/8, 172.16/12, 192.168/16) meant every router, firewall, and administrator could hard-code recognition of "this is private" without needing a registry lookup or negotiation protocol — a deliberate simplicity tradeoff. It works cleanly with NAT and CIDR precisely because it's just three more prefixes in the same aggregatable addressing scheme, not a separate namespace.

### 7. Why did IPv4 options end up effectively filtered/unusable across the real Internet (RFC 7126)?
**Untested.** Options sit in a variable-length, rarely-exercised part of the header that historically received far less security scrutiny and hardware fast-path support than the fixed fields — making them an attractive vector for both attacks and slow-path router processing DoS. RFC 7126 documents that operators, largely independently and pragmatically, converged on dropping most option types by default, turning a standardized feature into de facto dead code.

### 8. Why was there no authentication/encryption in the original IPv4 design, requiring IPsec as a decades-later retrofit?
**Untested.** The early Internet was a small, trusted research network; the design threat model didn't include hostile transit routers or widespread eavesdropping. IPsec (RFC 4301, 1998, updated 2005) had to be bolted on as an optional extension layer rather than integrated into the base header, which is a large part of why its adoption remained partial and operationally painful (see IPsec section below).

### 9. Why was ICMP (RFC 792) designed as a companion protocol layered on IP rather than an IP option or transport protocol?
**Untested.** ICMP needed to report errors *about* IP delivery itself (unreachable, TTL exceeded, fragmentation needed), so it had to be usable even when higher-layer transport state didn't exist yet or had already failed — ruling out a transport-layer design. Making it a full separate protocol (with its own type/code space) rather than an IP option kept the base IP header simple and let ICMP evolve its own message types independently.

### 10. Why was the Type of Service field redefined as DSCP (RFC 2474) rather than adding a new field?
**Untested.** The original 8-bit ToS field was barely used in practice, so redefining its bits was cheaper than exhausting scarce header space with a new field — but it meant DSCP had to be designed for backward compatibility with routers that might still interpret the old, mostly-unused ToS semantics, a constraint a fresh field wouldn't have carried.

### 11. Why did source-routing options go from a diagnostic tool to something routers drop by default (RFC 6274)?
**Untested.** Source routing lets the *sender* dictate a packet's path, which is exactly the capability an attacker wants to bypass firewalls, reach otherwise-unreachable internal hosts, or spoof return paths for reflection attacks. RFC 6274 catalogs these exploits and recommends dropping source-routed packets by default — a reversal from source routing's original use as a network diagnostic/traffic-engineering tool.

### 12. Why was minimum reassembly buffer size set to 576 bytes in RFC 791?
**Untested.** 576 bytes was chosen to comfortably fit a "reasonable" data block (512 bytes) plus generous header room over the low-bandwidth, high-latency links common in 1981; it became the de facto safe MTU floor for decades of tunnel/dial-up defaults (and still shows up in PPPoE-era MTU guidance) simply because it was the one number every implementation was guaranteed to support.

### 13. Why did DHCP (RFC 2131) replace BOOTP (RFC 951) rather than extend it?
**Untested.** BOOTP was static (manual host-to-address mapping, no lease concept), which didn't scale to dynamic networks with more hosts than permanent addresses. DHCP kept BOOTP's wire packet format (for compatibility with existing relay-agent infrastructure) but added leasing, dynamic pools, and option negotiation on top — a "new protocol, same wire format" strategy rather than either a clean break or a same-protocol extension.

### 14. Why does IPv4 anycast have no formal specification despite wide operational deployment?
**Untested.** IPv4 anycast (root DNS servers being the most famous example) emerged as an operational practice using ordinary unicast routing announced from multiple locations, not a protocol feature requiring new specification — routers don't need to know a prefix is "anycast" to route it correctly. RFC 4786 documents *operational* practices for anycast DNS specifically rather than defining anycast as an IPv4 addressing category, unlike IPv6 which gave it formal architectural recognition (RFC 4291) from early on.

### 15. Why was Martian/spoofed-source filtering left as a recommendation (RFC 1812, BCP 84) rather than mandatory?
**Untested.** Ingress/egress filtering requires operators to configure and maintain filters at their own network edges for the benefit of *other* networks (anti-spoofing protects victims elsewhere, not the filtering network itself) — a classic incentive misalignment the IETF can recommend but not enforce. Large-scale spoofed-source DDoS incidents progressively strengthened the guidance (BCP 38/84) without ever making it a protocol-level requirement, since compliance can't be verified from outside a network.

---

## IPv6

### 1. ✅ Why did IPv6 remove the header checksum IPv4 had, and what tradeoff did that reflect?

> IPv6 intentionally removed the internet-layer (header) checksum that IPv4 had. The explicit rationale stated in RFC 1883 (the original IPv6 specification), carried forward to RFC 2460 and RFC 8200, is:
>
> *"The reason for the change is to protect ICMP from misdelivery or corruption of those fields of the IPv6 header on which it depends, which, unlike IPv4, are not covered by an internet-layer checksum."*
>
> The design reflects a shift in philosophy: rather than protecting the IP header at the internet layer, IPv6 relies on upper-layer protocols (ICMPv6, TCP, UDP) to cover IP addresses and header fields via their own pseudo-header checksums. UDP checksums, optional in IPv4, became mandatory in IPv6 to close the gap this created.
>
> **Alternatives considered:** none documented in the retrieved materials — the decision appears to predate the indexed corpus.
>
> **Tradeoff:** eliminating redundant error detection at the internet layer in exchange for simpler per-hop header processing, betting that link-layer and transport-layer checks are sufficient. *(Agent noted the WG mail archive for this era wasn't available to confirm the live design debate, only the specs' own stated rationale.)*

### 2. Why was the address size set to 128 bits rather than a more modest expansion?
**Untested.** RFC 1752 ("The Recommendation for the IP Next Generation Protocol") compared competing address-size proposals; 128 bits was chosen to allow both a generous global routing hierarchy *and* a fixed, large per-subnet host portion (64 bits, enough for embedding MAC-derived identifiers), rather than the tightest size that would merely outlast IPv4 exhaustion. The design bet on abundance to avoid ever repeating the classful/CIDR renumbering pain, at the cost of larger headers.

### 3. ✅ Why do SLAAC (RFC 4862) and DHCPv6 (RFC 3315) coexist as non-exclusive mechanisms rather than the IETF choosing one?

> RFC 4862: *"The stateless approach is used when a site is not particularly concerned with the exact addresses hosts use, so long as they are unique and properly routable. On the other hand, [DHCPv6] is used when a site requires tighter control over exact address assignments. Both... may be used simultaneously."*
>
> SLAAC's design goal is minimal infrastructure: RFC 4862 states small single-link sites "should not require the presence of a DHCPv6 server" — plug-and-play networking with no centralized server. DHCPv6 (and stateless DHCPv6, RFC 3736) exists for sites needing centralized control over exact assignments or extra configuration (DNS servers, etc.) that SLAAC has no mechanism to deliver.
>
> **Coexistence is intentional and complementary**, not competing: RFC 4862 notes hosts "accept the union of all information received via Neighbor Discovery and DHCPv6," and explicitly tolerates (as non-fatal) inconsistency between the two sources if both are enabled at once.

### 4. Why did IPv6 eliminate router-level fragmentation entirely (RFC 8200)?
**Untested.** Router-level fragmentation forces every router on a path to do reassembly-relevant work and creates well-known DoS/security issues (fragment-based firewall evasion, resource exhaustion); pushing fragmentation to the sending host only (via the Fragment extension header) keeps routers simpler and forces reliance on Path MTU Discovery, which IPv4's later experience (RFC 1191, RFC 4459) had already validated as workable by the time IPv6 was designed.

### 5. Why was Neighbor Discovery (RFC 4861) built on ICMPv6 instead of reusing/extending ARP?
**Untested.** ARP was IPv4-and-Ethernet-specific and broadcast-based; ICMPv6-based NDP let address resolution, router discovery, and redirect functions share one extensible message framework across all IPv6 link types, using multicast instead of broadcast. The redesign's lack of built-in authentication (any host can claim to be a router or answer for any address) is exactly what later required SEND (RFC 3971) as a cryptographic retrofit — a security gap introduced by choosing a cleaner design over ARP's (also insecure, but narrower) model.

### 6. Why did the IETF standardize multiple competing v4/v6 transition mechanisms instead of converging on one?
**Untested.** Transition needs varied by deployment: 6to4 (RFC 3056) assumed a public IPv4 address to auto-derive a prefix; Teredo (RFC 4380) specifically targeted hosts behind NAT that 6to4 couldn't serve; NAT64/DNS64 (RFC 6144/6146) targeted IPv6-only networks needing to reach IPv4-only services, the opposite direction from the others. Each mechanism optimized for a different, genuinely distinct network topology rather than being redundant competitors, which is why several survived in parallel instead of one "winning."

### 7. Why was the header simplified to a fixed 40 bytes/8 fields, pushing extensibility into Extension Headers?
**Untested.** IPv4's variable-length options sat inline in the base header, forcing every router to parse variable-length data before reaching the payload even when it never touched options — a known processing/security cost (see IPv4 Q7 above). IPv6's fixed base header lets routers fast-path the common case, while Extension Headers (chained, mostly meant to be read only by the destination) move the variable-length complexity out of the router's critical path — at the cost of the chained-parsing complexity that later caused Hop-by-Hop options to be widely dropped in practice (RFC 7872, IPv6 Q13 below).

### 8. Why did Unique Local Addresses (RFC 4193) replace Site-Local addressing so soon after RFC 3879 deprecated it?
**Untested.** Site-Local used a single reserved prefix for all private networks, which meant merging two site-local networks (VPN, acquisition, etc.) could produce colliding addresses with no way to reliably distinguish them — an ambiguity discovered quickly once early deployments tried real-world multi-site scenarios. ULA fixed this by embedding a (probabilistically unique) random global ID in each site's prefix, trading Site-Local's simplicity for collision-avoidance.

### 9. Why does IPv6 mandate a 1280-byte minimum link MTU instead of inheriting IPv4's 576-byte minimum?
**Untested.** By the time IPv6 was designed (mid-1990s), 576 bytes was already recognized as needlessly conservative for real-world links, and IPv6 relies on Path MTU Discovery rather than router fragmentation — so hosts need a larger guaranteed floor to make PMTUD's own signaling (which itself needs headers/options room) practical without immediately fragmenting at the host. 1280 bytes was chosen as comfortably above what virtually all real links of the era supported.

### 10. Why was SLAAC Privacy Extensions (RFC 4941) a later retrofit rather than part of the original design?
**Untested.** Original SLAAC derived the interface identifier from the interface's MAC address (EUI-64), which is stable and globally unique — convenient for autoconfiguration but a privacy problem, since it lets a host be tracked across networks by its address's low 64 bits regardless of which network it's on. RFC 4941 retrofitted randomized, periodically-rotating interface identifiers once this tracking implication became a recognized concern, years after the original autoconfiguration design shipped.

### 11. Why was IPv6 anycast originally restricted to routers only?
**Untested.** Anycast-to-a-host raises ambiguity that anycast-to-a-router doesn't: a router can meaningfully answer "which of several identical servers should this request go to" at the routing layer, but transport-layer state (a TCP connection, in particular) has no well-defined behavior if successive packets of the same "connection" get routed to different anycast replicas — a correctness problem, not just a performance one. The original restriction avoided standardizing behavior nobody had a clean answer for yet.

### 12. Why was the Flow Label field included in the header from the start despite having no mandated use for over a decade?
**Untested.** The designers anticipated QoS/flow-differentiation needs would matter enough to reserve header space in advance, since adding a new field to a fixed 40-byte header *after* deployment would be far harder than defining its exact semantics later via a separate RFC (which RFC 6437 eventually did). It's a bet on "reserve the bits now, standardize the meaning later" — which mostly worked, at the cost of a decade of a field with unclear real-world use.

### 13. Why do routers widely drop the Hop-by-Hop Options header in practice (RFC 7872)?
**Untested.** Hop-by-Hop options require every router on the path to actually parse and act on them (unlike other IPv6 extension headers, meant for the destination only), which is exactly the kind of slow-path, security-sensitive processing operators avoid — echoing IPv4 options' fate (IPv4 Q7) for essentially the same reasons, despite IPv6's cleaner extension-header design.

### 14. Why was IPsec support downgraded from mandatory (RFC 2460) to merely recommended (RFC 6434)?
**Untested.** Mandating IPsec support in every implementation didn't translate into mandatory *use*, and the operational complexity of IPsec (see IPsec section) meant many real deployments shipped the required code without it being meaningfully deployed — so RFC 6434 aligned the specification with observed reality rather than continuing to require something largely unenforced and unused in practice.

### 15. Why did the IETF choose a clean break from IPv4 wire-compatibility rather than a compatible extension?
**Untested.** RFC 1752's design goals prioritized fixing IPv4's structural limits (address size chief among them) over minimizing the transition mechanism's complexity — a compatible-extension approach (like classful-to-CIDR's bit-reinterpretation trick) wasn't available for a 4x address-size jump. The clean break traded a harder, still-ongoing multi-decade transition for a protocol not permanently constrained by IPv4's header format.

### 16. Why was the original "IPv4-compatible address" mechanism (::a.b.c.d) deprecated in favor of NAT64-style addressing (RFC 6052)?
**Untested.** The original mechanism assumed automatic tunneling based on an embedded IPv4 address would be the primary transition path; as NAT64/DNS64 became the more practically deployed IPv6-only-to-IPv4 mechanism, RFC 6052's well-known prefix approach offered a more flexible, translator-mediated embedding that didn't require every dual-stack host to implement automatic tunneling logic — the ecosystem converged on a different transition strategy than the one originally specified.

### 17. Why does IPv6 require every interface to support multiple simultaneous addresses?
**Untested.** Multiple simultaneous addresses per interface directly enable renumbering without downtime (bring up a new prefix's address, run both in parallel, retire the old one), multihoming to more than one provider, and privacy-extension address rotation (IPv6 Q10) — none of which IPv4's typical single-address-per-interface convention could support gracefully, since IPv4 renumbering historically meant a hard cutover.

---

## BGP

### 1. Why did the Internet move from EGP (RFC 827) to BGP rather than extending EGP?
**Untested.** EGP assumed a tree-shaped Internet with a single backbone that all other networks attached to as leaves — it had no concept of arbitrary policy or loop-free routing among peer backbones. As the NSFNET-era Internet grew multiple, interconnected backbones (a mesh, not a tree), EGP's core topological assumption broke, requiring a genuinely different algorithm (path-vector) rather than an extension of EGP's tree-specific logic.

### 2. Why was path-vector (carrying full AS_PATH) chosen over distance-vector/link-state for inter-domain routing?
**Untested.** Link-state requires flooding full topology information to every participant, which doesn't scale to (and isn't appropriate to reveal across) mutually-distrusting autonomous systems with their own routing policies; plain distance-vector (hop count) has no way to express or detect policy-based loops between ASes. Carrying the explicit AS_PATH lets each AS trivially detect loops (reject any update whose path already contains its own AS number) and apply arbitrary policy based on which ASes a route passed through — expressiveness distance-vector metrics can't provide.

### 3. Why did BGP-4 need a specific redesign (from BGP-3) to carry CIDR prefixes?
**Untested.** BGP-3 assumed classful addressing (a prefix's length was implied by its class), so it had no field to carry an explicit prefix length — CIDR's entire premise (arbitrary-length prefixes) was incompatible with that assumption. BGP-4 added an explicit prefix-length field to every advertised route, a small-looking wire change that was actually mandatory for BGP to remain usable once IPv4 addressing itself changed underneath it.

### 4. Why is MED defined non-transitive, pushing operators toward Communities instead for broader traffic engineering?
**Untested.** MED is meant to let a *single* neighboring AS express a preference among its own multiple entry points into your network; making it transitive (compared across different neighboring ASes) would let unrelated networks' MED values interfere with each other in ways with no coherent shared meaning, since MED values aren't normalized across administrative domains. Communities (RFC 1997), being opaque tags interpreted by mutual agreement rather than numerically compared, don't have this cross-domain-comparison problem, which is why they became the tool for broader traffic-engineering signaling.

### 5. Why did route reflection and confederations emerge as two separate answers to iBGP full-mesh scaling?
**Untested.** Full-mesh iBGP (every router peering directly with every other router in an AS) scales quadratically and doesn't compose well across administrative sub-groups. Route reflection (RFC 4456) solves it *within* a single AS by letting designated routers reflect routes to clients without a full mesh; confederations (RFC 5065) instead split one AS into sub-ASes that peer with each other more cheaply, useful when an AS is large enough to want internal policy boundaries, not just fewer sessions. They optimize for different organizational structures rather than being redundant.

### 6. Why did securing BGP take over two decades to standardize?
**Untested.** BGP's original trust model assumed AS operators were mutually trustworthy, an assumption that held reasonably well while the Internet's core was small and its operators well-known to each other; as the Internet scaled to thousands of independently-operated ASes, that assumption stopped holding, but retrofitting cryptographic route authentication onto an already globally-deployed protocol required solving hard key-distribution/infrastructure problems (culminating in RPKI, RFC 6480) that simply didn't exist when BGP was first designed.

### 7. ✅ Why did the industry favor RPKI Origin Validation over the stronger BGPsec for actual deployment?

> BGPsec chose full path-signing over origin-only validation to provide stronger cryptographic assurance about the *entire route path*. RFC 8205 states a valid BGPsec update gives cryptographic assurance that (1) the route was originated by an authorized AS, and (2) *every* AS listed in AS_Path explicitly authorized the advertisement onward — protecting against path manipulation, not just origin spoofing, unlike RPKI Origin Validation (RFC 6811/7115) which only checks the originating AS.
>
> **Deployment tradeoff (RFC 8207):** *"Origin validation based on RPKI is in its early phases. As BGPsec may require larger memory and/or more modern CPUs, it is expected to be deployed incrementally over a longer time span."* RFC 7115 emphasizes origin validation "has been designed to be deployed on current routers without significant hardware upgrades" and suits everything from large backbones to small edge networks — the simpler deployment model is the explicit selling point.
>
> **Limitation the agent flagged:** the SIDR working group mailing-list discussion that would explain the actual *why-this-not-that* debate wasn't available (mail archive sync failed on this instance) — the answer above is reconstructed from the specs' own stated tradeoffs, not the live design debate.

### 8. Why was 4-byte AS number extension (RFC 6793) designed around an AS_TRANS compatibility trick?
**Untested.** By the time 2-byte AS numbers (65,536 total) approached exhaustion, BGP was already globally deployed with routers that hard-assumed a 2-byte AS_PATH field; requiring every router worldwide to upgrade simultaneously (a "flag day") was operationally infeasible. AS_TRANS (23456) lets old routers see a placeholder AS number in the path while new routers exchange the real 4-byte number in a separate attribute, letting the upgrade happen incrementally, router by router — the same incremental-deployment philosophy seen elsewhere (e.g., NAT as an IPv4-exhaustion stopgap).

### 9. Why does BGP use plain TCP rather than defining its own reliable transport?
**Untested.** Reliable, ordered delivery and congestion control are exactly what TCP already provides, and BGP's designers had no BGP-specific transport requirement (like QUIC's need for 0-RTT or multiplexed streams) that TCP couldn't satisfy — reusing TCP meant BGP's own specification could focus entirely on routing semantics rather than reinventing transport reliability.

### 10. Why was route flap damping introduced and then later recommended against?
**Untested.** Flap damping (RFC 2439) was meant to suppress route advertisements that oscillate rapidly, protecting router CPU from repeated recomputation — but operational experience showed the penalty algorithm could suppress a route for far longer than the actual instability justified, and worse, could penalize routes that were flapping for reasons entirely outside the origin's control (transit AS instability), unfairly punishing innocent prefixes. RFC 7196 documents this and recommends against damping with the original parameters given modern router CPU headroom no longer needs the protection as urgently.

---

## IPsec

### 1. ✅ Why does IPsec define AH and ESP as separate protocols when ESP-with-authentication already covers what AH provides?

> The agent was honest about a real limitation here rather than fabricating a design-debate answer:
>
> *"I was unable to locate explicit documentation in the RFC corpus explaining WHY AH was deprecated in favor of ESP with authentication."* It confirmed RFC 4302 (AH) and RFC 4303 (ESP) are both still current Standards Track RFCs with **no formal deprecation notice** — correcting the question's own premise that AH was formally deprecated.
>
> RFC 8221 has a section titled "Encryption Must Be Authenticated," suggesting a *preference* for authenticated encryption, though the agent couldn't retrieve that section's full text. Its working conclusion: *"the practical reality — that ESP with authentication provides both confidentiality and integrity in a single protocol, whereas AH provides only integrity — appears to be the underlying technical reason for preferring ESP with authentication, but this rationale is not explicitly articulated in the standards documents."*
>
> It correctly flagged that answering *why* (not just *that*) would need IPsec WG mailing-list history from the mid-2000s, unavailable on this instance.

### 2. Why did IKEv2 fully replace IKEv1 rather than patch it incrementally?
**Untested.** IKEv1 (RFC 2409) offered many different, independently-combinable negotiation modes (main/aggressive mode, multiple authentication methods), which made interoperability testing combinatorially difficult and was widely criticized as needlessly complex. IKEv2 (RFC 7296) collapsed this into a single, simpler exchange pattern — a clean-slate redesign was judged easier than trying to prune IKEv1's option space without breaking existing deployments.

### 3. Why define both transport and tunnel mode instead of one general mechanism?
**Untested.** Transport mode (protecting the payload, keeping original IP headers) fits host-to-host security where both endpoints are IPsec-aware; tunnel mode (encapsulating the whole original packet in a new IP header) fits gateway-to-gateway VPNs where the protected traffic's endpoints aren't IPsec-aware at all and need to stay hidden behind the gateway's own address. A single mode would either impose gateway-style overhead on host-to-host traffic or fail to hide endpoint addressing for VPN use — the two modes serve genuinely different topologies.

### 4. Why did NAT traversal need a bolted-on extension (RFC 3947/3948) rather than being part of the original design?
**Untested.** IPsec authenticates/encrypts packet contents including, in AH's case, parts of the IP header itself — exactly what NAT rewrites in transit, breaking authentication. The original design assumed no untrusted device would rewrite addresses in flight; NAT traversal had to add UDP encapsulation (so a NAT box can rewrite the outer UDP/IP header while leaving the inner protected IPsec packet untouched) as an after-the-fact accommodation for a topology (widespread NAT) that wasn't common when IPsec was first designed.

### 5. Why is a Security Association fundamentally unidirectional?
**Untested.** Keeping each direction's cryptographic state (keys, sequence numbers, replay windows) fully independent simplifies key management and lets each direction be rekeyed, torn down, or use different parameters without coordinating with the other direction — at the cost of needing to manage SAs in pairs for anything bidirectional, unlike a single shared bidirectional session construct (e.g., a TLS connection) would require.

### 6. How did IPsec's real-world deployment complexity feed back into IPv6 downgrading it from mandatory to optional?
**Untested.** See IPv6 Q14 above — the operational reality that IPsec's mandatory-to-implement status didn't translate into mandatory or even common *use* (due to the complexity documented throughout this section: IKEv1's combinatorial modes, NAT traversal retrofits, SA management overhead) is exactly what RFC 6434 cited in aligning the IPv6 spec with actual deployment.

### 7. Why did the IETF later tighten allowed cryptographic algorithms (RFC 8247) rather than leaving crypto-agility unconstrained?
**Untested.** Crypto-agility (letting implementations negotiate from a wide algorithm menu) is valuable for future-proofing but also means weak/broken algorithms (e.g., DES, MD5-based constructions) remain technically compliant indefinitely unless explicitly pruned; RFC 8247 exists to periodically re-curate that menu as cryptanalysis advances, since the original open-ended design had no built-in mechanism to retire algorithms on its own.

### 8. Why does the architecture separate the SPD from the SAD instead of merging policy and session state?
**Untested.** The Security Policy Database expresses durable, administrator-set rules ("traffic matching this selector must be protected this way"), while the Security Association Database holds live, ephemeral cryptographic session state derived from those policies at negotiation time — keeping them separate lets policy be edited/audited independently of whatever active sessions currently exist, and lets multiple SAs come and go under one stable policy rule.

### 9. Why did Opportunistic Encryption for IPsec never get widely standardized/adopted?
**Untested.** Opportunistic IPsec required discovering, per destination, whether the peer supported IPsec at all (historically via DNS records), which added latency and fragility to every new connection attempt and had no fallback path as clean as TLS's "just try HTTPS, fall back to HTTP" pattern at the application layer — a chicken-and-egg deployment problem that never reached critical mass, unlike TLS's opportunistic patterns which benefited from the web's single dominant application protocol.

### 10. Why did MOBIKE need to be a separate extension for surviving IP address changes?
**Untested.** The original IKEv2/IPsec design bound a Security Association's cryptographic state to a specific pair of IP addresses, assuming endpoints were stationary — mobile devices changing networks (WiFi to cellular) broke that assumption, and MOBIKE (RFC 4555) had to add explicit address-update signaling to let an existing SA survive an endpoint's address change rather than requiring a full renegotiation, a capability not anticipated in IKEv2's original scope.

---

## HTTP

### 1. ✅ Why did HTTP/2 adopt binary framing and multiplexing instead of extending HTTP/1.1, and what problem motivated it?

> HTTP/2 (originally SPDY) was designed as *"a protocol designed for low-latency transport of content over the World Wide Web"* with a binary framing layer enabling *"multiplexed, prioritized, and compressed data communication of many concurrent streams."* Two distinct layers were introduced: a lower general-purpose framing layer for multiplexed communication atop TCP, and an upper layer providing HTTP-like semantics for compatibility with existing servers.
>
> The shift was motivated by the explicit goal, stated in the draft-ietf-httpbis-http2-00 abstract: *"the primary goal of this protocol is to create a low-latency protocol for use with HTTP,"* requiring a move away from HTTP/1.1's text-based, sequential request-response model to enable concurrent stream handling with prioritization and compression.
>
> **Honest limitation the agent flagged:** the specific HTTP/1.1 problems (head-of-line blocking, connection overhead, inefficient headers) that most write-ups cite as HTTP/2's motivation "are not explicitly detailed in the RFC 7540 sections I was able to retrieve — the RFC focuses on describing the solution rather than extensively documenting the problem statement." It also notes the draft explicitly states reusing the *SPDY framing layer itself* was "a non-goal" — HTTP/2 evolved from SPDY as a starting point but wasn't required to stay wire-compatible with it.

### 2. Why did HTTP/3 abandon TCP for QUIC/UDP?
**Untested.** HTTP/2's multiplexing solved *application-layer* head-of-line blocking (one slow request no longer blocks others sharing the same logical connection), but all HTTP/2 streams still shared a single TCP connection, so a single lost TCP segment stalled *every* stream at the transport layer — HTTP/2's biggest remaining bottleneck. QUIC (built on UDP) gives each stream independent loss recovery, eliminating that transport-level HOL blocking, at the cost of real deployment friction: many middleboxes historically rate-limit or block UDP, and QUIC had to build its own encryption/congestion-control/reliability from scratch rather than reuse decades of TCP stack maturity.

### 3. Why did persistent connections become HTTP/1.1's default instead of HTTP/1.0's per-request model?
**Untested.** HTTP/1.0's default of opening a new TCP connection per request meant paying a full TCP handshake (and, once HTTPS became common, a TLS handshake too) for every single resource on a page — increasingly costly as pages grew to dozens of embedded resources through the 1990s. HTTP/1.1 made persistent connections the default (with `Connection: close` needed to opt out) once it was clear the handshake overhead dominated real-world page-load performance far more than the original per-request-connection simplicity was worth.

### 4. Why were cookies a years-later retrofit rather than part of the original stateless design?
**Untested.** HTTP was originally designed for stateless document retrieval, with no concept of a "session" needed for its original use case; cookies originated as a pragmatic Netscape browser mechanism (not an IETF design) to support emerging needs like shopping carts, later standardized after the fact (RFC 6265). Because they were retrofitted onto a protocol never designed with a security boundary between "site" and "browsing context" in mind, cookies inherited ambiguous cross-site behavior that took until SameSite (RFC 6265bis) — decades later — to meaningfully close.

### 5. Why was caching semantics split into its own document (RFC 9111) separate from core semantics (RFC 9110)?
**Untested.** RFC 2616's single-monolithic-document approach made it hard to reference or revise caching rules independently of core request/response semantics, even though caching is conceptually a distinct concern (intermediary behavior) from client/server message semantics. The 2022 HTTP semantics revision split them explicitly so each document's scope maps to one coherent topic, easing future targeted updates to either without touching the other.

### 6. Why was HTTP/2 server push effectively abandoned by browsers despite being a flagship feature?
**Untested.** Server push let a server proactively send resources a client hadn't asked for yet, based on the server's guess about what the page would need — but in practice, servers often pushed resources the client already had cached, wasting bandwidth, and push interacted poorly with browsers' own resource-priority heuristics. Real-world measurement showed it rarely improved (and often hurt) performance, leading major browsers to deprecate/remove support despite it being one of HTTP/2's headline capabilities at standardization time.

### 7. Why did HTTP's own authentication schemes stay weak/rarely used while the industry built session auth on top of HTTP instead?
**Untested.** Basic and Digest auth (RFC 7617/7616) offer no session lifecycle (no logout, no expiration UX, credentials resent on every request or tied to a fragile nonce scheme) and terrible browser UX (native auth dialogs, no custom login pages) — cookie-based sessions let applications fully control login flow, expiration, and UX, which is why the ecosystem converged on building auth *on top of* HTTP (cookies, then OAuth/bearer tokens) rather than using HTTP's own native auth headers.

### 8. Why was chunked transfer encoding needed to avoid pre-computing Content-Length?
**Untested.** Some responses (server-generated streaming content, long-running dynamically-produced output) don't have a known total length until generation finishes, but HTTP/1.0 required Content-Length to know where a response ended over a persistent connection. Chunked encoding lets a server send data in self-delimited chunks as it's produced, with a final zero-length chunk marking the end — enabling streaming responses without buffering the entire body first just to count its bytes.

### 9. Why did HPACK need a custom compression design instead of reusing gzip/DEFLATE?
**Untested.** The CRIME attack demonstrated that compressing attacker-influenced plaintext together with secrets (like session cookies) in the same compressed stream, over TLS, lets an attacker infer secret bytes by observing compressed-size changes — exactly the situation generic HTTP/2 header compression with gzip/DEFLATE would recreate. HPACK (RFC 7541) was purpose-built with compression boundaries and an indexing table designed specifically to avoid mixing attacker- and secret-controlled data in ways that leak information through size side-channels.

### 10. Why does the HTTP status code registry retain confusing historical codes instead of being cleaned up?
**Untested.** HTTP's evolution model prioritizes never breaking existing clients/servers that already implement a given status code's semantics over tidying the registry — removing or renumbering a code would require every implementation on the Internet to change simultaneously, the same flag-day problem seen elsewhere (BGP AS numbers, IPv4-to-IPv6). Codes like 305 Use Proxy get marked deprecated in prose (implementations are told not to *use* it) without ever being removed from the registry, since the registry itself is meant to be an append-only historical record.

---

## OAuth

### 1. Why did OAuth 2.0 drop OAuth 1.0's cryptographic request-signing in favor of relying entirely on TLS?
**Untested.** OAuth 1.0's signature scheme (HMAC-SHA1 over a canonicalized request) required precise, error-prone parameter normalization across every client implementation — a frequent source of interoperability bugs. By the time OAuth 2.0 was designed, TLS was becoming a reasonable universal assumption for web traffic, so moving integrity/confidentiality to the transport layer let the application-layer protocol drop an entire class of signature-canonicalization complexity, at the cost of now depending entirely on TLS actually being used correctly end-to-end.

### 2. ✅ Why was the Implicit Grant included in RFC 6749 and later formally deprecated in RFC 9700?

> RFC 9700 (Best Current Practice for OAuth 2.0 Security) recommends against the Implicit Grant based on accumulated evidence about how browser behavior and attack vectors evolved: *"The implicit grant... [is] vulnerable to access token leakage and access token replay"* and clients *"SHOULD NOT use the implicit grant... unless access token injection in the authorization response is prevented and the aforementioned token leakage vectors are mitigated."*
>
> **The key driver**, per RFC 9700's introduction: *"Technology has changed. For example, the way browsers treat fragments when redirecting requests has changed, and with it, the implicit grant's underlying security model."*
>
> Specific attack vectors cataloged: **access token leakage via browser history** (fragment-carried tokens ending up logged), **redirect URI validation attacks** (an attacker-controlled redirect URI directly receives the token), **fragment reattachment attacks** (user agents reattaching fragments to redirect targets lacking their own fragment, circumventing narrow redirect-URI patterns), and **token injection/replay** (the token being exposed in the authorization response itself).
>
> RFC 6819 (2013) had already documented these threats, but original RFC 6749 kept the Implicit Grant as legitimate for browser-based clients since it *"improve[s] the responsiveness and efficiency... since it reduces the number of round trips."* The deprecation reflects three converging factors: changed browser fragment-handling assumptions, an expanded attacker model in RFC 9700, and — critically — PKCE (RFC 7636) making the Authorization Code Grant *viable for public clients*, removing the Implicit Grant's original reason to exist at all.

### 3. Why was PKCE, designed for public clients, later recommended for confidential clients too?
**Untested.** PKCE was originally aimed at mobile/native apps that can't hold a client secret, protecting the authorization code from interception during the redirect. RFC 9700 broadened the recommendation once the threat model expanded to include authorization-code-interception attacks (e.g., via malicious apps registering the same custom URI scheme) that affect confidential clients too, not just secret-less ones — PKCE's proof-of-possession binding turned out to be valuable defense-in-depth regardless of client type, not just a workaround for missing a secret.

### 4. Why does OAuth define so many grant types instead of one unified flow?
**Untested.** Each grant type targets a client capability profile a single flow can't serve: Authorization Code needs a browser redirect and a backend to exchange the code (confidential, browser-present clients); Client Credentials needs no user at all (machine-to-machine); Resource Owner Password Credentials (now discouraged) assumed a highly-trusted first-party client; Device Code (RFC 8628) targets devices with no local browser or convenient text input at all. A single flow would either be unusable for some of these constraints or force capabilities (secret storage, browser redirect) onto clients that structurally don't have them.

### 5. Why was the Resource Owner Password Credentials grant included at all, given it contradicts OAuth's core goal?
**Untested.** ROPC exists for migration scenarios: a first-party client that already collects the user's password directly (pre-dating OAuth adoption) could get an access token without an immediate, disruptive UX rewrite to a redirect-based flow. It was always a compromise, not a recommended pattern, which is why RFC 9700 explicitly discourages it now that the migration-era use case it existed for is largely obsolete.

### 6. Why was `state` merely "recommended" rather than mandatory, and how did later guidance close that gap?
**Untested.** RFC 6749 treated CSRF protection as one of several security considerations left to implementer judgment, consistent with the spec's general pattern of separating core protocol mechanics from security hardening (moved into companion documents). RFC 6819 and then RFC 9700 progressively hardened this stance as real-world CSRF-via-OAuth-callback attacks were observed, moving `state` (or PKCE, which also provides CSRF protection as a side effect) from "recommended" to effectively mandatory best practice.

### 7. Why did OAuth deliberately exclude identity information, requiring OpenID Connect as a separate layer?
**Untested.** OAuth 2.0 was scoped specifically to *authorization* (letting a client act on a resource owner's behalf with limited access) rather than *authentication* (proving who the user is) — conflating the two had caused real confusion and misuse in OAuth 1.0-era deployments that used access-token issuance as a proxy for "login." OpenID Connect was built as a thin identity layer *on top of* OAuth 2.0 specifically to keep that separation clean rather than retrofitting identity semantics into the authorization spec itself.

### 8. Why did the Device Authorization Grant need a bespoke polling flow rather than extending an existing grant type?
**Untested.** Devices like smart TVs have no convenient way to render a redirect-capable browser or accept precise text input (a password), so the user instead authorizes on a *separate* device (phone/laptop) using a short code shown on the TV, while the TV polls the token endpoint until that out-of-band authorization completes. No existing grant type had a place for this "authorize elsewhere, poll here" pattern, since all of them assumed the requesting device itself could either hold a secret or host the redirect.

### 9. Why does the bearer token model provide no built-in replay protection, unlike OAuth 1.0's signed requests?
**Untested.** Bearer tokens (RFC 6750) trade OAuth 1.0's per-request-signature complexity for simplicity: whoever holds the token can use it, full stop, relying entirely on TLS and short token lifetimes to limit exposure rather than cryptographic proof-of-possession per request. This simplicity is exactly what later motivated DPoP (RFC 9449) and mTLS-bound tokens (RFC 8705) — both add back a proof-of-possession binding (so a stolen token alone isn't sufficient to use it) once bearer tokens' theft/replay risk was judged unacceptable for higher-security use cases, without reintroducing OAuth 1.0's full signing complexity.

### 10. Why was refresh token rotation only formalized as best practice in RFC 9700 rather than in the original design?
**Untested.** A static, long-lived refresh token that never changes is operationally simple (nothing to track across rotations) but means a single leaked refresh token grants indefinite access until manually revoked; rotation (issuing a new refresh token — and invalidating the old one — on every use) bounds how long a leaked token remains useful and lets a server detect reuse of an already-rotated (and therefore stolen) token as a clear compromise signal. This detection capability wasn't part of OAuth 2.0's original minimal design and was only formalized as the threat of long-lived credential leakage became better understood.

---

## Summary

- **72 questions** across 6 families (15 IPv4, 17 IPv6, 10 BGP, 10 IPsec, 10 HTTP, 10 OAuth).
- **7 tested live** against the fixed Panorama agent + Haiku 4.5, all producing real, RFC-grounded answers (no stubs).
- **65 untested** — domain-knowledge drafts meant to be verified, not assumed correct. A good next step would be running a batch of these (especially ones marked with cross-RFC "why this and not that" framing, Panorama's specialty) the same way the first 7 were tested.
- **Known gap:** no WG mail archives are imported on this deployment yet, so live answers are currently grounded in RFC/draft text only, not mailing-list design debate. BGP and IPsec's tested answers explicitly surfaced this limitation.
